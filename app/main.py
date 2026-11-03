"""Générateur de skill de voix de marque IA — micro-app stateless (FastAPI).

Flux : URLs → crawl4ai (scrape) → omniroute (distillation, T° basse) → 2 formats self-contained.
Gating : 1 génération anonyme, puis email (magic-link) ; caps compte / IP / global.
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
from . import gating, render
from . import wp_token as wp_token_mod
from .config import settings
from .costs import CostMeter
from .distill import DistillError, distill
from .mailer import send_magic_link
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
        # Jamais en prod (les cookies/magic-links seraient invalidés à chaque redémarrage)
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


def _session_email(request: Request) -> str | None:
    sess = _session(request)
    return sess.email if sess else None


def _access_ok(sess: gating.Session | None) -> bool:
    """Mode required : il faut une session membre (jeton WordPress) avec un niveau requis, le cas échéant."""
    if settings.auth_mode != "required":
        return True
    return bool(sess and sess.is_member and sess.has_level(settings.required_levels))


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
                        gating.sign_session(ident.email, "wordpress", ident.levels, ident.name),
                        settings.member_session_ttl_s)
            resp.delete_cookie(gating.ANON_COOKIE, path=settings.cookie_path)
            log.info("wp token ok user=%s levels=%s", ident.user_id, list(ident.levels))
            # Déclare l'usage à WordPress (→ Nimble) APRÈS l'envoi de la réponse : jamais bloquant, jeton encore valide
            resp.background = BackgroundTask(notify_tool_event, request.app.state.http, wp_token)
        else:
            log.warning("wp token rejeté (signature/expiration/format) — comportement anonyme")
        return resp

    sess = _session(request)
    anon = gating.read_anon(request.cookies.get(gating.ANON_COOKIE))
    login_url, register_url = _wp_link(request, "login_url"), _wp_link(request, "register_url")
    resp = templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "root": settings.root_path,
            "static_version": STATIC_VERSION,
            "mail_mode": settings.mail_mode,
            "embed": request.query_params.get("embed") == "1",
            "email": sess.email if sess else None,
            "session": sess,
            "is_member": bool(sess and sess.is_member),
            "locked": not _access_ok(sess),
            "auth_mode": settings.auth_mode,
            "login_url": login_url,
            "register_url": register_url,
            "anon_used": anon,
            "anon_free": settings.anon_free_generations,
            "connected": request.query_params.get("connected") == "1",
            "auth_error": request.query_params.get("auth") == "invalid",
            "max_urls": settings.max_urls,
            "min_urls": settings.min_urls,
            "min_words": settings.min_words_per_page,
        },
    )
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/health")
async def health():
    return {"status": "ok", "app": "voice-skill-generator", "mail_mode": settings.mail_mode}


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
        return _error(403, "need_login", "Cet outil est réservé aux membres. Connectez-vous ou créez un compte.")
    email = sess.email if sess else None
    anon_count = gating.read_anon(request.cookies.get(gating.ANON_COOKIE))
    store = get_store()
    gate = gating.Gate(store)
    decision, reserved = gate.check_and_reserve(email=email, anon_count=anon_count, ip=ip)
    if not decision.allowed:
        status = 429 if decision.reason in ("global_cap", "account_cap", "ip_cap") else 403
        return _error(status, decision.reason, decision.message, cta_url=settings.cta_url)

    http: httpx.AsyncClient = request.app.state.http
    meter = CostMeter()
    good: list = []
    try:
        pages = await fetch_pages(urls, client=http)
        good = check_corpus(pages)
        name, slug = render.brand_from_urls([p.url for p in good])
        guide = await distill(good, name, client=http, meter=meter)
    except InputError as exc:
        gate.release(reserved)
        return _error(422, "input", str(exc), pages=[_page_view(p) for p in pages] if "pages" in locals() else [])
    except ScrapeError as exc:
        gate.release(reserved)
        log.error("scrape: %s", exc)
        return _error(503, "scrape", "Impossible de lire les pages pour le moment. Réessayez dans quelques minutes.")
    except DistillError as exc:
        gate.release(reserved)
        log.error("distill: %s", exc)
        _record_cost(store, "distill_failed", good, None, meter)
        return _error(503, "distill", "La distillation n'a pas produit un profil conforme. "
                                      "Réessayez (ou essayez avec d'autres articles).")
    except Exception as exc:  # pragma: no cover — filet
        gate.release(reserved)
        log.exception("generate failed: %s", exc)
        _record_cost(store, "error", good, None, meter)
        return _error(500, "internal", "Erreur inattendue. Réessayez plus tard.")

    page_views = [_page_view(p) for p in pages]
    profile_id = None
    if email:
        store.bump_lead_generations(email)
        profile_id = _save_profile(store, sess, name, slug, page_views, guide)
    _record_cost(store, "ok", good, guide.model, meter)
    log.info("generation ok ip=%s email=%s pages=%d words=%d guide=%dc model=%s usage=%s cost=%.5f$ (%s, %d appel(s))",
             gating.ip_key(ip)[:8], "yes" if email else "no", len(good), sum(p.words for p in good),
             len(guide.markdown), guide.model, guide.usage, meter.llm_usd, meter.llm_source, meter.llm_calls)

    resp = JSONResponse(_result_payload(name, slug, guide.summary, guide.markdown, page_views, bool(email),
                                        profile_id))
    if not email:
        _set_cookie(resp, gating.ANON_COOKIE, gating.sign_anon(decision.anon_count), 365 * 24 * 3600)
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
# Historique (compte identifié)
# ---------------------------------------------------------------------------

def _history_item(row: dict) -> dict:
    return {"id": row["id"], "created_at": row["created_at"], "name": row["name"],
            "urls": [p["url"] for p in row["pages"] if p.get("ok")], "summary": row["summary"],
            "expired": row["purged"]}


@app.get("/api/history")
async def api_history(request: Request):
    sess = _session(request)
    if not sess:
        return _error(403, "need_email", "L'historique est réservé aux comptes identifiés.")
    rows = get_store().list_profiles(sess.email)
    return {"ok": True, "profiles": [_history_item(r) for r in rows],
            "retention_months": settings.retention_months(sess.levels),
            "max_profiles": settings.history_max_per_account}


@app.get("/api/history/{profile_id}")
async def api_history_profile(request: Request, profile_id: str):
    sess = _session(request)
    if not sess:
        return _error(403, "need_email", "L'historique est réservé aux comptes identifiés.")
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
    if not sess:
        return _error(403, "need_email", "L'historique est réservé aux comptes identifiés.")
    if not get_store().delete_profile(sess.email, profile_id[:64]):
        return _error(404, "not_found", "Ce profil n'existe pas ou plus.")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Email / magic-link
# ---------------------------------------------------------------------------

@app.post("/api/magic-link")
async def api_magic_link(request: Request, payload: dict = Body(...)):
    ip = client_ip(request)
    if not rate_limiter.allow("mail:" + ip):
        return _error(429, "rate_limited", "Trop de demandes. Patientez une minute.")
    email = str(payload.get("email") or "").strip().lower()
    consent = bool(payload.get("consent"))
    if settings.auth_mode == "required":
        return _error(403, "need_login", "Cet outil est réservé aux membres : la connexion passe par le site.")
    if not gating.valid_email(email):
        return _error(400, "email", "Adresse email invalide.")
    if not consent:
        return _error(400, "consent", "Le consentement est requis pour recevoir le lien.")
    store = get_store()
    # Anti-spam : 5 envois / jour / email
    if store.increment("magic", email) > 5:
        return _error(429, "rate_limited", "Trop de liens demandés pour cette adresse aujourd'hui.")
    store.upsert_lead(email, consent)
    if settings.mail_mode == "direct":
        # Pas de SMTP : l'email est capturé (lead) et l'accès débloqué immédiatement, sans vérification.
        resp = JSONResponse({"ok": True, "connected": True,
                             "message": "Merci ! Votre accès est débloqué, la page va se recharger."})
        _set_cookie(resp, gating.SESSION_COOKIE, gating.sign_session(email), settings.session_ttl_s)
        resp.delete_cookie(gating.ANON_COOKIE, path=settings.cookie_path)
        return resp
    token = gating.make_magic_token(email)
    link = f"{settings.public_base_url}/auth/verify?token={token}"
    try:
        send_magic_link(email, link)
    except Exception as exc:
        log.error("mail send failed: %s", exc)
        return _error(503, "mail", "Impossible d'envoyer l'email pour le moment. Réessayez plus tard.")
    body: dict[str, Any] = {"ok": True, "connected": False,
                            "message": "Lien envoyé. Vérifiez votre boîte mail (et les spams)."}
    if settings.debug:
        body["debug_link"] = link
    return JSONResponse(body)


@app.get("/auth/verify")
async def auth_verify(token: str = ""):
    email = gating.read_magic_token(token)
    home = settings.root_path or ""
    if not email:
        return RedirectResponse(f"{home}/?auth=invalid", status_code=303)
    store = get_store()
    store.upsert_lead(email, consent=True)
    store.mark_verified(email)
    resp = RedirectResponse(f"{home}/?connected=1", status_code=303)
    _set_cookie(resp, gating.SESSION_COOKIE, gating.sign_session(email), settings.session_ttl_s)
    resp.delete_cookie(gating.ANON_COOKIE, path=settings.cookie_path)
    return resp


@app.post("/auth/logout")
async def auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(gating.SESSION_COOKIE, path=settings.cookie_path)
    return resp


# ---------------------------------------------------------------------------
# Téléchargement du skill (stateless : le client renvoie le contenu généré)
# ---------------------------------------------------------------------------

@app.post("/api/download/skill")
async def api_download_skill(request: Request, payload: dict = Body(...)):
    email = _session_email(request)
    if not email:
        return _error(403, "need_email", "Le téléchargement du skill nécessite un email vérifié.")
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
