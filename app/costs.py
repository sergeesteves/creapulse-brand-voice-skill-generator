"""Coût réel d'une génération, poste par poste, pour le porte-monnaie de crédits.

Contrat attendu par creapulse-tools : `cost_llm_usd`, `cost_data_usd`, `cost_other_usd`, chacun le
total absolu de son poste en dollars, et `cost_complete`, vrai quand tous les postes sont couverts.

- **llm** : chaque appel au modèle qui a répondu. Le coût est lu dans l'en-tête de réponse du routeur
  OmniRoute, qui reprend le prix OpenRouter (vérifié le 2026-11-03 sur la clé du générateur : 0,1080 $
  facturés pour 71 287 jetons d'entrée et 12 118 de sortie, soit exactement le prix catalogue). Sans
  en-tête exploitable, le coût est calculé à partir des jetons et des prix de la config : il est alors
  estimé, et le relevé le dit.
- **data** : les pages sont lues par crawl4ai auto-hébergé, sans proxy payant, ou en direct. Aucun coût
  à la génération ; le serveur est une charge fixe, qui n'est pas imputée ici.
- **autre** : aucun poste. Ni Skills API, ni conteneur, ni autre API payante.
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import settings

COST_HEADER = "x-omniroute-response-cost"
DATA_USD = 0.0   # crawl4ai auto-hébergé ou lecture directe : rien de payé à l'appel
OTHER_USD = 0.0  # aucun autre service payant dans une génération


def header_cost(raw: str | None) -> float | None:
    """Coût annoncé par le routeur, ou None s'il est absent, illisible ou nul.

    Un coût nul pour un appel qui a consommé des jetons n'est pas un coût : c'est un modèle sans ligne
    de prix dans le routeur, comme les modèles OpenRouter avant le 2026-09-30."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


@dataclass
class CostMeter:
    """Additionne le coût des appels au modèle d'une même génération, relances comprises."""

    llm_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_usd: float = 0.0
    estimated_calls: int = 0  # coût calculé à partir des jetons, faute d'en-tête
    unknown_calls: int = 0    # ni en-tête ni jetons : coût inconnu

    def add_llm_call(self, usage: dict | None, raw_header: str | None) -> None:
        usage = usage or {}
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        self.llm_calls += 1
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        cost = header_cost(raw_header)
        if cost is None:
            if prompt or completion:
                cost = (prompt * settings.llm_price_in_per_m + completion * settings.llm_price_out_per_m) / 1_000_000
                self.estimated_calls += 1
            else:
                cost = 0.0
                self.unknown_calls += 1
        self.llm_usd += cost

    @property
    def llm_source(self) -> str:
        """`measured` (coût du routeur), `estimated` (au moins un appel calculé), `unknown`, `none`."""
        if not self.llm_calls:
            return "none"
        if self.unknown_calls:
            return "unknown"
        return "estimated" if self.estimated_calls else "measured"

    @property
    def complete(self) -> bool:
        return self.unknown_calls == 0

    def contract(self) -> dict:
        """Les champs attendus par le porte-monnaie de crédits."""
        return {
            "cost_llm_usd": round(self.llm_usd, 6),
            "cost_data_usd": DATA_USD,
            "cost_other_usd": OTHER_USD,
            "cost_complete": self.complete,
        }
