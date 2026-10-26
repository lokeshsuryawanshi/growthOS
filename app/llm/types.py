from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

Tier = Literal["big", "small", "judge", "guard"]


class ToolSpec(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {"type": "function", "function": self.model_dump()}


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: str  # raw JSON text exactly as the model produced it


class LLMRequest(BaseModel):
    agent: str
    tier: Tier
    messages: list[dict[str, Any]]
    tools: list[ToolSpec] | None = None
    json_mode: bool = False
    temperature: float = 0.4
    max_tokens: int = 2500
    cacheable: bool = False  # deterministic-ish work (research, scoring) may be served from cache
    metadata: dict[str, Any] = Field(default_factory=dict)  # passed to mock responders


class LLMResponse(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    provider: str = ""
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    cached: bool = False
    fallback_used: bool = False


class BudgetExceeded(Exception):
    pass


class LLMUnavailable(Exception):
    pass


@dataclass
class Budget:
    """Spend cap shared by everything in one run. Safe under asyncio (single thread)."""
    max_usd: float
    max_calls: int = 400
    spent_usd: float = 0.0
    calls: int = 0

    def check(self) -> None:
        if self.spent_usd >= self.max_usd:
            raise BudgetExceeded(f"run budget ${self.max_usd:.2f} exhausted (spent ${self.spent_usd:.4f})")
        if self.calls >= self.max_calls:
            raise BudgetExceeded(f"run call cap {self.max_calls} reached")

    def charge(self, usd: float) -> None:
        self.spent_usd += usd
        self.calls += 1


@dataclass
class CallContext:
    tenant_id: str
    run_id: str | None = None
    node_key: str = ""
    budget: Budget | None = None
    cancel: asyncio.Event | None = None
    extra: dict[str, Any] = field(default_factory=dict)
