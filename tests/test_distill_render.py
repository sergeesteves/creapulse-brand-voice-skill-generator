import pytest

from app.distill import DistillError, parse_guide
from app.render import (brand_from_urls, excerpts_section, pick_excerpts, render_prompt_block,
                        render_skill, slugify)
from app.scraper import PageText

GUIDE = """```markdown
## Ton
- **Vouvoiement complice** : « Vous me direz… », « Imaginez bien que… »
## Style de phrase
- **Attaques orales** : « Bon, », « En gros »
## Vocabulaire (inclut les mots-signature ET les mots/tics à éviter)
- Mots-signature : « mécanique », « budget de crawl »
- À éviter : « révolutionnaire »
## Structure / format
- H2 interrogatifs : « C'est quoi des logs ? »
## Principes édito / positionnement
- Pragmatique, preuve chiffrée : « prouver par A + B »
## Résumé en une phrase
Une voix de consultant direct, orale et vulgarisatrice, qui tranche et prouve par les chiffres.
```"""


def test_parse_guide_extracts_sections_and_summary():
    canonical, sections, summary = parse_guide(GUIDE)
    assert list(sections) == ["Ton", "Style de phrase", "Vocabulaire", "Structure / format",
                              "Principes édito / positionnement", "Résumé en une phrase"]
    assert summary.startswith("Une voix de consultant direct")
    assert canonical.startswith("## Ton")
    assert "## Vocabulaire\n" in canonical  # parenthèse de consigne retirée
    assert "```" not in canonical


def test_parse_guide_summary_strips_bullet_bold_and_citation():
    raw = GUIDE.replace("Une voix de consultant direct, orale et vulgarisatrice, qui tranche et prouve par les chiffres.",
                        "- **Une voix de consultant direct qui prouve par les chiffres.** — « prouver par A + B »")
    _, _, summary = parse_guide(raw)
    assert summary == "Une voix de consultant direct qui prouve par les chiffres."


def test_parse_guide_accepts_h3_titles():
    raw = GUIDE.replace("## ", "### ").replace("```markdown", "").replace("```", "")
    canonical, sections, _ = parse_guide(raw)
    assert len(sections) == 6


def test_parse_guide_rejects_missing_sections():
    with pytest.raises(DistillError):
        parse_guide("## Ton\n- bla\n## Style de phrase\n- bla")


def test_brand_from_urls():
    name, slug = brand_from_urls(["https://www.creapulse.fr/a/", "https://www.creapulse.fr/b/"])
    assert (name, slug) == ("Creapulse", "creapulse")
    name, slug = brand_from_urls(["https://medium.com/@jane-doe/post-1"])
    assert slug == "jane-doe"
    assert slugify("Éléphant & Co !") == "elephant-co"


def test_render_templates_are_self_contained():
    canonical, _, summary = parse_guide(GUIDE)
    prompt = render_prompt_block("Creapulse", canonical)
    assert prompt.startswith("# Voix de marque — Creapulse")
    assert "Comment l'utiliser" in prompt and "ChatGPT" in prompt and "Claude" in prompt
    assert "## Ton" in prompt and "## Résumé en une phrase" in prompt
    assert "creapulse.fr" in prompt
    skill = render_skill("Creapulse", "creapulse", summary, canonical)
    assert skill.startswith("---\nname: voix-creapulse\ndescription: \"Applique la voix de marque Creapulse.")
    assert "## Ton" in skill and "brand/style-guide" not in skill  # jamais de pointeur vers un repo
    assert "\n---\n\n# Voix de marque — Creapulse" in skill


def test_pick_excerpts_is_deterministic_and_prose_only():
    prose = ("Bon, pour faire simple, voici une phrase de prose assez longue qui compte des mots utiles pour la voix "
             "éditoriale du site et qui continue encore un peu pour dépasser le seuil minimum de caractères requis. ") * 3
    page = PageText("https://ex.fr/a", "Article A", "## Titre\n\n* puce\n\n" + prose.strip() + "\n\nCourt.", 200, True)
    page2 = PageText("https://ex.fr/b", "Article B", prose.strip(), 200, True)
    ex1 = pick_excerpts([page, page2])
    ex2 = pick_excerpts([page, page2])
    assert ex1 == ex2 and len(ex1) == 2
    assert ex1[0][0] == "Article A" and not ex1[0][1].startswith("#")
    section = excerpts_section(ex1)
    assert section.startswith("## Extraits de style (verbatim)") and "**Extrait 2**" in section
    assert excerpts_section([]) == ""
