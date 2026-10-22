"""API métier : comptes et transactions fictives. Le token est vérifié auprès du service auth."""
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

DATABASE_URL = os.environ["DATABASE_URL"]
AUTH_VERIFY_URL = os.environ.get("AUTH_VERIFY_URL", "http://auth:8000/verify")
AUTH_TIMEOUT = float(os.getenv("AUTH_TIMEOUT_SECONDS", "2"))

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=False, kwargs={"row_factory": dict_row})


def event(**kv):
    print(json.dumps({"ts": time.time(), **kv}), flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    app.state.http = httpx.AsyncClient(timeout=AUTH_TIMEOUT)
    yield
    await app.state.http.aclose()
    pool.close()


app = FastAPI(title="RESILIENCE-ALPHA — api", lifespan=lifespan)
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
Instrumentator(excluded_handlers=["/metrics", "/health"]).add(
    metrics.default(latency_lowr_buckets=_LATENCY_BUCKETS)
).instrument(app).expose(app, include_in_schema=False)


async def current_user(authorization: str = Header(default="")) -> dict:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        r = await app.state.http.get(AUTH_VERIFY_URL, headers={"Authorization": authorization})
    except httpx.HTTPError as exc:
        event(event="auth_unreachable", error=exc.__class__.__name__)
        raise HTTPException(status_code=503, detail="auth service unavailable")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="invalid token")
    return r.json()


class TxIn(BaseModel):
    to_account: int = Field(gt=0)
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    origin: str = Field(default="web", pattern="^(web|simulator|verifier)$")


class TxOut(BaseModel):
    id: int
    from_account: int
    to_account: int
    amount: Decimal
    status: str
    reason: str | None
    origin: str
    created_at: datetime


@app.get("/accounts/me")
def my_accounts(user: dict = Depends(current_user)):
    with pool.connection() as conn:
        return conn.execute(
            "SELECT id, label, balance, currency FROM accounts WHERE user_id = %s ORDER BY id", (int(user["sub"]),)
        ).fetchall()


@app.post("/transactions", status_code=201, response_model=TxOut)
def create_transaction(body: TxIn, user: dict = Depends(current_user)):
    user_id = int(user["sub"])
    with pool.connection() as conn, conn.transaction():
        src = conn.execute(
            "SELECT id, balance FROM accounts WHERE user_id = %s ORDER BY id LIMIT 1 FOR UPDATE", (user_id,)
        ).fetchone()
        if src is None:
            raise HTTPException(status_code=404, detail="no account for this user")
        if body.to_account == src["id"]:
            raise HTTPException(status_code=400, detail="cannot pay yourself")
        dst = conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (body.to_account,)).fetchone()
        if dst is None:
            raise HTTPException(status_code=404, detail="destination account not found")
        if src["balance"] < body.amount:
            row = conn.execute(
                "INSERT INTO transactions (from_account, to_account, amount, status, reason, origin) "
                "VALUES (%s, %s, %s, 'rejected', 'insufficient_funds', %s) RETURNING *",
                (src["id"], body.to_account, body.amount, body.origin),
            ).fetchone()
            event(event="tx_rejected", tx_id=row["id"], reason="insufficient_funds", origin=body.origin)
            raise HTTPException(status_code=409, detail={"status": "rejected", "reason": "insufficient_funds", "id": row["id"]})
        conn.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s", (body.amount, src["id"]))
        conn.execute("UPDATE accounts SET balance = balance + %s WHERE id = %s", (body.amount, body.to_account))
        row = conn.execute(
            "INSERT INTO transactions (from_account, to_account, amount, status, origin) "
            "VALUES (%s, %s, %s, 'completed', %s) RETURNING *",
            (src["id"], body.to_account, body.amount, body.origin),
        ).fetchone()
    event(event="tx_completed", tx_id=row["id"], amount=str(body.amount), origin=body.origin)
    return row


@app.get("/transactions", response_model=list[TxOut])
def list_transactions(user: dict = Depends(current_user), limit: int = 50):
    limit = max(1, min(limit, 200))
    with pool.connection() as conn:
        return conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id IN (t.from_account, t.to_account) "
            "WHERE a.user_id = %s ORDER BY t.created_at DESC, t.id DESC LIMIT %s",
            (int(user["sub"]), limit),
        ).fetchall()


@app.get("/transactions/{tx_id}", response_model=TxOut)
def get_transaction(tx_id: int, user: dict = Depends(current_user)):
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT t.* FROM transactions t JOIN accounts a ON a.id IN (t.from_account, t.to_account) "
            "WHERE t.id = %s AND a.user_id = %s LIMIT 1",
            (tx_id, int(user["sub"])),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return row


@app.get("/health")
def health():
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc.__class__.__name__}")
    return {"status": "ok", "service": "api"}
