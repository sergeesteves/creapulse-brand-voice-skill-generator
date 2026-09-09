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
    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
    # Préfixe de chemin si l'app est servie sous un sous-dossier (ex. /outils/voix-de-marque) ; vide sinon.
    root_path: str = ("/" + os.getenv("ROOT_PATH", "").strip("/")) if os.getenv("ROOT_PATH", "").strip("/") else ""
    cta_url: str = os.getenv("CTA_URL", "https://www.creapulse.fr/contact/")
    privacy_url: str = os.getenv("PRIVACY_URL", "https://www.creapulse.fr/politique-de-confidentialite/")
    debug: bool = _bool("DEBUG", False)

    # --- Secrets ---
    secret_key: str = os.getenv("SECRET_KEY", "")

    # --- Identité WordPress (jeton signé émis par creapulse-tools) ---
    wp_token_secret: str = os.getenv("WP_TOKEN_SECRET", "")
    auth_mode: str = (os.getenv("AUTH_MODE", "optional").strip().lower() or "optional")  # open | optional | required
    required_levels: tuple[int, ...] = tuple(
        int(x) for x in os.getenv("REQUIRED_LEVELS", "").replace(";", ",").split(",") if x.strip().isdigit()
    )
    member_session_ttl_s: int = _int("MEMBER_SESSION_TTL_S", 8 * 3600)   # session courte : suit la déconnexion WP
    wp_site_prefix: str = os.getenv("WP_SITE_PREFIX", "https://www.creapulse.fr/")  # allowlist des login_url/register_url
    # Déclaration d'usage à WordPress (creapulse-tools `tool-event` → Nimble). URL vide = désactivé.
    wp_tool_event_url: str = os.getenv("WP_TOOL_EVENT_URL", "https://www.creapulse.fr/wp-json/creapulse-tools/v1/tool-event").strip()
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

    # --- Garde-fous qualité d'entrée ---
    min_urls: int = _int("MIN_URLS", 3)   # 3 articles = minimum pour une voix fiable (décision Serge, 2026-09-08)
    max_urls: int = _int("MAX_URLS", 5)
    min_words_per_page: int = _int("MIN_WORDS_PER_PAGE", 300)
    min_words_total: int = _int("MIN_WORDS_TOTAL", 600)
    max_words_per_page: int = _int("MAX_WORDS_PER_PAGE", 2500)
    max_words_total: int = _int("MAX_WORDS_TOTAL", 9000)
    max_link_density: float = _float("MAX_LINK_DENSITY", 0.35)

    # --- Gating (coût + leads) ---
    anon_free_generations: int = _int("ANON_FREE_GENERATIONS", 1)
    anon_ip_daily_cap: int = _int("ANON_IP_DAILY_CAP", 2)
    account_daily_cap: int = _int("ACCOUNT_DAILY_CAP", 3)
    global_daily_cap: int = _int("GLOBAL_DAILY_CAP", 40)
    ip_rate_limit_per_min: int = _int("IP_RATE_LIMIT_PER_MIN", 6)
    magic_link_ttl_s: int = _int("MAGIC_LINK_TTL_S", 30 * 60)
    session_ttl_s: int = _int("SESSION_TTL_S", 30 * 24 * 3600)
    cookie_secure: bool = _bool("COOKIE_SECURE", True)

    # --- Email (magic-link) ---
    smtp_host: str = os.getenv("SMTP_HOST", "")
    smtp_port: int = _int("SMTP_PORT", 587)
    smtp_user: str = os.getenv("SMTP_USER", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_from: str = os.getenv("SMTP_FROM", "Creapulse <no-reply@creapulse.fr>")
    smtp_tls: bool = _bool("SMTP_TLS", True)

    trusted_proxies: bool = _bool("TRUST_PROXY_HEADERS", True)

    extra: dict = field(default_factory=dict)

    @property
    def mail_mode(self) -> str:
        """smtp = magic-link envoyé par email (email vérifié) ; direct = email capturé sans vérification."""
        return "smtp" if self.smtp_host else "direct"

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
        if self.auth_mode not in ("open", "optional", "required"):
            problems.append(f"AUTH_MODE invalide : {self.auth_mode}")
        if self.auth_mode == "required" and not self.wp_token_secret:
            problems.append("AUTH_MODE=required exige WP_TOKEN_SECRET")
        return problems


settings = Settings()
