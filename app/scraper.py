"""Scrape via le crawl4ai existant + nettoyage + garde-fous de qualité d'entrée.

Principe : mieux vaut refuser proprement une page faible que produire un profil bidon.
Le contenu scrapé ne vit qu'en mémoire, le temps de la requête.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import settings

# ---------------------------------------------------------------------------
# Validation d'URL
# ---------------------------------------------------------------------------

_BLOCKED_HOSTS = {"localhost", "localhost.localdomain"}


class InputError(ValueError):
    """Erreur d'entrée à afficher telle quelle au visiteur."""


def normalize_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise InputError("URL vide.")
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https"):
        raise InputError(f"Seules les URLs http(s) sont acceptées : {raw}")
    host = (parts.hostname or "").lower()
    if not host or "." not in host or host in _BLOCKED_HOSTS:
        raise InputError(f"URL invalide : {raw}")
    if _is_private_host(host):
        raise InputError(f"Adresse non publique refusée : {host}")
    # Pas de fragment, pas de credentials
    if parts.username or parts.password:
        raise InputError(f"URL avec identifiants refusée : {host}")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def _is_private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        pass
    # Résolution DNS best-effort (crawl4ai a aussi son propre garde anti-SSRF)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
            if not ip.is_global:
                return True
        except ValueError:
            continue
    return False


def parse_urls(raw_urls: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in raw_urls:
        if not (raw or "").strip():
            continue
        url = normalize_url(raw)
        if url not in seen:
            seen.append(url)
    if not seen:
        raise InputError("Indiquez au moins une URL d'article que vous avez écrit.")
    if len(seen) > settings.max_urls:
        raise InputError(f"Maximum {settings.max_urls} URLs.")
    return seen


# ---------------------------------------------------------------------------
# Nettoyage du Markdown renvoyé par crawl4ai
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]\([^)]*\)")  # tolère un niveau de [crochets] imbriqués
_IMG_RE = re.compile(r"!\[(?:[^\[\]]|\[[^\[\]]*\])*\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")
_WORD_RE = re.compile(r"[\w'’-]+", re.U)

# Marqueurs de fin d'article (boilerplate de blog) — coupe tout ce qui suit,
# uniquement si on est déjà dans la seconde moitié du texte.
_TAIL_MARKERS = re.compile(
    r"^(#+\s*)?("
    r"laisser un commentaire|leave a (comment|reply)|commentaires?\b|comments?\b|"
    r"vous aimerez (peut-être )?aussi|articles? (similaires|reli[ée]s|recommand[ée]s)|"
    r"related (posts|articles)|à propos de l'auteur|a propos de|about the author|"
    r"partag(er|es)\b|share (this|via)|"
    r"ce site utilise des cookies|nous utilisons des cookies|this (web)?site uses cookies|"
    r"préférences pour les cookies|cookie (settings|preferences|policy)|"
    r"newsletter|abonnez-vous|subscribe\b|"
    r"rejoindre la discussion|inscrivez-vous"
    r")",
    re.I,
)
_COOKIE_LINE = re.compile(r"cookie|rgpd|gdpr|consent|tracking|google analytics|webfont", re.I)
_BREADCRUMB = re.compile(r"^(vous êtes ici|you are here)\b|^accueil\s*[>›»/]", re.I)
_TOC_HEAD = re.compile(r"^(sommaire|table des matières|table of contents|contents?)\s*:?\s*$", re.I)


@dataclass
class PageText:
    url: str
    title: str
    text: str
    words: int
    ok: bool
    reason: str = ""
    language: str = ""


def _strip_links(line: str) -> str:
    line = _IMG_RE.sub("", line)
    line = _LINK_RE.sub(r"\1", line)
    line = _URL_RE.sub("", line)
    return line


def _link_density(text: str) -> float:
    """Part des mots situés dans des liens Markdown (0..1) — les URLs ne comptent pas comme mots."""
    text = _IMG_RE.sub("", text)
    in_links = sum(len(_WORD_RE.findall(m.group(1))) for m in _LINK_RE.finditer(text))
    visible = _URL_RE.sub("", _LINK_RE.sub(r"\1", text))
    total = len(_WORD_RE.findall(visible))
    if total == 0:
        return 1.0
    return min(1.0, in_links / total)


