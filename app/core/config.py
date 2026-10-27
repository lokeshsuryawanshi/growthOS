"""Runtime configuration, loaded from environment / .env."""
from functools import lru_cache
from typing import Literal

import re

from pydantic import AliasChoices, Field, field_validator
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
    search_providers: str = "tavily,gemini_search"   # web search fallback order (Tavily first: free tier, no billing)
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
    database_url: str = "sqlite+aiosqlite:///./growthos.db"   # or postgres://... (any hosted Postgres URL works as given)
    ephemeral_storage_override: bool | None = Field(None, validation_alias=AliasChoices("EPHEMERAL_STORAGE"))

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
    lead_fit_threshold: int = 70                      # below this a lead is dropped (and reported)
    critic_pass_score: float = 4.0
    critic_max_rounds: int = 2                         # editor rounds per attempt (one revision); attempts are bounded too
    draft_attempts: int = 3                            # fresh drafts per slot until one passes guardrails and the bar
    content_time_budget_s: float = 270.0               # no new redraft attempt starts after this (in-flight ones finish)
    outreach_daily_send_cap: int = 25

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        """Accept the URL hosted Postgres providers hand out (postgres://...?sslmode=require) as is."""
        v = (v or "").strip()
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                v = "postgresql+asyncpg://" + v[len(prefix):]
        if v.startswith("postgresql+asyncpg://"):
            v = re.sub(r"([?&])sslmode=(require|verify-full|verify-ca|prefer)", r"\1ssl=\2", v)
            v = re.sub(r"[?&]channel_binding=\w+", "", v)
        return v

    @property
    def ephemeral_storage(self) -> bool:
        """SQLite in /tmp (e.g. the free Render plan): data is lost on redeploy, restart and idle spin-down.
        EPHEMERAL_STORAGE=true/false overrides the guess."""
        if self.ephemeral_storage_override is not None:
            return self.ephemeral_storage_override
        return self.database_url.startswith("sqlite") and "/tmp/" in self.database_url

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
        if self.ephemeral_storage:
            warnings.append("The database is on ephemeral storage: accounts and data reset on redeploy, restart or idle "
                            "spin-down. Set DATABASE_URL to a hosted Postgres to keep them.")
        return warnings


# USD per 1M tokens (input, output), paid list prices checked on the providers' pages on 2026-10-27
# (console.groq.com/docs/models, ai.google.dev/gemini-api/docs/pricing). Free tiers bill $0; the ledger records what
# the same usage would cost on a paid plan.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "meta-llama/llama-prompt-guard-2-86m": (0.04, 0.04),
    "qwen/qwen3.8-27b": (0.80, 4.00),
    "meta-llama/llama-prompt-guard-2-22m": (0.03, 0.03),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "mock-big": (0.15, 0.60),
    "mock-research": (0.075, 0.30),
    "mock-small": (0.075, 0.30),
    "mock-extract": (0.10, 0.40),
    "mock-judge": (0.10, 0.40),
    "mock-guard": (0.04, 0.04),
}


# Non-model services, USD per unit at list prices checked on 2026-10-27 (tavily.com/pricing + docs.tavily.com,
# ai.google.dev pricing, hunter.io/pricing). Free tiers make the real cost $0 until they run out.
EXTERNAL_PRICES: dict[tuple[str, str], tuple[float, float]] = {
    # (provider, operation): (credits per call, USD per credit)
    ("tavily", "search"): (1.0, 0.008),               # basic search = 1 credit; pay-as-you-go $0.008/credit
    ("gemini_search", "search"): (1.0, 0.014),         # grounding: $14 per 1,000 requests after 5,000 free a month
    ("hunter", "domain_search"): (1.0, 0.0245),        # Starter $49 / 2,000 credits; one credit per query with results
    ("hunter", "verify"): (0.5, 0.0245),               # a verification is half a credit
    ("gmail", "send"): (1.0, 0.0),
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
