"""Déclaration d'usage à WordPress (route creapulse-tools `tool-event`, feature 019) → Nimble « Outils utilisés ».

Appel serveur → serveur, fire-and-forget : jamais bloquant pour le membre, jamais de retry, jeton jamais loggé.
Le jeton est envoyé dans le CORPS (jamais en query string), c'est la seule authentification.
"""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("voice-skill.wp-events")


async def notify_tool_event(client: httpx.AsyncClient, wp_token: str) -> None:
    """À appeler avec un jeton DÉJÀ vérifié localement, dans la même requête (le jeton vit 10 min)."""
    if not settings.wp_tool_event_url or not settings.tool_slug:
        return
    try:
        resp = await client.post(
            settings.wp_tool_event_url,
            json={"wp_token": wp_token, "tool": settings.tool_slug},
            headers={"Content-Type": "application/json", "User-Agent": "creapulse-voice-skill/1.0"},
            timeout=settings.wp_tool_event_timeout_s,
        )
        recorded = None
        if resp.headers.get("content-type", "").startswith("application/json"):
            try:
                recorded = resp.json().get("recorded")
            except ValueError:
                recorded = None
        log.info("tool-event tool=%s http=%s recorded=%s", settings.tool_slug, resp.status_code, recorded)
    except httpx.HTTPError as exc:
        log.warning("tool-event tool=%s échec réseau (%s) — ignoré", settings.tool_slug, exc.__class__.__name__)
    except Exception as exc:  # pragma: no cover — filet : ne jamais remonter
        log.warning("tool-event tool=%s erreur inattendue (%s) — ignoré", settings.tool_slug, exc.__class__.__name__)
