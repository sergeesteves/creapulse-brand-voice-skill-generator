"""Distillation LLM (via omniroute, OpenAI-compatible) → guide de voix sur 5 axes."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import httpx

from .config import settings
from .scraper import PageText

log = logging.getLogger("voice-skill.distill")

SECTIONS = [
    "Ton",
    "Style de phrase",
    "Vocabulaire",
    "Structure / format",
    "Principes édito / positionnement",
    "Résumé en une phrase",
]

SYSTEM_PROMPT = """Tu es un analyste de style éditorial. À partir des extraits d'articles fournis
(tous écrits par la même personne/marque), distille sa VOIX éditoriale en un
guide autonome et actionnable.

RÈGLES ABSOLUES
- Base-toi UNIQUEMENT sur les textes fournis. N'invente rien.
- INTERDIT les descripteurs génériques passe-partout (« professionnel », « clair »,
  « accessible », « engageant »). Chaque trait doit être SPÉCIFIQUE et VÉRIFIABLE
  dans les textes.
- Ancre chaque trait : cite 1-3 tournures/formules RÉELLEMENT présentes (courtes,
  entre guillemets).
- Le guide doit être utilisable par un LLM tiers qui n'a JAMAIS lu ces articles :
  il doit pouvoir écrire un nouveau texte dans cette voix sur n'importe quel sujet.
  Décris la MÉCANIQUE de la voix (comment elle écrit), jamais les SUJETS traités.
- Examine explicitement : la personne grammaticale et l'adresse au lecteur (je / on / vous,
  tutoiement ou vouvoiement), le registre (oral ou écrit, élisions, familiarités), l'humour ou
  l'auto-dérision, le rythme (longueur des phrases, punchlines, anaphores), la ponctuation
  (parenthèses, points de suspension, gras), les connecteurs et formules de liaison, les
  analogies, les questions rhétoriques.
- Dans « Vocabulaire » : les mots-signature sont des marqueurs de STYLE (formules, connecteurs,
  tics, mots favoris transversaux à tous les sujets), PAS le lexique du domaine traité. Ne cite le
  jargon du domaine que pour dire COMMENT il est manié (toujours vulgarisé, jamais défini, imagé…).
  Les mots à éviter se déduisent de ce que la voix ne fait JAMAIS dans les textes (ex. aucun
  superlatif, aucune formule commerciale).
- « Résumé en une phrase » : une seule phrase, sans puce, sans gras, sans citation.
- Réponds dans la LANGUE des articles.
- Pas d'introduction ni de conclusion hors des sections : commence directement par « ## Ton ».
- LONGUEUR : le guide complet doit tenir en MOINS DE {max_chars} CARACTÈRES. Vise 3 à 5 traits par
  section et 1 à 2 citations par trait. La spécificité prime sur l'exhaustivité : quatre traits
  ancrés valent mieux qu'une liste complète et vague. Un guide trop long noie le modèle qui l'applique.

SORTIE (Markdown, exactement ces sections, dans cet ordre, titres en H2 tels quels) :
## Ton
## Style de phrase
## Vocabulaire   (inclut les mots-signature ET les mots/tics à éviter)
## Structure / format
## Principes édito / positionnement
## Résumé en une phrase   (la voix en 1 phrase — servira de description courte)

Dans chaque section : des puces concrètes, en gras le trait, puis l'ancrage entre guillemets."""

# Passe de raccourcissement : n'envoie QUE le guide, jamais les articles (le modèle n'a rien à
# re-vérifier, il ne fait que couper) — l'appel coûte ~1,5 k tokens au lieu de ~11 k.
SHORTEN_PROMPT = """Ce guide de voix éditoriale fait {actual} caractères. La limite est {max_chars}.
Raccourcis-le sans l'affadir : un guide trop long noie le modèle qui l'applique.

COMMENT RACCOURCIR, DANS CET ORDRE
1. Coupe d'abord dans « Structure / format », puis dans « Principes édito / positionnement » :
   ce sont les sections les moins porteuses de voix.
2. Supprime les traits redondants d'une section à l'autre, et les traits vagues.
3. Réduis le nombre de citations par trait, en en gardant TOUJOURS au moins une.
4. Resserre la formulation de chaque puce.

INTERDIT
- Supprimer une des 6 sections, ou la vider.
- Retirer toutes les citations d'un trait : ce sont elles qui transmettent la voix.
- Résumer le guide ou le rendre générique. On coupe le moins utile, on ne dilue pas le reste.
- Toucher au « Résumé en une phrase ».

Renvoie le guide COMPLET, mêmes 6 sections H2 dans le même ordre, et rien d'autre.

GUIDE À RACCOURCIR :
{guide}"""

