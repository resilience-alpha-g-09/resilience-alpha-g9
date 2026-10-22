"""Back-office RESILIENCE-ALPHA — authentification renforcée (consigne additionnelle 1).

Flux : identifiant → caméra (agent du PC Hôte, reconnaissance faciale) → si reconnu : mot de passe → session.
Sinon : identifiant effacé, caméra éteinte, LED rouge 30 s. Deux échecs (visage ou mot de passe) → blocage 5 min.
Habilitations : opérateurs, rôles (admin / operator), enrôlement des visages, activation, déblocage, journal des tentatives.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import bcrypt
import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_fastapi_instrumentator import Instrumentator
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel, Field

from .led import STATES, Led

ROOT = "/admin"
DATABASE_URL = os.environ["DATABASE_URL"]
SECRET = os.environ["BACKOFFICE_SECRET"]
AGENT_URL = os.environ.get("AGENT_URL", "http://host.docker.internal:8090").rstrip("/")
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
ADMIN_LOGIN = os.environ.get("BACKOFFICE_ADMIN_LOGIN", "admin")
ADMIN_PASSWORD = os.environ.get("BACKOFFICE_ADMIN_PASSWORD", "")
MAX_FAILURES = int(os.environ.get("BO_MAX_FAILURES", "2"))          # « peut recommencer 1 fois puis est bloqué »
LOCKOUT_SECONDS = int(os.environ.get("BO_LOCKOUT_SECONDS", "300"))  # 5 min
FACE_STEP_TTL = int(os.environ.get("BO_FACE_STEP_TTL", "120"))      # délai pour saisir le mot de passe après reconnaissance
FACE_TIMEOUT = float(os.environ.get("BO_FACE_TIMEOUT", "10"))
LED_SOLID = int(os.environ.get("BO_LED_SOLID_SECONDS", "30"))
LED_RED_BLINK = int(os.environ.get("BO_LED_RED_BLINK_SECONDS", "5"))
SESSION_HOURS = 8
CAMERA_MODE = os.environ.get("CAMERA_MODE", "browser")          # browser : webcam du navigateur (page) · server : webcam ouverte par l'agent
FACE_STABILITY = int(os.environ.get("BO_FACE_STABILITY", "5"))  # verdicts « reconnu » consécutifs requis (mode navigateur)
FLOWS: dict = {}                                                 # flux d'identification en cours (mode navigateur), par id

pool = ConnectionPool(DATABASE_URL, min_size=1, max_size=6, open=False, kwargs={"row_factory": dict_row})
led = Led()
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=3.0), headers={"X-Agent-Token": AGENT_TOKEN})


def event(**kv):
    print(json.dumps({"ts": time.time(), "service": "backoffice", **kv}), flush=True)


def now():
    return datetime.now(timezone.utc)


# ── schéma & amorçage ──
SCHEMA = """
CREATE TABLE IF NOT EXISTS bo_operators (
    id SERIAL PRIMARY KEY, login TEXT UNIQUE NOT NULL, display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'operator' CHECK (role IN ('admin','operator')),
    face_enrolled BOOLEAN NOT NULL DEFAULT false, active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS bo_login_attempts (
    id BIGSERIAL PRIMARY KEY, login TEXT NOT NULL, step TEXT NOT NULL, result TEXT NOT NULL,
    detail TEXT, ip TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS bo_login_attempts_created_idx ON bo_login_attempts (created_at DESC);
CREATE TABLE IF NOT EXISTS bo_lockouts (login TEXT PRIMARY KEY, failures INT NOT NULL DEFAULT 0, locked_until TIMESTAMPTZ);
"""


def init_db():
    with pool.connection() as conn:
        conn.execute(SCHEMA)
        n = conn.execute("SELECT count(*) AS n FROM bo_operators").fetchone()["n"]
        if n == 0 and ADMIN_PASSWORD:
            conn.execute(
                "INSERT INTO bo_operators (login, display_name, password_hash, role) VALUES (%s, %s, %s, 'admin')",
                (ADMIN_LOGIN, "Administrateur", bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt(10)).decode()),
            )
            event(event="bo_admin_seeded", login=ADMIN_LOGIN)


async def sync_enrollments():
    """Aligne le drapeau face_enrolled sur ce que l'agent connaît réellement."""
    try:
        r = await http.get(f"{AGENT_URL}/health", timeout=3)
        enrolled = set(r.json().get("enrolled", []))
    except Exception:  # noqa: BLE001
        return None
    with pool.connection() as conn:
        conn.execute("UPDATE bo_operators SET face_enrolled = (login = ANY(%s))", (list(enrolled),))
    return enrolled


# ── cookies signés ──
def _sign(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    sig = hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    raw, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except Exception:  # noqa: BLE001
        return None
    return data if data.get("exp", 0) > time.time() else None


def set_cookie(resp, name, value, max_age):
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax", path=ROOT)


def clear_cookie(resp, name):
    resp.delete_cookie(name, path=ROOT)


def current_user(request: Request) -> dict | None:
    tok = request.cookies.get("bo_session")
    if not tok:
        return None
    try:
        return jwt.decode(tok, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def require_user(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(status_code=401, detail="session requise")
    return u


def require_admin(request: Request) -> dict:
    u = require_user(request)
    if u.get("role") != "admin":
        raise HTTPException(status_code=403, detail="réservé aux administrateurs")
    return u


def client_ip(request: Request) -> str:
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


# ── verrouillage & journal ──
def record(login: str, step: str, result: str, ip: str, detail: str | None = None):
    with pool.connection() as conn:
        conn.execute("INSERT INTO bo_login_attempts (login, step, result, detail, ip) VALUES (%s,%s,%s,%s,%s)",
                     (login, step, result, detail, ip))
    event(event="bo_login_attempt", login=login, step=step, result=result, ip=ip, detail=detail)


def lock_status(login: str) -> dict:
    with pool.connection() as conn:
        row = conn.execute("SELECT failures, locked_until FROM bo_lockouts WHERE login=%s", (login,)).fetchone()
    if row and row["locked_until"] and row["locked_until"] > now():
        return {"locked": True, "until": row["locked_until"], "failures": row["failures"]}
    return {"locked": False, "until": None, "failures": row["failures"] if row else 0}


def register_failure(login: str) -> dict:
    """Retourne {locked, until, remaining} après comptabilisation de l'échec."""
    with pool.connection() as conn, conn.transaction():
        row = conn.execute("SELECT failures, locked_until FROM bo_lockouts WHERE login=%s FOR UPDATE", (login,)).fetchone()
        failures = (row["failures"] if row and not (row["locked_until"] and row["locked_until"] <= now()) else 0) + 1
        if failures >= MAX_FAILURES:
            until = now() + timedelta(seconds=LOCKOUT_SECONDS)
            conn.execute("INSERT INTO bo_lockouts (login, failures, locked_until) VALUES (%s, 0, %s) "
                         "ON CONFLICT (login) DO UPDATE SET failures = 0, locked_until = EXCLUDED.locked_until", (login, until))
            event(event="bo_lockout", login=login, until=until.isoformat())
            return {"locked": True, "until": until, "remaining": 0}
        conn.execute("INSERT INTO bo_lockouts (login, failures, locked_until) VALUES (%s, %s, NULL) "
                     "ON CONFLICT (login) DO UPDATE SET failures = EXCLUDED.failures, locked_until = NULL", (login, failures))
    return {"locked": False, "until": None, "remaining": MAX_FAILURES - failures}


def clear_failures(login: str):
    with pool.connection() as conn:
        conn.execute("DELETE FROM bo_lockouts WHERE login=%s", (login,))


def get_operator(login: str) -> dict | None:
    with pool.connection() as conn:
        return conn.execute("SELECT * FROM bo_operators WHERE login=%s", (login,)).fetchone()


# ── application ──
@asynccontextmanager
async def lifespan(app: FastAPI):
    pool.open()
    init_db()
    await sync_enrollments()
    led.off("startup")
    event(event="backoffice_started", agent=AGENT_URL, max_failures=MAX_FAILURES, lockout_s=LOCKOUT_SECONDS)
    yield
    await http.aclose()
    pool.close()


app = FastAPI(title="RESILIENCE-ALPHA — back-office", lifespan=lifespan, docs_url=None, redoc_url=None)
Instrumentator(excluded_handlers=["/metrics", "/health", f"{ROOT}/api/led-state", f"{ROOT}/api/access-log"]).instrument(app).expose(app, include_in_schema=False)
app.mount(f"{ROOT}/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
ASSET_V = str(int(os.path.getmtime(os.path.join(_STATIC_DIR, "style.css"))))   # versionne la feuille de style : plus de cache périmé


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"root": ROOT, "user": current_user(request), "asset_v": ASSET_V, **ctx})


@app.get("/health")
def health():
    return {"status": "ok", "service": "backoffice", "led": led.current()["state"]}


# ── LED ──
@app.get(f"{ROOT}/api/led-state")
def led_state():
    return JSONResponse(led.current(), headers={"Cache-Control": "no-store"})


@app.get(f"{ROOT}/api/access-log")
def access_log(response: Response, limit: int = 8):
    """Lecture seule pour la page de supervision : dernières tentatives, identifiants bloqués, compteurs."""
    limit = max(1, min(limit, 50))
    with pool.connection() as conn:
        attempts = conn.execute("SELECT id, login, step, result, detail, created_at FROM bo_login_attempts ORDER BY id DESC LIMIT %s", (limit,)).fetchall()
        locked = conn.execute("SELECT login, locked_until FROM bo_lockouts WHERE locked_until > now() ORDER BY locked_until").fetchall()
        counts = conn.execute("SELECT count(*) FILTER (WHERE face_enrolled) AS enrolled, count(*) FILTER (WHERE active) AS active FROM bo_operators").fetchone()
    response.headers["Cache-Control"] = "no-store"
    return {"attempts": attempts, "locked": locked, "counts": counts, "led": led.current()}   # datetime sérialisés par FastAPI


class LedTest(BaseModel):
    state: str


@app.post(f"{ROOT}/api/led/test")
def led_test(body: LedTest, user: dict = Depends(require_admin)):
    if body.state not in STATES:
        raise HTTPException(status_code=422, detail=f"état inconnu, attendu : {', '.join(STATES)}")
    seconds = {"green_solid": LED_SOLID, "red_solid": LED_SOLID, "red_blink": LED_RED_BLINK, "green_blink": FACE_STEP_TTL}.get(body.state)
    led.set(body.state, seconds, f"test:{user['sub']}")
    return led.current()


# ── connexion en trois étapes ──
class IdentifyIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")


class PasswordIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)


@app.get(f"{ROOT}/login", response_class=HTMLResponse)
def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(f"{ROOT}/", status_code=303)
    flow = _unsign(request.cookies.get("bo_flow"))
    return render(request, "login.html", flow=flow, face_timeout=int(FACE_TIMEOUT), camera_mode=CAMERA_MODE,
                  needs_bootstrap=_needs_bootstrap())


def _needs_bootstrap() -> bool:
    with pool.connection() as conn:
        return conn.execute("SELECT count(*) AS n FROM bo_operators WHERE face_enrolled").fetchone()["n"] == 0


# ── amorçage : enrôler le visage du premier administrateur (mot de passe seul, uniquement tant qu'aucun visage n'existe) ──
class BootstrapIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=1, max_length=256)


@app.get(f"{ROOT}/bootstrap", response_class=HTMLResponse)
def bootstrap_page(request: Request):
    if not _needs_bootstrap():
        return RedirectResponse(f"{ROOT}/login", status_code=303)
    return render(request, "bootstrap.html", camera_mode=CAMERA_MODE)


@app.post(f"{ROOT}/api/bootstrap/password")
def bootstrap_password(body: BootstrapIn, request: Request):
    if not _needs_bootstrap():
        raise HTTPException(status_code=409, detail="un visage est déjà enrôlé : passer par la connexion normale")
    op = get_operator(body.login)
    if not (op and op["active"] and op["role"] == "admin" and bcrypt.checkpw(body.password.encode(), op["password_hash"].encode())):
        record(body.login, "bootstrap", "rejected", client_ip(request))
        raise HTTPException(status_code=401, detail="identifiant ou mot de passe administrateur incorrect")
    record(body.login, "bootstrap", "password_ok", client_ip(request))
    resp = JSONResponse({"ok": True, "login": body.login})
    set_cookie(resp, "bo_boot", _sign({"login": body.login, "exp": time.time() + 300}), 300)
    return resp


class ImagesIn(BaseModel):
    images: list = Field(min_length=1, max_length=80)


async def _enroll_images_for(login: str, images: list, by: str) -> dict:
    try:
        r = await http.post(f"{AGENT_URL}/enroll-images", json={"login": login, "images": images}, timeout=60)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"agent de reconnaissance injoignable ({exc.__class__.__name__})")
    if r.status_code != 200:
        detail = r.json().get("detail", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
        raise HTTPException(status_code=422 if r.status_code == 422 else 502, detail=detail)
    with pool.connection() as conn:
        conn.execute("UPDATE bo_operators SET face_enrolled = true WHERE login=%s", (login,))
    event(event="bo_face_enrolled", login=login, by=by, frames=r.json().get("frames"), source="browser")
    return r.json()


@app.post(f"{ROOT}/api/bootstrap/enroll-images")
async def bootstrap_enroll(body: ImagesIn, request: Request):
    boot = _unsign(request.cookies.get("bo_boot"))
    if not boot or not _needs_bootstrap():
        raise HTTPException(status_code=401, detail="amorçage non autorisé")
    res = await _enroll_images_for(boot["login"], body.images, "bootstrap")
    resp = JSONResponse({**res, "redirect": f"{ROOT}/login"})
    clear_cookie(resp, "bo_boot")
    return resp


@app.post(f"{ROOT}/api/operators/{{login}}/enroll-images")
async def enroll_operator_images(login: str, body: ImagesIn, user: dict = Depends(require_admin)):
    """Enrôlement depuis la webcam du navigateur (page Habilitations) : la personne se place devant la caméra du PC Hôte."""
    if not get_operator(login):
        raise HTTPException(status_code=404, detail="opérateur inconnu")
    return await _enroll_images_for(login, body.images, user["sub"])


def _face_ok_response(login: str, ip: str, detail: str):
    record(login, "face", "recognized", ip, detail)
    led.set("green_blink", FACE_STEP_TTL, f"face_ok:{login}")
    resp = JSONResponse({"step": "password", "login": login, "ttl_s": FACE_STEP_TTL})
    set_cookie(resp, "bo_flow", _sign({"login": login, "face_ok": True, "exp": time.time() + FACE_STEP_TTL}), FACE_STEP_TTL)
    return resp


def _face_rejected_response(login: str, ip: str, reason: str):
    record(login, "face", "rejected", ip, reason)
    led.set("red_solid", LED_SOLID, f"face_rejected:{login}")
    fail = register_failure(login)
    resp = JSONResponse({"step": "identify", "error": "Visage non reconnu : identifiant effacé, caméra éteinte",
                         "red_seconds": LED_SOLID, "remaining_attempts": fail["remaining"],
                         "locked_until": fail["until"].isoformat() if fail["locked"] else None},
                        status_code=423 if fail["locked"] else 401)
    clear_cookie(resp, "bo_flow")
    return resp


def _purge_flows():
    now_t = time.time()
    for k in [k for k, v in FLOWS.items() if v["deadline"] + 60 < now_t]:
        FLOWS.pop(k, None)


class FrameIn(BaseModel):
    image: str = Field(min_length=100, max_length=2_000_000)


@app.post(f"{ROOT}/api/login/start")
def login_start(body: IdentifyIn, request: Request):
    """Mode navigateur, étape 1 → 2 : l'identifiant est saisi, la page va activer la webcam et envoyer des images."""
    login, ip = body.login, client_ip(request)
    st = lock_status(login)
    if st["locked"]:
        record(login, "identify", "locked", ip)
        return JSONResponse({"step": "identify", "error": "Identifiant bloqué", "locked_until": st["until"].isoformat()}, status_code=423)
    _purge_flows()
    fid = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    FLOWS[fid] = {"login": login, "streak": 0, "frames": 0, "deadline": time.time() + FACE_TIMEOUT, "best": None}
    record(login, "identify", "camera_started", ip)
    resp = JSONResponse({"step": "camera", "login": login, "timeout_s": FACE_TIMEOUT, "stability": FACE_STABILITY})
    set_cookie(resp, "bo_flow", _sign({"login": login, "face_ok": False, "fid": fid, "exp": time.time() + FACE_TIMEOUT + 30}), int(FACE_TIMEOUT) + 30)
    return resp


@app.post(f"{ROOT}/api/login/frame")
async def login_frame(body: FrameIn, request: Request):
    """Mode navigateur, étape 2 : une image de la webcam → verdict de l'agent → compteur de verdicts concordants."""
    flow_c = _unsign(request.cookies.get("bo_flow"))
    ip = client_ip(request)
    fl = FLOWS.get(flow_c.get("fid")) if flow_c and not flow_c.get("face_ok") else None
    if not fl:
        resp = JSONResponse({"step": "identify", "error": "Identification expirée : recommencer"}, status_code=401)
        clear_cookie(resp, "bo_flow")
        return resp
    login = fl["login"]
    op = get_operator(login)
    try:
        r = await http.post(f"{AGENT_URL}/analyze", json={"login": login, "image": body.image}, timeout=8)
        res = r.json() if r.status_code == 200 else {"verdict": "agent_error", "detail": r.text[:120]}
    except httpx.HTTPError as exc:
        FLOWS.pop(flow_c["fid"], None)
        record(login, "face", "camera_unavailable", ip, exc.__class__.__name__)
        led.off("camera_unavailable")
        resp = JSONResponse({"step": "identify", "error": "Agent de reconnaissance injoignable sur le PC Hôte"}, status_code=503)
        clear_cookie(resp, "bo_flow")
        return resp
    fl["frames"] += 1
    verdict = res.get("verdict", "none")
    if res.get("distance") is not None:
        fl["best"] = res["distance"] if fl["best"] is None else min(fl["best"], res["distance"])
    valid_op = op is not None and op["active"] and op["face_enrolled"]
    fl["streak"] = fl["streak"] + 1 if (verdict == "recognized" and valid_op) else 0
    if fl["streak"] >= FACE_STABILITY:
        FLOWS.pop(flow_c["fid"], None)
        return _face_ok_response(login, ip, f"distance={fl['best']} frames={fl['frames']}")
    if time.time() >= fl["deadline"]:
        FLOWS.pop(flow_c["fid"], None)
        reason = "timeout" if fl["frames"] else "no_frames"
        if not valid_op:
            reason = "unknown_operator"
        return _face_rejected_response(login, ip, reason)
    return {"step": "camera", "verdict": verdict if valid_op or verdict in ("none", "multiple") else "unknown",
            "streak": fl["streak"], "needed": FACE_STABILITY, "box": res.get("box"),
            "remaining_s": max(0, round(fl["deadline"] - time.time(), 1))}


@app.post(f"{ROOT}/api/login/identify")
async def login_identify(body: IdentifyIn, request: Request):
    """Étape 1 → 2 : identifiant saisi, la caméra s'active pour confirmer l'identité."""
    login, ip = body.login, client_ip(request)
    st = lock_status(login)
    if st["locked"]:
        record(login, "identify", "locked", ip)
        return JSONResponse({"step": "identify", "error": "Identifiant bloqué", "locked_until": st["until"].isoformat()}, status_code=423)
    op = get_operator(login)
    # Un identifiant inconnu ou désactivé suit le même chemin (caméra, échec) : pas d'énumération des comptes.
    try:
        r = await http.post(f"{AGENT_URL}/verify", json={"login": login, "timeout_s": FACE_TIMEOUT})
        res = r.json() if r.status_code == 200 else {"result": "agent_error", "detail": r.text[:200]}
    except httpx.HTTPError as exc:
        record(login, "face", "camera_unavailable", ip, exc.__class__.__name__)
        led.off("camera_unavailable")
        return JSONResponse({"step": "identify", "error": "Caméra indisponible : agent du PC Hôte injoignable"}, status_code=503)
    recognized = res.get("result") == "recognized" and op is not None and op["active"] and op["face_enrolled"]
    if recognized:
        return _face_ok_response(login, ip, f"distance={res.get('distance')}")
    reason = res.get("result", "unknown") if op and op["active"] else "unknown_operator"
    return _face_rejected_response(login, ip, reason)


@app.post(f"{ROOT}/api/login/password")
def login_password(body: PasswordIn, request: Request):
    """Étape 3 : mot de passe, autorisé seulement après reconnaissance du visage."""
    flow = _unsign(request.cookies.get("bo_flow"))
    ip = client_ip(request)
    if not flow or not flow.get("face_ok"):
        led.off("flow_expired")
        resp = JSONResponse({"step": "identify", "error": "Reconnaissance expirée : recommencer l'identification"}, status_code=401)
        clear_cookie(resp, "bo_flow")
        return resp
    login = flow["login"]
    op = get_operator(login)
    ok = bool(op and op["active"] and bcrypt.checkpw(body.password.encode(), op["password_hash"].encode()))
    if ok:
        record(login, "password", "success", ip)
        clear_failures(login)
        led.set("green_solid", LED_SOLID, f"login_ok:{login}")
        token = jwt.encode({"sub": login, "role": op["role"], "name": op["display_name"],
                            "exp": int(time.time()) + SESSION_HOURS * 3600}, SECRET, algorithm="HS256")
        resp = JSONResponse({"ok": True, "redirect": f"{ROOT}/"})
        set_cookie(resp, "bo_session", token, SESSION_HOURS * 3600)
        clear_cookie(resp, "bo_flow")
        return resp
    record(login, "password", "rejected", ip)
    led.set("red_blink", LED_RED_BLINK, f"password_rejected:{login}")
    fail = register_failure(login)
    resp = JSONResponse({"step": "identify", "error": "Mot de passe refusé : retour à l'identification",
                         "remaining_attempts": fail["remaining"],
                         "locked_until": fail["until"].isoformat() if fail["locked"] else None},
                        status_code=423 if fail["locked"] else 401)
    clear_cookie(resp, "bo_flow")
    return resp


@app.post(f"{ROOT}/api/login/cancel")
def login_cancel(request: Request):
    flow_c = _unsign(request.cookies.get("bo_flow"))
    if flow_c and flow_c.get("fid"):
        FLOWS.pop(flow_c["fid"], None)
    led.off("cancelled")
    resp = JSONResponse({"step": "identify"})
    clear_cookie(resp, "bo_flow")
    return resp


@app.get(f"{ROOT}/logout")
def logout():
    resp = RedirectResponse(f"{ROOT}/login", status_code=303)
    clear_cookie(resp, "bo_session")
    clear_cookie(resp, "bo_flow")
    return resp


# ── pages ──
@app.get(f"{ROOT}/", response_class=HTMLResponse)
async def home(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"{ROOT}/login", status_code=303)
    try:
        agent = (await http.get(f"{AGENT_URL}/health", timeout=3)).json()
    except Exception:  # noqa: BLE001
        agent = None
    with pool.connection() as conn:
        counts = conn.execute("SELECT count(*) AS total, count(*) FILTER (WHERE face_enrolled) AS enrolled, "
                              "count(*) FILTER (WHERE active) AS active FROM bo_operators").fetchone()
        attempts = conn.execute("SELECT * FROM bo_login_attempts ORDER BY id DESC LIMIT 8").fetchall()
        locked = conn.execute("SELECT login, locked_until FROM bo_lockouts WHERE locked_until > now() ORDER BY locked_until").fetchall()
    return render(request, "home.html", agent=agent, counts=counts, attempts=attempts, locked=locked, led=led.current(), states=STATES)


@app.get(f"{ROOT}/operators", response_class=HTMLResponse)
def operators_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"{ROOT}/login", status_code=303)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="réservé aux administrateurs")
    with pool.connection() as conn:
        ops = conn.execute("SELECT o.*, l.failures, l.locked_until FROM bo_operators o LEFT JOIN bo_lockouts l ON l.login = o.login ORDER BY o.id").fetchall()
    return render(request, "operators.html", operators=ops, now=now(), camera_mode=CAMERA_MODE)


