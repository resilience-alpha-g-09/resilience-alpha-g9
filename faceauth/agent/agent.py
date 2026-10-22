"""Agent caméra du PC Hôte — enveloppe la reconnaissance faciale de Karim (Haar + LBPH, OpenCV) dans une petite API locale.

Tourne SUR le PC Hôte (Windows, hors Docker : WSL2 ne voit pas les webcams USB). Le back-office conteneurisé l'appelle
via http://host.docker.internal:8090 avec un jeton partagé (AGENT_TOKEN).

    python agent.py serve                 # API sur 0.0.0.0:8090
    python agent.py enroll <login>        # enrôle un visage en ligne de commande (amorçage du premier admin)
    python agent.py verify <login>        # test manuel

Données biométriques : faceauth/agent/data/<login>/*.png + model/ — jamais versionnées (.gitignore).
Mode simulé pour les tests sans caméra : AGENT_MOCK=1 (AGENT_MOCK_RESULT=recognized|unknown|timeout).
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
DATA = HERE / "data"
MODEL_DIR = HERE / "model"
MODEL_FILE = MODEL_DIR / "modele_visage.yml"
LABELS_FILE = MODEL_DIR / "labels.json"

CAMERA_INDEX = int(os.environ.get("CAMERA_INDEX", "0"))
SEUIL = float(os.environ.get("FACE_THRESHOLD", "60"))      # distance LBPH max (plus bas = plus strict) — valeur de Karim
STABILITE = int(os.environ.get("FACE_STABILITY", "5"))     # analyses concordantes consécutives avant de conclure
INTERVALLE = 3                                             # analyse une image sur trois (fluidité)
FRAMES_ENROLL = int(os.environ.get("FACE_ENROLL_FRAMES", "30"))
AGENT_TOKEN = os.environ.get("AGENT_TOKEN", "")
MOCK = os.environ.get("AGENT_MOCK", "0") == "1"
MOCK_RESULT = os.environ.get("AGENT_MOCK_RESULT", "recognized")
MOCK_DELAY = float(os.environ.get("AGENT_MOCK_DELAY", "3"))
SHOW_WINDOW = os.environ.get("AGENT_WINDOW", "1") == "1"   # fenêtre caméra visible sur l'écran du PC Hôte pendant l'opération
WINDOW = "RESILIENCE-ALPHA - Camera"

camera_lock = threading.Lock()   # une seule opération caméra à la fois
_state = {"model": None, "labels": {}, "busy": False, "last": None}

# Les fenêtres OpenCV (imshow) doivent vivre sur le thread principal (obligatoire sous macOS, plus sûr sous Windows) :
# les handlers HTTP soumettent leurs opérations caméra à ce worker et attendent le résultat.
import queue  # noqa: E402
from concurrent.futures import Future  # noqa: E402

_jobs: "queue.Queue" = queue.Queue()
_main_worker = {"active": False}


def run_on_main(fn, *args, **kwargs):
    if not _main_worker["active"]:
        return fn(*args, **kwargs)
    fut = Future()
    _jobs.put((fn, args, kwargs, fut))
    return fut.result()


def main_loop():
    _main_worker["active"] = True
    while True:
        try:
            fn, args, kwargs, fut = _jobs.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            fut.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 — transmis au handler HTTP
            fut.set_exception(exc)


def log(**kv):
    print(json.dumps({"ts": time.time(), **kv}), flush=True)


# ── OpenCV (chargé paresseusement : le mode simulé n'en a pas besoin) ──
def _cv():
    import cv2  # noqa: WPS433
    return cv2


def _detector():
    cv2 = _cv()
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if det.empty():
        raise RuntimeError("détecteur Haar introuvable")
    return det


def _prep_gray(image):
    """Niveaux de gris + égalisation d'histogramme : rend Haar et LBPH bien plus robustes au contre-jour."""
    cv2 = _cv()
    return cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))


def _open_camera():
    cv2 = _cv()
    flags = [cv2.CAP_DSHOW] if sys.platform == "win32" else []
    cam = cv2.VideoCapture(CAMERA_INDEX, *flags)
    if not cam.isOpened():
        raise RuntimeError(f"caméra {CAMERA_INDEX} introuvable")
    cam.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    return cam


