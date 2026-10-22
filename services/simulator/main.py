"""Trafic métier normal : des clients fictifs paient une boutique fictive, via l'edge comme un vrai client.
Sert de baseline au modèle ML et rend visible l'état « avant / pendant / après » sur le dashboard."""
import asyncio
import json
import os
import random
import time

import httpx
from prometheus_client import Counter, Gauge, Histogram, start_http_server

TARGET = os.environ.get("TARGET_URL", "http://edge:8080").rstrip("/")
RATE = float(os.getenv("RATE_PER_SECOND", "3"))
USERS = [u for u in os.environ["SIM_USERS"].split(",") if u]
PASSWORD = os.environ["SIM_PASSWORD"]
PAYEES = [10, 10, 10, 10, 1, 2, 3]  # surtout la boutique (compte 10), parfois des particuliers

REQUESTS = Counter("sim_requests_total", "Paiements simulés", ["outcome"])
LATENCY = Histogram("sim_latency_seconds", "Latence bout en bout d'un paiement simulé",
                    buckets=(0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10))
LOGGED_IN = Gauge("sim_logged_in_users", "Utilisateurs simulés détenant un token valide")

tokens: dict[str, str] = {}


def event(**kv):
    print(json.dumps({"ts": time.time(), **kv}), flush=True)


async def login(client: httpx.AsyncClient, email: str) -> bool:
    try:
        r = await client.post(f"{TARGET}/auth/login", json={"email": email, "password": PASSWORD}, timeout=5)
        if r.status_code == 200:
            tokens[email] = r.json()["access_token"]
            LOGGED_IN.set(len(tokens))
            return True
        event(event="login_failed", email=email, status=r.status_code)
    except httpx.HTTPError as exc:
        event(event="login_error", email=email, error=exc.__class__.__name__)
    tokens.pop(email, None)
    LOGGED_IN.set(len(tokens))
    return False


async def one_payment(client: httpx.AsyncClient, email: str):
    if email not in tokens and not await login(client, email):
        REQUESTS.labels("auth_error").inc()
        return
    payload = {"to_account": random.choice(PAYEES), "amount": round(random.uniform(1, 80), 2), "origin": "simulator"}
    t0 = time.perf_counter()
    try:
        r = await client.post(f"{TARGET}/api/transactions", json=payload,
                              headers={"Authorization": f"Bearer {tokens[email]}"}, timeout=10)
    except httpx.HTTPError as exc:
        REQUESTS.labels("network_error").inc()
        event(event="payment_failed", error=exc.__class__.__name__)
        return
    LATENCY.observe(time.perf_counter() - t0)
    if r.status_code == 201:
        REQUESTS.labels("ok").inc()
    elif r.status_code == 401:
        tokens.pop(email, None)
        LOGGED_IN.set(len(tokens))
        REQUESTS.labels("auth_error").inc()
    elif r.status_code == 409:
        REQUESTS.labels("rejected").inc()
    else:
        REQUESTS.labels("http_error").inc()
        event(event="payment_failed", status=r.status_code)


async def main():
    start_http_server(9100)
    async with httpx.AsyncClient() as client:
        while True:
            try:
                if (await client.get(f"{TARGET}/healthz", timeout=3)).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
        event(event="simulator_started", target=TARGET, rate=RATE, users=len(USERS))
        interval = 1.0 / RATE
        while True:
            asyncio.create_task(one_payment(client, random.choice(USERS)))
            await asyncio.sleep(interval)


if __name__ == "__main__":
    asyncio.run(main())