def clean_markdown(md: str) -> str:
    """Retire TOC, listes de liens, partage, cookies, méta d'auteur ; garde le corps."""
    if not md:
        return ""
    lines = md.replace("\r", "").split("\n")
    out: list[str] = []
    n = len(lines)
    in_toc = False
    seen_prose = False  # avant le 1er vrai paragraphe, on ignore puces et fils d'Ariane (nav de série…)
    for i, raw in enumerate(lines):
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            in_toc = False
            out.append("")
            continue
        if _BREADCRUMB.search(stripped):
            continue
        if not seen_prose:
            if stripped.startswith("#"):
                seen_prose = True
            elif stripped.startswith(("*", "-", "+")) or len(_WORD_RE.findall(_strip_links(stripped))) < 20:
                continue
            else:
                seen_prose = True
        # Sommaire : on saute le titre et la liste de liens qui suit
        if _TOC_HEAD.match(stripped):
            in_toc = True
            continue
        if in_toc and stripped.startswith(("*", "-", "+")) and _LINK_RE.search(stripped):
            continue
        in_toc = False
        # Coupe le pied de page dès qu'on est dans la seconde moitié
        if i > n * 0.5 and _TAIL_MARKERS.match(stripped):
            break
        if _COOKIE_LINE.search(stripped) and i > n * 0.5:
            continue
        # Ligne faite (quasi) uniquement de liens = navigation / partage / "lire aussi"
        if _LINK_RE.search(stripped) and _link_density(stripped) > 0.7:
            continue
        # Puce qui n'est qu'un lien
        if re.match(r"^[*\-+]\s*\[?[^\]]*\]\(", stripped) and _link_density(stripped) > 0.5:
            continue
        cleaned = _strip_links(line)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        # Ligne de métadonnées WordPress "05/12/2017/par Auteur"
        if re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}/?\s*(par|by)\b", cleaned.strip(), re.I):
            continue
        out.append(cleaned)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def word_count(text: str) -> int:
    return len(_WORD_RE.findall(text))


def truncate_words(text: str, max_words: int) -> str:
    """Tronque proprement à max_words (coupe à la fin d'un paragraphe si possible)."""
    if word_count(text) <= max_words:
        return text
    kept: list[str] = []
    count = 0
    for block in text.split("\n"):  # crawl4ai sépare les blocs par une seule ligne
        w = word_count(block)
        if count + w > max_words:
            remaining = max_words - count
            if remaining > 0:  # on coupe à l'intérieur du bloc, on ne le jette pas
                kept.append(" ".join(block.split()[:remaining]) + " […]")
            break
        kept.append(block)
        count += w
    return "\n".join(kept).strip()


_NOT_FOUND_TITLE = re.compile(r"\b(404|introuvable|not found|page non trouvée|erreur)\b", re.I)
_LISTING_PATH = re.compile(
    r"^/?$"                                                         # racine du site
    r"|^/(blog|articles?|news|actualites?|feed|archives?)/?$"       # index de blog (segment seul)
    r"|^/(category|categorie|categories|tag|tags|author|auteur|page|search|recherche)(/|$)"
    r"|/page/[0-9]+/?$",                                            # pagination
    re.I,
)


def is_listing_url(url: str) -> bool:
    """Accueil, catégorie, tag, pagination, auteur… : jamais un article."""
    path = urlsplit(url).path or "/"
    return bool(_LISTING_PATH.search(path))


def assess_page(url: str, status_code: int | None, success: bool, error: str, title: str, raw_md: str,
                language: str = "") -> PageText:
    """Applique les garde-fous et renvoie le texte nettoyé ou une raison de refus."""
    title = (title or "").strip()
    if is_listing_url(url):
        return PageText(url, title, "", 0, False,
                        "page d'accueil ou de liste (catégorie, tag, pagination) — indiquez l'URL d'un article", language)
    if not success or (status_code and status_code >= 400):
        why = f"page inaccessible (HTTP {status_code})" if status_code else "page inaccessible"
        if error and "anti-bot" in error.lower():
            why = "contenu vide ou protégé (anti-bot)"
        return PageText(url, title, "", 0, False, why, language)
    if _NOT_FOUND_TITLE.search(title) and word_count(raw_md) < 400:
        return PageText(url, title, "", 0, False, "page d'erreur (404 ?)", language)
    density = _link_density(raw_md)
    text = clean_markdown(raw_md)
    words = word_count(text)
    if words < settings.min_words_per_page:
        if density > settings.max_link_density:
            reason = "page de navigation (accueil, catégorie…) plutôt qu'un article"
        else:
            reason = f"texte trop court ({words} mots, minimum {settings.min_words_per_page})"
        return PageText(url, title, "", words, False, reason, language)
    if density > 0.6:
        return PageText(url, title, "", words, False,
                        "page de liens (accueil, catégorie, sommaire) plutôt qu'un article", language)
    headings = len(re.findall(r"^#{1,6}\s", text, re.M))
    if headings >= 10 and words / headings < 60:
        return PageText(url, title, "", words, False,
                        "page de liste d'articles (extraits + titres) plutôt qu'un article complet", language)
    text = truncate_words(text, settings.max_words_per_page)
    return PageText(url, title, text, word_count(text), True, "", language)


