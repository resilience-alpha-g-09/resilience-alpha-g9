#!/usr/bin/env python3
"""Chaos engineering (sujet §6, jeudi) : injecter des pannes de conteneurs en série et mesurer la reprise autonome.

Ce ne sont pas des attaques (celles-ci partent du PC Attaquant) : on casse volontairement un composant sur le PC Hôte
et on vérifie que la chaîne watchdog → remédiateur → transaction de vérification rétablit le service, run après run.

    python3 tests/chaos.py --runs 10                  # ou : make chaos RUNS=10
    python3 tests/chaos.py --runs 3 --scenarios stop-api,pause-auth --db-run 0

Chaque run : attendre IDLE (cooldown du remédiateur compris) → injecter → attendre RECOVERED / FAILED → payer → remettre
en état si besoin (APRÈS la mesure : ce n'est pas une intervention pendant la reprise, et c'est noté dans le CSV).
Le run « db » est une observation : la perte de la base n'est couverte que partiellement par le PCA/PRA.
Aucune dépendance : bibliothèque standard + CLI docker. Résultats : tests/out/chaos-<date>.csv + lignes pour timings.md.
"""
import argparse
import csv
import json
import os
import socket
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

EDGE = os.environ.get("EDGE", "http://127.0.0.1").rstrip("/")
BFF = os.environ.get("BFF", "http://127.0.0.1:3000/bff").rstrip("/")
EMAIL = os.environ.get("EMAIL", "alice@demo.local")
PASSWORD = os.environ.get("PASSWORD", os.environ.get("DEMO_PASSWORD", "demo1234"))
CONTAINERS = ("ra-api", "ra-auth", "ra-db")

# scénario → (description, commandes docker d'injection)
SCENARIOS = {
    "stop-api": ("arrêt de l'API (docker stop)", [["stop", "-t", "0", "ra-api"]]),
    "stop-auth": ("arrêt de l'authentification (docker stop)", [["stop", "-t", "0", "ra-auth"]]),
    "pause-auth": ("auth figé : vivant mais ne répond plus (docker pause)", [["pause", "ra-auth"]]),
}
DB_SCENARIO = "stop-api+kill-db"  # l'API tombe, puis la base est tuée pendant la reprise


def say(msg: str) -> None:
    print(f"\033[1;35m[chaos]\033[0m {msg}", flush=True)


def http(method: str, url: str, body: dict | None = None, token: str | None = None, timeout: float = 5):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        return 0, None


def remediator_state() -> dict | None:
    code, body = http("GET", f"{BFF}/state", timeout=3)
    return body if code == 200 else None


def pay() -> int:
    code, body = http("POST", f"{EDGE}/auth/login", {"email": EMAIL, "password": PASSWORD})
    if code != 200:
        return code
    code, _ = http("POST", f"{EDGE}/api/transactions", {"to_account": 10, "amount": "2.00"}, token=body["access_token"])
    return code


def docker(*args: str) -> str:
    return subprocess.run(["docker", *args], capture_output=True, text=True).stdout.strip()


def container_status(name: str) -> str:
    return docker("inspect", "-f", "{{.State.Status}}", name) or "absent"


