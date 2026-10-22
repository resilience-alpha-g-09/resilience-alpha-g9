"""Moteur de remédiation — la seule brique autorisée à agir sur la plateforme.

Boucle : surveillance (watchdog) ou alerte du détecteur → DETECTED → MITIGATING (bloquer la source,
redémarrer le service malade) → VERIFYING (une vraie transaction via l'edge) → RECOVERED / FAILED.
Accès Docker limité au socket-proxy (restart / kill uniquement). Aucun endpoint n'est publié hors du réseau ops.
"""
import asyncio
import ipaddress
import json
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import IntEnum

import httpx
from fastapi import FastAPI, HTTPException
from prometheus_client import Counter, Gauge, make_asgi_app
from pydantic import BaseModel

# ── configuration ──
DOCKER_API = os.environ.get("DOCKER_API", "http://socket-proxy:2375").rstrip("/")
EDGE_URL = os.environ.get("EDGE_URL", "http://edge:8080").rstrip("/")
EDGE_CONTAINER = os.environ.get("EDGE_CONTAINER", "ra-edge")
BLOCKLIST_PATH = os.environ.get("BLOCKLIST_PATH", "/blocklist/blocklist.conf")
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
# Filet de sécurité : sans IP fournie par le détecteur, chercher soi-même la source externe dominante dans les logs de l'edge
AUTO_SUSPECT = os.environ.get("AUTO_SUSPECT_FROM_LOGS", "true").lower() == "true"
AUTO_SUSPECT_MIN_SHARE = float(os.environ.get("AUTO_SUSPECT_MIN_SHARE", "0.5"))
INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8")]
VERIFIER_EMAIL = os.environ["VERIFIER_EMAIL"]
VERIFIER_PASSWORD = os.environ["VERIFIER_PASSWORD"]
VERIFY_TO_ACCOUNT = int(os.environ.get("VERIFY_TO_ACCOUNT", "10"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL_SECONDS", "5"))
FAIL_THRESHOLD = int(os.environ.get("FAIL_THRESHOLD", "2"))
COOLDOWN = float(os.environ.get("COOLDOWN_SECONDS", "60"))
NEVER_BLOCK = {os.environ.get("HOTSPOT_GATEWAY", "192.168.137.1"), "127.0.0.1", "::1"}
# Passerelles NAT partagées (Docker Desktop présente TOUTES les connexions publiées en 192.168.65.1) : on étrangle au lieu
# de bloquer, sinon les clients légitimes derrière le même NAT (dont le navigateur de l'hôte) seraient coupés.
SHARED_NAT_IPS = {ip.strip() for ip in os.environ.get("SHARED_NAT_IPS", "192.168.65.1").split(",") if ip.strip()}
THROTTLE_PATH = os.environ.get("THROTTLE_PATH", "/blocklist/throttle.conf")

# "api=http://api:8000/health=ra-api,auth=http://auth:8000/health=ra-auth"
WATCHED: dict[str, dict] = {}
for item in os.environ.get("WATCHED_SERVICES", "").split(","):
    if item.strip():
        name, url, container = item.split("=")
        WATCHED[name] = {"health_url": url, "container": container}


class State(IntEnum):
    IDLE = 0
    DETECTED = 1
    MITIGATING = 2
    VERIFYING = 3
    RECOVERED = 4
    FAILED = 5


# ── métriques ──
G_STATE = Gauge("remediator_state", "0 idle,1 detected,2 mitigating,3 verifying,4 recovered,5 failed")
G_SERVICE_UP = Gauge("remediator_service_up", "Santé vue par le remédiateur", ["service"])
G_LAST_MTTR = Gauge("remediator_last_mttr_seconds", "Durée du dernier incident, détection → vérification OK")
G_BLOCKED = Gauge("remediator_blocked_ips", "Nombre d'IP bloquées à l'edge")
G_THROTTLED = Gauge("remediator_throttled_ips", "Nombre d'IP étranglées à l'edge (NAT partagé)")
C_INCIDENTS = Counter("remediator_incidents_total", "Incidents pris en charge", ["source"])
C_ACTIONS = Counter("remediator_actions_total", "Actions exécutées", ["action", "result"])
C_VERIF = Counter("remediator_verifications_total", "Transactions de vérification", ["result"])


# ── état ──
class Store:
    def __init__(self):
        self.state = State.IDLE
        self.busy = False
        self.lock = asyncio.Lock()
        self.events: deque = deque(maxlen=300)
        self.health = {n: {"ok": None, "consecutive_failures": 0, "last_check": None} for n in WATCHED}
        self.current: dict | None = None
        self.history: deque = deque(maxlen=20)
        self.cooldown_until = 0.0
        self.blocked_ips: set[str] = set()
        self.throttled_ips: set[str] = set()


store = Store()
http = httpx.AsyncClient(timeout=10)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def event(level: str, msg: str, **kv):
    rec = {"ts": now_iso(), "level": level, "msg": msg, **kv}
    store.events.appendleft(rec)
    print(json.dumps({"event": msg, "level": level, **kv}), flush=True)


def set_state(s: State):
    store.state = s
    G_STATE.set(int(s))
    event("info", "state_changed", state=s.name)


# ── blocklist (fichier inclus par nginx, rechargé par SIGHUP) ──
_DENY_RE = re.compile(r"^\s*deny\s+([0-9a-fA-F.:]+)\s*;")


_THROTTLE_RE = re.compile(r"^\s*([0-9a-fA-F.:]+)\s+1\s*;")


def load_blocklist():
    try:
        with open(BLOCKLIST_PATH) as fh:
            store.blocked_ips = {m.group(1) for line in fh if (m := _DENY_RE.match(line))}
    except FileNotFoundError:
        store.blocked_ips = set()
    try:
        with open(THROTTLE_PATH) as fh:
            store.throttled_ips = {m.group(1) for line in fh if (m := _THROTTLE_RE.match(line))}
    except FileNotFoundError:
        store.throttled_ips = set()
    G_BLOCKED.set(len(store.blocked_ips))
    G_THROTTLED.set(len(store.throttled_ips))


def write_throttle():
    lines = ["# Géré par le remédiateur — une ligne \"<ip> 1;\" par source étranglée (6 req/min sur /api/ et /auth/). Vidé par `make reset-blocklist`.\n"]
    lines += [f"{ip} 1;\n" for ip in sorted(store.throttled_ips)]
    with open(THROTTLE_PATH, "w") as fh:
        fh.writelines(lines)
    G_THROTTLED.set(len(store.throttled_ips))


def write_blocklist():
    lines = ["# Géré par le remédiateur — une ligne \"deny <ip>;\" par source bloquée. Vidé par `make reset`.\n"]
    lines += [f"deny {ip};\n" for ip in sorted(store.blocked_ips)]
    with open(BLOCKLIST_PATH, "w") as fh:
        fh.writelines(lines)
    G_BLOCKED.set(len(store.blocked_ips))


async def docker_post(path: str) -> int:
    try:
        r = await http.post(f"{DOCKER_API}{path}", timeout=20)
        return r.status_code
    except httpx.HTTPError as exc:
        event("error", "docker_api_error", path=path, error=exc.__class__.__name__)
        return 0


async def reload_edge() -> bool:
    ok = await docker_post(f"/containers/{EDGE_CONTAINER}/kill?signal=SIGHUP") == 204
    C_ACTIONS.labels("reload_edge", "ok" if ok else "error").inc()
    return ok


async def restart_container(name: str) -> bool:
    ok = await docker_post(f"/containers/{name}/restart?t=3") == 204
    C_ACTIONS.labels("restart", "ok" if ok else "error").inc()
    return ok


async def block_ip(ip: str) -> str:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return "invalid_ip"
    if ip in NEVER_BLOCK or addr.is_loopback:
        return "refused_protected_ip"
    if ip in SHARED_NAT_IPS:
        return await throttle_ip(ip)
    if ip in store.blocked_ips:
        return "already_blocked"
    store.blocked_ips.add(ip)
    write_blocklist()
    result = "blocked" if await reload_edge() else "written_but_reload_failed"
    C_ACTIONS.labels("block_ip", result).inc()
    event("warn", "ip_blocked", ip=ip, result=result)
    return result


async def throttle_ip(ip: str) -> str:
    """Source partagée (NAT) : limitation à 6 req/min sur les routes métier au lieu d'un blocage total."""
    if ip in store.throttled_ips:
        return "already_throttled"
    store.throttled_ips.add(ip)
    write_throttle()
    result = "throttled" if await reload_edge() else "written_but_reload_failed"
    C_ACTIONS.labels("throttle_ip", result).inc()
    event("warn", "ip_throttled", ip=ip, result=result, reason="shared_nat_gateway")
    return result


async def unblock_ip(ip: str) -> str:
    if ip not in store.blocked_ips and ip not in store.throttled_ips:
        return "not_blocked"
    store.blocked_ips.discard(ip)
    store.throttled_ips.discard(ip)
    write_blocklist()
    write_throttle()
    await reload_edge()
    event("info", "ip_unblocked", ip=ip)
    return "unblocked"


async def top_external_talker(window: str = "30s") -> tuple[str | None, float]:
    """IP externe la plus active à l'edge et sa part du trafic (les IP internes — simulateur, vérificateur — sont ignorées)."""
    q = f'sum by (client_ip) (count_over_time({{container="edge"}} | json | __error__="" [{window}]))'
    try:
        r = await http.get(f"{LOKI_URL}/loki/api/v1/query", params={"query": q}, timeout=3)
        rows = [(x["metric"].get("client_ip", ""), float(x["value"][1])) for x in r.json()["data"]["result"]]
    except Exception:  # noqa: BLE001
        return None, 0.0
    # part calculée sur les seules lignes portant une IP (les audits ModSecurity n'en ont pas et gonfleraient le total)
    with_ip, external = [], []
    for ip, c in rows:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        with_ip.append(c)
        if not any(addr in net for net in INTERNAL_NETS):
            external.append((ip, c))
    if not external:
        return None, 0.0
    ip, c = max(external, key=lambda t: t[1])
    return ip, c / (sum(with_ip) or 1.0)


# ── santé & vérification ──
async def check_health(name: str) -> bool:
    try:
        r = await http.get(WATCHED[name]["health_url"], timeout=2)
        return r.status_code == 200
    except httpx.HTTPError:
        return False


async def wait_healthy(name: str, timeout: float = 30) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check_health(name):
            return True
        await asyncio.sleep(1)
    return False


async def verify_transaction() -> tuple[bool, dict]:
    """La preuve de reprise : une transaction fictive réelle, par le même chemin qu'un client (edge → WAF → api → auth → db)."""
    try:
        r = await http.post(f"{EDGE_URL}/auth/login", json={"email": VERIFIER_EMAIL, "password": VERIFIER_PASSWORD}, timeout=5)
        if r.status_code != 200:
            return False, {"step": "login", "status": r.status_code}
        headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
        r = await http.post(f"{EDGE_URL}/api/transactions", headers=headers,
                            json={"to_account": VERIFY_TO_ACCOUNT, "amount": "1.00", "origin": "verifier"}, timeout=5)
        if r.status_code != 201:
            return False, {"step": "create", "status": r.status_code}
        tx_id = r.json()["id"]
        r = await http.get(f"{EDGE_URL}/api/transactions/{tx_id}", headers=headers, timeout=5)
        if r.status_code != 200 or r.json().get("status") != "completed":
            return False, {"step": "readback", "status": r.status_code}
        return True, {"tx_id": tx_id}
    except httpx.HTTPError as exc:
        return False, {"step": "network", "error": exc.__class__.__name__}


async def verify_with_retries(attempts: int = 3, delay: float = 2) -> tuple[bool, dict]:
    detail: dict = {}
    for i in range(1, attempts + 1):
        ok, detail = await verify_transaction()
        C_VERIF.labels("ok" if ok else "fail").inc()
        event("info" if ok else "warn", "verification", attempt=i, ok=ok, **detail)
        if ok:
            return True, detail
        await asyncio.sleep(delay)
    return False, detail


# ── l'incident ──
async def run_incident(source: str, reason: str, services: list[str] | None = None,
                       suspect_ip: str | None = None, score: float | None = None):
    async with store.lock:
        if store.busy:
            return
        store.busy = True
    t0 = time.monotonic()
    incident = {"id": int(time.time()), "source": source, "reason": reason, "started_at": now_iso(),
                "suspect_ip": suspect_ip, "score": score, "actions": [], "mttr_seconds": None, "outcome": None}
    store.current = incident
    C_INCIDENTS.labels(source).inc()
    try:
        set_state(State.DETECTED)
        event("warn", "incident_detected", source=source, reason=reason, suspect_ip=suspect_ip, score=score)

        set_state(State.MITIGATING)
        if not suspect_ip and AUTO_SUSPECT:
            ip, share = await top_external_talker()
            event("info", "suspect_lookup", top_talker=ip, share=round(share, 2))
            if ip and share >= AUTO_SUSPECT_MIN_SHARE:
                suspect_ip = ip
                incident["suspect_ip"], incident["suspect_source"] = ip, "edge_logs"
        if suspect_ip:
            res = await block_ip(suspect_ip)
            incident["actions"].append({"action": "throttle_ip" if suspect_ip in SHARED_NAT_IPS else "block_ip",
                                        "target": suspect_ip, "result": res})
        targets = services or list(WATCHED)
        for name in targets:
            if await check_health(name):
                incident["actions"].append({"action": "check", "target": name, "result": "healthy"})
                continue
            restarted = await restart_container(WATCHED[name]["container"])
            healthy = restarted and await wait_healthy(name, 30)
            incident["actions"].append({"action": "restart", "target": name,
                                        "result": "healthy" if healthy else "still_unhealthy"})
            event("warn" if healthy else "error", "service_restarted", service=name, healthy=healthy)

        set_state(State.VERIFYING)
        ok, detail = await verify_with_retries()
        if not ok:
            # dernier recours : tout redémarrer dans l'ordre, puis re-vérifier
            event("error", "escalation_full_restart")
            for name in WATCHED:
                await restart_container(WATCHED[name]["container"])
                await wait_healthy(name, 30)
                incident["actions"].append({"action": "restart", "target": name, "result": "escalation"})
            ok, detail = await verify_with_retries(attempts=5, delay=3)

        mttr = round(time.monotonic() - t0, 1)
        incident["mttr_seconds"] = mttr
        if ok:
            incident["outcome"] = "recovered"
            G_LAST_MTTR.set(mttr)
            set_state(State.RECOVERED)
            event("info", "service_recovered", mttr_seconds=mttr, tx_id=detail.get("tx_id"))
        else:
            incident["outcome"] = "failed"
            set_state(State.FAILED)
            event("error", "recovery_failed", mttr_seconds=mttr, **detail)
    finally:
        incident["ended_at"] = now_iso()
        store.history.appendleft(incident)
        store.current = None
        store.cooldown_until = time.monotonic() + COOLDOWN
        store.busy = False
        asyncio.create_task(back_to_idle_after(COOLDOWN))


async def back_to_idle_after(delay: float):
    await asyncio.sleep(delay)
    if not store.busy and store.state in (State.RECOVERED, State.FAILED):
        set_state(State.IDLE)


async def watchdog():
    while True:
        for name in WATCHED:
            ok = await check_health(name)
            h = store.health[name]
            h["ok"], h["last_check"] = ok, now_iso()
            h["consecutive_failures"] = 0 if ok else h["consecutive_failures"] + 1
            G_SERVICE_UP.labels(name).set(1 if ok else 0)
            if (not ok and h["consecutive_failures"] >= FAIL_THRESHOLD and not store.busy
                    and time.monotonic() >= store.cooldown_until):
                asyncio.create_task(run_incident("watchdog", f"{name}_unhealthy", services=[name]))
        await asyncio.sleep(POLL_INTERVAL)


# ── application ──
@asynccontextmanager
async def lifespan(_: FastAPI):
    load_blocklist()
    G_STATE.set(0)
    task = asyncio.create_task(watchdog())
    event("info", "remediator_started", watched=list(WATCHED), blocked_ips=sorted(store.blocked_ips))
    yield
    task.cancel()
    await http.aclose()


app = FastAPI(title="RESILIENCE-ALPHA — remediator", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


class IncidentIn(BaseModel):
    source: str = "detector"
    reason: str = "ml_anomaly"
    suspect_ip: str | None = None
    score: float | None = None
    services: list[str] | None = None


@app.post("/incident", status_code=202)
async def incident(body: IncidentIn):
    if store.busy:
        return {"accepted": False, "why": "incident_in_progress"}
    if time.monotonic() < store.cooldown_until:
        return {"accepted": False, "why": "cooldown"}
    asyncio.create_task(run_incident(body.source, body.reason, body.services, body.suspect_ip, body.score))
    return {"accepted": True}


@app.get("/state")
def state():
    return {
        "state": int(store.state), "state_name": store.state.name, "busy": store.busy,
        "cooldown_remaining": max(0, round(store.cooldown_until - time.monotonic())),
        "services": store.health, "blocked_ips": sorted(store.blocked_ips), "throttled_ips": sorted(store.throttled_ips),
        "current_incident": store.current, "last_incident": store.history[0] if store.history else None,
    }


@app.get("/events")
def events(limit: int = 100):
    return list(store.events)[: max(1, min(limit, 300))]


@app.get("/incidents")
def incidents():
    return list(store.history)


async def prom(query: str):
    try:
        r = await http.get(f"{PROMETHEUS_URL}/api/v1/query", params={"query": query}, timeout=3)
        res = r.json()["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:  # noqa: BLE001
        return None


@app.get("/summary")
async def summary():
    """BFF du dashboard : quelques indicateurs Prometheus prêts à afficher."""
    q = {
        "tx_rate": 'sum(rate(http_requests_total{job="api",handler="/transactions",method="POST"}[30s]))',
        "api_p95": 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[30s])))',
        "api_error_ratio": '(sum(rate(http_requests_total{job="api",status=~"5.."}[30s])) or vector(0)) / clamp_min(sum(rate(http_requests_total{job="api"}[30s])),0.001)',
        "sim_success_ratio": 'sum(rate(sim_requests_total{outcome="ok"}[30s])) / clamp_min(sum(rate(sim_requests_total[30s])),0.001)',
        "anomaly_score": "detector_anomaly_score",
        "anomaly_threshold": "detector_threshold",
        "api_cpu": 'sum(rate(process_cpu_seconds_total{job="api"}[30s]))',
        "auth_cpu": 'sum(rate(process_cpu_seconds_total{job="auth"}[30s]))',
    }
    values = await asyncio.gather(*(prom(v) for v in q.values()))
    return {"ts": now_iso(), **dict(zip(q.keys(), values))}


@app.post("/admin/unblock/{ip}")
async def admin_unblock(ip: str):
    return {"result": await unblock_ip(ip)}


@app.post("/admin/reset")
async def admin_reset():
    """Remise à zéro entre deux démos : vide la blocklist. Réseau interne uniquement."""
    store.blocked_ips.clear()
    store.throttled_ips.clear()
    write_blocklist()
    write_throttle()
    await reload_edge()
    store.cooldown_until = 0
    if not store.busy:
        set_state(State.IDLE)
    event("info", "admin_reset")
    return {"result": "reset"}


@app.get("/health")
def health():
    return {"status": "ok", "service": "remediator", "state": store.state.name}