# Ordre de coupe du filet déterministe : du moins au plus porteur de voix.
# « Résumé en une phrase » n'y figure pas : il n'est jamais touché.
TRIM_ORDER = [
    "Structure / format",
    "Principes édito / positionnement",
    "Vocabulaire",
    "Ton",
    "Style de phrase",
]
MIN_BULLETS = 2  # plancher : en dessous, une section ne dit plus rien d'utile


def build_system_prompt() -> str:
    return SYSTEM_PROMPT.format(max_chars=settings.max_guide_chars)


def build_user_prompt(pages: list[PageText], brand_name: str) -> str:
    parts = [f"Marque / auteur : {brand_name}", f"Nombre d'articles : {len(pages)}", "", "Textes fournis :"]
    for i, p in enumerate(pages, 1):
        parts.append(f"\n=== ARTICLE {i} — {p.title or p.url} ===\n{p.text}\n=== FIN ARTICLE {i} ===")
    return "\n".join(parts)


@dataclass
class Guide:
    markdown: str            # les 6 sections telles que renvoyées (nettoyées)
    sections: dict[str, str]  # titre → contenu
    summary: str              # la ligne « Résumé en une phrase »
    model: str
    usage: dict


class DistillError(RuntimeError):
    """Sortie LLM non conforme ou LLM indisponible."""


_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.M)


def _norm(title: str) -> str:
    t = title.strip().lower()
    t = re.sub(r"\s*\(.*?\)\s*", " ", t)  # retire les parenthèses de consigne
    t = re.sub(r"[^\w/]+", " ", t, flags=re.U)
    return re.sub(r"\s+", " ", t).strip()


_EXPECTED = {_norm(s): s for s in SECTIONS}


def parse_guide(raw: str) -> tuple[str, dict[str, str], str]:
    """Découpe le Markdown en sections attendues. Lève DistillError si incomplet."""
    raw = (raw or "").strip()
    # Retire un éventuel bloc ```markdown ... ```
    raw = re.sub(r"^```[a-z]*\s*\n", "", raw, flags=re.I).strip()
    raw = re.sub(r"\n```\s*$", "", raw).strip()
    # Certains modèles renvoient des H1/H3 : on normalise en H2 pour les titres connus
    def _fix(m: re.Match) -> str:
        title = m.group(2).strip()
        return f"## {title}" if _norm(title) in _EXPECTED else m.group(0)
    raw = re.sub(r"^(#{1,4})\s+(.+?)\s*$", _fix, raw, flags=re.M)

    matches = list(_H2_RE.finditer(raw))
    sections: dict[str, str] = {}
    for idx, m in enumerate(matches):
        key = _norm(m.group(1))
        if key not in _EXPECTED:
            continue
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        sections[_EXPECTED[key]] = raw[start:end].strip()
    missing = [s for s in SECTIONS if s not in sections or not sections[s]]
    if missing:
        raise DistillError("sections manquantes : " + ", ".join(missing))
    summary = sections["Résumé en une phrase"].split("\n")[0]
    summary = re.sub(r"^[\-\*\s>]+", "", summary).replace("**", "")
    summary = re.split(r"\s+[—–-]\s+[«\"“]", summary)[0].strip()  # retire un éventuel « ancrage » cité
    sections["Résumé en une phrase"] = summary  # la section = la phrase nue (le modèle ajoute parfois puce/gras)
    return canonical_markdown(sections), sections, summary


def canonical_markdown(sections: dict[str, str]) -> str:
    """Markdown canonique : ordre garanti, titres propres."""
    return "\n\n".join(f"## {s}\n{sections[s]}" for s in SECTIONS)


def split_bullets(body: str) -> list[str]:
    """Découpe une section en puces. Une puce peut tenir sur plusieurs lignes (continuations)."""
    out: list[str] = []
    for line in body.split("\n"):
        if re.match(r"^\s*[-*+]\s", line) or not out:
            out.append(line)
        else:
            out[-1] += "\n" + line
    return [b for b in out if b.strip()]


def trim_to_budget(sections: dict[str, str], max_chars: int) -> tuple[dict[str, str], int]:
    """Filet déterministe, appliqué quand le LLM n'a pas respecté le plafond.

    Retire les DERNIÈRES puces des sections les moins porteuses de voix, jamais en dessous de
    MIN_BULLETS, jamais dans le résumé. On coupe le moins utile plutôt que de résumer l'ensemble.
    Renvoie (sections, nombre de puces retirées).
    """
    sections = dict(sections)
    removed = 0
    while len(canonical_markdown(sections)) > max_chars:
        for name in TRIM_ORDER:
            bullets = split_bullets(sections.get(name, ""))
            if len(bullets) > MIN_BULLETS:
                sections[name] = "\n".join(bullets[:-1]).strip()
                removed += 1
                break
        else:
            break  # plus rien à retirer sans vider une section : on s'arrête et on loggue
    return sections, removed