def _show(image, title: str, boxes=(), status: str = "", color=(255, 255, 255)):
    """Affiche l'image de la caméra avec le cadre du visage et un bandeau d'état (si SHOW_WINDOW)."""
    if not SHOW_WINDOW:
        return
    cv2 = _cv()
    for (x, y, w, h, c) in boxes:
        cv2.rectangle(image, (x, y), (x + w, y + h), c, 2)
    cv2.rectangle(image, (0, 0), (image.shape[1], 36), (40, 30, 72), -1)
    cv2.putText(image, title, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (228, 230, 243), 2)
    if status:
        cv2.putText(image, status, (10, image.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
    try:
        cv2.imshow(WINDOW, image)
        cv2.waitKey(1)
    except Exception as exc:  # noqa: BLE001 — pas d'écran / thread incompatible : on continue sans fenêtre
        globals()["SHOW_WINDOW"] = False
        log(event="window_disabled", error=str(exc)[:120])


def _close_window(final=None, title: str = "", color=(255, 255, 255), hold_s: float = 1.2):
    if not SHOW_WINDOW:
        return
    cv2 = _cv()
    try:
        if final is not None:
            _show(final, title, status="", color=color)
            cv2.waitKey(int(hold_s * 1000))
        cv2.destroyWindow(WINDOW)
        cv2.waitKey(1)
    except Exception:  # noqa: BLE001
        pass


def load_model():
    if not (MODEL_FILE.exists() and LABELS_FILE.exists()):
        _state["model"], _state["labels"] = None, {}
        return
    cv2 = _cv()
    model = cv2.face.LBPHFaceRecognizer_create()
    model.read(str(MODEL_FILE))
    _state["model"] = model
    _state["labels"] = {int(k): v for k, v in json.loads(LABELS_FILE.read_text()).items()}


def train():
    """Ré-entraîne LBPH sur tous les opérateurs enrôlés (un label entier par login)."""
    import numpy as np
    cv2 = _cv()
    images, labels, mapping = [], [], {}
    for i, d in enumerate(sorted(p for p in DATA.iterdir() if p.is_dir()) if DATA.exists() else []):
        pngs = sorted(d.glob("*.png"))
        if not pngs:
            continue
        mapping[i] = d.name
        for png in pngs:
            img = cv2.imread(str(png), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                images.append(cv2.resize(img, (200, 200)))
                labels.append(i)
    MODEL_DIR.mkdir(exist_ok=True)
    if not images:
        for f in (MODEL_FILE, LABELS_FILE):
            f.unlink(missing_ok=True)
        _state["model"], _state["labels"] = None, {}
        return 0
    model = cv2.face.LBPHFaceRecognizer_create()
    model.train(images, np.array(labels, dtype=np.int32))
    model.write(str(MODEL_FILE))
    LABELS_FILE.write_text(json.dumps(mapping))
    load_model()
    return len(mapping)


def _label_of(login: str):
    for k, v in _state["labels"].items():
        if v == login:
            return k
    return None


def do_enroll(login: str, frames: int = FRAMES_ENROLL, timeout_s: float = 60) -> dict:
    if MOCK:
        (DATA / login).mkdir(parents=True, exist_ok=True)
        (DATA / login / "mock.txt").write_text("mock")
        return {"login": login, "frames": frames, "operators": len(list(DATA.iterdir()))}
    cv2 = _cv()
    det = _detector()
    target = DATA / login
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.png"):
        old.unlink()
    with camera_lock:
        cam = _open_camera()
        try:
            n, t0, last = 0, time.monotonic(), 0.0
            while n < frames and time.monotonic() - t0 < timeout_s:
                ok, image = cam.read()
                if not ok:
                    continue
                gris = _prep_gray(image)
                visages = det.detectMultiScale(gris, scaleFactor=1.08, minNeighbors=4, minSize=(80, 80))
                _show(image, f"Enrolement : {login}", [(x, y, w, h, (0, 255, 0)) for (x, y, w, h) in visages],
                      f"Captures : {n}/{frames}" + ("" if len(visages) == 1 else "  (un seul visage attendu)"), (0, 255, 0))
                if len(visages) == 1 and time.monotonic() - last > 0.2:   # ~5 captures/s, un seul visage
                    x, y, w, h = visages[0]
                    crop = cv2.resize(gris[y:y + h, x:x + w], (200, 200))
                    cv2.imwrite(str(target / f"visage_{n:03d}.png"), crop)
                    n += 1
                    last = time.monotonic()
        finally:
            cam.release()
            _close_window()
    operators = train()
    log(event="enrolled", login=login, frames=n, operators=operators)
    if n < max(5, frames // 3):
        raise HTTPException(status_code=422, detail=f"seulement {n} captures : visage mal cadré ou absent")
    return {"login": login, "frames": n, "operators": operators}


def do_verify(login: str, timeout_s: float = 10) -> dict:
    """Reprend la logique de tester_reconnaissance.py : une image sur trois, détection sur image réduite,
    conclusion après STABILITE analyses concordantes. Résultat : recognized | unknown | timeout | no_face."""
    if MOCK:
        time.sleep(MOCK_DELAY)
        res = MOCK_RESULT if MOCK_RESULT in ("recognized", "unknown", "timeout") else "recognized"
        return {"login": login, "result": res, "distance": 32.0 if res == "recognized" else 88.0, "frames": 15}
    cv2 = _cv()
    if _state["model"] is None:
        load_model()
    label = _label_of(login)
    det = _detector()
    with camera_lock:
        cam = _open_camera()
        try:
            t0, compteur, pending, streak, seen_face, best = time.monotonic(), 0, None, 0, False, None
            boxes, last_image = [], None
            while time.monotonic() - t0 < timeout_s:
                ok, image = cam.read()
                if not ok:
                    continue
                last_image = image
                compteur += 1
                remaining = int(timeout_s - (time.monotonic() - t0))
                if compteur % INTERVALLE:
                    _show(image, f"Identification : {login}", boxes, f"Regardez l'objectif  ({remaining} s)", (228, 230, 243))
                    continue
                gris = _prep_gray(image)
                petit = cv2.resize(gris, None, fx=0.5, fy=0.5)
                visages = det.detectMultiScale(petit, scaleFactor=1.08, minNeighbors=4, minSize=(40, 40))
                verdict = None
                boxes = []
                if len(visages) == 1:
                    seen_face = True
                    x, y, w, h = [v * 2 for v in visages[0]]
                    crop = cv2.resize(gris[y:y + h, x:x + w], (200, 200))
                    if _state["model"] is not None and label is not None:
                        pred, dist = _state["model"].predict(crop)
                        best = dist if best is None else min(best, dist)
                        verdict = "recognized" if (pred == label and dist < SEUIL) else "unknown"
                    else:
                        verdict = "unknown"
                    boxes = [(x, y, w, h, (0, 255, 0) if verdict == "recognized" else (0, 0, 255))]
                elif len(visages) > 1:
                    boxes = [(x * 2, y * 2, w * 2, h * 2, (0, 165, 255)) for (x, y, w, h) in visages]
                _show(image, f"Identification : {login}", boxes,
                      "Plusieurs visages" if len(visages) > 1 else (f"Analyse {streak + 1}/{STABILITE}" if verdict else f"Regardez l'objectif  ({remaining} s)"),
                      (0, 165, 255) if len(visages) > 1 else (228, 230, 243))
                if verdict == pending:
                    streak += 1
                else:
                    pending, streak = verdict, 1 if verdict else 0
                if verdict and streak >= STABILITE:
                    log(event="verify", login=login, result=verdict, distance=best)
                    ok_v = verdict == "recognized"
                    _show(image, f"Identification : {login}", boxes, "VISAGE RECONNU" if ok_v else "VISAGE NON RECONNU",
                          (0, 255, 0) if ok_v else (0, 0, 255))
                    _close_window(image, f"Identification : {login}", (0, 255, 0) if ok_v else (0, 0, 255))
                    return {"login": login, "result": verdict, "distance": best, "frames": compteur}
        finally:
            cam.release()
    res = "timeout" if seen_face else "no_face"
    if last_image is not None:
        _show(last_image, f"Identification : {login}", [], "AUCUN VISAGE" if res == "no_face" else "DELAI DEPASSE", (0, 0, 255))
    _close_window(last_image, f"Identification : {login}", (0, 0, 255))
    log(event="verify", login=login, result=res, distance=best)
    return {"login": login, "result": res, "distance": best, "frames": compteur}


# ── mode « caméra navigateur » : l'agent ne touche pas la webcam, il analyse des images JPEG reçues en base64 ──
def _decode_image(b64: str):
    import base64 as _b64
    import numpy as np
    cv2 = _cv()
    if "," in b64[:40]:                       # data:image/jpeg;base64,....
        b64 = b64.split(",", 1)[1]
    buf = np.frombuffer(_b64.b64decode(b64), dtype=np.uint8)
    img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(status_code=422, detail="image illisible")
    return img


def analyze_image(login: str, b64: str) -> dict:
    """Une image → {faces, verdict (recognized | unknown | none | multiple), distance}. Sans état : le back-office compte."""
    if MOCK:
        res = MOCK_RESULT if MOCK_RESULT in ("recognized", "unknown") else "recognized"
        return {"faces": 1, "verdict": res, "distance": 32.0 if res == "recognized" else 88.0}
    cv2 = _cv()
    if _state["model"] is None:
        load_model()
    img = _decode_image(b64)
    gris = _prep_gray(img)
    det = _detector()
    visages = det.detectMultiScale(gris, scaleFactor=1.08, minNeighbors=4, minSize=(80, 80))
    if len(visages) == 0:
        return {"faces": 0, "verdict": "none", "distance": None}
    if len(visages) > 1:
        return {"faces": len(visages), "verdict": "multiple", "distance": None}
    x, y, w, h = visages[0]
    crop = cv2.resize(gris[y:y + h, x:x + w], (200, 200))
    label = _label_of(login)
    if _state["model"] is None or label is None:
        return {"faces": 1, "verdict": "unknown", "distance": None, "box": [int(x), int(y), int(w), int(h)]}
    pred, dist = _state["model"].predict(crop)
    verdict = "recognized" if (pred == label and dist < SEUIL) else "unknown"
    return {"faces": 1, "verdict": verdict, "distance": float(dist), "box": [int(x), int(y), int(w), int(h)]}


def enroll_images(login: str, images: list) -> dict:
    """Lot d'images (≈ 40 sur 10 s) : garde celles avec exactement un visage, ré-entraîne."""
    if MOCK:
        (DATA / login).mkdir(parents=True, exist_ok=True)
        (DATA / login / "mock.txt").write_text("mock")
        return {"login": login, "frames": len(images), "operators": len(list(DATA.iterdir()))}
    cv2 = _cv()
    det = _detector()
    target = DATA / login
    target.mkdir(parents=True, exist_ok=True)
    for old in target.glob("*.png"):
        old.unlink()
    n = 0
    for b64 in images:
        try:
            gris = _prep_gray(_decode_image(b64))
        except HTTPException:
            continue
        visages = det.detectMultiScale(gris, scaleFactor=1.08, minNeighbors=4, minSize=(80, 80))
        if len(visages) != 1:
            continue
        x, y, w, h = visages[0]
        cv2.imwrite(str(target / f"visage_{n:03d}.png"), cv2.resize(gris[y:y + h, x:x + w], (200, 200)))
        n += 1
    operators = train()
    log(event="enrolled", login=login, frames=n, operators=operators, source="browser")
    if n < 8:
        raise HTTPException(status_code=422, detail=f"seulement {n} images exploitables (un seul visage, de face) : recommencer")
    return {"login": login, "frames": n, "operators": operators}


# ── API ──
app = FastAPI(title="RESILIENCE-ALPHA — agent caméra")


def auth(x_agent_token: str = Header(default="")):
    if AGENT_TOKEN and x_agent_token != AGENT_TOKEN:
        raise HTTPException(status_code=401, detail="jeton agent invalide")


class EnrollIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    frames: int = Field(FRAMES_ENROLL, ge=5, le=100)


class VerifyIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    timeout_s: float = Field(10, ge=2, le=30)


def _guard_busy():
    if _state["busy"]:
        raise HTTPException(status_code=409, detail="caméra déjà en cours d'utilisation")


@app.get("/health")
def health():
    enrolled = sorted(p.name for p in DATA.iterdir() if p.is_dir()) if DATA.exists() else []
    return {"status": "ok", "mock": MOCK, "model_loaded": _state["model"] is not None or MOCK,
            "enrolled": enrolled, "threshold": SEUIL, "busy": _state["busy"], "modes": ["browser", "server"]}


@app.post("/verify", dependencies=[Depends(auth)])
def verify(body: VerifyIn):
    _guard_busy()
    _state["busy"] = True
    try:
        return run_on_main(do_verify, body.login, body.timeout_s)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    finally:
        _state["busy"] = False


@app.post("/enroll", dependencies=[Depends(auth)])
def enroll(body: EnrollIn):
    _guard_busy()
    _state["busy"] = True
    try:
        return run_on_main(do_enroll, body.login, body.frames)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    finally:
        _state["busy"] = False


class AnalyzeIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    image: str = Field(min_length=100, max_length=2_000_000)


class EnrollImagesIn(BaseModel):
    login: str = Field(min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    images: list = Field(min_length=1, max_length=80)


@app.post("/analyze", dependencies=[Depends(auth)])
def analyze(body: AnalyzeIn):
    return analyze_image(body.login, body.image)


@app.post("/enroll-images", dependencies=[Depends(auth)])
def enroll_from_images(body: EnrollImagesIn):
    return enroll_images(body.login, body.images)


@app.delete("/enroll/{login}", dependencies=[Depends(auth)])
def unenroll(login: str):
    import shutil
    target = DATA / login
    if target.exists():
        shutil.rmtree(target)
    operators = 0 if MOCK else train()
    log(event="unenrolled", login=login)
    return {"login": login, "operators": operators}


@app.on_event("startup")
def _startup():
    if not MOCK:
        try:
            load_model()
        except Exception as exc:  # noqa: BLE001
            log(event="model_load_failed", error=repr(exc))
    log(event="agent_started", mock=MOCK, camera=CAMERA_INDEX, threshold=SEUIL, token=bool(AGENT_TOKEN))


# ── Balise LED : annonce en broadcast UDP (port 4210, 1×/s) l'URL de l'état LED sur chaque IP locale.
# L'ESP32 l'écoute et adopte l'adresse sans configuration : indispensable sur un hotspot de téléphone,
# où l'IP du PC Hôte change. LED_BEACON=0 pour désactiver. Le serveur annoncé est l'edge (port 80) de ce poste.
def _local_ipv4s() -> list[str]:
    import socket
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))          # aucun paquet envoyé : donne l'IP de la route par défaut
        ips.add(probe.getsockname()[0]); probe.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and not ip.startswith("169.254."))


def beacon_loop():
    import socket
    port = int(os.environ.get("LED_BEACON_PORT", "4210"))
    path = os.environ.get("LED_BEACON_PATH", "/admin/api/led-state")
    last = None
    while True:
        ips = _local_ipv4s()
        if ips != last:
            log(event="led_beacon", urls=[f"http://{ip}{path}" for ip in ips]); last = ips
        msg = ("RA-LED " + " ".join(f"http://{ip}{path}" for ip in ips)).encode()
        for ip in ips:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind((ip, 0))                     # sort par l'interface de cette IP
                sock.sendto(msg, ("255.255.255.255", port)); sock.close()
            except OSError:
                pass
        time.sleep(1)


if __name__ == "__main__":
    import uvicorn
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "serve":
        if os.environ.get("LED_BEACON", "1") != "0":
            threading.Thread(target=beacon_loop, daemon=True).start()
        config = uvicorn.Config(app, host=os.environ.get("AGENT_HOST", "0.0.0.0"), port=int(os.environ.get("AGENT_PORT", "8090")), log_level="info")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True).start()
        try:
            main_loop()          # caméra + fenêtres sur le thread principal
        except KeyboardInterrupt:
            pass
    elif cmd == "enroll" and len(sys.argv) > 2:
        print(do_enroll(sys.argv[2]))
    elif cmd == "verify" and len(sys.argv) > 2:
        load_model()
        print(do_verify(sys.argv[2]))
    elif cmd == "train":
        print({"operators": train()})
    else:
        print(__doc__)
