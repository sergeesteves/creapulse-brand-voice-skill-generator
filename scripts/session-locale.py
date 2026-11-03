"""Ouvre l'outil local avec une session membre, sans passer par une page WordPress.

Pour la recette en local : l'outil tourne sur http://localhost:8000 et parle au porte-monnaie du
staging. Le shortcode WordPress ne peut pas ouvrir de session dans un iframe localhost (cookie
SameSite=Lax refusé en contexte tiers), donc ce script signe un jeton au même format que le plugin,
avec le WP_TOKEN_SECRET du .env local, et ouvre le navigateur dessus. Le jeton vaut 10 minutes et
n'est jamais affiché. L'identifiant doit être celui d'un vrai compte du site visé par WP_BASE_URL :
le porte-monnaie débite ce compte.

    .venv\\Scripts\\python.exe scripts\\session-locale.py --user-id 1 --email vous@exemple.fr
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.wp_token import AUDIENCE, make_token  # noqa: E402  (après le chargement du .env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user-id", type=int, required=True, help="identifiant WordPress du compte (site de WP_BASE_URL)")
    parser.add_argument("--email", required=True, help="email du compte")
    parser.add_argument("--name", default="", help="nom affiché (facultatif)")
    parser.add_argument("--levels", default="", help="niveaux PMPro, ex. 1,3 (facultatif)")
    parser.add_argument("--base", default="http://localhost:8000", help="adresse de l'outil local")
    args = parser.parse_args()

    secret = os.getenv("WP_TOKEN_SECRET", "")
    if not secret:
        print("WP_TOKEN_SECRET manquant dans .env", file=sys.stderr)
        return 1
    now = int(time.time())
    levels = [int(x) for x in args.levels.split(",") if x.strip().isdigit()]
    token = make_token({"sub": args.user_id, "email": args.email, "name": args.name, "levels": levels,
                        "aud": AUDIENCE, "iat": now, "exp": now + 600}, secret)
    webbrowser.open(f"{args.base.rstrip('/')}/?wp_token={token}")
    print(f"Navigateur ouvert sur {args.base} (session du compte {args.user_id}, jeton valable 10 min).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
