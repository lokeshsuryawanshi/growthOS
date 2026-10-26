"""Runtime configuration, loaded from environment / .env."""
from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- providers ---------------------------------------------------------
    groq_api_key: str = ""
    google_api_key: str = ""
    # "live" calls real providers; "mock" uses deterministic offline responders
    # (tests, CI, stress test). "auto" = live if a Groq key is present.
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
    jwt_secret: str = "dev-only-change-me-0123456789abcdef"
    jwt_ttl_minutes: int = 12 * 60

    # --- concurrency & limits ----------------------------------------------
    global_max_concurrency: int = 16       # concurrent DAG nodes across all tenants
    per_tenant_max_concurrency: int = 3    # fairness: no tenant hogs workers
    provider_max_concurrency: int = 8      # in-flight requests per LLM provider
    llm_max_retries: int = 4
    circuit_failure_threshold: int = 5
    circuit_cooldown_s: float = 30.0

    # --- budgets -----------------------------------------------------------
    default_tenant_monthly_budget_usd: float = 25.0
    default_run_budget_usd: float = 2.0
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
    def effective_llm_mode(self) -> str:
        if self.llm_mode == "auto":
            return "live" if self.groq_api_key else "mock"
        return self.llm_mode


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
