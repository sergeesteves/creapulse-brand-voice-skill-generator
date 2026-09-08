"""Enrobage déterministe du guide LLM → 2 formats SELF-CONTAINED (prompt universel, SKILL.md)."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

from .config import settings

TOOL_NAME = "Générateur de skill de voix de marque IA"


# ---------------------------------------------------------------------------
# Nom / slug dérivés du domaine
# ---------------------------------------------------------------------------

def brand_from_urls(urls: list[str]) -> tuple[str, str]:
    """Retourne (nom lisible, slug) à partir du domaine majoritaire des URLs."""
    hosts = [urlsplit(u).hostname or "" for u in urls]
    hosts = [h.lower().removeprefix("www.") for h in hosts if h]
    host = max(set(hosts), key=hosts.count) if hosts else "ma-marque"
    label = host.split(".")[0] if host.count(".") >= 1 else host
    # medium.com/@x, substack… : le premier segment de chemin est plus parlant
    generic = {"medium", "substack", "linkedin", "blogspot", "wordpress", "notion", "github"}
    if label in generic:
        for u in urls:
            path = [s for s in urlsplit(u).path.split("/") if s]
            if path:
                label = path[0].lstrip("@")
                break
    slug = slugify(label) or "ma-marque"
    name = label.replace("-", " ").replace("_", " ").strip().title() if label else "Ma marque"
    return name, slug


def slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return value[:40]


# ---------------------------------------------------------------------------
# Templates B1 / B2
# ---------------------------------------------------------------------------

USAGE_BLOCK = """> **Comment l'utiliser**
> - **ChatGPT** : collez ce bloc dans les instructions d'un *Projet* ou d'un *GPT* (ou dans *Instructions personnalisées* pour toutes vos conversations).
> - **Claude** : créez un *Projet* et collez-le dans ses instructions (ou importez la version « skill » dans Paramètres → Skills).
> - Il définit COMMENT écrire, pas QUOI écrire : donnez ensuite votre sujet normalement."""


def render_prompt_block(name: str, guide_md: str, extra_section: str = "") -> str:
    body = guide_md.strip()
    if extra_section:
        body += "\n\n" + extra_section.strip()
    return f"""# Voix de marque — {name}
{USAGE_BLOCK}

Adoptez systématiquement la voix suivante pour rédiger/réviser du contenu {name}.
N'inventez pas de traits absents ; en cas de doute, restez neutre.

{body}

—
Ceci est la couche explicite de votre voix. Le système complet ajoute des exemples verbatim, l'adaptation par canal et l'automatisation de production.
Généré par le {TOOL_NAME} — creapulse.fr
Automatiser la production à cette voix (blog, réseaux, Reddit) ? → {settings.cta_url}
"""


def _yaml_escape(value: str) -> str:
    value = value.replace("\n", " ").replace('"', "'").strip()
    return value


def render_skill(name: str, slug: str, summary: str, guide_md: str, extra_section: str = "") -> str:
    body = guide_md.strip()
    if extra_section:
        body += "\n\n" + extra_section.strip()
    description = _yaml_escape(f"Applique la voix de marque {name}. {summary} Pour rédiger ou réviser tout contenu {name}.")
    return f"""---
name: voix-{slug}
description: "{description}"
---

# Voix de marque — {name}

Applique cette voix pour toute rédaction/révision {name}. COMMENT écrire, pas QUOI.
N'invente pas de traits absents ; en cas de doute, reste neutre.

{body}

---
Ceci est la couche explicite de la voix. Le système complet ajoute des exemples verbatim, l'adaptation par canal et l'automatisation de production.
Généré par creapulse.fr — automatiser la production à cette voix : {settings.cta_url}
"""


def skill_filename(slug: str) -> str:
    return f"voix-{slug}-SKILL.md"