def wait_idle(timeout: float) -> bool:
    """IDLE, aucun incident en cours, cooldown écoulé : sinon le remédiateur ignorerait la panne suivante."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        s = remediator_state()
        if s and s["state_name"] == "IDLE" and not s["busy"] and s["cooldown_remaining"] == 0 \
                and all(h.get("ok") for h in s["services"].values()):
            return True
        time.sleep(2)
    return False


def restore() -> str:
    """Remise en état APRÈS la mesure. Retourne ce qui a dû être fait à la main (vide si rien)."""
    done = []
    for name in CONTAINERS:
        status = container_status(name)
        if status == "paused":
            docker("unpause", name)
            done.append(f"unpause {name}")
        elif status in ("exited", "created", "dead"):
            docker("start", name)
            done.append(f"start {name}")
    return ", ".join(done)


def run_once(n: int, scenario: str, max_seconds: float, idle_timeout: float) -> dict:
    row = {"run": n, "scenario": scenario, "outcome": "", "detect_s": "", "mttr_remediator_s": "",
           "total_s": "", "pay_after": "", "manual_restore": "", "note": ""}
    if not wait_idle(idle_timeout):
        row.update(outcome="skipped", note="plateforme pas revenue à IDLE")
        return row
    before = (remediator_state() or {}).get("last_incident") or {}

    if scenario == DB_SCENARIO:
        say(f"run {n} · {scenario} : arrêt de ra-api, puis docker kill ra-db pendant la reprise")
        t0 = time.monotonic()
        docker("stop", "-t", "0", "ra-api")
    else:
        desc, cmds = SCENARIOS[scenario]
        say(f"run {n} · {scenario} : {desc}")
        t0 = time.monotonic()
        for cmd in cmds:
            docker(*cmd)

    db_killed, detected_at, last, shown = False, None, "?", None
    while True:
        elapsed = time.monotonic() - t0
        s = remediator_state()
        if s:
            last = s["state_name"]
            if last != shown:
                print(f"   t+{elapsed:5.1f}s  état={last}", flush=True)
                shown = last
            if detected_at is None and (s["busy"] or last not in ("IDLE",)):
                detected_at = elapsed
            if scenario == DB_SCENARIO and not db_killed and last in ("MITIGATING", "VERIFYING"):
                docker("kill", "ra-db")
                db_killed = True
                print(f"   t+{elapsed:5.1f}s  ra-db tuée (état {last})", flush=True)
            inc = s.get("last_incident") or {}
            if inc.get("id") and inc.get("id") != before.get("id") and not s["busy"]:
                row["outcome"] = inc["outcome"]
                row["mttr_remediator_s"] = inc["mttr_seconds"]
                break
        if elapsed >= max_seconds:
            row.update(outcome="timeout", note=f"bloqué en {last} après {max_seconds:.0f} s")
            break
        time.sleep(1)

    row["total_s"] = round(time.monotonic() - t0, 1)
    row["detect_s"] = round(detected_at, 1) if detected_at is not None else ""
    row["pay_after"] = pay()
    row["manual_restore"] = restore()
    if scenario == DB_SCENARIO:
        row["note"] = (row["note"] + " observation : perte de la base (PCA/PRA partiel)").strip()
    if row["manual_restore"]:
        # remise en état faite : combien de temps avant qu'un paiement repasse (api/auth se reconnectent seuls à la base)
        t1 = time.monotonic()
        while pay() != 201 and time.monotonic() - t1 < 120:
            time.sleep(2)
        back = round(time.monotonic() - t1, 1)
        msg = f"paiement de nouveau OK {back} s après la remise en état" if back < 120 else "paiement toujours KO 120 s après la remise en état"
        print(f"   {msg}", flush=True)
        row["note"] = (row["note"] + f" ; {msg}").strip(" ;")
    return row


def verdict(row: dict) -> str:
    if row["scenario"] == DB_SCENARIO:
        return "observation"
    return "OK" if row["outcome"] == "recovered" and row["pay_after"] == 201 and not row["manual_restore"] else "ÉCHEC"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--scenarios", default="stop-api,stop-auth,pause-auth", help="liste tournante, séparée par des virgules")
    ap.add_argument("--db-run", type=int, default=5, help="numéro du run où la base est tuée (0 = jamais)")
    # 240 s : l'escalade du remédiateur (redémarrage complet + 5 vérifications) peut durer ~3 min avant FAILED
    ap.add_argument("--max-seconds", type=float, default=240, help="durée max d'un run avant de le déclarer bloqué")
    ap.add_argument("--idle-timeout", type=float, default=150, help="attente max du retour à IDLE entre deux runs")
    ap.add_argument("--machine", default=os.environ.get("MACHINE", socket.gethostname()))
    args = ap.parse_args()

    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown or not scenarios:
        sys.exit(f"scénario inconnu : {unknown} — choix : {', '.join(SCENARIOS)}")
    if remediator_state() is None:
        sys.exit(f"remédiateur injoignable via {BFF} — la plateforme tourne-t-elle ? (make up)")
    if pay() != 201:
        sys.exit("le paiement nominal ne passe pas avant le chaos : corriger d'abord (make e2e)")

    plan = [DB_SCENARIO if i == args.db_run else scenarios[(i - 1) % len(scenarios)] for i in range(1, args.runs + 1)]
    say(f"{args.runs} runs sur {args.machine} : {', '.join(plan)}")
    say("compter ~75 s par run (cooldown du remédiateur : 60 s). Ctrl+C remet les conteneurs en état.")

    rows: list[dict] = []
    try:
        for i, scenario in enumerate(plan, 1):
            row = run_once(i, scenario, args.max_seconds, args.idle_timeout)
            row["verdict"] = verdict(row)
            rows.append(row)
            say(f"run {i} → {row['verdict']} · {row['outcome']} · détection {row['detect_s']} s · "
                f"MTTR {row['mttr_remediator_s'] or '—'} s · total {row['total_s']} s · paiement {row['pay_after']}"
                + (f" · remis en état : {row['manual_restore']}" if row["manual_restore"] else ""))
    except KeyboardInterrupt:
        say(f"interrompu — remise en état : {restore() or 'rien à faire'}")

    if not rows:
        return 1
    out = Path(__file__).parent / "out"
    out.mkdir(exist_ok=True)
    path = out / f"chaos-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    print(f"\n{'run':>3}  {'scénario':<18} {'verdict':<12} {'issue':<10} {'détect.':>8} {'MTTR':>6} {'total':>6}  paiement  remise en état")
    for r in rows:
        print(f"{r['run']:>3}  {r['scenario']:<18} {r['verdict']:<12} {r['outcome']:<10} {str(r['detect_s']):>8} "
              f"{str(r['mttr_remediator_s']):>6} {str(r['total_s']):>6}  {str(r['pay_after']):<8}  {r['manual_restore'] or '—'}")
    counted = [r for r in rows if r["verdict"] != "observation"]
    ok = [r for r in counted if r["verdict"] == "OK"]
    totals = [r["total_s"] for r in ok]
    print(f"\nrésultat : {len(ok)}/{len(counted)} reprises autonomes réussies"
          + (f" · total min {min(totals)} s / médiane {statistics.median(totals)} s / max {max(totals)} s" if totals else ""))
    print(f"CSV : {path}")

    print("\nLignes pour le tableau des temps de reprise :")
    today = datetime.now().strftime("%Y-%m-%d")
    for r in rows:
        mitig = round(r["total_s"] - r["detect_s"], 1) if r["detect_s"] != "" else "—"
        remark = f"chaos run {r['run']}, {r['verdict']}, paiement après : {r['pay_after']}"
        if r["manual_restore"]:
            remark += f", remis en état à la main : {r['manual_restore']}"
        print(f"| {today} | {args.machine} | chaos `{r['scenario']}` | {r['detect_s'] or '—'} | {mitig} | {r['total_s']} | {remark} |")
    return 0 if len(ok) == len(counted) else 1


if __name__ == "__main__":
    sys.exit(main())
