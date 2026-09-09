"""Gating : 1 génération anonyme, puis email (magic-link) ; caps par compte, par IP et global."""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import re
import time
from dataclasses import dataclass

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings
from .store import Store

ANON_COOKIE = "vsg_anon"
SESSION_COOKIE = "vsg_session"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def valid_email(email: str) -> bool:
    email = (email or "").strip()
    return bool(email) and len(email) <= 254 and bool(_EMAIL_RE.match(email))


def _serializer(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=salt)


# --- Cookies signés ---------------------------------------------------------

def sign_anon(count: int) -> str:
    return _serializer("anon").dumps({"n": int(count)})


def read_anon(value: str | None) -> int:
    if not value:
        return 0
    try:
        data = _serializer("anon").loads(value, max_age=365 * 24 * 3600)
        return int(data.get("n", 0))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return 0


@dataclass
class Session:
    email: str
    source: str = "email"            # email (capture directe / magic-link) | wordpress (jeton signé)
    levels: tuple[int, ...] = ()     # niveaux PMPro actifs (source wordpress uniquement)
    name: str = ""

    @property
    def is_member(self) -> bool:
        return self.source == "wordpress"

    def has_level(self, required: tuple[int, ...]) -> bool:
        return not required or any(l in self.levels for l in required)


def sign_session(email: str, source: str = "email", levels: tuple[int, ...] = (), name: str = "") -> str:
    return _serializer("session").dumps({"e": email.strip().lower(), "s": source, "l": list(levels), "n": name[:80]})


def read_session(value: str | None) -> Session | None:
    if not value:
        return None
    # Une session membre expire plus vite (elle suit la déconnexion WordPress) : on lit sans max_age,
    # puis on applique le TTL selon la source.
    try:
        data, ts = _serializer("session").loads(value, max_age=settings.session_ttl_s, return_timestamp=True)
        email = data.get("e")
        if not valid_email(email or ""):
            return None
        source = data.get("s") or "email"
        if source == "wordpress":
            age = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds()
            if age > settings.member_session_ttl_s:
                return None
        levels = tuple(int(x) for x in (data.get("l") or []) if isinstance(x, int))
        return Session(email=email, source=source, levels=levels, name=str(data.get("n") or ""))
    except (BadSignature, SignatureExpired, ValueError, TypeError, AttributeError):
        return None


# --- Magic-link -------------------------------------------------------------

def make_magic_token(email: str) -> str:
    return _serializer("magic").dumps({"e": email.strip().lower()})


def read_magic_token(token: str) -> str | None:
    try:
        data = _serializer("magic").loads(token, max_age=settings.magic_link_ttl_s)
        email = data.get("e")
        return email if valid_email(email or "") else None
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
    reason: str = ""          # code : ok | need_email | account_cap | global_cap | ip_cap
    message: str = ""
    email: str | None = None
    anon_count: int = 0


class Gate:
    """Réserve un slot (incrément) AVANT le travail coûteux ; `release()` en cas d'échec."""

    def __init__(self, store: Store):
        self.store = store

    def check_and_reserve(self, *, email: str | None, anon_count: int, ip: str) -> tuple[Decision, list[tuple[str, str]]]:
        reserved: list[tuple[str, str]] = []

        # 1. Cap global (protège le budget, tous visiteurs confondus)
        g = self.store.increment("global", "all")
        reserved.append(("global", "all"))
        if g > settings.global_daily_cap:
            self._release(reserved)
            return Decision(False, "global_cap",
                            "Le quota gratuit du jour est atteint (tous visiteurs confondus). "
                            "Revenez demain — ou parlons directement de votre projet."), []

        # 2. Cap par IP (anti-abus, valable connecté ou non)
        ipk = ip_key(ip)
        ip_n = self.store.increment("ip", ipk)
        reserved.append(("ip", ipk))
        ip_cap = settings.anon_ip_daily_cap if not email else max(settings.anon_ip_daily_cap, settings.account_daily_cap * 2)
        if ip_n > ip_cap:
            self._release(reserved)
            code = "need_email" if not email else "ip_cap"
            msg = ("Vous avez utilisé votre génération gratuite. Entrez votre email pour continuer "
                   "(et débloquer le téléchargement du skill)."
                   if not email else "Trop de générations depuis cette connexion aujourd'hui. Revenez demain.")
            return Decision(False, code, msg), []

        # 3. Compte (email vérifié) → cap par compte
        if email:
            n = self.store.increment("email", email)
            reserved.append(("email", email))
            if n > settings.account_daily_cap:
                self._release(reserved)
                return Decision(False, "account_cap",
                                f"Vous avez atteint la limite de {settings.account_daily_cap} générations "
                                "par jour pour ce compte. Revenez demain — ou parlons de votre projet."), []
            return Decision(True, "ok", email=email), reserved

        # 4. Anonyme → N génération(s) gratuite(s) (cookie signé)
        if anon_count >= settings.anon_free_generations:
            self._release(reserved)
            return Decision(False, "need_email",
                            "Vous avez utilisé votre génération gratuite. Entrez votre email pour continuer "
                            "(et débloquer le téléchargement du skill)."), []
        return Decision(True, "ok", anon_count=anon_count + 1), reserved

    def _release(self, reserved: list[tuple[str, str]]) -> None:
        for scope, key in reserved:
            self.store.decrement(scope, key)

    def release(self, reserved: list[tuple[str, str]]) -> None:
        self._release(reserved)
