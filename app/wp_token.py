"""Vérification du jeton signé émis par WordPress (plugin creapulse-tools, feature 016).

Contrat : `base64url(payload_json) . base64url(HMAC-SHA256(payload_b64, secret))`, base64url sans padding,
signature calculée sur la CHAÎNE base64url du payload. Payload :
{"sub":int,"email":str,"name":str,"levels":[int…],"aud":"creapulse-tools","iat":int,"exp":int}, exp = iat + 600.
Tout échec → None (comportement anonyme), jamais d'exception.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

AUDIENCE = "creapulse-tools"
IAT_SKEW_S = 60


@dataclass(frozen=True)
class WpIdentity:
    user_id: int
    email: str
    name: str
    levels: tuple[int, ...]
    iat: int
    exp: int


def _b64url_decode(data: str) -> bytes:
    data = data.strip()
    if not data or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in data):
        raise ValueError("base64url invalide")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def sign(payload_b64: str, secret: str) -> str:
    mac = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")


def make_token(payload: dict, secret: str) -> str:
    """Utilisé par les tests (et un éventuel outil de debug) — même algorithme que le plugin."""
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sign(payload_b64, secret)}"


def verify(token: str | None, secret: str, now: float | None = None) -> WpIdentity | None:
    if not token or not secret:
        return None
    try:
        payload_b64, sig_b64 = token.strip().split(".", 1)
        if "." in sig_b64:
            return None
        expected = sign(payload_b64, secret)
        if not hmac.compare_digest(expected, sig_b64.strip()):
            return None
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("aud") != AUDIENCE:
            return None
        now = time.time() if now is None else now
        iat, exp = int(payload["iat"]), int(payload["exp"])
        if now >= exp or iat > now + IAT_SKEW_S:
            return None
        sub = int(payload["sub"])
        if sub <= 0:
            return None
        levels = payload.get("levels")
        if not isinstance(levels, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in levels):
            return None
        email = str(payload.get("email") or "").strip().lower()
        if "@" not in email:
            return None
        return WpIdentity(user_id=sub, email=email, name=str(payload.get("name") or "").strip(),
                          levels=tuple(levels), iat=iat, exp=exp)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
