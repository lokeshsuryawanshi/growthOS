"""Runtime configuration, loaded from environment / .env."""
from functools import lru_cache
from typing import Literal

import re

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEV_JWT_SECRET = "dev-only-change-me-0123456789abcdef"


def normalize_database_url(v: str) -> str:
    """Accept the URL hosted Postgres providers hand out (postgres://...?sslmode=require) as is."""
    v = (v or "").strip()
    for prefix in ("postgres://", "postgresql://"):
        if v.startswith(prefix):
            v = "postgresql+asyncpg://" + v[len(prefix):]
    if v.startswith("postgresql+asyncpg://"):
        v = re.sub(r"([?&])sslmode=(require|verify-full|verify-ca|prefer)", r"\1ssl=\2", v)
        v = re.sub(r"[?&]channel_binding=\w+", "", v)
    return v


class ConfigError(RuntimeError):
    """The deployment is misconfigured in a way that must stop startup."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    # --- environment ---------------------------------------------------------
    # production: real providers only. No fixtures, no mock models, no demo data.
    # demo / test: fixtures and mock models are allowed (automated tests, load test, labelled local demo).
    app_env: Literal["production", "demo", "test"] = "production"
    app_base_url: str = "http://localhost:8000"      # public URL; the Google sign-in redirect and secure cookies
    allowed_emails: str = ""                          # optional comma-separated list; empty = anyone may sign in
    contact_email: str = ""                           # shown on the privacy page for data requests

    # --- sign-in and stored keys ----------------------------------------------
    # Sign in with Google (openid email profile). Redirect URI: APP_BASE_URL/auth/google/callback
    google_oauth_client_id: str = Field("", validation_alias=AliasChoices("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_CLIENT_ID"))
    google_oauth_client_secret: str = Field("", validation_alias=AliasChoices("GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET"))
    # Operators' API keys are encrypted with Fernet. ENCRYPTION_KEYS (comma-separated, newest first) rotates keys:
    # the first encrypts, all decrypt. Production refuses to start without a valid key.
    encryption_key: str = ""
    encryption_keys: str = ""

    # --- models ----------------------------------------------------------------
    # Every operator brings their own AI key (Groq, OpenAI or Anthropic); the server holds none. "mock" uses offline
    # responders (tests and the labelled demo only). "auto" = mock in test/demo, live in production.
    llm_mode: Literal["live", "mock", "auto"] = "auto"
    model_routes_json: str = ""  # development override of PROVIDER_ROUTES, e.g. {"groq": {"big": ["openai/gpt-oss-20b"]}}
    route_spill_wait_s: float = 4.0
    model_limits_json: str = ""  # override MODEL_LIMITS, e.g. {"openai/gpt-oss-120b": {"tpm": 250000, "rpm": 300}}

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

    # --- budgets (the operator's own money: their keys, their bill) -----------
    default_run_budget_usd: float = 2.0               # hard stop per run; the account's optional monthly cap is set in the UI

    # --- business targets --------------------------------------------------
    posts_per_week_target: int = 5
    calendar_weeks: int = 8
    leads_per_week_min: int = 10
    leads_per_week_max: int = 20
    lead_fit_threshold: int = 70                      # below this a lead is dropped (and reported)
    critic_pass_score: float = 4.0
    critic_max_rounds: int = 2                         # editor rounds per attempt (one revision); attempts are bounded too
    draft_attempts: int = 3                            # fresh drafts per slot until one passes guardrails and the bar
    content_time_budget_s: float = 420.0               # no new redraft attempt starts after this (in-flight ones finish)

    @field_validator("ephemeral_storage_override", mode="before")
    @classmethod
    def _blank_is_unset(cls, v):
        return None if isinstance(v, str) and not v.strip() else v   # "EPHEMERAL_STORAGE=" in a .env means "decide for me"

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        return normalize_database_url(v)

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
        """live | mock. Production is always live: each account's own key decides what runs, never a mock."""
        if self.llm_mode == "mock":
            if not self.fixtures_allowed:
                raise ConfigError("LLM_MODE=mock is only allowed when APP_ENV is 'demo' or 'test'")
            return "mock"
        return "mock" if self.llm_mode == "auto" and self.fixtures_allowed else "live"

    def route_chain(self, provider: str, tier: str) -> list[str]:
        """The ordered models a tier tries on one provider (the first leads; the rest take over when it is busy)."""
        import json
        routes = PROVIDER_ROUTES.get(provider, {})
        if self.model_routes_json:
            routes = {**routes, **json.loads(self.model_routes_json).get(provider, {})}
        return list(routes.get(tier, []))

    @property
    def fernet_keys(self) -> list[str]:
        raw = self.encryption_keys or self.encryption_key
        return [k.strip() for k in raw.split(",") if k.strip()]

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
            from cryptography.fernet import Fernet
            if not self.fernet_keys:
                raise ConfigError("ENCRYPTION_KEY must be set in production: it encrypts operators' API keys. Generate one "
                                  "with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"")
            for k in self.fernet_keys:
                try:
                    Fernet(k.encode())
                except (ValueError, TypeError):
                    raise ConfigError("ENCRYPTION_KEY (or an entry of ENCRYPTION_KEYS) is not a valid Fernet key")
        warnings = []
        if self.app_env == "production" and not self.app_base_url.startswith("https://"):
            warnings.append("APP_BASE_URL is not https: session cookies are not marked Secure.")
        if self.ephemeral_storage:
            warnings.append("The database is on ephemeral storage: accounts and data reset on redeploy, restart or idle "
                            "spin-down. Set DATABASE_URL to a hosted Postgres to keep them.")
        return warnings


# Model table: provider -> tier -> models in order. Tiers: big (posts, outreach, strategy, calendar, voice), research
# (choose searches and pages), small (scoring, query planning, reflection), extract (one structured extraction from an
# evidence pack), judge (the critic: a different model than the writer), guard (prompt-injection check).
# IDs checked 2026-10-29: Groq GET /openai/v1/models with a real key; OpenAI developers.openai.com/api/docs/pricing;
# Anthropic's current model list. OpenAI and Anthropic offer no separate injection classifier: their guard tier is a
# small model asked to score the text (see LLMGateway._for_provider).
PROVIDER_ROUTES: dict[str, dict[str, list[str]]] = {
    "groq": {
        "big": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b"],
        "research": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
        "small": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b", "openai/gpt-oss-120b"],
        "extract": ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"],
        "judge": ["qwen/qwen3.8-27b", "openai/gpt-oss-120b"],          # a different family than the writer
        "guard": ["meta-llama/llama-prompt-guard-2-86m", "meta-llama/llama-prompt-guard-2-22m"],
    },
    "openai": {
        "big": ["gpt-6.1-sol"], "research": ["gpt-6-luna"], "small": ["gpt-6-luna"], "extract": ["gpt-6.1-terra"],
        "judge": ["gpt-6.1-terra"], "guard": ["gpt-6-luna"],
    },
    "anthropic": {
        "big": ["claude-sonnet-5-5"], "research": ["claude-haiku-5-5"], "small": ["claude-haiku-5-5"],
        "extract": ["claude-haiku-5-5"], "judge": ["claude-haiku-5-5"], "guard": ["claude-haiku-5-5"],
    },
}
PROVIDER_LABELS = {"groq": "Groq", "openai": "OpenAI", "anthropic": "Anthropic (Claude)"}


# USD per 1M tokens (input, output), paid list prices. Groq checked 2026-10-27 (console.groq.com/docs/models); OpenAI
# checked 2026-10-29 (developers.openai.com/api/docs/pricing, standard tier, short context); Anthropic checked
# 2026-10-29 (Haiku 5.5 price is for prompts up to 100K tokens). Free tiers bill $0; the ledger records what the same
# usage would cost on a paid plan.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "meta-llama/llama-prompt-guard-2-86m": (0.04, 0.04),
    "qwen/qwen3.8-27b": (0.80, 4.00),
    "meta-llama/llama-prompt-guard-2-22m": (0.03, 0.03),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6.1-terra": (1.00, 5.00),
    "gpt-6-luna": (0.10, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
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
    ("hunter", "domain_search"): (1.0, 0.0245),        # Starter $49 / 2,000 credits; one credit per query with results
    ("hunter", "verify"): (0.5, 0.0245),               # a verification is half a credit
}


# Groq free-tier limits per model, read from the x-ratelimit-limit-* headers on 2026-10-27 (tokens per minute; Groq
# also caps 1,000 requests and about 200k tokens per day per model, enforced by 429s). A request whose prompt +
# max_tokens exceeds a Groq model's TPM is rejected outright, so the gateway skips that model for it. Applied to Groq
# keys only, per key: OpenAI and Anthropic limits depend on the account, so those rely on 429 / retry-after handling.
MODEL_LIMITS: dict[str, dict[str, int]] = {
    "openai/gpt-oss-120b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "openai/gpt-oss-20b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "qwen/qwen3.8-27b": {"tpm": 8000, "rpm": 30, "max_request": 8000},
    "meta-llama/llama-prompt-guard-2-86m": {"tpm": 15000, "rpm": 30},
    "meta-llama/llama-prompt-guard-2-22m": {"tpm": 15000, "rpm": 30},
}
MODEL_TPM: dict[str, int] = {m: v["tpm"] for m, v in MODEL_LIMITS.items()}   # kept for callers that only need TPM

@lru_cache
def get_settings() -> Settings:
    return Settings()
