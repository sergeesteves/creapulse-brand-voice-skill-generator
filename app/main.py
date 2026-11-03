"""Générateur de skill de voix de marque IA — micro-app stateless (FastAPI).

Flux : URLs → crawl4ai (scrape) → débit du porte-monnaie → omniroute (distillation, T° basse) → 2 formats
self-contained. Compte WordPress obligatoire (jeton signé du shortcode) ; plafonds par compte et global.
"""
from __future__ import annotations

import io
import logging
import secrets
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask

from . import distill as distill_mod
from . import gating, render, wallet
from . import wp_token as wp_token_mod
from .config import settings
from .costs import CostMeter
from .distill import DistillError, distill
from .scraper import InputError, ScrapeError, check_corpus, fetch_pages, parse_urls
from .store import get_store
from .wp_events import notify_tool_event

log = logging.getLogger("voice-skill")
logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
rate_limiter = gating.RateLimiter(settings.ip_rate_limit_per_min)


@asynccontextmanager
async def lifespan(app: FastAPI):
    problems = settings.validate()
    if problems:
        msg = "Config incomplète : " + " ; ".join(problems)
        if settings.debug:
            log.warning(msg)
        else:
            log.error(msg)
    if not settings.secret_key:
        # Jamais en prod (les sessions seraient invalidées à chaque redémarrage)
        settings.secret_key = secrets.token_urlsafe(32)
        log.warning("SECRET_KEY absente → clé éphémère générée (dev uniquement)")
    store = get_store()
    try:
        store.purge_old_counters()
    except Exception as exc:  # pragma: no cover
        log.warning("purge compteurs impossible : %s", exc)
    try:
        store.purge_expired_profiles()
    except Exception as exc:  # pragma: no cover
        log.warning("purge des profils expirés impossible : %s", exc)
    app.state.http = httpx.AsyncClient(timeout=max(settings.crawl_timeout_s, settings.llm_timeout_s))
    await _replay_wallet(app.state.http)
    yield
    await app.state.http.aclose()
    store.close()


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
              root_path=settings.root_path)
STATIC_DIR = (BASE_DIR / "static").resolve()
# Version des statiques = empreinte du contenu → l'URL change à chaque déploiement, le cache navigateur ne colle plus
import hashlib as _hashlib
STATIC_VERSION = _hashlib.sha1(b"".join(p.read_bytes() for p in sorted(STATIC_DIR.glob("*")) if p.is_file())).hexdigest()[:10]


@app.get("/static/{filename}")
async def static_file(filename: str):
    """Fichiers statiques servis par une route explicite : fiable derrière un préfixe de chemin (ROOT_PATH)."""
    target = (STATIC_DIR / filename).resolve()
    if target.parent != STATIC_DIR or not target.is_file():
        return _error(404, "not_found", "Fichier introuvable.")
    return FileResponse(str(target), headers={"Cache-Control": "public, max-age=3600"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def client_ip(request: Request) -> str:
    if settings.trusted_proxies:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else "?"


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    resp.set_cookie(name, value, max_age=max_age, httponly=True, samesite="lax",
                    secure=settings.cookie_secure, path=settings.cookie_path)


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message, **extra}, status_code=status)


def _session(request: Request) -> gating.Session | None:
    return gating.read_session(request.cookies.get(gating.SESSION_COOKIE))


def _access_ok(sess: gating.Session | None) -> bool:
    """Il faut une session membre (jeton WordPress) avec un niveau requis, le cas échéant."""
    return bool(sess and sess.has_level(settings.required_levels))


def _wp_link(request: Request, name: str) -> str:
    """login_url / register_url transmis par le shortcode WordPress — acceptés seulement sur le site."""
    raw = unquote(request.query_params.get(name) or "").strip()
    ok = any(raw.startswith(p) for p in settings.wp_site_prefixes)
    return raw if ok and " " not in raw else ""


