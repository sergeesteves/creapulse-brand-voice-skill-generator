import pytest

from app.scraper import (InputError, assess_page, check_corpus, clean_markdown, parse_urls,
                         truncate_words, word_count)

ARTICLE = """Sommaire:
  * [C'est quoi des logs ?](https://ex.fr/a/#logs)
  * [A quoi servent les logs](https://ex.fr/a/#servent)

**[SEO avancé](https://ex.fr/series/)**
  * [Le maillage interne](https://ex.fr/maillage/)
  * [Le glossaire du crawl](https://ex.fr/glossaire/)

Je parle souvent de l'intérêt de l'analyse de logs en SEO et j'avais pas pris le temps encore d'en faire un article tant le sujet peut être long et complexe. Comme à mon habitude, je vais essayer de vulgariser au maximum mais si certains termes vous échappent, lisez les [définitions autour du crawl](https://ex.fr/glossaire/).
## C'est quoi des logs ?
Les logs sont des fichiers hébergés sur le serveur web du site à analyser qui enregistrent le passage de Googlebot. Bon, pour faire simple, c'est mécanique : pas de crawl, pas de visites. On y reviendra plus en détail, mais retenez que Google ne crawle jamais l'intégralité d'un site.
## Conclusion
Bon, j'espère que je vous ai pas trop perdu, je sais pas si ça se voit mais j'ai fait un gros effort de clarté.

#### A propos de Serge Esteves
Consultant SEO.
05/12/2017/par Serge Esteves
##### Vous aimerez peut-être aussi
[**Le Maillage interne**](https://ex.fr/maillage/)
### Laisser un commentaire
Ce site utilise des cookies qui permettent de fournir des données anonymes.
Préférences pour les cookies
"""


def test_clean_markdown_removes_boilerplate():
    text = clean_markdown(ARTICLE)
    assert "Sommaire" not in text
    assert "SEO avancé" not in text
    assert "cookies" not in text.lower()
    assert "Laisser un commentaire" not in text
    assert "A propos de" not in text
    assert "05/12/2017" not in text
    # le corps est conservé, liens aplatis
    assert "définitions autour du crawl" in text
    assert "](http" not in text
    assert "pas de crawl, pas de visites" in text


def test_word_count_and_truncate():
    text = "\n\n".join(["mot " * 50] * 5)
    assert word_count(text) == 250
    t = truncate_words(text, 120)
    assert word_count(t) == 120 and t.endswith("[…]")  # coupe à l'intérieur du 3e bloc


def test_parse_urls_normalizes_and_dedupes():
    urls = parse_urls(["www.example.com/a", "https://www.example.com/a", "", "https://Example.com/b"])
    assert urls == ["https://www.example.com/a", "https://example.com/b"]


def test_parse_urls_rejects_bad_inputs():
    with pytest.raises(InputError):
        parse_urls([""])
    with pytest.raises(InputError):
        parse_urls(["ftp://example.com/x"])
    with pytest.raises(InputError):
        parse_urls(["http://localhost/x"])
    with pytest.raises(InputError):
        parse_urls(["http://127.0.0.1/x"])
    with pytest.raises(InputError):
        parse_urls([f"https://example.com/{i}" for i in range(6)])


def test_assess_page_rejects_404_and_short_and_navigation():
    p = assess_page("https://ex.fr/x", 404, False, "", "", "", "")
    assert not p.ok and "404" in p.reason
    p = assess_page("https://ex.fr/x", 200, False, "Blocked by anti-bot protection: Near-empty content", "", "", "")
    assert not p.ok and "anti-bot" in p.reason
    p = assess_page("https://ex.fr/x", 200, True, "", "Erreur 404 - page introuvable", "Aucune page ne correspond.", "")
    assert not p.ok and "404" in p.reason
    p = assess_page("https://ex.fr/x", 200, True, "", "Court", "Trois mots seulement.", "")
    assert not p.ok and "trop court" in p.reason
    nav = "\n".join(f"* [Article {i}](https://ex.fr/{i}/) lire" for i in range(200))
    p = assess_page("https://ex.fr/", 200, True, "", "Accueil", nav, "")
    assert not p.ok and "accueil" in p.reason
    p = assess_page("https://ex.fr/plan-du-site", 200, True, "", "Plan", nav, "")
    assert not p.ok and ("navigation" in p.reason or "liens" in p.reason)


def test_assess_page_accepts_real_article():
    body = "\n\n".join(["Bon, pour faire simple, voici une phrase de prose qui compte des mots utiles pour la voix éditoriale du site."] * 40)
    p = assess_page("https://ex.fr/mon-article", 200, True, "", "Un article", body, "fr")
    assert p.ok and p.words >= 300 and p.language == "fr"


def test_check_corpus_requires_enough_words():
    short = assess_page("https://ex.fr/a", 200, True, "", "A", "mot " * 50, "")
    with pytest.raises(InputError) as exc:
        check_corpus([short])
    assert "https://ex.fr/a" in str(exc.value)
