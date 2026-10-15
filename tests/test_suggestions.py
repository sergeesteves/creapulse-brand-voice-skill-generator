"""Répertoire minimum et expressions suggérées, marquées « (à valider) ».

Une liste de deux ou trois expressions se recycle : le modèle qui applique le guide pioche toujours
la même. Mesuré sur le guide Creapulse, où trois attaques familières citées produisaient « Bon, »
à chaque section.
"""
import pytest
from fastapi.testclient import TestClient

from app import distill as distill_mod
from app import gating, render
from app.config import settings
from app.distill import SUGGESTION_MARK, build_system_prompt, count_suggestions
from app.distill import Guide
from app.scraper import PageText, word_count

GUIDE_AVEC = """## Ton
- **Adresse directe** : « Vous me direz »
## Style de phrase
- **Incises familières**, à varier : « Bon, », « En gros », « Ceci dit », D'ailleurs [suggestion]
## Vocabulaire
- **Jugement** : « inutile », « néfaste »
## Structure / format
- **Intro personnelle** : « Je vais tenter »
## Principes édito / positionnement
- **Preuve chiffrée** : « prouver par A + B »
## Résumé en une phrase
Une voix directe."""

GUIDE_SANS = GUIDE_AVEC.replace(", D'ailleurs [suggestion]", "")


def test_system_prompt_carries_repertoire_and_suggestion_rules(monkeypatch):
    monkeypatch.setattr(settings, "min_repertoire", 5)
    monkeypatch.setattr(settings, "max_suggestions", 3)
    p = build_system_prompt()
    assert "AU MOINS 5 expressions DIFFÉRENTES" in p
    assert "3 au plus" in p and SUGGESTION_MARK in p
    assert "sans guillemets" in p and "à l'intérieur d'une citation" in p
    assert "(constant), (fréquent), (occasionnel) ou (rare)" in p
    assert "{" not in p.split("SORTIE")[0]  # tous les gabarits sont remplis


def test_count_suggestions():
    assert count_suggestions(GUIDE_AVEC) == 1
    assert count_suggestions(GUIDE_SANS) == 0


def test_note_appears_only_when_suggestions_are_present():
    assert render.suggestion_note(GUIDE_SANS) == ""
    note = render.suggestion_note(GUIDE_AVEC)
    assert SUGGESTION_MARK in note and "supprimez les autres" in note

    avec = render.render_prompt_block("Ex", GUIDE_AVEC)
    sans = render.render_prompt_block("Ex", GUIDE_SANS)
    assert "supprimez les autres" in avec and "supprimez les autres" not in sans
    # la note est au-dessus du guide, pas noyée dedans
    assert avec.index("supprimez les autres") < avec.index("## Ton")

    skill = render.render_skill("Ex", "ex", "Une voix directe.", GUIDE_AVEC)
    assert "supprimez les autres" in skill
    assert skill.startswith("---\nname: voix-ex")
    assert "## Ton" in render.render_skill("Ex", "ex", "V.", GUIDE_SANS)


def test_shorten_prompt_protects_the_repertoire(monkeypatch):
    monkeypatch.setattr(settings, "min_repertoire", 5)
    body = distill_mod.SHORTEN_PROMPT.format(actual=9000, max_chars=5000, min_repertoire=5,
                                             guide=GUIDE_AVEC)
    assert "sous 5 entrées" in body and "Coupe ailleurs" in body


PROSE = "\n\n".join([("Bon, pour faire simple, voici une phrase de prose qui compte des mots utiles "
                      "pour la voix éditoriale. " * 5).strip()] * 8)


@pytest.fixture
def client(monkeypatch):
    from app import main

    async def fake_fetch(urls, client=None):
        return [PageText(u, "T", PROSE, word_count(PROSE), True, "", "fr") for u in urls]

    async def fake_distill(pages, name, client=None):
        return Guide(GUIDE_AVEC, {}, "Une voix directe.", "fake", {})

    monkeypatch.setattr(main, "fetch_pages", fake_fetch)
    monkeypatch.setattr(main, "distill", fake_distill)
    monkeypatch.setattr(main, "rate_limiter", gating.RateLimiter(1000))
    monkeypatch.setattr(settings, "min_urls", 1)
    with TestClient(main.app) as c:
        yield c


def test_api_reports_the_number_of_suggestions(client):
    r = client.post("/api/generate", json={"urls": ["https://example.org/a"]})
    assert r.status_code == 200
    data = r.json()
    assert data["suggestions"] == 1
    assert "supprimez les autres" in data["prompt_block"]
