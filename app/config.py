"""Configuration — tout vient des variables d'environnement (12-factor, stateless)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # --- Identité / URLs publiques ---
    app_name: str = "Générateur de skill de voix de marque IA"
    # Préfixe de chemin si l'app est servie sous un sous-dossier (ex. /outils/voix-de-marque) ; vide sinon.
    root_path: str = ("/" + os.getenv("ROOT_PATH", "").strip("/")) if os.getenv("ROOT_PATH", "").strip("/") else ""
    cta_url: str = os.getenv("CTA_URL", "https://www.creapulse.fr/contact/")
    privacy_url: str = os.getenv("PRIVACY_URL", "https://www.creapulse.fr/politique-de-confidentialite/")
    debug: bool = _bool("DEBUG", False)

    # --- Secrets ---
    secret_key: str = os.getenv("SECRET_KEY", "")

    # --- Identité WordPress (jeton signé émis par creapulse-tools) : compte obligatoire ---
    wp_token_secret: str = os.getenv("WP_TOKEN_SECRET", "")
    required_levels: tuple[int, ...] = tuple(
        int(x) for x in os.getenv("REQUIRED_LEVELS", "").replace(";", ",").split(",") if x.strip().isdigit()
    )
    member_session_ttl_s: int = _int("MEMBER_SESSION_TTL_S", 8 * 3600)   # session courte : suit la déconnexion WP
    # Allowlist des login_url / register_url transmis par le shortcode. Plusieurs préfixes séparés
    # par une virgule : l'outil est embarqué dans la prod ET dans le staging, qui envoie ses propres
    # liens. Un seul préfixe ferait disparaître « Se connecter » côté staging, sans erreur visible.
    wp_site_prefixes: tuple[str, ...] = tuple(
        p.strip() for p in os.getenv("WP_SITE_PREFIX",
                                     "https://www.creapulse.fr/,https://staging.creapulse.fr/").split(",")
        if p.strip().startswith("https://")
    )
    # Site WordPress qui porte le porte-monnaie et la déclaration d'usage : staging pour la recette, www en prod.
    wp_base_url: str = os.getenv("WP_BASE_URL", "https://www.creapulse.fr").strip().rstrip("/")
    # Déclaration d'usage à WordPress (creapulse-tools `tool-event` → Nimble). "off" = désactivé.
    wp_tool_event_url: str = ("" if os.getenv("WP_TOOL_EVENT_URL", "").strip() == "off" else
                              os.getenv("WP_TOOL_EVENT_URL", "").strip()
                              or os.getenv("WP_BASE_URL", "https://www.creapulse.fr").strip().rstrip("/")
                              + "/wp-json/creapulse-tools/v1/tool-event")
    tool_slug: str = os.getenv("TOOL_SLUG", "voix-de-marque").strip()
    wp_tool_event_timeout_s: float = _float("WP_TOOL_EVENT_TIMEOUT_S", 3.0)

    # --- Store (Postgres en prod, SQLite en dev/tests) ---
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./voice-skill.db")

    # --- crawl4ai (réutilisé, pas de nouveau scraper) ---
    crawl4ai_url: str = os.getenv("CRAWL4AI_URL", "https://crawl4ai.creapulse.fr").rstrip("/")
    crawl4ai_token: str = os.getenv("CRAWL4AI_TOKEN", "")
    crawl_timeout_s: float = _float("CRAWL_TIMEOUT_S", 90.0)
    crawl_page_timeout_ms: int = _int("CRAWL_PAGE_TIMEOUT_MS", 45000)
    # Repli si crawl4ai est en panne (proxy 402, tunnel, timeout) : GET direct + trafilatura
    direct_fallback: bool = _bool("DIRECT_FALLBACK", True)
    direct_timeout_s: float = _float("DIRECT_TIMEOUT_S", 20.0)

    # --- LLM via omniroute (OpenAI-compatible) ---
    llm_base_url: str = os.getenv("LLM_BASE_URL", "https://omniroute.creapulse.fr/api/v1").rstrip("/")
    llm_api_key: str = os.getenv("LLM_API_KEY", "")
    llm_model: str = os.getenv("LLM_MODEL", "openai/gpt-5.4-mini")
    llm_temperature: float = _float("LLM_TEMPERATURE", 0.2)
    llm_timeout_s: float = _float("LLM_TIMEOUT_S", 120.0)
    llm_max_output_tokens: int = _int("LLM_MAX_OUTPUT_TOKENS", 2500)
    # Prix du modèle en $ par million de jetons (catalogue OpenRouter relevé le 2026-11-03). Ne sert
    # qu'à estimer le coût d'un appel quand le routeur ne l'annonce pas dans sa réponse.
    llm_price_in_per_m: float = _float("LLM_PRICE_IN_PER_M", 0.75)
    llm_price_out_per_m: float = _float("LLM_PRICE_OUT_PER_M", 4.50)
    # Plafond du guide 5 axes. Un profil trop long noie le modèle qui l'applique (constat mesuré
    # le 2026-10-02 sur le guide Creapulse : 10 100 car. = 70 % des consignes du rédacteur).
    # 5 500 depuis le 2026-10-15 : le guide porte désormais la fréquence de chaque trait et des
    # répertoires d'au moins 5 expressions, deux ajouts qui empêchent la sur-utilisation des tics.
    max_guide_chars: int = _int("MAX_GUIDE_CHARS", 5500)
    # Minimum d'expressions pour un trait à variantes (incises, vocabulaire de jugement…).
    # En dessous, le modèle qui applique le guide recycle toujours la même : mesuré sur le guide
    # Creapulse, où trois exemples d'attaques familières produisaient « Bon, » à chaque section.
    min_repertoire: int = _int("MIN_REPERTOIRE", 5)
    max_suggestions: int = _int("MAX_SUGGESTIONS", 3)

    # --- Garde-fous qualité d'entrée ---
    min_urls: int = _int("MIN_URLS", 3)   # 3 articles = minimum pour une voix fiable (décision Serge, 2026-09-08)
    max_urls: int = _int("MAX_URLS", 5)
    min_words_per_page: int = _int("MIN_WORDS_PER_PAGE", 300)
    min_words_total: int = _int("MIN_WORDS_TOTAL", 600)
    max_words_per_page: int = _int("MAX_WORDS_PER_PAGE", 2500)
    max_words_total: int = _int("MAX_WORDS_TOTAL", 9000)
    max_link_density: float = _float("MAX_LINK_DENSITY", 0.35)

    # --- Porte-monnaie de crédits commun (creapulse-tools 079). Le prix se règle dans wp-admin, jamais ici. ---
    credits_secret: str = os.getenv("CREAPULSE_CREDITS_SECRET_VOIX", "")
    credits_tool_slug: str = os.getenv("CREDITS_TOOL_SLUG", "voix").strip()
    credits_tier: str = os.getenv("CREDITS_TIER", "generation").strip()
    credits_timeout_s: float = _float("CREDITS_TIMEOUT_S", 8.0)

    # --- Plafonds (protègent le budget LLM, en plus des crédits) ---
    account_daily_cap: int = _int("ACCOUNT_DAILY_CAP", 3)
    global_daily_cap: int = _int("GLOBAL_DAILY_CAP", 40)
    ip_rate_limit_per_min: int = _int("IP_RATE_LIMIT_PER_MIN", 6)
    cookie_secure: bool = _bool("COOKIE_SECURE", True)

    # --- Historique des profils (comptes identifiés seulement) ---
    # Mêmes noms et même logique que l'outil de clustering : conservation selon le niveau PMPro,
    # au-delà le profil est effacé et la ligne reste (l'historique dit « expiré »).
    free_retention_months: int = _int("FREE_RETENTION_MONTHS", 3)
    level_retention_months: str = os.getenv("LEVEL_RETENTION_MONTHS", "")  # "niveau:mois", ex. "5:6,7:12" ; 0 = sans limite
    # Plafond par compte : au-delà, le plus ancien est supprimé. Un profil pèse ~6 Ko, 20 profils ~120 Ko.
    history_max_per_account: int = _int("HISTORY_MAX_PER_ACCOUNT", 20)

    trusted_proxies: bool = _bool("TRUST_PROXY_HEADERS", True)

    extra: dict = field(default_factory=dict)

    def retention_months(self, levels: tuple[int, ...] = ()) -> int:
        """Conservation d'un profil : la plus généreuse parmi les niveaux du membre, sinon le gratuit."""
        per_level = {}
        for part in self.level_retention_months.replace(";", ",").split(","):
            lv, _, months = part.partition(":")
            if lv.strip().isdigit() and months.strip().isdigit():
                per_level[int(lv)] = int(months)
        values = [per_level[lv] for lv in levels if lv in per_level]
        if 0 in values:  # un niveau sans limite l'emporte
            return 0
        return max(values, default=self.free_retention_months)

    @property
    def cookie_path(self) -> str:
        return self.root_path or "/"

    def validate(self) -> list[str]:
        """Retourne la liste des problèmes de config bloquants."""
        problems = []
        if not self.secret_key or len(self.secret_key) < 16:
            problems.append("SECRET_KEY manquante ou trop courte (>= 16 caractères)")
        if not self.crawl4ai_token:
            problems.append("CRAWL4AI_TOKEN manquant")
        if not self.llm_api_key:
            problems.append("LLM_API_KEY manquante (clé omniroute)")
        if not self.wp_token_secret:
            problems.append("WP_TOKEN_SECRET manquant (compte WordPress obligatoire)")
        if len(self.credits_secret) < 32:
            problems.append("CREAPULSE_CREDITS_SECRET_VOIX manquant ou trop court (>= 32 caractères)")
        return problems


settings = Settings()
