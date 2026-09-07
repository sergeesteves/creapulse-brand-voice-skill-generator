"""Distillation LLM (via omniroute, OpenAI-compatible) → guide de voix sur 5 axes."""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from .config import settings
from .scraper import PageText

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

SORTIE (Markdown, exactement ces sections, dans cet ordre, titres en H2 tels quels) :
## Ton
## Style de phrase
## Vocabulaire   (inclut les mots-signature ET les mots/tics à éviter)
## Structure / format
## Principes édito / positionnement
## Résumé en une phrase   (la voix en 1 phrase — servira de description courte)

Dans chaque section : des puces concrètes, en gras le trait, puis l'ancrage entre guillemets."""


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
    # Reconstruit un Markdown canonique (ordre garanti, titres propres)
    canonical = "\n\n".join(f"## {s}\n{sections[s]}" for s in SECTIONS)
    return canonical, sections, summary


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


async def distill(pages: list[PageText], brand_name: str, client: httpx.AsyncClient | None = None) -> Guide:
    own = client is None
    client = client or httpx.AsyncClient(timeout=settings.llm_timeout_s)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
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
            usage = {k: (usage.get(k, 0) or 0) + (usage2.get(k, 0) or 0) for k in set(usage) | set(usage2)}
    finally:
        if own:
            await client.aclose()
    return Guide(markdown=canonical, sections=sections, summary=summary, model=model, usage=usage)