def _home_url(request: Request, drop: tuple[str, ...]) -> str:
    """URL de la page courante (avec préfixe) sans les paramètres listés."""
    kept = [(k, v) for k, v in request.query_params.multi_items() if k not in drop]
    return (settings.root_path or "") + "/" + (("?" + urlencode(kept)) if kept else "")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # 1. Jeton signé WordPress (shortcode creapulse-tools) → session membre, puis on retire le jeton de l'URL
    wp_token = request.query_params.get("wp_token")
    if wp_token:
        ident = wp_token_mod.verify(wp_token, settings.wp_token_secret)
        resp = RedirectResponse(_home_url(request, drop=("wp_token", "login_url", "register_url")), status_code=302)
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        if ident:
            store = get_store()
            store.upsert_lead(ident.email, consent=True, source="wordpress")
            store.mark_verified(ident.email, source="wordpress")
            _set_cookie(resp, gating.SESSION_COOKIE,
                        gating.sign_session(ident.email, ident.user_id, ident.levels, ident.name),
                        settings.member_session_ttl_s)
            log.info("wp token ok user=%s levels=%s", ident.user_id, list(ident.levels))
            # Déclare l'usage à WordPress (→ Nimble) APRÈS l'envoi de la réponse : jamais bloquant, jeton encore valide
            resp.background = BackgroundTask(notify_tool_event, request.app.state.http, wp_token)
        else:
            log.warning("wp token rejeté (signature/expiration/format) : page de connexion")
        return resp

    sess = _session(request)
    login_url, register_url = _wp_link(request, "login_url"), _wp_link(request, "register_url")
    resp = templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "root": settings.root_path,
            "static_version": STATIC_VERSION,
            "embed": request.query_params.get("embed") == "1",
            "email": sess.email if sess else None,
            "session": sess,
            "locked": not _access_ok(sess),
            "login_url": login_url,
            "register_url": register_url,
            "max_urls": settings.max_urls,
            "min_urls": settings.min_urls,
            "min_words": settings.min_words_per_page,
            "retention_months": settings.retention_months(sess.levels if sess else ()),
        },
    )
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/health")
async def health():
    return {"status": "ok", "app": "voice-skill-generator"}


# ---------------------------------------------------------------------------
# Génération
# ---------------------------------------------------------------------------

def _record_cost(store, status: str, pages: list, model: str | None, meter: CostMeter) -> None:
    """Enregistre le coût d'une génération qui a appelé le modèle. Ne fait jamais échouer la réponse."""
    if not meter.llm_calls:
        return
    try:
        store.record_generation_cost(status=status, pages=len(pages), words=sum(p.words for p in pages),
                                     model=model or settings.llm_model, meter=meter)
    except Exception as exc:  # le relevé de coût ne doit pas priver le visiteur de son profil
        log.warning("coût de génération non enregistré : %s", exc)


# --- Porte-monnaie -----------------------------------------------------------

def _usage_ref() -> str:
    """Un par génération : `{analyse}:{essai}`. Chaque génération est une analyse neuve, donc l'essai 1."""
    return f"voix{secrets.token_hex(8)}:1"


# Solde à 0 sans bienvenue : adresse jetable, ou plafond mensuel des bienvenues atteint
WELCOME_CLOSED = ("Les crédits de bienvenue ne sont pas disponibles pour ce compte en ce moment. "
                  "Vous pouvez recharger vos crédits pour lancer une génération.")
FREE_BUDGET = "Les essais gratuits sont en pause jusqu'au mois prochain. Les crédits achetés restent utilisables."
NOT_ENOUGH = "Votre solde ne suffit pas pour une génération."
# Outil décoché « Actif » dans wp-admin, ou palier absent : le solde n'y est pour rien, recharger ne sert à rien
UNAVAILABLE = "L'outil est momentanément indisponible. Vos crédits ne sont pas touchés, réessayez un peu plus tard."