@app.get(f"{ROOT}/attempts", response_class=HTMLResponse)
def attempts_page(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse(f"{ROOT}/login", status_code=303)
    with pool.connection() as conn:
        rows = conn.execute("SELECT * FROM bo_login_attempts ORDER BY id DESC LIMIT 200").fetchall()
    return render(request, "attempts.html", attempts=rows)


# ── habilitations (admin) ──
class OperatorIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=256)
    role: str = Field("operator", pattern="^(admin|operator)$")


@app.post(f"{ROOT}/api/operators", status_code=201)
def create_operator(body: OperatorIn, user: dict = Depends(require_admin)):
    with pool.connection() as conn:
        if conn.execute("SELECT 1 FROM bo_operators WHERE login=%s", (body.login,)).fetchone():
            raise HTTPException(status_code=409, detail="identifiant déjà utilisé")
        row = conn.execute("INSERT INTO bo_operators (login, display_name, password_hash, role) VALUES (%s,%s,%s,%s) RETURNING id, login, role",
                           (body.login, body.display_name, bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(10)).decode(), body.role)).fetchone()
    event(event="bo_operator_created", login=body.login, role=body.role, by=user["sub"])
    return row


@app.post(f"{ROOT}/api/operators/{{login}}/enroll")
async def enroll_operator(login: str, user: dict = Depends(require_admin)):
    """Enrôlement du visage : la personne se place devant la caméra du PC Hôte, 30 captures automatiques."""
    if not get_operator(login):
        raise HTTPException(status_code=404, detail="opérateur inconnu")
    try:
        r = await http.post(f"{AGENT_URL}/enroll", json={"login": login})
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"agent caméra injoignable ({exc.__class__.__name__})")
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"agent : {r.text[:200]}")
    with pool.connection() as conn:
        conn.execute("UPDATE bo_operators SET face_enrolled = true WHERE login=%s", (login,))
    event(event="bo_face_enrolled", login=login, by=user["sub"], frames=r.json().get("frames"))
    return r.json()


