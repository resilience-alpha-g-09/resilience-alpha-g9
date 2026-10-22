"""Tests d'intégration contre la stack démarrée (make up). Lancer : pytest tests/ -q
Variables : EDGE (défaut http://127.0.0.1), DEMO_PASSWORD (défaut demo1234)."""
import os

import httpx
import pytest

EDGE = os.environ.get("EDGE", "http://127.0.0.1").rstrip("/")
PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")


@pytest.fixture(scope="session")
def client():
    with httpx.Client(base_url=EDGE, timeout=10) as c:
        yield c


@pytest.fixture(scope="session")
def token(client):
    r = client.post("/auth/login", json={"email": "alice@demo.local", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_healthz(client):
    assert client.get("/healthz").status_code == 200


def test_login_rejects_bad_password(client):
    assert client.post("/auth/login", json={"email": "alice@demo.local", "password": "nope"}).status_code == 401


def test_requires_token(client):
    assert client.get("/api/transactions").status_code == 401


def test_payment_flow(client, token):
    h = {"Authorization": f"Bearer {token}"}
    before = client.get("/api/accounts/me", headers=h).json()[0]["balance"]
    r = client.post("/api/transactions", headers=h, json={"to_account": 10, "amount": "3.50"})
    assert r.status_code == 201, r.text
    tx = r.json()
    assert tx["status"] == "completed"
    assert client.get(f"/api/transactions/{tx['id']}", headers=h).json()["id"] == tx["id"]
    after = client.get("/api/accounts/me", headers=h).json()[0]["balance"]
    assert round(float(before) - float(after), 2) == 3.5


def test_insufficient_funds(client, token):
    h = {"Authorization": f"Bearer {token}"}
    r = client.post("/api/transactions", headers=h, json={"to_account": 10, "amount": "999999999.00"})
    assert r.status_code == 409


def test_waf_blocks_obvious_injection(client):
    r = client.get("/api/transactions", params={"q": "' OR 1=1 --"})
    assert r.status_code == 403
