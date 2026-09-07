# CLAUDE.md — creapulse-voice-skill-generator

Point d'entrée pour reprendre le projet dans ce dossier.

## Ce qu'est ce projet

**« Générateur de skill de voix de marque IA »** — outil gratuit lead-gen sur creapulse.fr.
1-5 URLs d'articles → crawl4ai (scrape) → omniroute (distillation LLM, T° basse) → profil de voix
sur 5 axes, rendu en 2 formats **self-contained** (bloc prompt universel / `SKILL.md` standalone).
Gating : 1 génération anonyme, puis email magic-link ; caps compte / IP / global. CTA = automatiser
la production de contenu à cette voix (services Creapulse). Le « comment » de l'automatisation n'est
jamais révélé au visiteur.

Détails d'archi, fichiers, variables et déploiement : [README.md](./README.md).

## Règles du projet

- **Qualité perçue > tout** : refuser proprement une entrée faible plutôt que sortir un profil médiocre.
  Les seuils sont dans `app/config.py` (env), les heuristiques de nettoyage dans `app/scraper.py`.
- **L'artefact du visiteur est autonome et générique** : voix inline, jamais un pointeur vers un repo,
  jamais de mention de l'archi interne (skill façade, n8n, pipeline).
- **Le LLM ne produit que le guide 5 axes** (prompt dans `app/distill.py`). Tout l'enrobage est
  déterministe (`app/render.py`). Ne pas déplacer de logique de template vers le prompt.
- **Verbatim = option**, jamais par défaut (risque de calquer le sujet plutôt que la mécanique).
- **Rien du contenu scrapé n'est stocké.** Le store ne contient que compteurs + leads.
- Secrets uniquement en variables d'env Coolify. Ne jamais committer `.env`.
- Repo sur Google Drive : avant toute opération git, `find .git -name desktop.ini -delete`.

## Barre de qualité

La référence de spécificité attendue est `brand/style-guide.md` du repo privé
`sergeesteves/creapulse-knowledge-base` (traits ancrés, mots-signature, mots bannis, zéro générique).
Pour juger une sortie : chaque trait doit être vérifiable dans les textes et cité entre guillemets.

## Tests

`.venv/Scripts/python.exe -m pytest -q` — scraper et LLM sont mockés dans `tests/test_gating_api.py`.
Pour un test réel de bout en bout : `.env` renseigné + `uvicorn app.main:app` + une URL d'article creapulse.fr.

## État / à faire

Voir la section « Reste à faire » du dernier compte rendu de session (et le README pour le déploiement).
