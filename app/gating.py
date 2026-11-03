"""Gating : session membre WordPress obligatoire ; plafonds par compte et global, en plus des crédits."""
from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings
from .store import Store

SESSION_COOKIE = "vsg_session"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def valid_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


@dataclass
class Session:
    """Session d'un membre WordPress, ouverte par le jeton signé du shortcode (compte obligatoire)."""
    email: str
    user_id: int                     # `sub` du jeton : identifiant du membre pour le porte-monnaie
    levels: tuple[int, ...] = ()     # niveaux PMPro actifs
    name: str = ""

    def has_level(self, required: tuple[int, ...]) -> bool:
        return not required or any(l in self.levels for l in required)


def sign_session(email: str, user_id: int, levels: tuple[int, ...] = (), name: str = "") -> str:
    return _serializer("member").dumps({"e": email.strip().lower(), "u": int(user_id), "l": list(levels),
                                        "n": name[:80]})


def read_session(value: str | None) -> Session | None:
    """La session suit la déconnexion WordPress : elle expire après `member_session_ttl_s`."""
    if not value:
        return None
    try:
        data = _serializer("member").loads(value, max_age=settings.member_session_ttl_s)
        email, user_id = data.get("e"), data.get("u")
        if not valid_email(email or "") or not isinstance(user_id, int) or user_id <= 0:
            return None
        levels = tuple(int(x) for x in (data.get("l") or []) if isinstance(x, int))
        return Session(email=email, user_id=user_id, levels=levels, name=str(data.get("n") or ""))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None


# --- Hash IP (jamais l'IP en clair dans le store) -----------------------------

def ip_key(ip: str) -> str:
    return hmac.new(settings.secret_key.encode(), (ip or "?").encode(), hashlib.sha256).hexdigest()[:32]


# --- Rate-limit mémoire (anti-rafale, par process) ----------------------------

class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < 60]
        if len(hits) >= self.per_minute:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        if len(self._hits) > 5000:  # borne mémoire
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60}
        return True


# --- Décision ----------------------------------------------------------------

@dataclass
class Decision:
    allowed: bool
    reason: str = ""          # code : ok | account_cap | global_cap
    message: str = ""
    email: str | None = None


class Gate:
    """Réserve un slot (incrément) AVANT le travail coûteux ; `release()` en cas d'échec."""

    def __init__(self, store: Store):
        self.store = store

    def check_and_reserve(self, *, email: str) -> tuple[Decision, list[tuple[str, str]]]:
        reserved: list[tuple[str, str]] = []

        # 1. Cap global (protège le budget LLM, tous membres confondus)
        g = self.store.increment("global", "all")
        reserved.append(("global", "all"))
        if g > settings.global_daily_cap:
            self._release(reserved)
            return Decision(False, "global_cap",
                            "Le nombre de générations du jour est atteint (tous membres confondus). "
                            "Revenez demain, vos crédits vous attendent."), []

        # 2. Cap par compte
        n = self.store.increment("email", email)
        reserved.append(("email", email))
        if n > settings.account_daily_cap:
            self._release(reserved)
            return Decision(False, "account_cap",
                            f"Vous avez atteint la limite de {settings.account_daily_cap} générations "
                            "par jour pour ce compte. Revenez demain."), []
        return Decision(True, "ok", email=email), reserved

    def _release(self, reserved: list[tuple[str, str]]) -> None:
        for scope, key in reserved:
            self.store.decrement(scope, key)

    def release(self, reserved: list[tuple[str, str]]) -> None:
        self._release(reserved)
