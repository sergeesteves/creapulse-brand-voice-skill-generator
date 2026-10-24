"""Taux d'elision du « ne » dans les articles publies de Serge.

Le guide de voix affirme « elision du ne dans environ une negation sur huit ».
On verifie. Methode : pour chaque adverbe de negation, on regarde si un « ne »
ou « n' » le precede dans la meme proposition (fenetre de 45 caracteres, bornee
par une ponctuation forte).

Résultat du 2026-10-24 : NON FIABLE. Le motif compte comme élidées des négations où le « ne »
suit le mot (« aucune plateforme n'est ») et des coordinations sans verbe (« et pas », « avec pas
ou peu »). Gardé pour mémoire ; la seule mesure retenue dans l'article est la paire
« c'est pas » / « ce n'est pas ».

Usage : python mesure_elision.py <chemin du clone de creapulse-knowledge-base>
"""
import re
import subprocess
import sys
from collections import Counter

if len(sys.argv) != 2:
    sys.exit("usage : python mesure_elision.py <chemin du clone de creapulse-knowledge-base>")
REPO = sys.argv[1]


def git(*a):
    return subprocess.run(["git", "-C", REPO, *a], capture_output=True,
                          encoding="utf-8", errors="replace").stdout


noms = [f for f in git("ls-tree", "--name-only", "origin/main",
                       "docs-perso/articles-publies/").split("\n") if f.strip().endswith(".md")]
docs = []
for p in noms:
    b = git("show", "origin/main:" + p)
    b = re.sub(r"^---.*?\n---\n", "", b, flags=re.S)
    b = re.sub(r"```.*?```", " ", b, flags=re.S)
    b = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", b)
    b = re.sub(r"`[^`]*`|https?://\S+", " ", b)
    b = b.replace("\u2019", "'")
    docs.append((p.split("/")[-1], b))

# « ne » ou « n' » quelque part avant, dans la meme proposition
A_NE = re.compile(r"(?:\bne\b|\bn')")
NEG = re.compile(r"\b(pas|jamais|rien|personne|guere|aucun|aucune)\b", re.I)

elid, plein = 0, 0
ex_e, ex_p = [], []
par_mot = Counter()
for nom, b in docs:
    for m in NEG.finditer(b):
        mot = m.group(1).lower()
        # fenetre = meme proposition : on remonte jusqu'a une ponctuation forte, max 45 car.
        deb = max(0, m.start() - 45)
        fen = b[deb:m.start()]
        coupe = max(fen.rfind("."), fen.rfind("!"), fen.rfind("?"), fen.rfind(";"),
                    fen.rfind(":"), fen.rfind("\n"), fen.rfind("("))
        if coupe != -1:
            fen = fen[coupe + 1:]
        suite = b[m.end():m.end() + 14].lower()
        # « personne » nom commun, pas negation
        if mot == "personne" and re.search(r"(une|la|les|des|cette|chaque|quelle|per)\s*$", fen, re.I):
            continue
        # « aucun doute que » reste une negation ; on garde tout le reste tel quel
        if A_NE.search(fen):
            plein += 1
            if len(ex_p) < 4:
                ex_p.append(re.sub(r"\s+", " ", (fen + "[" + m.group(1) + "]")).strip())
        else:
            elid += 1
            par_mot[mot] += 1
            if len(ex_e) < 8:
                ex_e.append(re.sub(r"\s+", " ", (fen + "[" + m.group(1) + "]" + suite)).strip())

tot = elid + plein
print(f"negations : {tot}   avec « ne » : {plein} ({100*plein/tot:.1f} %)   "
      f"elidees : {elid} ({100*elid/tot:.1f} %)")
print(f"=> une negation sur {tot/elid:.1f} est elidee" if elid else "=> aucune elision")
print()
print("elidees, par adverbe :", dict(par_mot))
print()
print("exemples d'elision :")
for e in ex_e:
    print("   ·", e)
print()
print("exemples avec « ne » :")
for e in ex_p:
    print("   ·", e)
