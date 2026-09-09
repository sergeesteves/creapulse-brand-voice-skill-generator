"""Repli d'extraction directe quand crawl4ai est en panne (proxy 402 / tunnel / timeout / service KO)."""
import asyncio
import json

import httpx
import pytest

from app.config import settings
from app.scraper import fetch_pages

def _para(i: int) -> str:  # paragraphes tous différents (trafilatura déduplique les blocs identiques)
    return " ".join(f"Bon, pour faire simple, voici la phrase {i}-{j} de prose qui compte des mots utiles pour la "
                    f"voix éditoriale du site et qui continue un peu avec le chiffre {i * 7 + j}." for j in range(4))


HTML = ("<html><head><title>Mon article de fond</title><meta name='description' content='x'></head><body>"
        "<nav><a href='/'>Accueil</a> <a href='/blog'>Blog</a></nav>"
        "<article><h1>Mon article de fond</h1>" + "".join(f"<p>{_para(i)}</p>" for i in range(12)) +
        "</article><footer>Ce site utilise des cookies. <a href='/x'>Mentions</a></footer></body></html>")


def _run(handler, urls):
    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_pages(urls, client=client)
    return asyncio.run(go())


def _crawl_result(url, **kw):
    base = {"url": url, "success": False, "status_code": None,
            "error_message": "Failed on navigating ACS-GOTO: net::ERR_TUNNEL_CONNECTION_FAILED", "markdown": {}}
    base.update(kw)
    return base


def test_tunnel_failure_falls_back_to_direct_fetch():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, str(req.url)))
        if req.url.path == "/crawl":
            urls = json.loads(req.content)["urls"]
            return httpx.Response(200, json={"success": True, "results": [_crawl_result(u) for u in urls]})
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html; charset=utf-8"})

    pages = _run(handler, ["https://ex.fr/mon-article"])
    assert seen[0] == ("POST", f"{settings.crawl4ai_url}/crawl") and seen[1] == ("GET", "https://ex.fr/mon-article")
    p = pages[0]
    assert p.ok and p.words >= 300 and p.title == "Mon article de fond"
    assert "cookies" not in p.text.lower() and "Accueil" not in p.text  # boilerplate retiré par trafilatura


def test_crawl4ai_unreachable_falls_back_for_all_urls():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/crawl":
            raise httpx.ConnectTimeout("crawl4ai down")
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    pages = _run(handler, ["https://ex.fr/a", "https://ex.fr/b"])
    assert all(p.ok for p in pages)


def test_real_http_errors_are_not_retried_directly():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        urls = json.loads(req.content)["urls"]
        return httpx.Response(200, json={"success": True, "results": [
            _crawl_result(u, status_code=404, error_message="") for u in urls]})

    pages = _run(handler, ["https://ex.fr/introuvable"])
    assert not pages[0].ok and "404" in pages[0].reason
    assert calls == ["/crawl"]  # un vrai 404 n'est pas rejoué en direct


def test_direct_fetch_respects_status_and_content_type():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/crawl":
            raise httpx.ConnectTimeout("down")
        if req.url.path == "/pdf":
            return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        return httpx.Response(403, text="forbidden")

    pages = _run(handler, ["https://ex.fr/pdf", "https://ex.fr/bloque"])
    assert not pages[0].ok and "HTML" in pages[0].reason
    assert not pages[1].ok and "403" in pages[1].reason


def test_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "direct_fallback", False)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        urls = json.loads(req.content)["urls"]
        return httpx.Response(200, json={"success": True, "results": [_crawl_result(u) for u in urls]})

    pages = _run(handler, ["https://ex.fr/a"])
    assert not pages[0].ok and calls == ["/crawl"]
