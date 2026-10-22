"""Machine à états de la LED bicolore (consigne additionnelle 1). Un seul poste d'authentification → un seul état global.

off          : par défaut
green_blink  : visage reconnu, en attente du mot de passe (jusqu'à saisie ou expiration de l'étape)
green_solid  : mot de passe validé — 30 s puis off
red_blink    : mot de passe refusé — 10 clignotements (≈ 5 s) puis off, retour à l'identification
red_solid    : visage non reconnu — 30 s puis off, puis nouvelle saisie possible
L'ESP interroge GET /admin/api/led-state toutes les 500 ms et applique le motif localement.
"""
import threading
import time

STATES = ("off", "green_blink", "green_solid", "red_blink", "red_solid")


class Led:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = "off"
        self._until: float | None = None
        self._since = time.time()
        self._reason = "boot"

    def set(self, state: str, seconds: float | None = None, reason: str = ""):
        if state not in STATES:
            raise ValueError(state)
        with self._lock:
            self._state, self._reason, self._since = state, reason, time.time()
            self._until = (time.time() + seconds) if seconds else None

    def off(self, reason: str = ""):
        self.set("off", None, reason)

    def current(self) -> dict:
        with self._lock:
            now = time.time()
            if self._until is not None and now >= self._until:
                self._state, self._until, self._reason, self._since = "off", None, "expired", now
            remaining = int((self._until - now) * 1000) if self._until else None
            return {"state": self._state, "remaining_ms": remaining, "reason": self._reason,
                    "since": int(self._since), "blinks": 10 if self._state == "red_blink" else None}
