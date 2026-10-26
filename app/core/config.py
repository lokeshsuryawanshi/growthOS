"""Runtime configuration, loaded from environment / .env."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_JWT_SECRET = "dev-only-change-me-0123456789abcdef"


class ConfigError(RuntimeError):
    """The deployment is misconfigured in a way that must stop startup."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- environment ---------------------------------------------------------
    # production: real providers only. No fixtures, no mock models, no demo data.
    # demo / test: fixtures and mock models are allowed (automated tests, load test, labelled local demo).
    app_env: Literal["production", "demo", "test"] = "production"
    app_base_url: str = "http://localhost:8000"      # public URL; used for OAuth redirects and secure cookies
    signup_invite_code: str = ""                      # if set, signup requires it
    admin_email: str = ""                             # optional platform admin created at startup
    admin_password: str = ""
    global_daily_budget_usd: float = 5.0              # hard stop on total model spend per UTC day

    # --- providers ---------------------------------------------------------
    groq_api_key: str = ""
    google_api_key: str = ""
    tavily_api_key: str = ""                          # optional web search
    hunter_api_key: str = ""                          # optional: business email discovery + verification
    google_oauth_client_id: str = ""                  # optional: Gmail sending
    google_oauth_client_secret: str = ""
    encryption_key: str = ""                          # Fernet key for stored OAuth tokens; derived from JWT_SECRET if empty
    # "live" calls real providers; "mock" uses deterministic offline responders
    # (tests, load test, demo only). "auto" = live if a Groq key is present.
    llm_mode: Literal["live", "mock", "auto"] = "auto"

    # Model routing by tier. Writer and judge are deliberately different families.
    groq_model: str = "openai/gpt-oss-120b"          # big: strategy, writing
    small_model: str = "openai/gpt-oss-20b"          # small: scoring, extraction, classification
    guard_model: str = "meta-llama/llama-prompt-guard-2-86m"  # injection classifier
    judge_model: str = "gemini-3.5-flash-lite"       # judge (Gemini), falls back to judge_fallback_model
    judge_fallback_model: str = "qwen/qwen3.8-27b"
    research_model: str = "gemini-3.5-flash-lite"    # grounded web search; falls back to seeded corpus

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite+aiosqlite:///./growthos.db"

    # --- auth --------------------------------------------------------------
    jwt_secret: str = DEV_JWT_SECRET
    jwt_ttl_minutes: int = 12 * 60

    # --- concurrency & limits ----------------------------------------------
    global_max_concurrency: int = 16       # concurrent DAG nodes across all tenants
    per_tenant_max_concurrency: int = 3    # fairness: no tenant hogs workers
    provider_max_concurrency: int = 8      # in-flight requests per LLM provider
    llm_max_retries: int = 4              # transient errors (5xx, timeouts, malformed output)
    llm_rate_limit_retries: int = 10      # 429s are expected under load: wait them out
    llm_rate_limit_max_wait_s: float = 60.0
    circuit_failure_threshold: int = 5
    circuit_cooldown_s: float = 30.0

    # --- budgets -----------------------------------------------------------
    default_tenant_monthly_budget_usd: float = 25.0
    default_run_budget_usd: float = 2.0
    self_serve_monthly_budget_usd: float = 2.0       # budget for accounts created through public signup
    budget_alert_ratio: float = 0.8

    # --- business targets --------------------------------------------------
    posts_per_week_target: int = 5
    calendar_weeks: int = 8
    leads_per_week_min: int = 10
    leads_per_week_max: int = 20
    lead_fit_threshold: int = 60
    critic_pass_score: float = 4.0
    critic_max_rounds: int = 3
    outreach_daily_send_cap: int = 25

    @property
    def fixtures_allowed(self) -> bool:
        return self.app_env != "production"

    @property
    def cookie_secure(self) -> bool:
        return self.app_base_url.startswith("https://")

    @property
    def effective_llm_mode(self) -> str:
        """live | mock | unconfigured. Production never falls back to mock."""
        if self.llm_mode == "mock":
            if not self.fixtures_allowed:
                raise ConfigError("LLM_MODE=mock is only allowed when APP_ENV is 'demo' or 'test'")
            return "mock"
        if self.groq_api_key:
            return "live"
        if self.llm_mode == "auto" and self.fixtures_allowed:
            return "mock"
        return "unconfigured"

    @property
    def uses_fixture_sources(self) -> bool:
        """Seeded research/prospects are used if and only if the models are mocked too."""
        return self.fixtures_allowed and self.effective_llm_mode == "mock"

    def validate_runtime(self) -> list[str]:
        """Fatal problems stop startup; warnings are returned for logging and the integrations page."""
        if self.app_env == "production":
            if self.jwt_secret == DEV_JWT_SECRET or len(self.jwt_secret) < 32:
                raise ConfigError("JWT_SECRET must be set to a random value of at least 32 characters in production "
                                  "(generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\")")
            if self.llm_mode == "mock":
                raise ConfigError("LLM_MODE=mock is not allowed in production")
        warnings = []
        if self.effective_llm_mode == "unconfigured":
            warnings.append("GROQ_API_KEY is not set: research and drafting are unavailable until it is.")
        if self.app_env == "production" and not self.app_base_url.startswith("https://"):
            warnings.append("APP_BASE_URL is not https: session cookies are not marked Secure.")
        return warnings


# USD per 1M tokens (input, output). Approximate list prices; verify against
# provider pricing pages before relying on absolute numbers.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "meta-llama/llama-prompt-guard-2-86m": (0.04, 0.04),
    "qwen/qwen3.8-27b": (0.29, 0.59),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "mock-big": (0.15, 0.60),
    "mock-small": (0.075, 0.30),
    "mock-judge": (0.10, 0.40),
    "mock-guard": (0.04, 0.04),
}


# Tokens-per-minute per model, from the provider's x-ratelimit-limit-tokens header
# (Groq on-demand tier). The gateway paces requests with a token bucket per model.
MODEL_TPM: dict[str, int] = {
    "openai/gpt-oss-120b": 8000,
    "openai/gpt-oss-20b": 8000,
    "qwen/qwen3.8-27b": 8000,
    "meta-llama/llama-prompt-guard-2-86m": 15000,
}


@lru_cache
def get_settings() -> Settings:
    return Settings()