async def _chat(messages: list[dict], client: httpx.AsyncClient, with_temperature: bool = True) -> tuple[str, dict, str]:
    body: dict = {
        "model": settings.llm_model,
        "messages": messages,
        "max_tokens": settings.llm_max_output_tokens,
    }
    if with_temperature:
        body["temperature"] = settings.llm_temperature
    try:
        resp = await client.post(
            f"{settings.llm_base_url}/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"},
            timeout=settings.llm_timeout_s,
        )
    except httpx.HTTPError as exc:
        raise DistillError(f"LLM injoignable : {exc.__class__.__name__}") from exc
    if resp.status_code == 400 and with_temperature and "temperature" in resp.text.lower():
        # Certains modèles (famille reasoning) refusent temperature ≠ 1 → on réessaie sans.
        return await _chat(messages, client, with_temperature=False)
    if resp.status_code != 200:
        raise DistillError(f"LLM HTTP {resp.status_code} : {resp.text[:200]}")
    data = resp.json()
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise DistillError("réponse LLM sans contenu") from exc
    return content, data.get("usage") or {}, data.get("model") or settings.llm_model


def _merge_usage(a: dict, b: dict) -> dict:
    return {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0)
            for k in set(a) | set(b) if isinstance(a.get(k, 0), int) and isinstance(b.get(k, 0), int)}


async def enforce_budget(canonical: str, sections: dict[str, str], summary: str,
                         client: httpx.AsyncClient) -> tuple[str, dict[str, str], str, dict]:
    """Garantit que le guide tient sous le plafond. Une passe LLM de raccourcissement ciblé, puis un
    filet déterministe. Le plafond est tenu par le code, jamais par la seule consigne au modèle."""
    max_chars = settings.max_guide_chars
    if len(canonical) <= max_chars:
        return canonical, sections, summary, {}

    usage: dict = {}
    before = len(canonical)
    try:
        content, usage, _ = await _chat(
            [{"role": "system", "content": "Tu raccourcis un guide de voix éditoriale sans l'affadir."},
             {"role": "user", "content": SHORTEN_PROMPT.format(actual=before, max_chars=max_chars,
                                                               guide=canonical)}],
            client,
        )
        short_canonical, short_sections, short_summary = parse_guide(content)
        # On ne garde la version courte que si elle est effectivement plus courte et non vidée.
        if len(short_canonical) < len(canonical):
            canonical, sections, summary = short_canonical, short_sections, short_summary
    except DistillError as exc:
        log.warning("raccourcissement du guide impossible (%s) — on passe au filet déterministe", exc)

    sections, removed = trim_to_budget(sections, max_chars)
    canonical = canonical_markdown(sections)
    log.info("guide %d → %d car. (plafond %d) ; puces retirées par le code : %d",
             before, len(canonical), max_chars, removed)
    if len(canonical) > max_chars:
        log.warning("guide encore à %d car. après raccourcissement : plancher de %d puces atteint",
                    len(canonical), MIN_BULLETS)
    return canonical, sections, summary, usage


async def distill(pages: list[PageText], brand_name: str, client: httpx.AsyncClient | None = None) -> Guide:
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.llm_timeout_s)
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": build_user_prompt(pages, brand_name)},
    ]
    try:
        content, usage, model = await _chat(messages, client)
        try:
            canonical, sections, summary = parse_guide(content)
        except DistillError as first:
            # Une seule relance, avec rappel strict du format (température basse → stable)
            retry = messages + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Ta réponse ne respecte pas le format. Renvoie le guide COMPLET avec "
                                            "exactement les 6 sections H2 demandées, dans l'ordre, et rien d'autre."},
            ]
            content, usage2, model = await _chat(retry, client)
            try:
                canonical, sections, summary = parse_guide(content)
            except DistillError as second:
                raise DistillError(f"profil non conforme après relance ({second})") from first
            usage = _merge_usage(usage, usage2)
        canonical, sections, summary, extra = await enforce_budget(canonical, sections, summary, client)
        usage = _merge_usage(usage, extra)
    finally:
        if own:
            await client.aclose()
    return Guide(markdown=canonical, sections=sections, summary=summary, model=model, usage=usage)