def _credits_block(data: dict, cost) -> tuple[str, bool]:
    """Pourquoi `balance` refuse le palier : (message, recharger aide-t-il ?).

    Le porte-monnaie ne donne pas de raison quand l'outil est désactivé : si le solde couvre le prix
    (ou si le palier n'a pas de prix), ce n'est pas un problème de solde.
    """
    reason, balance = data.get("closed_reason"), data.get("balance")
    if reason == "welcome_closed":
        return WELCOME_CLOSED, True
    if reason == "free_budget":
        return FREE_BUDGET, True
    if reason == "tool_disabled":
        return UNAVAILABLE, False
    if isinstance(balance, int) and isinstance(cost, int) and balance < cost:
        return NOT_ENOUGH, True
    return UNAVAILABLE, False


def _wallet_refusal(exc: wallet.WalletError) -> JSONResponse:
    """Traduit un refus du porte-monnaie pour le membre. Rien n'a été lancé."""
    if exc.status == 402:
        required, left = exc.data.get("required"), exc.data.get("balance")
        msg = f"Une génération demande {required} crédits, il vous en reste {left}."
        if exc.data.get("closed_reason") == "welcome_closed":
            msg = WELCOME_CLOSED
        return _error(402, "need_credits", msg, balance=left, required=required,
                      recharge_url=exc.data.get("recharge_url"))
    if exc.status == 403:
        reason = exc.data.get("closed_reason")
        if reason == "free_budget":
            return _error(403, "closed", FREE_BUDGET, closed_reason=reason, recharge_url=exc.data.get("recharge_url"))
        return _error(403, "closed", UNAVAILABLE, closed_reason=reason)
    if exc.status in (404, 410):
        return _error(403, "unknown_member", "Votre compte n'est pas reconnu. Reconnectez-vous depuis creapulse.fr.")
    if exc.status == 401:
        log.error("porte-monnaie : outil non authentifié (secret ou slug)")
    return _error(503, "wallet", "Le porte-monnaie de crédits ne répond pas. Rien n'a été lancé ni débité : "
                                 "réessayez dans quelques minutes.")


async def _replay_wallet(http: httpx.AsyncClient) -> None:
    try:
        await wallet.replay_outbox(http, get_store())
    except Exception as exc:  # pragma: no cover — la file ne doit jamais bloquer l'app
        log.warning("file du porte-monnaie non rejouée : %s", exc)


async def _report_cost(http: httpx.AsyncClient, user_id: int, usage_ref: str, meter: CostMeter) -> None:
    """La file en attente, puis le coût réel de la génération (échecs compris). Après la réponse.
    Dans cet ordre, un envoi qui vient d'échouer attend le prochain passage au lieu d'être rejoué aussitôt."""
    await _replay_wallet(http)
    if meter.llm_calls:
        await wallet.report_cost(http, get_store(), user_id, usage_ref, meter.contract())


@app.get("/api/credits")
async def api_credits(request: Request):
    sess = _session(request)
    if not _access_ok(sess):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour utiliser l'outil.")
    try:
        data = await wallet.balance(request.app.state.http, sess.user_id)
    except wallet.WalletError as exc:
        log.warning("solde illisible : %s", exc)
        return _error(503, "wallet", "Solde indisponible pour le moment.")
    tier = settings.credits_tier
    can_start = bool((data.get("can_start") or {}).get(tier))
    cost = (data.get("costs") or {}).get(tier)
    message, recharge = None, True
    if not can_start:
        message, recharge = _credits_block(data, cost)
    return {"ok": True, "balance": data.get("balance"), "cost": cost,
            "can_start": can_start, "recharge_url": data.get("recharge_url") if recharge else None,
            "closed_reason": data.get("closed_reason"), "message": message}


