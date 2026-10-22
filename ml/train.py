"""Entraîne l'IsolationForest sur la baseline normale et fixe le seuil d'anomalie.
Usage : make train   (ou : python ml/train.py [--contamination 0.01] avec ml/requirements.txt)
Produit ml/model/model.joblib + ml/model/threshold.json, montés en lecture seule dans le détecteur."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import csv

import joblib
import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from features import NAMES

here = Path(__file__).parent
parser = argparse.ArgumentParser()
parser.add_argument("--data", default=str(here / "data" / "baseline.csv"))
parser.add_argument("--contamination", type=float, default=0.01)
parser.add_argument("--margin", type=float, default=0.02, help="marge sous le quantile pour limiter les faux positifs")
args = parser.parse_args()

with open(args.data, newline="") as fh:
    rows = list(csv.DictReader(fh))
X = np.array([[float(r[n]) for n in NAMES] for r in rows if r["label"] == "normal"], dtype=float)
X_incident = np.array([[float(r[n]) for n in NAMES] for r in rows if r["label"] == "incident"], dtype=float)
if len(X) < 120:
    raise SystemExit(f"Trop peu d'échantillons normaux ({len(X)}). Lancer ml/collect.py plus longtemps (≥ 10 min).")

model = make_pipeline(
    StandardScaler(),
    IsolationForest(n_estimators=300, contamination=args.contamination, random_state=42),
)
model.fit(X)
scores = model.score_samples(X)
threshold = float(np.quantile(scores, args.contamination) - args.margin)

(here / "model").mkdir(exist_ok=True)
joblib.dump(model, here / "model" / "model.joblib")
meta = {
    "threshold": threshold, "features": NAMES, "n_samples": int(len(X)),
    "contamination": args.contamination, "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "score_stats": {"min": float(scores.min()), "p01": float(np.quantile(scores, 0.01)),
                    "median": float(np.median(scores)), "max": float(scores.max())},
}
(here / "model" / "threshold.json").write_text(json.dumps(meta, indent=2))
print(json.dumps(meta, indent=2))

if len(X_incident):
    s = model.score_samples(X_incident)
    print(f"Évaluation sur {len(s)} échantillons 'incident' : {np.mean(s < threshold) * 100:.0f} % détectés "
          f"(scores min {s.min():.3f} / médiane {np.median(s):.3f}, seuil {threshold:.3f})")
