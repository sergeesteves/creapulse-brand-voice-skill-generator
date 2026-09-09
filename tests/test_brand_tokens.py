"""Fige les valeurs de la charte visuelle Creapulse recopiées dans style.css.

Source de vérité : brand/visual-identity.md (repo sergeesteves/creapulse-knowledge-base). Une divergence doit
être un échec visible, jamais une dérive silencieuse (règle « zéro doublon » de la charte).
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "app" / "static" / "style.css").read_text(encoding="utf-8")

CHARTE = {
    "--cp-accent": "#e61781",        # fuchsia vif : aplats sans texte, filets
    "--cp-accent-fill": "#e2177f",   # fuchsia avec texte blanc (4,50:1)
    "--cp-accent-hover": "#be136b",
    "--cp-primary": "#029ae5",       # bleu vif : aplats sans texte, bordures
    "--cp-primary-fill": "#027dba",  # bleu avec texte blanc, et texte bleu sur blanc (4,52:1)
    "--cp-primary-hover": "#01699c",
    "--cp-ink": "#111111",
    "--cp-ink-muted": "#595959",
    "--cp-rule": "rgba(0, 0, 0, 0.12)",
    "--cp-surface-subtle": "rgba(0, 0, 0, 0.03)",
}


def _token(name: str) -> str:
    m = re.search(rf"{re.escape(name)}:\s*([^;]+);", CSS)
    assert m, f"jeton {name} absent de style.css"
    return m.group(1).strip()


def test_tokens_match_visual_identity():
    for name, value in CHARTE.items():
        assert _token(name) == value, f"{name} = {_token(name)} ≠ charte {value}"


def test_roles_use_fill_versions_for_text():
    assert _token("--accent") == "var(--cp-accent-fill)"
    assert _token("--secondary") == "var(--cp-primary-fill)"
    assert _token("--link") == "var(--cp-primary-fill)"
    # #029ae5 n'est jamais une couleur de texte : seulement bordures / filets / spinner
    for line in CSS.splitlines():
        if "var(--cp-primary)" in line:
            assert "color:" not in line.replace("border-top-color", "").replace("border-color", ""), line


def test_single_cta_per_page():
    html = (Path(__file__).resolve().parents[1] / "app" / "templates" / "index.html").read_text(encoding="utf-8")
    assert html.count('class="primary"') + html.count('class="primary button"') == 1  # « Distiller ma voix »