@app.post("/api/generate")
async def api_generate(request: Request, payload: dict = Body(...)):
    ip = client_ip(request)
    if not rate_limiter.allow(ip):
        return _error(429, "rate_limited", "Trop de requêtes. Patientez une minute.")

    problems = settings.validate()
    if problems:
        return _error(503, "misconfigured", "Service en cours de configuration, réessayez plus tard.")

    raw_urls = payload.get("urls") or []
    if not isinstance(raw_urls, list) or len(raw_urls) > 20:
        return _error(400, "bad_request", "Format d'entrée invalide.")
    try:
        urls = parse_urls([str(u)[:2048] for u in raw_urls])
    except InputError as exc:
        return _error(400, "input", str(exc))

    sess = _session(request)
    if not _access_ok(sess):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour utiliser l'outil.")
    store = get_store()
    gate = gating.Gate(store)
    decision, reserved = gate.check_and_reserve(email=sess.email)
    if not decision.allowed:
        return _error(429, decision.reason, decision.message)

    http: httpx.AsyncClient = request.app.state.http
    meter = CostMeter()
    good: list = []
    usage_ref = _usage_ref()
    debited: dict = {}
    try:
        pages = await fetch_pages(urls, client=http)
        good = check_corpus(pages)
        name, slug = render.brand_from_urls([p.url for p in good])
        # Débit juste avant le modèle : lire et vérifier les pages ne coûte rien, une entrée refusée ne débite pas
        debited = await wallet.debit(http, sess.user_id, usage_ref)
        guide = await distill(good, name, client=http, meter=meter)
    except InputError as exc:
        gate.release(reserved)
        return _error(422, "input", str(exc), pages=[_page_view(p) for p in pages] if "pages" in locals() else [])
    except ScrapeError as exc:
        gate.release(reserved)
        log.error("scrape: %s", exc)
        return _error(503, "scrape", "Impossible de lire les pages pour le moment. Réessayez dans quelques minutes.")
    except wallet.WalletError as exc:
        gate.release(reserved)
        log.warning("debit %s refusé : %s %s", usage_ref, exc, exc.data)
        if exc.retryable:  # issue inconnue : le remboursement empêche un débit tardif de compter
            await wallet.refund(http, store, sess.user_id, usage_ref, "debit_timeout")
        return _wallet_refusal(exc)
    except DistillError as exc:
        gate.release(reserved)
        log.error("distill: %s", exc)
        _record_cost(store, "distill_failed", good, None, meter)
        await wallet.refund(http, store, sess.user_id, usage_ref, "distill_failed")
        resp = _error(503, "distill", "La distillation n'a pas produit un profil conforme, vos crédits sont "
                                      "remboursés. Réessayez (ou essayez avec d'autres articles).")
        resp.background = BackgroundTask(_report_cost, http, sess.user_id, usage_ref, meter)
        return resp
    except Exception as exc:  # pragma: no cover — filet
        gate.release(reserved)
        log.exception("generate failed: %s", exc)
        _record_cost(store, "error", good, None, meter)
        if debited:
            await wallet.refund(http, store, sess.user_id, usage_ref, "error")
        resp = _error(500, "internal", "Erreur inattendue. Rien ne vous a été débité. Réessayez plus tard.")
        resp.background = BackgroundTask(_report_cost, http, sess.user_id, usage_ref, meter)
        return resp

    page_views = [_page_view(p) for p in pages]
    store.bump_lead_generations(sess.email)
    profile_id = None
    # Opt-in explicite : sans la case cochée, le profil n'est gardé nulle part
    if payload.get("save") is True:
        profile_id = _save_profile(store, sess, name, slug, page_views, guide)
    _record_cost(store, "ok", good, guide.model, meter)
    log.info("generation ok ip=%s user=%s pages=%d words=%d guide=%dc model=%s usage=%s cost=%.5f$ (%s, %d appel(s)) "
             "ref=%s",
             gating.ip_key(ip)[:8], sess.user_id, len(good), sum(p.words for p in good), len(guide.markdown),
             guide.model, guide.usage, meter.llm_usd, meter.llm_source, meter.llm_calls, usage_ref)

    body = _result_payload(name, slug, guide.summary, guide.markdown, page_views, True, profile_id)
    body["balance"] = debited.get("balance")
    resp = JSONResponse(body)
    resp.background = BackgroundTask(_report_cost, http, sess.user_id, usage_ref, meter)
    return resp


def _page_view(p) -> dict:
    return {"url": p.url, "title": p.title, "words": p.words, "ok": p.ok, "reason": p.reason}


