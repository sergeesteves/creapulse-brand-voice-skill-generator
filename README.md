# Générateur de skill de voix de marque IA

Outil membre de creapulse.fr, payé en crédits du porte-monnaie commun de creapulse-tools : le membre
colle 3 à 5 URLs d'articles qu'il a écrits → l'outil distille sa **voix de marque** sur 5 axes (ton, style de phrase,
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
               │  2. session membre WordPress obligatoire ; caps compte / global
               ├──> crawl4ai (existant) : POST /crawl → markdown "fit" → nettoyage + seuils de mots
               ├──> porte-monnaie WordPress : debit (rejeté si solde insuffisant), refund si échec, cost
               ├──> omniroute (existant) : POST /chat/completions, T° 0.2 → guide 5 axes (Markdown)
               │  3. enrobage déterministe → prompt universel + SKILL.md (téléchargeable en .md ou .zip importable dans Claude)
               └──> store minuscule : usage_counters, leads, generation_costs, profiles, wallet_outbox
```

- Aucun contenu scrapé n'est stocké : le texte des pages vit en mémoire le temps de la requête. Le
  téléchargement du skill est stateless (le navigateur renvoie le contenu généré).
- **Historique « Mes profils »**, en **opt-in** : un profil n'est gardé que si un compte identifié coche
  « Garder ce profil » (`"save": true` dans `/api/generate`) ; sinon rien n'est écrit. Table `profiles` avec le guide, le
  résumé, le nom, et l'URL et le titre des pages lues. Bloc prompt et SKILL.md se recalculent à la
  lecture. Conservation selon le niveau PMPro, mêmes noms et même logique que l'outil de clustering
  (`FREE_RETENTION_MONTHS`, `LEVEL_RETENTION_MONTHS`) : au-delà, guide et résumé sont effacés et la ligne
  reste, affichée « expiré » avec un bouton qui reprend les URLs. Plafond `HISTORY_MAX_PER_ACCOUNT` par
  compte, le plus ancien part. Purge au démarrage et après chaque enregistrement, sans tâche planifiée.
  Routes : `GET /api/history`, `GET /api/history/{id}`, `DELETE /api/history/{id}`.
- Le **cap global quotidien** borne la charge et le budget LLM, même en pic viral. Le slot est réservé
  *avant* le travail coûteux et rendu en cas d'échec (entrée refusée, panne scraper/LLM).
- Le LLM ne produit **que** le guide 5 axes ; les templates (B1/B2) et la sélection d'extraits sont
  déterministes, donc gratuits et cohérents.

## Fichiers

| Fichier | Rôle |
|---|---|
| `app/main.py` | routes : `/`, `/api/generate`, `/api/credits`, `/api/history…`, `/api/download/skill`, `/health` |
| `app/wallet.py` | client du porte-monnaie commun (balance, debit, refund, cost) + file de renvoi ; autonome, réutilisable |
| `app/scraper.py` | appel crawl4ai, nettoyage du Markdown (TOC, partage, cookies, méta), garde-fous qualité |
| `app/distill.py` | prompt de distillation, appel omniroute, parsing strict des 6 sections (1 relance max) |
| `app/render.py` | nom/slug depuis le domaine, templates prompt universel + SKILL.md |
| `app/gating.py` | session membre signée (itsdangerous), rate-limit, plafonds + réservation de slot |
| `app/store.py` | Postgres ou SQLite, incréments atomiques `ON CONFLICT … RETURNING` |
| `app/templates/index.html` + `app/static/style.css` | front (vanilla JS, aucune dépendance) |
| `tests/` | pytest : nettoyage, seuils, parsing, templates, gating de bout en bout (scraper/LLM mockés) |

## Lancer en local

```bash
uv venv .venv && uv pip install -p .venv/Scripts/python.exe -r requirements.txt pytest
cp .env.example .env   # renseigner SECRET_KEY, CRAWL4AI_TOKEN, LLM_API_KEY ; laisser DATABASE_URL en sqlite
.venv/Scripts/python.exe -m pytest -q
set -a; . ./.env; set +a; .venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

