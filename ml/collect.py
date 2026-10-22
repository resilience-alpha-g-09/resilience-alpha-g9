"""Collecte la baseline : un vecteur de features toutes les 5 s, depuis Prometheus (publié sur 127.0.0.1:9090).
Usage : make collect MINUTES=60   (ou : python ml/collect.py --minutes 60 [--label normal|incident])
Laisser tourner la plateforme avec le simulator seul (pas d'incident) pendant la collecte."""
import argparse
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from features import NAMES, fetch_features

parser = argparse.ArgumentParser()
parser.add_argument("--prometheus", default="http://127.0.0.1:9090")
parser.add_argument("--minutes", type=float, default=60)
parser.add_argument("--interval", type=float, default=5)
parser.add_argument("--label", default="normal", help="normal | incident (pour l'évaluation, pas l'entraînement)")
parser.add_argument("--out", default=str(Path(__file__).parent / "data" / "baseline.csv"))
args = parser.parse_args()

out = Path(args.out)
out.parent.mkdir(parents=True, exist_ok=True)
new_file = not out.exists()
n = 0
with httpx.Client() as client, out.open("a", newline="") as fh:
    w = csv.writer(fh)
    if new_file:
        w.writerow(["ts", "label", *NAMES])
    deadline = time.time() + args.minutes * 60
    while time.time() < deadline:
        f = fetch_features(client, args.prometheus)
        w.writerow([datetime.now(timezone.utc).isoformat(timespec="seconds"), args.label, *[f[k] for k in NAMES]])
        fh.flush()
        n += 1
        if n % 12 == 0:
            print(f"{n} échantillons — api_rps={f['api_rps']:.2f} p95={f['api_p95']:.3f}s succès={f['sim_success_ratio']:.2f}")
        time.sleep(args.interval)
print(f"OK : {n} échantillons ajoutés à {out}")
