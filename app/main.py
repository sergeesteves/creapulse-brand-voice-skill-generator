"""Générateur de skill de voix de marque IA — micro-app stateless (FastAPI).

Flux : URLs → crawl4ai (scrape) → omniroute (distillation, T° basse) → 2 formats self-contained.
Gating : 1 génération anonyme, puis email (magic-link) ; caps compte / IP / global.
"""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import gating, render
from .config import settings
from .distill import DistillError, distill
from .mailer import send_magic_link
from .scraper import InputError, ScrapeError, check_corpus, fetch_pages, parse_urls
from .store import get_store

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
    app.state.http = httpx.AsyncClient(timeout=max(settings.crawl_timeout_s, settings.llm_timeout_s))
    yield
    await app.state.http.aclose()
    store.close()


app = FastAPI(title=settings.app_name, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan,
              root_path=settings.root_path)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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


def _session_email(request: Request) -> str | None:
    return gating.read_session(request.cookies.get(gating.SESSION_COOKIE))


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    email = _session_email(request)
    anon = gating.read_anon(request.cookies.get(gating.ANON_COOKIE))
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "settings": settings,
            "root": settings.root_path,
            "mail_mode": settings.mail_mode,
            "email": email,
            "anon_used": anon,
            "anon_free": settings.anon_free_generations,
            "connected": request.query_params.get("connected") == "1",
            "auth_error": request.query_params.get("auth") == "invalid",
            "max_urls": settings.max_urls,
            "min_words": settings.min_words_per_page,
        },
    )


@app.get("/health")
async def health():
    return {"status": "ok", "app": "voice-skill-generator", "mail_mode": settings.mail_mode}


# ---------------------------------------------------------------------------
# Génération
# ---------------------------------------------------------------------------

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
    want_verbatim = bool(payload.get("verbatim"))
    try:
        urls = parse_urls([str(u)[:2048] for u in raw_urls])
    except InputError as exc:
        return _error(400, "input", str(exc))

    email = _session_email(request)
    anon_count = gating.read_anon(request.cookies.get(gating.ANON_COOKIE))
    store = get_store()
    gate = gating.Gate(store)
    decision, reserved = gate.check_and_reserve(email=email, anon_count=anon_count, ip=ip)
    if not decision.allowed:
        status = 429 if decision.reason in ("global_cap", "account_cap", "ip_cap") else 403
        return _error(status, decision.reason, decision.message, cta_url=settings.cta_url)

    http: httpx.AsyncClient = request.app.state.http
    try:
        pages = await fetch_pages(urls, client=http)
        good = check_corpus(pages)
        name, slug = render.brand_from_urls([p.url for p in good])
        guide = await distill(good, name, client=http)
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
        return _error(503, "distill", "La distillation n'a pas produit un profil conforme. "
                                      "Réessayez (ou essayez avec d'autres articles).")
    except Exception as exc:  # pragma: no cover — filet
        gate.release(reserved)
        log.exception("generate failed: %s", exc)
        return _error(500, "internal", "Erreur inattendue. Réessayez plus tard.")

    verbatim_allowed = bool(email)
    extra = ""
    excerpts_count = 0
    if want_verbatim and verbatim_allowed:
        excerpts = render.pick_excerpts(good)
        extra = render.excerpts_section(excerpts)
        excerpts_count = len(excerpts)

    prompt_block = render.render_prompt_block(name, guide.markdown, extra)
    skill_md = render.render_skill(name, slug, guide.summary, guide.markdown, extra)

    if email:
        store.bump_lead_generations(email)
    log.info("generation ok ip=%s email=%s pages=%d words=%d model=%s usage=%s",
             gating.ip_key(ip)[:8], "yes" if email else "no", len(good), sum(p.words for p in good),
             guide.model, guide.usage)

    resp = JSONResponse({
        "ok": True,
        "name": name,
        "slug": slug,
        "summary": guide.summary,
        "guide": guide.markdown,
        "prompt_block": prompt_block,
        "skill_md": skill_md,
        "skill_filename": render.skill_filename(slug),
        "pages": [_page_view(p) for p in pages],
        "verbatim": {"requested": want_verbatim, "applied": excerpts_count > 0, "count": excerpts_count,
                     "available": verbatim_allowed},
        "download_available": bool(email),
        "connected": bool(email),
        "cta_url": settings.cta_url,
    })
    if not email:
        _set_cookie(resp, gating.ANON_COOKIE, gating.sign_anon(decision.anon_count), 365 * 24 * 3600)
    return resp


def _page_view(p) -> dict:
    return {"url": p.url, "title": p.title, "words": p.words, "ok": p.ok, "reason": p.reason}


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
    filename = render.skill_filename(slug)
    return Response(content, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