En local, `COOKIE_SECURE=false` et `DEBUG=true`. Sans session membre, l'outil affiche « Réservé aux membres ».

## Déployer sur Coolify

État au 2026-09-07 : app **`brand-voice-skill-generator`** dans le projet « Mes outils », build pack Dockerfile,
port 8000, healthcheck `/health` (l'image embarque `curl`). Repo privé lu via une deploy key dédiée
(clé Coolify `voice-skill-deploy`, clé publique à déclarer en *Deploy key* lecture seule sur GitHub).

1. **Chemin, pas de sous-domaine** : l'app est servie sous `https://www.creapulse.fr/outils/voix-de-marque`.
   Coolify/Traefik route ce préfixe vers le conteneur (règle Host + PathPrefix, prioritaire sur WordPress) et
   retire le préfixe (`stripprefix`). Côté app, `ROOT_PATH=/outils/voix-de-marque` sert à générer les liens,
   cookies (`path`) et redirections. Aucun réglage WordPress nécessaire.
2. **Store** : SQLite sur un volume persistant (`/data`, `DATABASE_URL=sqlite:////data/voice-skill.db`) —
   zéro RAM, zéro base à créer ; 1 worker donc aucun souci de concurrence. Postgres reste possible en changeant
   `DATABASE_URL` (tables créées au démarrage).
3. **Variables** : voir `.env.example`. Secrets à saisir à la main dans Coolify : `CRAWL4AI_TOKEN`
   (= `CRAWL4AI_API_TOKEN` de l'app crawl4ai) et `LLM_API_KEY` (clé créée dans le dashboard omniroute,
   dédiée à cet outil pour suivre son coût). `SECRET_KEY` est générée au déploiement initial.
4. **Porte-monnaie** : `CREAPULSE_CREDITS_SECRET_VOIX` (posée par Serge, jamais lue ni affichée ailleurs) et
   `WP_BASE_URL` (staging pour la recette, www en prod). Voir « Porte-monnaie de crédits » ci-dessous.
5. Appels internes possibles via le réseau Docker `coolify` (`CRAWL4AI_URL=http://<container>:11235`,
   `LLM_BASE_URL=http://<container>:20128/api/v1`) pour éviter l'aller-retour public.

**Scale-to-zero** : Coolify n'a pas de scale-to-zero natif. L'app est dimensionnée pour un repos
négligeable (1 worker uvicorn, aucun modèle en RAM, pool Postgres `min_size=0`). Si un vrai idle=0 est
requis, le code est portable tel quel vers un runtime serverless (Cloud Run, Fly machines) — rien
dans l'app ne dépend de Coolify.

## Identité WordPress (membres Paid Memberships Pro)

La landing embarque l'outil via le shortcode du plugin creapulse-tools. Le plugin ajoute au `src` soit un
**jeton signé** (`wp_token`, HMAC-SHA256, 10 min) pour un visiteur connecté, soit `login_url` / `register_url`
pour un visiteur sans session. Le compte est **obligatoire** depuis le porte-monnaie de crédits (079) : plus
de génération anonyme ni de capture d'email. Côté app (`app/wp_token.py`, `app/main.py`) :

- jeton valide → session **membre** (cookie signé avec l'email, le `sub` du jeton, qui sert d'`user_id` au
  porte-monnaie, et les niveaux PMPro ; TTL `MEMBER_SESSION_TTL_S`), lead marqué `verified`, puis **302 vers
  la même URL sans le jeton** (`Referrer-Policy: no-referrer`, `Cache-Control: no-store`) ; jeton invalide → ignoré ;
- sans session → « Réservé aux membres » et boutons « Se connecter / Créer un compte » (cible `_top`, URLs
  acceptées seulement sous `WP_SITE_PREFIX`) ; l'API refuse aussi les accès directs (403 `need_login`) ;
- `REQUIRED_LEVELS="2,3"` (optionnel) réserve l'outil à certains niveaux PMPro.

Le secret `WP_TOKEN_SECRET` = `CREAPULSE_TOOLS_TOKEN_SECRET` côté WordPress. Vecteur de référence pinné dans
`tests/test_wp_token.py`. L'auto-hauteur de l'iframe reste assurée par un petit bloc HTML après le shortcode
(écoute `vsg-height` sur `iframe.creapulse-tool`).

## Réglages qualité / gating (env)

| Variable | Défaut | Effet |
|---|---|---|
| `MIN_WORDS_PER_PAGE` / `MIN_WORDS_TOTAL` | 300 / 600 | refus propre si extraction vide ou courte |
| `MAX_WORDS_PER_PAGE` / `MAX_WORDS_TOTAL` | 2500 / 9000 | budget tokens (tronque équitablement) |
| `ACCOUNT_DAILY_CAP` | 3 | générations par membre et par jour, en plus des crédits |
| `GLOBAL_DAILY_CAP` | 40 | tous membres : protège le budget LLM en dur |
| `WP_BASE_URL` | `https://www.creapulse.fr` | site du porte-monnaie et de `tool-event` (staging pour la recette) |
| `CREAPULSE_CREDITS_SECRET_VOIX` | (vide) | secret partagé avec WordPress, 32 caractères au moins ; sans lui, l'outil refuse de lancer |
| `CREDITS_TOOL_SLUG` / `CREDITS_TIER` | `voix` / `generation` | outil et palier débités ; le **prix** se règle dans wp-admin → Crédits |
| `LLM_MODEL` / `LLM_TEMPERATURE` | `openai/gpt-5.4-mini` / 0.2 | choix du modèle dans omniroute ; T° basse = mêmes URLs → même profil |
| `MAX_GUIDE_CHARS` | 5000 | plafond du guide 5 axes, garanti par le code (voir ci-dessous) |
| `FREE_RETENTION_MONTHS` | 3 | conservation d'un profil dans l'historique, sans niveau PMPro |
| `LEVEL_RETENTION_MONTHS` | (vide) | conservation par niveau, `niveau:mois` séparés par des virgules (ex. `5:6,7:12`) ; `0` = sans limite ; le niveau le plus généreux l'emporte |
| `HISTORY_MAX_PER_ACCOUNT` | 20 | profils gardés par compte (~6 Ko chacun) |
| `LLM_PRICE_IN_PER_M` / `LLM_PRICE_OUT_PER_M` | 0.75 / 4.50 | prix du modèle en $ par million de jetons ; sert seulement quand omniroute n'annonce pas le coût d'un appel |

Si le modèle refuse `temperature` (famille reasoning), l'app réessaie automatiquement sans.

## Plafond de longueur du guide

Un profil trop long noie le modèle qui l'applique. Mesuré le 2026-10-02 sur le guide de voix Creapulse :
10 100 caractères pesaient 70 % des consignes système du rédacteur, et le plan n'était plus suivi.
Le guide produit ici est donc plafonné à `MAX_GUIDE_CHARS` (5 000 par défaut), en trois temps :

1. **Le budget est dans le prompt de distillation** (`build_system_prompt`), avec la consigne de viser
   3 à 5 traits par section et 1 à 2 citations par trait. Dans le cas courant, rien d'autre ne se déclenche.
2. **Le code vérifie** (`enforce_budget`). Si ça dépasse, **une** passe de raccourcissement ciblé, qui
   n'envoie que le guide et jamais les articles (~1,5 k tokens au lieu de ~11 k). La consigne dit où couper :
   d'abord « Structure / format », puis « Principes édito », puis les traits redondants, puis le nombre de
   citations — jamais la dernière citation d'un trait, jamais une section entière, jamais le résumé.
3. **Filet déterministe** (`trim_to_budget`) si le modèle ignore la consigne : retire les dernières puces
   des sections les moins porteuses de voix, dans l'ordre de `TRIM_ORDER`, avec un plancher de `MIN_BULLETS`
   par section. Le plafond est donc tenu par le code, pas par la bonne volonté du modèle.

Mesuré sur un guide réel de 5 987 caractères : le filet seul ramène à 4 970 en retirant 7 puces, toutes
dans « Structure / format » et « Principes édito ». Ton, style de phrase et vocabulaire sortent intacts,
puisque ce sont eux qui transmettent la voix.

## Porte-monnaie de crédits

Contrat creapulse-tools 079 (`app/wallet.py`) : `POST <WP_BASE_URL>/wp-json/creapulse-tools/v1/credits/{balance,
debit,refund,cost}`, en-têtes `X-Creapulse-Tool: voix` et `X-Credits-Secret`. Une génération :

1. lit et vérifie les pages (gratuit : une entrée refusée ne débite rien) ;
2. **débite** le palier `generation` avec un `usage_ref` neuf (`voix<16 hex>:1`). Délai dépassé ou 503 : le même
   `usage_ref` est rejoué une fois, puis un `refund` part et rien ne se lance. 402 → message et lien de recharge ;
   403 fermé (`free_budget`, `tool_disabled`), 404/410 compte inconnu : rien ne se lance ;
3. distille ; si ça échoue, **rembourse** (`refund`, raison `distill_failed` ou `error`) ;
4. **déclare le coût réel** (`cost`, le `CostMeter.contract()`), réussite ou échec, après la réponse.

`refund` et `cost` passent par une file (`wallet_outbox`) sur 503 ou erreur réseau seulement, rejouée au
démarrage et avant chaque déclaration de coût ; tout autre refus est loggé et abandonné. `GET /api/credits`
donne au front le solde, le prix du palier et le lien de recharge. Les crédits de bienvenue (une fois par
compte) remplacent l'ancienne génération anonyme gratuite.

## Suivi coût / usage

Chaque génération qui a appelé le modèle, réussie ou non, enregistre son coût dans la table
`generation_costs` (`app/costs.py`), au format attendu par le porte-monnaie de crédits de creapulse-tools :
`cost_llm_usd`, `cost_data_usd`, `cost_other_usd` et `cost_complete`. Aucune adresse, URL ni texte n'y figure.

| Poste | Comment il est obtenu |
|---|---|
| llm | coût annoncé par omniroute pour chaque appel (en-tête `X-OmniRoute-Response-Cost`) ; à défaut, jetons × prix de la config, et la ligne le dit (`cost_llm_source` = `estimated`) |
| data | 0 : crawl4ai auto-hébergé sans proxy payant, ou lecture directe ; le serveur est une charge fixe |
| autre | 0 : ni Skills API, ni conteneur, ni autre API payante |

Mesure du 2026-11-03 sur la clé omniroute de l'outil (toute sa durée de vie) : 8 appels, 71 287 jetons
d'entrée, 12 118 de sortie, 0,1080 $, soit 0,0135 $ par appel. Le prix catalogue OpenRouter
(0,75 $ / 4,50 $ par million) redonne exactement ce montant. Pire cas calculé depuis les plafonds
(9 000 mots, relance de format et passe de raccourcissement) : environ 0,06 $.

La ligne `generation ok …` du journal reprend le coût, sa source et le nombre d'appels. Le dashboard
omniroute garde le total par clé (clé dédiée à cet outil).

## RGPD

- Plus de capture d'email propre à l'outil : l'identité vient du compte WordPress (jeton signé).
- Table `leads` : email du membre, dates, nombre de générations (les lignes de l'ancienne capture restent).
  Rien d'autre. Les IP ne sont stockées que hachées (HMAC + `SECRET_KEY`), dans des compteurs purgés après 7 jours.
- Le contenu scrapé n'est jamais persisté.
- Table `profiles` : pour un compte identifié qui a coché « Garder ce profil » (opt-in, décochée par défaut), ses profils (guide, résumé, URL et titre des pages lues),
  effacés au bout de la durée de conservation ; suppression à la demande depuis « Mes profils ».
  La politique de confidentialité du site doit mentionner cet historique.
