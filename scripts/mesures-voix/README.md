# Mesures de voix et schémas de l'article de lancement

Scripts et schémas qui ont servi à l'article « Votre voix de marque est déjà dans vos articles
publiés » (octobre 2026). Ils ne font pas partie de l'application : ils mesurent un corpus d'articles
et dessinent les schémas, pour pouvoir refaire les mesures ou les schémas plus tard.

## Scripts

| Fichier | Ce qu'il mesure ou produit | Fiabilité |
|---|---|---|
| `mesures_article.py` | Volume du corpus, incises du répertoire et leur fréquence, marqueurs rares, longueur des phrases | Fiable, **sauf la section 1** (élision du « ne ») |
| `mesure_elision.py` | Deuxième tentative de mesurer l'élision du « ne » | Non fiable, gardé pour mémoire |
| `gen_moyenne.py` | Génère le schéma « La moyenne n'a la voix de personne » | |

Les deux scripts de mesure lisent les articles publiés dans un clone de la base de connaissances :

```
python mesures_article.py <chemin du clone de creapulse-knowledge-base>
python gen_moyenne.py schemas/voix-moyenne.svg
```

Seule mesure fiable de l'oralité retenue dans l'article : la paire « c'est pas » contre
« ce n'est pas » (10 contre 26 au 2026-10-24). Un comptage global des négations élidées par
expression régulière confond des formes qui n'ont rien à élider.

## Schémas

`schemas/` contient les trois schémas de l'article, tels qu'ils sont dans la page : aux couleurs de
la charte visuelle Creapulse, `viewBox` de 720 px de large, libellés mesurés en Open Sans pour qu'aucun
ne déborde.
