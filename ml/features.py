"""Vecteur de features partagé entre la collecte (ml/collect.py), l'entraînement et le détecteur.
Chaque feature = une requête PromQL instantanée. NaN / absence → 0."""
import math

FEATURES = [
    ("api_rps", 'sum(rate(http_requests_total{job="api"}[30s]))'),
    ("api_p95", 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="api"}[30s])))'),
    ("api_5xx_ratio", 'sum(rate(http_requests_total{job="api",status=~"5.."}[30s])) / clamp_min(sum(rate(http_requests_total{job="api"}[30s])),0.001)'),
    ("api_4xx_ratio", 'sum(rate(http_requests_total{job="api",status=~"4.."}[30s])) / clamp_min(sum(rate(http_requests_total{job="api"}[30s])),0.001)'),
    ("auth_rps", 'sum(rate(http_requests_total{job="auth"}[30s]))'),
    ("auth_p95", 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{job="auth"}[30s])))'),
    ("auth_401_ratio", 'sum(rate(http_requests_total{job="auth",status="401"}[30s])) / clamp_min(sum(rate(http_requests_total{job="auth"}[30s])),0.001)'),
    ("sim_success_ratio", 'sum(rate(sim_requests_total{outcome="ok"}[30s])) / clamp_min(sum(rate(sim_requests_total[30s])),0.001)'),
    ("sim_p95", 'histogram_quantile(0.95, sum by (le) (rate(sim_latency_seconds_bucket[30s])))'),
    # CPU / mémoire des processus : exposés par prometheus_client dans chaque service (indépendant de cAdvisor)
    ("api_cpu", 'sum(rate(process_cpu_seconds_total{job="api"}[30s]))'),
    ("auth_cpu", 'sum(rate(process_cpu_seconds_total{job="auth"}[30s]))'),
    ("api_mem_mb", 'process_resident_memory_bytes{job="api"} / 1e6'),
]
NAMES = [n for n, _ in FEATURES]


def _clean(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if math.isnan(f) or math.isinf(f) else f


def fetch_features(client, prometheus_url: str) -> dict:
    """client : httpx.Client (sync). Retourne {nom: valeur}."""
    out = {}
    for name, query in FEATURES:
        try:
            r = client.get(f"{prometheus_url}/api/v1/query", params={"query": query}, timeout=3)
            res = r.json()["data"]["result"]
            out[name] = _clean(res[0]["value"][1]) if res else 0.0
        except Exception:  # noqa: BLE001 — une feature absente vaut 0, le modèle ne doit pas planter
            out[name] = 0.0
    return out


def to_vector(features: dict) -> list:
    return [features.get(n, 0.0) for n in NAMES]
