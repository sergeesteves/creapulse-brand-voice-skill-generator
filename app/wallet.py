"""Client du porte-monnaie de crédits commun (plugin creapulse-tools, feature 079).

Contrat : `POST <WordPress>/wp-json/creapulse-tools/v1/credits/{balance,debit,refund,cost}`, corps JSON,
en-têtes `X-Creapulse-Tool: <slug>` et `X-Credits-Secret: <secret>`. Le secret n'est jamais loggé.

- **debit** avant le travail coûteux. Délai dépassé ou 503 : on rejoue le même `usage_ref` une fois, puis
  on envoie `refund` (un débit tardif répond alors 409 et rien ne se lance). WordPress injoignable : on
  ne lance pas.
- **refund** si la génération échoue, **cost** dès qu'un appel au modèle a été payé, échec compris.
  Les deux passent par une file de renvoi, rejouée sur 503 et erreur réseau seulement : au démarrage
  et après chaque génération, sans tâche planifiée.

Le module ne dépend que de `config` et du store : il se reprend tel quel dans un autre outil Python.
"""
from __future__ import annotations

import json
import logging

import httpx

from .config import settings

log = logging.getLogger("voice-skill.wallet")

MAX_ATTEMPTS = 50  # au-delà, l'envoi est abandonné et loggé en erreur (Serge régularise dans wp-admin)


class WalletError(Exception):
    """Réponse autre que 200. `status` vaut None pour une erreur réseau ou un délai dépassé."""

    def __init__(self, status: int | None, code: str = "", data: dict | None = None):
        super().__init__(f"porte-monnaie {status or 'injoignable'} {code}".strip())
        self.status = status
        self.code = code
        self.data = data or {}

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 503


def _url(route: str) -> str:
    return f"{settings.wp_base_url}/wp-json/creapulse-tools/v1/credits/{route}"


async def call(client: httpx.AsyncClient, route: str, body: dict) -> dict:
    headers = {"X-Creapulse-Tool": settings.credits_tool_slug, "X-Credits-Secret": settings.credits_secret,
               "Content-Type": "application/json", "User-Agent": "creapulse-voice-skill/1.0"}
    try:
        resp = await client.post(_url(route), json=body, headers=headers, timeout=settings.credits_timeout_s)
    except httpx.HTTPError as exc:
        raise WalletError(None, exc.__class__.__name__) from None
    try:
        payload = resp.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    if resp.status_code == 200:
        return payload
    # Erreur WordPress : {"code": "...", "message": "...", "data": {"status": 402, ...}}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    raise WalletError(resp.status_code, str(payload.get("code") or ""), data)


async def balance(client: httpx.AsyncClient, user_id: int) -> dict:
    return await call(client, "balance", {"user_id": user_id})


async def debit(client: httpx.AsyncClient, user_id: int, usage_ref: str) -> dict:
    body = {"user_id": user_id, "tier": settings.credits_tier, "usage_ref": usage_ref}
    try:
        return await call(client, "debit", body)
    except WalletError as exc:
        if not exc.retryable:
            raise
    return await call(client, "debit", body)  # même usage_ref : WordPress répond `replayed` s'il avait débité


async def deliver(client: httpx.AsyncClient, store, route: str, body: dict) -> bool:
    """Envoie `refund` ou `cost` ; sur 503 ou erreur réseau, met l'envoi en file. Vrai si WordPress l'a reçu."""
    try:
        await call(client, route, body)
        return True
    except WalletError as exc:
        if exc.retryable:
            store.enqueue_wallet(route, json.dumps(body), str(exc))
            log.warning("%s %s mis en file : %s", route, body.get("usage_ref"), exc)
        else:
            log.error("%s %s refusé : %s %s", route, body.get("usage_ref"), exc, exc.data)
        return False


async def refund(client: httpx.AsyncClient, store, user_id: int, usage_ref: str, reason: str) -> bool:
    return await deliver(client, store, "refund", {"user_id": user_id, "usage_ref": usage_ref, "reason": reason})


async def report_cost(client: httpx.AsyncClient, store, user_id: int, usage_ref: str, contract: dict) -> bool:
    return await deliver(client, store, "cost", {"user_id": user_id, "usage_ref": usage_ref, **contract})


async def replay_outbox(client: httpx.AsyncClient, store) -> int:
    """Rejoue la file. Renvoie le nombre d'envois réglés (reçus, ou refusés pour de bon)."""
    settled = 0
    for item in store.pending_wallet():
        try:
            await call(client, item["route"], json.loads(item["body"]))
        except WalletError as exc:
            if exc.retryable and item["attempts"] + 1 < MAX_ATTEMPTS:
                store.bump_wallet(item["id"], str(exc))
                continue
            log.error("%s abandonné après %d essai(s) : %s %s", item["route"], item["attempts"] + 1, exc, exc.data)
        store.drop_wallet(item["id"])
        settled += 1
    return settled
