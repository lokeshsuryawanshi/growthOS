"""Runtime configuration, loaded from environment / .env."""
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_JWT_SECRET = "dev-only-change-me-0123456789abcdef"


class ConfigError(RuntimeError):
    """The deployment is misconfigured in a way that must stop startup."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

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
    # optional: Gmail sending. GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are accepted as aliases.
    google_oauth_client_id: str = Field("", validation_alias=AliasChoices("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"))
    google_oauth_client_secret: str = Field("", validation_alias=AliasChoices("GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"))
    google_redirect_uri: str = ""                     # must match the Google console exactly; default APP_BASE_URL/oauth/google/callback
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
    research_model: str = "gemini-3.5-flash-lite"    # Google Search grounding (web search provider)
    # Tier -> fallback chain, "provider:model" comma-separated. Empty = the default chain built from the models above
    # (see Settings.route_chain). Free-tier limits are per model, so each tier leads with a different model and spills
    # over to the next one when its own limit would make the call wait (route_spill_wait_s).
    route_big: str = ""          # posts, outreach, strategy, calendar, voice
    route_research: str = ""     # research steps: choose searches and pages to read
    route_small: str = ""        # scoring, query planning, reflection
    route_extract: str = ""      # one structured extraction from a large evidence pack
    route_judge: str = ""        # critic: a different model family than the writer
    route_guard: str = ""        # prompt-injection classifier
    route_spill_wait_s: float = 4.0
    model_limits_json: str = ""  # override MODEL_LIMITS, e.g. {"gemini-3.5-flash-lite": {"tpm": 250000, "rpm": 15}}

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

    def route_chain(self, tier: str) -> list[tuple[str, str]]:
        """The ordered (provider, model) fallback chain for a tier: env override or the default."""
        raw = getattr(self, f"route_{tier}", "") or ",".join(DEFAULT_ROUTES[tier](self))
        out = []
        for item in raw.split(","):
            provider, _, model = item.strip().partition(":")
            if provider and model:
                out.append((provider.strip(), model.strip()))
        return out

    def model_limits(self) -> dict[str, dict[str, int]]:
        import json
        limits = {m: dict(v) for m, v in MODEL_LIMITS.items()}
        if self.model_limits_json:
            for m, v in json.loads(self.model_limits_json).items():
                limits.setdefault(m, {}).update(v)
        return limits

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
    "meta-llama/llama-prompt-guard-2-22m": (0.03, 0.03),
    "gemini-3.5-flash-lite": (0.10, 0.40),
    "mock-big": (0.15, 0.60),
    "mock-research": (0.075, 0.30),
    "mock-small": (0.075, 0.30),
    "mock-extract": (0.10, 0.40),
    "mock-judge": (0.10, 0.40),
    "mock-guard": (0.04, 0.04),
}


# Free-tier limits per model. Groq values were read from the x-ratelimit-limit-* headers on 2026-10-27 (tokens per
# minute; Groq also caps 1,000 requests and about 200k tokens per day per model, enforced by 429s). A request whose
# prompt + max_tokens exceeds a Groq model's TPM is rejected outright, so the gateway skips that model for it.
# Gemini's free tier is not reported in headers: the values are conservative and can be raised via MODEL_LIMITS_JSON.
MODEL_LIMITS: dict[str, dict[str, int]] = {
    "openai/gpt-oss-120b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "openai/gpt-oss-20b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "qwen/qwen3.8-27b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "meta-llama/llama-prompt-guard-2-86m": {"tpm": 15000, "rpm": 30},
    "meta-llama/llama-prompt-guard-2-22m": {"tpm": 15000, "rpm": 30},
    "gemini-3.5-flash-lite": {"tpm": 250000, "rpm": 15},
}
MODEL_TPM: dict[str, int] = {m: v["tpm"] for m, v in MODEL_LIMITS.items()}   # kept for callers that only need TPM

DEFAULT_ROUTES = {
    "big": lambda s: [f"groq:{s.groq_model}", f"groq:{s.judge_fallback_model}"],
    "research": lambda s: [f"groq:{s.small_model}", f"groq:{s.judge_fallback_model}", f"gemini:{s.judge_model}"],
    "small": lambda s: [f"groq:{s.small_model}", f"groq:{s.judge_fallback_model}", f"gemini:{s.judge_model}"],
    "extract": lambda s: [f"gemini:{s.judge_model}", f"groq:{s.groq_model}", f"groq:{s.judge_fallback_model}"],
    "judge": lambda s: [f"gemini:{s.judge_model}", f"groq:{s.judge_fallback_model}"],
    "guard": lambda s: [f"groq:{s.guard_model}", "groq:meta-llama/llama-prompt-guard-2-22m"],
}


@lru_cache
def get_settings() -> Settings:
    return Settings()
