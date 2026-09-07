from app.scraper import assess_page, is_listing_url, truncate_words, word_count, _link_density


def test_listing_urls_are_rejected():
    for u in ["https://www.creapulse.fr/", "https://www.creapulse.fr", "https://ex.fr/blog/", "https://ex.fr/articles",
              "https://ex.fr/category/seo/", "https://ex.fr/tag/ia", "https://ex.fr/page/2/", "https://ex.fr/blog/page/3",
              "https://ex.fr/author/serge/", "https://ex.fr/actualites"]:
        assert is_listing_url(u), u
    for u in ["https://www.creapulse.fr/lanalyse-de-logs/", "https://ex.fr/blog/mon-article/", "https://ex.fr/mon-article",
              "https://ex.fr/2024/05/mon-article", "https://medium.com/@jane/post-1", "https://ex.fr/news/2026/un-titre"]:
        assert not is_listing_url(u), u
    p = assess_page("https://www.creapulse.fr/", 200, True, "", "Accueil", "mot " * 1000)
    assert not p.ok and "accueil" in p.reason


def test_excerpt_listing_detected_by_heading_density():
    md = "\n".join("### Titre %d\n%s" % (i, "mot " * 40) for i in range(12))
    p = assess_page("https://ex.fr/dossier/mon-article", 200, True, "", "Dossier", md)
    assert not p.ok and "liste d'articles" in p.reason
    # Un vrai article très structuré (≥ 60 mots par titre) passe
    md = "\n".join("## Titre %d\n%s" % (i, "mot " * 90) for i in range(12))
    p = assess_page("https://ex.fr/dossier/mon-article", 200, True, "", "Dossier", md)
    assert p.ok


def test_link_density_ignores_urls_and_keeps_link_text():
    assert _link_density("* [Le maillage interne](https://ex.fr/maillage-interne/)") == 1.0
    assert _link_density("Lisez les [définitions du crawl](https://ex.fr/g/) avant de continuer la lecture ici") < 0.4


def test_truncate_cuts_inside_block_instead_of_dropping():
    text = "court\n" + "mot " * 1000
    t = truncate_words(text, 100)
    assert word_count(t) == 100 and t.startswith("court\nmot") and t.endswith("[…]")
