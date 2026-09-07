# Générateur de skill de voix de marque IA

Outil gratuit et autonome (lead-gen) pour creapulse.fr : le visiteur colle 1 à 5 URLs d'articles
qu'il a écrits → l'outil distille sa **voix de marque** sur 5 axes (ton, style de phrase,
vocabulaire, structure, principes édito) → il repart avec un profil **self-contained**, en deux
formats au choix :

- **A. Bloc prompt universel** (défaut) — à coller dans les instructions personnalisées de ChatGPT,
  un Projet Claude, un Custom GPT ;
- **B. Skill** — un `SKILL.md` standalone (frontmatter + règles inline) à déposer dans `~/.claude/skills/`.

L'artefact est donné sans retenue ; ce qui est gaté, c'est l'**automatisation de production**
(CTA « parlons-en »). L'artefact ne pointe jamais vers un fichier externe ni ne révèle l'architecture
interne de Creapulse.

## Architecture (stateless, rayon de souffle minimal)

```
visiteur ──> FastAPI (1 worker, ~80 Mo)
               │  1. garde-fous d'entrée (URLs publiques, ≤ 5, dédoublonnage)
               │  2. gating (anonyme ×1 → email magic-link ; caps compte / IP / global)
               ├──> crawl4ai (existant) : POST /crawl → markdown "fit" → nettoyage + seuils de mots
               ├──> omniroute (existant) : POST /chat/completions, T° 0.2 → guide 5 axes (Markdown)
               │  3. enrobage déterministe → prompt universel + SKILL.md (+ extraits verbatim en option)
               └──> Postgres minuscule : usage_counters (day, scope, key, count) + leads (email…)
```

- Aucun contenu scrapé ni profil n'est stocké : tout vit en mémoire le temps de la requête. Le
  téléchargement du skill est lui aussi stateless (le navigateur renvoie le contenu généré).
- Le **cap global quotidien** borne la charge et le budget LLM, même en pic viral. Le slot est réservé
  *avant* le travail coûteux et rendu en cas d'échec (entrée refusée, panne scraper/LLM).
- Le LLM ne produit **que** le guide 5 axes ; les templates (B1/B2) et la sélection d'extraits sont
  déterministes, donc gratuits et cohérents.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app/main.py` | routes : `/`, `/api/generate`, `/api/magic-link`, `/auth/verify`, `/api/download/skill`, `/health` |
| `app/scraper.py` | appel crawl4ai, nettoyage du Markdown (TOC, partage, cookies, méta), garde-fous qualité |
| `app/distill.py` | prompt de distillation, appel omniroute, parsing strict des 6 sections (1 relance max) |
| `app/render.py` | nom/slug depuis le domaine, templates prompt universel + SKILL.md, extraits verbatim |
| `app/gating.py` | cookies signés, magic-link (itsdangerous), rate-limit, décision + réservation de slot |
| `app/store.py` | 2 tables (Postgres ou SQLite), incréments atomiques `ON CONFLICT … RETURNING` |
| `app/mailer.py` | envoi SMTP du magic-link (ou mode log si `SMTP_HOST` vide) |
| `app/templates/index.html` + `app/static/style.css` | front (vanilla JS, aucune dépendance) |
| `tests/` | pytest : nettoyage, seuils, parsing, templates, gating de bout en bout (scraper/LLM mockés) |

## Lancer en local

```bash
uv venv .venv && uv pip install -p .venv/Scripts/python.exe -r requirements.txt pytest
cp .env.example .env   # renseigner SECRET_KEY, CRAWL4AI_TOKEN, LLM_API_KEY ; laisser DATABASE_URL en sqlite
.venv/Scripts/python.exe -m pytest -q
set -a; . ./.env; set +a; .venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

En local, `COOKIE_SECURE=false` et `DEBUG=true` : le magic-link s'affiche dans la réponse (mode log).

## Déployer sur Coolify

1. **Repo Git** → app Coolify « Dockerfile » (port 8000), comme `keyword-clustering`. Healthcheck : `/health`
   (l'image embarque `curl`, le healthcheck Coolify fonctionne). Limite mémoire conseillée : 256 Mo.
2. **Base** : créer une base `voice_skill` dans le Postgres Coolify existant (pas de nouvelle instance →
   zéro RAM en plus) et renseigner `DATABASE_URL` (réseau interne `coolify`). Les tables sont créées au
   démarrage.
3. **Variables** : voir `.env.example`. Obligatoires : `SECRET_KEY`, `CRAWL4AI_TOKEN`, `LLM_API_KEY`,
   `PUBLIC_BASE_URL`, `DATABASE_URL`. Email : `SMTP_*` (Brevo/Resend/Postmark en SMTP, offre gratuite
   suffisante) — sans SMTP, le lien part dans les logs (inutilisable en prod).
4. **Domaine** : ex. `voix.creapulse.fr` (DNS A → serveur) ; la landing sur creapulse.fr pointe dessus.
5. Appels internes possibles via le réseau Docker `coolify` (`CRAWL4AI_URL=http://<container>:11235`,
   `LLM_BASE_URL=http://<container>:20128/api/v1`) pour éviter l'aller-retour public.

**Scale-to-zero** : Coolify n'a pas de scale-to-zero natif. L'app est dimensionnée pour un repos
négligeable (1 worker uvicorn, aucun modèle en RAM, pool Postgres `min_size=0`). Si un vrai idle=0 est
requis, le code est portable tel quel vers un runtime serverless (Cloud Run, Fly machines) — rien
dans l'app ne dépend de Coolify.

## Réglages qualité / gating (env)

| Variable | Défaut | Effet |
|---|---|---|
| `MIN_WORDS_PER_PAGE` / `MIN_WORDS_TOTAL` | 300 / 600 | refus propre si extraction vide ou courte |
| `MAX_WORDS_PER_PAGE` / `MAX_WORDS_TOTAL` | 2500 / 9000 | budget tokens (tronque équitablement) |
| `ANON_FREE_GENERATIONS` | 1 | générations sans email (cookie signé) |
| `ANON_IP_DAILY_CAP` | 2 | filet si le cookie est effacé |
| `ACCOUNT_DAILY_CAP` | 3 | par email vérifié |
| `GLOBAL_DAILY_CAP` | 40 | tous visiteurs — protège le budget en dur |
| `LLM_MODEL` / `LLM_TEMPERATURE` | `openai/gpt-5.4-mini` / 0.2 | choix du modèle dans omniroute ; T° basse = mêmes URLs → même profil |

Si le modèle refuse `temperature` (famille reasoning), l'app réessaie automatiquement sans.

## Suivi coût / usage

Chaque génération est loguée (`generation ok … model=… usage=…`) avec les tokens renvoyés par omniroute.
Le tracking centralisé se lit dans le dashboard omniroute (clé API dédiée à cet outil → filtre par clé).

## RGPD

- Consentement explicite (case à cocher) avant capture de l'email ; lien vers la politique de confidentialité.
- Table `leads` : email, dates (création, consentement, vérification, dernière visite), nombre de générations.
  Rien d'autre. Les IP ne sont stockées que hachées (HMAC + `SECRET_KEY`), dans des compteurs purgés après 7 jours.
- Le contenu scrapé n'est jamais persisté.
