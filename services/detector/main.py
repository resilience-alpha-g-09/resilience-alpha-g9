"""Détecteur d'anomalies (AIOps). Toutes les 5 s : features Prometheus → score IsolationForest.
Deux fenêtres anormales consécutives → incident envoyé au remédiateur, avec l'IP la plus bavarde (Loki) si elle est externe.
Sans modèle (J1) : logue les features et expose les métriques, ne déclenche rien."""
import asyncio
import ipaddress
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import joblib
from fastapi import FastAPI
from prometheus_client import Counter, Gauge, make_asgi_app

from features import NAMES, fetch_features, to_vector

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
REMEDIATOR_URL = os.environ.get("REMEDIATOR_URL", "http://remediator:8000").rstrip("/")
MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/model/model.joblib"))
THRESHOLD_PATH = Path(os.environ.get("THRESHOLD_PATH", "/model/threshold.json"))
INTERVAL = float(os.environ.get("INTERVAL_SECONDS", "5"))
CONSECUTIVE = int(os.environ.get("CONSECUTIVE_WINDOWS", "2"))
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
LOCAL_COOLDOWN = 60
INTERNAL_NETS = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8")]

G_SCORE = Gauge("detector_anomaly_score", "Score IsolationForest (plus bas = plus anormal)")
G_THRESHOLD = Gauge("detector_threshold", "Seuil d'anomalie")
G_ANOMALY = Gauge("detector_anomaly", "1 si la fenêtre courante est anormale")
G_MODE = Gauge("detector_mode", "0 = pas de modèle (observation), 1 = modèle chargé")
G_FEATURE = Gauge("detector_feature", "Dernière valeur de chaque feature", ["name"])
C_ALERTS = Counter("detector_alerts_total", "Incidents transmis au remédiateur", ["accepted"])

state = {"model": None, "threshold": None, "consecutive": 0, "last_alert": 0.0, "last": {}}


def event(**kv):
    print(json.dumps({"ts": time.time(), **kv}), flush=True)


def load_model():
    if MODEL_PATH.exists() and THRESHOLD_PATH.exists():
        state["model"] = joblib.load(MODEL_PATH)
        meta = json.loads(THRESHOLD_PATH.read_text())
        state["threshold"] = float(meta["threshold"])
        if meta.get("features") != NAMES:
            event(event="feature_mismatch", model=meta.get("features"), code=NAMES)
        G_THRESHOLD.set(state["threshold"])
        G_MODE.set(1)
        event(event="model_loaded", threshold=state["threshold"], n_samples=meta.get("n_samples"))
    else:
        G_MODE.set(0)
        event(event="no_model", hint="lancer ml/collect.py puis ml/train.py — mode observation")


def top_talker(client: httpx.Client) -> tuple[str | None, float]:
    """IP la plus active à l'edge sur 30 s et sa part du trafic. Ignore les IP internes (simulator, vérificateur)."""
    q = 'sum by (client_ip) (count_over_time({container="edge"} | json | __error__="" [30s]))'
    try:
        r = client.get(f"{LOKI_URL}/loki/api/v1/query", params={"query": q}, timeout=3)
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


def alert(client: httpx.Client, score: float, features: dict):
    ip, share = top_talker(client)
    suspect = ip if ip and share >= 0.5 else None
    payload = {"source": "detector", "reason": "ml_anomaly", "score": round(score, 4), "suspect_ip": suspect}
    event(event="anomaly_confirmed", **payload, top_talker=ip, share=round(share, 2), features=features)
    if DRY_RUN:
        event(event="dry_run_skip_remediation")
        return
    try:
        r = client.post(f"{REMEDIATOR_URL}/incident", json=payload, timeout=3)
        accepted = bool(r.json().get("accepted"))
    except Exception as exc:  # noqa: BLE001
        accepted = False
        event(event="remediator_unreachable", error=exc.__class__.__name__)
    C_ALERTS.labels(str(accepted).lower()).inc()
    state["last_alert"] = time.monotonic()


def tick(client: httpx.Client):
    features = fetch_features(client, PROMETHEUS_URL)
    state["last"] = features
    for k, v in features.items():
        G_FEATURE.labels(k).set(v)
    if state["model"] is None:
        event(event="features", **features)
        return
    score = float(state["model"].score_samples([to_vector(features)])[0])
    anomalous = score < state["threshold"]
    G_SCORE.set(score)
    G_ANOMALY.set(1 if anomalous else 0)
    state["consecutive"] = state["consecutive"] + 1 if anomalous else 0
    if anomalous:
        event(event="window_anomalous", score=round(score, 4), consecutive=state["consecutive"])
    if state["consecutive"] >= CONSECUTIVE and time.monotonic() - state["last_alert"] > LOCAL_COOLDOWN:
        alert(client, score, features)


async def loop():
    with httpx.Client() as client:
        while True:
            try:
                await asyncio.to_thread(tick, client)
            except Exception as exc:  # noqa: BLE001
                event(event="tick_error", error=repr(exc))
            await asyncio.sleep(INTERVAL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    load_model()
    task = asyncio.create_task(loop())
    yield
    task.cancel()


app = FastAPI(title="RESILIENCE-ALPHA — detector", lifespan=lifespan)
app.mount("/metrics", make_asgi_app())


@app.get("/health")
def health():
    return {"status": "ok", "service": "detector", "model": state["model"] is not None, "dry_run": DRY_RUN}


@app.get("/state")
def get_state():
    return {"model_loaded": state["model"] is not None, "threshold": state["threshold"],
            "consecutive": state["consecutive"], "last_features": state["last"]}


@app.post("/reload")
def reload_model():
    load_model()
    return {"model_loaded": state["model"] is not None}
