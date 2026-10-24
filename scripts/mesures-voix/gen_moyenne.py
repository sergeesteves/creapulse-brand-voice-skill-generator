"""Schéma de principe « La moyenne n'a la voix de personne » (section 1 de l'article).

Courbe en cloche sans échelle : au sommet, ce que le modèle écrit par défaut ; dans la queue,
la voix de l'auteur, seul élément en accent (charte §6 : un seul accent par visuel).
Attributs entre apostrophes pour pouvoir passer le SVG tel quel dans un bloc html.
"""
import math
import sys

BASE, H, MU, SIGMA = 330.0, 150.0, 300.0, 90.0
X0, X1 = 52.0, 668.0
VOIX_X = 560.0


def y(x):
    return BASE - H * math.exp(-((x - MU) ** 2) / (2 * SIGMA ** 2))


pts = [(X0 + i * (X1 - X0) / 120, 0) for i in range(121)]
d = "M" + " L".join(f"{x:.1f} {y(x):.1f}" for x, _ in pts)
vy = y(VOIX_X)
pic = y(MU)

svg = f"""<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 720 432' role='img' aria-labelledby='vm-t vm-d' font-family='Open Sans, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif'><title id='vm-t'>La moyenne n'a la voix de personne</title><desc id='vm-d'>Schéma de principe, sans échelle. Une courbe en cloche représente tous les textes qu'un modèle a lus. À son sommet, là où se trouvent la plupart des contenus, le texte que le modèle écrit par défaut : propre, interchangeable. Loin dans la queue de la courbe, la voix de l'auteur, ce qui s'écarte de la moyenne : une façon de trancher, un mot inattendu, un rythme.</desc><rect x='0.5' y='0.5' width='719' height='431' fill='#f7f7f7' stroke='#000000' stroke-opacity='0.12'/><text x='32' y='44' font-size='21' font-weight='700' fill='#111111'>La moyenne n'a la voix de personne</text><text x='32' y='68' font-size='14' fill='#76797d'>Ce qu'un modèle écrit sans indication, et ce qui se remarque</text><text x='{MU:.0f}' y='{pic - 14:.0f}' font-size='14' fill='#111111' text-anchor='middle'><tspan font-weight='700'>Par défaut</tspan> : un texte propre, interchangeable</text><path d='{d}' fill='none' stroke='#029ae5' stroke-width='2.5' stroke-linejoin='round'/><text x='{MU:.0f}' y='{BASE - 34:.0f}' font-size='13' fill='#111111' text-anchor='middle'>La plupart des contenus</text><line x1='{X0:.0f}' y1='{BASE:.0f}' x2='{X1:.0f}' y2='{BASE:.0f}' stroke='#000000' stroke-opacity='0.25'/><path d='M{VOIX_X:.0f} {vy - 10:.0f} L{VOIX_X:.0f} 264' stroke='#e61781' fill='none'/><circle cx='{VOIX_X:.0f}' cy='{vy:.1f}' r='7' fill='#e61781'/><text x='{VOIX_X - 76:.0f}' y='196' font-size='15' font-weight='700' fill='#111111'>Votre voix</text><text x='{VOIX_X - 76:.0f}' y='216' font-size='13' fill='#76797d'>ce qui s'écarte de la moyenne :</text><text x='{VOIX_X - 76:.0f}' y='234' font-size='13' fill='#76797d'>une façon de trancher, un mot</text><text x='{VOIX_X - 76:.0f}' y='252' font-size='13' fill='#76797d'>inattendu, un rythme</text><text x='360' y='358' font-size='13' fill='#76797d' text-anchor='middle'>Tous les textes que le modèle a lus, du plus courant au plus singulier</text><text x='32' y='414' font-size='12' fill='#76797d'>Schéma de principe, sans échelle.</text></svg>"""

open(sys.argv[1], "w", encoding="utf-8", newline="\n").write(svg)
print(f"pic y={pic:.0f}, voix y={vy:.1f}, {len(svg)} octets")
