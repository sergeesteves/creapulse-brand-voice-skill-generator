"""Mesures de la voix de Serge sur ses articles publies, pour l'article
« Votre voix de marque est deja dans vos articles publies ».

Chaque chiffre cite dans l'article doit sortir d'ici. Rien d'estime.
Corpus : docs-perso/articles-publies/ sur origin/main de creapulse-knowledge-base.

Usage : python mesures_article.py <chemin du clone de creapulse-knowledge-base>
"""
import re
import subprocess
import sys
from collections import Counter

if len(sys.argv) != 2:
    sys.exit("usage : python mesures_article.py <chemin du clone de creapulse-knowledge-base>")
REPO = sys.argv[1]


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True,
                          encoding="utf-8", errors="replace").stdout


def corpus():
    """Rend [(nom, corps nettoye)] : sans frontmatter, sans code, sans URL, sans images."""
    noms = [f for f in git("ls-tree", "--name-only", "origin/main",
                           "docs-perso/articles-publies/").split("\n") if f.strip().endswith(".md")]
    out = []
    for p in noms:
        b = git("show", "origin/main:" + p)
        b = re.sub(r"^---.*?\n---\n", "", b, flags=re.S)
        b = re.sub(r"```.*?```", " ", b, flags=re.S)
        b = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", b)   # garde le libelle du lien
        b = re.sub(r"`[^`]*`|https?://\S+", " ", b)
        out.append((p.split("/")[-1], b))
    return out


MOT = r"[\w'\u2019-]+"
docs = corpus()
mots_tot = sum(len(re.findall(MOT, b)) for _, b in docs)
print(f"CORPUS : {len(docs)} articles, {mots_tot} mots (hors code, URL, frontmatter)")
print()

# ---------------------------------------------------------------- 1. elision du « ne »
# NON FIABLE (2026-10-24) : « n'est » forme un seul mot pour MOT, donc le « n' » n'est jamais vu
# et presque toutes les négations passent pour élidées (75 %). Ne pas citer ce chiffre.
# Une negation en « pas / plus / jamais / rien / personne » est elidee quand aucun
# « ne / n' » ne la precede dans les 4 mots qui la precedent.
NEG = r"\b(pas|plus|jamais|rien|personne)\b"
elid = plein = 0
ex_elid = []
for nom, b in docs:
    for m in re.finditer(NEG, b, re.I):
        avant = b[max(0, m.start() - 60):m.start()]
        mots_avant = re.findall(MOT, avant)[-4:]
        # « plus » comparatif/additif n'est pas une negation : on l'ecarte s'il suit
        # « de / en / le / au » ou s'il precede « de »
        if m.group(1).lower() == "plus":
            apres = re.findall(MOT, b[m.end():m.end() + 20])[:1]
            if (mots_avant and mots_avant[-1].lower() in ("de", "en", "le", "au", "des", "y")) \
               or (apres and apres[0].lower() in ("de", "d", "que", "qu")):
                continue
        if any(w.lower() in ("ne", "n") for w in mots_avant):
            plein += 1
        else:
            elid += 1
            if len(ex_elid) < 6:
                ex_elid.append(re.sub(r"\s+", " ", b[max(0, m.start() - 45):m.end() + 12]).strip())
tot_neg = elid + plein
print("1. ELISION DU « ne »")
print(f"   negations reperees : {tot_neg}  |  avec « ne » : {plein}  |  elidees : {elid}")
if tot_neg:
    print(f"   taux d'elision : {100*elid/tot_neg:.1f} %  (soit 1 sur {tot_neg/elid:.1f})" if elid else "   aucune elision")
for e in ex_elid:
    print("     ·", e)
print()

# ---------------------------------------------------------------- 2. densite des incises
REPERTOIRE = ["Attention,", "D\u2019ailleurs", "D'ailleurs", "Ceci dit", "En fait", "En gros",
              "Bon,", "L\u00e0 aussi", "Autant dire", "A noter que", "\u00c0 noter que",
              "Pour faire simple", "Ce qu\u2019il faut retenir", "Ce qu'il faut retenir",
              "Vous me direz", "Imaginez"]
c = Counter()
for nom, b in docs:
    for inc in REPERTOIRE:
        n = len(re.findall(re.escape(inc), b, re.I))
        if n:
            c[inc.replace("\u2019", "'")] += n
tot_inc = sum(c.values())
print("2. INCISES DU REPERTOIRE")
print(f"   total : {tot_inc} occurrences, soit une tous les {mots_tot/tot_inc:.0f} mots")
for k, v in c.most_common():
    print(f"     {k:28} {v}")
print()

# ---------------------------------------------------------------- 3. marqueur « mecanique »
for mot in ("m\u00e9canique", "m\u00e9caniquement", "d\u00e9s\u00e9quilibre", "pertinence", "gaspill"):
    n = sum(len(re.findall(mot, b, re.I)) for _, b in docs)
    arts = sum(1 for _, b in docs if re.search(mot, b, re.I))
    print(f"3. « {mot} » : {n} occurrences, dans {arts} articles sur {len(docs)}")
print()

# ---------------------------------------------------------------- 4. longueur de phrase
longueurs = []
for nom, b in docs:
    corps = "\n".join(l for l in b.split("\n")
                      if l.strip() and l.strip()[0] not in "#->|*+" and len(re.findall(MOT, l)) >= 25)
    for ph in re.split(r"(?<=[.!?])\s+", corps):
        n = len(re.findall(MOT, ph))
        if 2 <= n <= 120:
            longueurs.append(n)
longueurs.sort()
if longueurs:
    med = longueurs[len(longueurs)//2]
    courtes = sum(1 for n in longueurs if n <= 8)
    longues = sum(1 for n in longueurs if n >= 35)
    print("4. LONGUEUR DE PHRASE (prose seule)")
    print(f"   {len(longueurs)} phrases | mediane {med} mots | "
          f"<= 8 mots : {courtes} ({100*courtes/len(longueurs):.1f} %) | "
          f">= 35 mots : {longues} ({100*longues/len(longueurs):.1f} %)")
