"""Service d'authentification : émet et vérifie des JWT. Mots de passe bcrypt (pgcrypto côté seed)."""
import json
import os
import time
from contextlib import asynccontextmanager

import bcrypt
import jwt
from fastapi import FastAPI, Header, HTTPException, Request
from prometheus_fastapi_instrumentator import Instrumentator, metrics
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]
JWT_SECRET = os.environ["JWT_SECRET"]
JWT_TTL = int(os.getenv("JWT_TTL_SECONDS", "3600"))

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=8, open=False)
# hash factice pour garder un temps de réponse constant quand l'email n'existe pas
_DUMMY_HASH = bcrypt.hashpw(b"not-a-real-password", bcrypt.gensalt(10))


def event(**kv):
    print(json.dumps({"ts": time.time(), **kv}), flush=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    pool.open()
    yield
    pool.close()


app = FastAPI(title="RESILIENCE-ALPHA — auth", lifespan=lifespan)
_LATENCY_BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
Instrumentator(excluded_handlers=["/metrics", "/health"]).add(
    metrics.default(latency_lowr_buckets=_LATENCY_BUCKETS)
).instrument(app).expose(app, include_in_schema=False)


class LoginIn(BaseModel):
    email: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


def _check_credentials(email: str, password: str):
    with pool.connection() as conn:
        row = conn.execute("SELECT id, password_hash, role FROM users WHERE email = %s", (email,)).fetchone()
    if row is None:
        bcrypt.checkpw(password.encode(), _DUMMY_HASH)
        return None
    user_id, pw_hash, role = row
    return (user_id, role) if bcrypt.checkpw(password.encode(), pw_hash.encode()) else None


@app.post("/login", response_model=TokenOut)
def login(body: LoginIn, request: Request):
    client_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "?")
    result = _check_credentials(body.email, body.password)
    if result is None:
        event(event="login_failed", email=body.email, ip=client_ip)
        raise HTTPException(status_code=401, detail="invalid credentials")
    user_id, role = result
    now = int(time.time())
    token = jwt.encode(
        {"sub": str(user_id), "email": body.email, "role": role, "iat": now, "exp": now + JWT_TTL},
        JWT_SECRET,
        algorithm="HS256",
    )
    event(event="login_ok", user_id=user_id, ip=client_ip)
    return TokenOut(access_token=token, expires_in=JWT_TTL)


@app.get("/verify")
def verify(authorization: str = Header(default="")):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        claims = jwt.decode(authorization[7:], JWT_SECRET, algorithms=["HS256"])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail=f"invalid token: {exc.__class__.__name__}")
    return {"sub": claims["sub"], "email": claims.get("email"), "role": claims.get("role")}


@app.get("/health")
def health():
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — on veut un 503 quelle que soit la cause
        raise HTTPException(status_code=503, detail=f"db unavailable: {exc.__class__.__name__}")
    return {"status": "ok", "service": "auth"}
