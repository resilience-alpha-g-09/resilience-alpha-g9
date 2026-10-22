"""Faux positifs du détecteur : laisser tourner le trafic normal (simulator seul) et compter ce que le modèle signale.
Usage : make fp-check MINUTES=30   (tourne dans l'image du détecteur, comme collect/train)

Aucune attaque ni panne pendant la mesure : toute alerte confirmée est donc un faux positif. Le remédiateur n'est pas
neutralisé (pas de DRY_RUN) : on mesure le comportement réel de la plateforme, y compris sa réaction à une fausse alerte.
Les chiffres viennent de Prometheus (detector_anomaly, detector_alerts_total, detector_anomaly_score), scrapé toutes les 5 s
comme le détecteur calcule ses fenêtres : un échantillon ≈ une fenêtre."""
import argparse
import socket
import sys
import time
from datetime import datetime

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--minutes", type=float, default=30)
parser.add_argument("--prometheus", default="http://prometheus:9090")
parser.add_argument("--detector", default="http://ra-detector:8000")
parser.add_argument("--remediator", default="http://remediator:8000")
parser.add_argument("--machine", default=socket.gethostname())
args = parser.parse_args()


def prom(client: httpx.Client, query: str, at: float) -> float | None:
    r = client.get(f"{args.prometheus}/api/v1/query", params={"query": query, "time": at}, timeout=10)
    r.raise_for_status()
    res = r.json()["data"]["result"]
    return float(res[0]["value"][1]) if res else None


def measure(client: httpx.Client, start: float, end: float) -> dict:
    w = f"[{max(int(end - start), 5)}s]"
    return {
        "windows": prom(client, f"count_over_time(detector_anomaly{w})", end) or 0,
        "anomalous": prom(client, f"sum_over_time(detector_anomaly{w})", end) or 0,
        "alerts": prom(client, f"sum(increase(detector_alerts_total{w}))", end) or 0,
        "incidents": prom(client, f'sum(increase(remediator_incidents_total{{source="detector"}}{w}))', end) or 0,
        "score_min": prom(client, f"min_over_time(detector_anomaly_score{w})", end),
        "score_p05": prom(client, f"quantile_over_time(0.05, detector_anomaly_score{w})", end),
        "threshold": prom(client, "detector_threshold", end),
        "mode_min": prom(client, f"min_over_time(detector_mode{w})", end),
    }


with httpx.Client() as client:
    try:
        health = client.get(f"{args.detector}/health", timeout=5).json()
        rem = client.get(f"{args.remediator}/state", timeout=5).json()
        summary = client.get(f"{args.remediator}/summary", timeout=10).json()
    except httpx.HTTPError as exc:
        sys.exit(f"plateforme injoignable ({exc.__class__.__name__}) — make up")
    if not health.get("model"):
        sys.exit("aucun modèle chargé : rien à mesurer. Faire d'abord : make collect puis make train")
    if rem.get("state_name") != "IDLE" or rem.get("busy"):
        sys.exit(f"remédiateur en {rem.get('state_name')} : attendre le retour à IDLE (aucun incident en cours)")
    if (summary.get("tx_rate") or 0) <= 0:
        sys.exit("aucun paiement simulé en cours : le simulator doit tourner pour fournir du trafic normal")

    print(f"Mesure des faux positifs : {args.minutes:g} min de trafic normal sur {args.machine} "
          f"(dry_run={health.get('dry_run')}). Ne lancer ni attaque ni panne pendant la mesure.", flush=True)
    start = time.time()
    end = start + args.minutes * 60
    while time.time() < end:
        time.sleep(min(60, max(1, end - time.time())))
        m = measure(client, start, time.time())
        print(f"  {int((time.time() - start) / 60):>3} min · {int(m['windows'])} fenêtres · {int(m['anomalous'])} anormales"
              f" · {int(m['alerts'])} alerte(s)", flush=True)

    time.sleep(6)  # laisser Prometheus scraper la dernière fenêtre
    m = measure(client, start, time.time())

windows, anomalous, alerts = int(m["windows"]), int(m["anomalous"]), int(round(m["alerts"]))
rate = anomalous / windows if windows else 0.0
fmt = lambda v: "—" if v is None else f"{v:.3f}"  # noqa: E731
margin = None if m["score_min"] is None or m["threshold"] is None else m["score_min"] - m["threshold"]

print(f"""
Résultat ({args.minutes:g} min, {datetime.now():%Y-%m-%d %H:%M}, {args.machine})
  fenêtres évaluées      : {windows}
  fenêtres anormales     : {anomalous} ({rate:.1%})  — une fenêtre isolée ne déclenche rien
  alertes confirmées     : {alerts}  = faux positifs (2 fenêtres anormales consécutives)
  incidents remédiateur  : {int(round(m['incidents']))} (source detector)
  score minimum observé  : {fmt(m['score_min'])}  · 5e centile {fmt(m['score_p05'])}  · seuil {fmt(m['threshold'])}
  marge au seuil         : {fmt(margin)}  (positive = le trafic normal n'a jamais franchi le seuil)""")
if margin is not None and 0 <= margin < 0.02:
    print("  ATTENTION : marge faible, le trafic normal frôle le seuil. Allonger la collecte (make collect) puis make train")
if m["mode_min"] is not None and m["mode_min"] < 1:
    print("  ATTENTION : le modèle n'a pas été chargé pendant toute la mesure, résultat partiel")

print(f"""
Ligne pour le Dossier (Détection > Résultats) :
Sur {args.minutes:g} minutes de trafic normal ({args.machine}, {datetime.now():%d/%m/%Y}), {windows} fenêtres évaluées : \
{anomalous} jugées anormales ({rate:.1%}), {alerts} alerte confirmée{'s' if alerts > 1 else ''}, soit {alerts} faux positif{'s' if alerts > 1 else ''}. \
Score minimum {fmt(m['score_min'])} pour un seuil de {fmt(m['threshold'])}.""")
sys.exit(0 if alerts == 0 else 1)