# ---------------------------------------------------------------------------
# Appel crawl4ai
# ---------------------------------------------------------------------------

def _crawl_payload(urls: list[str]) -> dict:
    return {
        "urls": urls,
        "browser_config": {"type": "BrowserConfig", "params": {"headless": True, "text_mode": True}},
        "crawler_config": {
            "type": "CrawlerRunConfig",
            "params": {
                "cache_mode": "bypass",
                "page_timeout": settings.crawl_page_timeout_ms,
                "excluded_tags": ["nav", "footer", "aside", "header", "form"],
                "exclude_external_links": True,
                "markdown_generator": {
                    "type": "DefaultMarkdownGenerator",
                    "params": {
                        "content_filter": {
                            "type": "PruningContentFilter",
                            "params": {"threshold": 0.45, "threshold_type": "dynamic"},
                        }
                    },
                },
            },
        },
    }


class ScrapeError(RuntimeError):
    """Panne côté scraper (pas une erreur du visiteur)."""


async def fetch_pages(urls: list[str], client: httpx.AsyncClient | None = None) -> list[PageText]:
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.crawl_timeout_s)
    try:
        resp = await client.post(
            f"{settings.crawl4ai_url}/crawl",
            json=_crawl_payload(urls),
            headers={"Authorization": f"Bearer {settings.crawl4ai_token}"},
            timeout=settings.crawl_timeout_s,
        )
    except httpx.HTTPError as exc:  # réseau / timeout
        raise ScrapeError(f"crawl4ai injoignable : {exc.__class__.__name__}") from exc
    finally:
        if own:
            await client.aclose()
    if resp.status_code != 200:
        raise ScrapeError(f"crawl4ai a répondu HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise ScrapeError("réponse crawl4ai illisible") from exc
    by_url: dict[str, dict] = {}
    for r in data.get("results", []) or []:
        by_url[(r.get("url") or "").rstrip("/")] = r
    pages: list[PageText] = []
    for url in urls:
        r = by_url.get(url.rstrip("/")) or {}
        md = r.get("markdown") or {}
        if isinstance(md, str):
            raw_md = md
        else:
            raw_md = md.get("fit_markdown") or md.get("raw_markdown") or ""
        meta = r.get("metadata") or {}
        pages.append(
            assess_page(
                url=url,
                status_code=r.get("status_code"),
                success=bool(r.get("success")) if r else False,
                error=r.get("error_message") or ("aucun résultat" if not r else ""),
                title=meta.get("title") or "",
                raw_md=raw_md,
                language=(meta.get("language") or "")[:8],
            )
        )
    return pages


def check_corpus(pages: list[PageText]) -> list[PageText]:
    """Vérifie qu'on a assez de matière ; sinon lève une InputError détaillée."""
    good = [p for p in pages if p.ok]
    total = sum(p.words for p in good)
    if not good or total < settings.min_words_total:
        details = "\n".join(f"• {p.url} — {p.reason or f'{p.words} mots'}" for p in pages)
        raise InputError(
            "Pas assez de texte exploitable pour distiller une voix fiable "
            f"({total} mots, minimum {settings.min_words_total}). "
            "Indiquez des URLs d'articles complets que vous avez écrits (pas d'accueil, de catégorie "
            "ni de page vitrine).\n" + details
        )
    # Budget total : on tronque équitablement si nécessaire
    if total > settings.max_words_total:
        per_page = max(settings.min_words_per_page, settings.max_words_total // len(good))
        for p in good:
            p.text = truncate_words(p.text, per_page)
            p.words = word_count(p.text)
    return good