@app.post(f"{ROOT}/api/operators/{{login}}/unenroll")  # POST : le WAF (CRS) n'autorise pas DELETE par défaut
async def unenroll_operator(login: str, user: dict = Depends(require_admin)):
    try:
        await http.delete(f"{AGENT_URL}/enroll/{login}")
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=503, detail=f"agent caméra injoignable ({exc.__class__.__name__})")
    with pool.connection() as conn:
        conn.execute("UPDATE bo_operators SET face_enrolled = false WHERE login=%s", (login,))
    event(event="bo_face_removed", login=login, by=user["sub"])
    return {"login": login, "face_enrolled": False}


@app.post(f"{ROOT}/api/operators/{{login}}/toggle")
def toggle_operator(login: str, user: dict = Depends(require_admin)):
    if login == user["sub"]:
        raise HTTPException(status_code=400, detail="impossible de désactiver son propre compte")
    with pool.connection() as conn:
        row = conn.execute("UPDATE bo_operators SET active = NOT active WHERE login=%s RETURNING active", (login,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="opérateur inconnu")
    event(event="bo_operator_toggled", login=login, active=row["active"], by=user["sub"])
    return {"login": login, "active": row["active"]}


@app.post(f"{ROOT}/api/operators/{{login}}/unlock")
def unlock_operator(login: str, user: dict = Depends(require_admin)):
    clear_failures(login)
    event(event="bo_operator_unlocked", login=login, by=user["sub"])
    return {"login": login, "locked": False}


@app.post(f"{ROOT}/api/operators/{{login}}/role")
def set_role(login: str, body: dict, user: dict = Depends(require_admin)):
    role = body.get("role")
    if role not in ("admin", "operator"):
        raise HTTPException(status_code=422, detail="rôle invalide")
    if login == user["sub"] and role != "admin":
        raise HTTPException(status_code=400, detail="impossible de retirer son propre rôle admin")
    with pool.connection() as conn:
        conn.execute("UPDATE bo_operators SET role=%s WHERE login=%s", (role, login))
    event(event="bo_role_changed", login=login, role=role, by=user["sub"])
    return {"login": login, "role": role}


@app.post(f"{ROOT}/api/sync-enrollments")
async def sync(user: dict = Depends(require_admin)):
    enrolled = await sync_enrollments()
    if enrolled is None:
        raise HTTPException(status_code=503, detail="agent caméra injoignable")
    return {"enrolled": sorted(enrolled)}