def _result_payload(name: str, slug: str, summary: str, guide_md: str, pages: list[dict], connected: bool,
                    profile_id: str | None = None) -> dict:
    """Réponse d'un profil, qu'il sorte d'une génération ou de l'historique : la page l'affiche pareil."""
    return {
        "ok": True,
        "id": profile_id,
        "name": name,
        "slug": slug,
        "summary": summary,
        "guide": guide_md,
        "prompt_block": render.render_prompt_block(name, guide_md),
        "skill_md": render.render_skill(name, slug, summary, guide_md),
        "skill_filename": render.skill_filename(slug),
        "pages": pages,
        "suggestions": distill_mod.count_suggestions(guide_md),
        "download_available": connected,
        "connected": connected,
        "cta_url": settings.cta_url,
    }


def _save_profile(store, sess: gating.Session, name: str, slug: str, pages: list[dict], guide) -> str | None:
    """Range le profil dans l'historique du compte. Ne fait jamais échouer la réponse."""
    try:
        profile_id = store.save_profile(owner=sess.email, name=name, slug=slug, pages=pages, summary=guide.summary,
                                        guide=guide.markdown, retention_months=settings.retention_months(sess.levels))
        store.purge_expired_profiles()
        return profile_id
    except Exception as exc:  # l'historique est un confort : le visiteur garde son profil à l'écran
        log.warning("profil non enregistré dans l'historique : %s", exc)
        return None


# ---------------------------------------------------------------------------
# Historique (opt-in du membre)
# ---------------------------------------------------------------------------

def _history_item(row: dict) -> dict:
    return {"id": row["id"], "created_at": row["created_at"], "name": row["name"],
            "urls": [p["url"] for p in row["pages"] if p.get("ok")], "summary": row["summary"],
            "expired": row["purged"]}


@app.get("/api/history")
async def api_history(request: Request):
    sess = _session(request)
    if not _access_ok(sess):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour retrouver vos profils.")
    rows = get_store().list_profiles(sess.email)
    return {"ok": True, "profiles": [_history_item(r) for r in rows],
            "retention_months": settings.retention_months(sess.levels),
            "max_profiles": settings.history_max_per_account}


@app.get("/api/history/{profile_id}")
async def api_history_profile(request: Request, profile_id: str):
    sess = _session(request)
    if not _access_ok(sess):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour retrouver vos profils.")
    row = get_store().get_profile(sess.email, profile_id[:64])
    if not row:
        return _error(404, "not_found", "Ce profil n'existe pas ou plus.")
    if row["purged"]:
        months = row["retention_months"]
        return _error(410, "expired", f"Ce profil a dépassé sa durée de conservation ({months} mois) "
                                      "et son contenu a été effacé. Relancez une génération avec les mêmes URLs.",
                      urls=[p["url"] for p in row["pages"] if p.get("ok")])
    return _result_payload(row["name"], row["slug"], row["summary"] or "", row["guide"], row["pages"], True,
                           row["id"])


@app.delete("/api/history/{profile_id}")
async def api_history_delete(request: Request, profile_id: str):
    sess = _session(request)
    if not _access_ok(sess):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour retrouver vos profils.")
    if not get_store().delete_profile(sess.email, profile_id[:64]):
        return _error(404, "not_found", "Ce profil n'existe pas ou plus.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Téléchargement du skill (stateless : le client renvoie le contenu généré)
# ---------------------------------------------------------------------------

@app.post("/api/download/skill")
async def api_download_skill(request: Request, payload: dict = Body(...)):
    if not _access_ok(_session(request)):
        return _error(403, "need_login", "Connectez-vous depuis creapulse.fr pour télécharger le skill.")
    content = str(payload.get("skill_md") or "")
    if not content.startswith("---\nname: voix-") or len(content) > 60_000:
        return _error(400, "bad_request", "Contenu invalide.")
    slug = render.slugify(str(payload.get("slug") or "ma-marque")) or "ma-marque"
    if str(payload.get("format") or "md") == "zip":
        # Archive prête à importer dans Claude (Paramètres → Skills) : un dossier voix-<slug>/SKILL.md
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(f"voix-{slug}/SKILL.md", content)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="voix-{slug}.zip"'})
    filename = render.skill_filename(slug)
    return Response(content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
