"""Provider adapters (Groq, OpenAI, Anthropic, and the offline mock). Each normalises errors into the classes the gateway
understands."""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.llm.types import LLMRequest, ToolCall


class RateLimited(Exception):
    def __init__(self, msg: str, retry_after: float | None = None, daily: bool = False):
        super().__init__(msg)
        self.retry_after = retry_after
        self.daily = daily      # a per-day quota (Groq TPD/RPD): waiting a minute cannot help, use another model


class TransientError(Exception):
    """Timeouts, 5xx, malformed tool calls: worth retrying."""


class FatalError(Exception):
    """Bad request, auth, unknown model: retrying the same provider won't help."""


@dataclass
class RawResponse:
    content: str | None
    tool_calls: list[ToolCall]
    prompt_tokens: int
    completion_tokens: int
    # Provider rate-limit state after this call, when the provider reports it (Groq headers).
    remaining_tokens: float | None = None
    reset_tokens_s: float | None = None


def retry_hint(message: str) -> float | None:
    """Groq puts the wait in the message: 'Please try again in 10.14s' / '450ms'; Gemini says 'retry in 32s'."""
    m = re.search(r"(?:try again|retry) in (?:(\d+)m)?([\d.]+)(ms|s)", message)
    if not m:
        return None
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return secs + 60 * int(m.group(1) or 0)


def parse_duration(text: str | None) -> float | None:
    """Groq reset headers: '600ms', '7.66s', '1m26.4s', '2h3m1s'."""
    if not text:
        return None
    total, found = 0.0, False
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", text):
        found = True
        total += float(num) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[unit]
    return total if found else None


def is_daily_limit(message: str) -> bool:
    """Groq names the exhausted window in the 429 text: 'tokens per day (TPD)' / 'requests per day (RPD)'."""
    m = message.lower()
    return "per day" in m or "(tpd)" in m or "(rpd)" in m


class TokenBucket:
    """Client-side pacing per model: tokens per minute and requests per minute, so we queue instead of collecting 429s.

    Debt-based: a request takes its tokens immediately and sleeps until the debt is repaid, so callers are served in
    arrival order without holding a lock while they sleep, and anyone can ask how long a new request would wait
    (`estimate_wait`), which is what the gateway uses to route around a congested model. The provider's own view,
    when it reports one (Groq's x-ratelimit headers), overrides ours via `sync`."""

    def __init__(self, tokens_per_minute: int, requests_per_minute: int | None = None):
        self.capacity = float(tokens_per_minute)
        self.tokens = float(tokens_per_minute)
        self.rate = tokens_per_minute / 60.0
        # Burst of half a minute's requests: Groq answered 429 when a full minute's worth arrived at once.
        self.req_capacity = max(1.0, (requests_per_minute or 0) / 2) if requests_per_minute else 0.0
        self.reqs = self.req_capacity
        self.req_rate = (requests_per_minute or 0) / 60.0
        self.blocked_until = 0.0          # set after a 429 that names a wait
        self.updated = time.monotonic()

    def _refill(self) -> float:
        now = time.monotonic()
        dt = now - self.updated
        self.tokens = min(self.capacity, self.tokens + dt * self.rate)
        if self.req_rate:
            self.reqs = min(self.req_capacity, self.reqs + dt * self.req_rate)
        self.updated = now
        return now

    def estimate_wait(self, amount: int) -> float:
        now = self._refill()
        amount = min(float(amount), self.capacity)
        wait = max(0.0, (amount - self.tokens) / self.rate)
        if self.req_rate:
            wait = max(wait, (1 - self.reqs) / self.req_rate)
        return max(wait, self.blocked_until - now)

    async def acquire(self, amount: int) -> float:
        """Reserve `amount` tokens and one request; returns the seconds spent waiting."""
        now = self._refill()
        amount = min(float(amount), self.capacity)
        self.tokens -= amount
        wait = max(0.0, -self.tokens / self.rate)
        if self.req_rate:
            self.reqs -= 1
            wait = max(wait, -self.reqs / self.req_rate)
        wait = max(wait, self.blocked_until - now)
        if wait > 0:
            await asyncio.sleep(wait)
        return wait

    def sync(self, remaining: float | None, reset_s: float | None) -> None:
        """Adopt the provider's count when it is lower than ours (it also counts work we did not pace)."""
        if remaining is None:
            return
        self._refill()
        if remaining < self.tokens:
            self.tokens = float(remaining)

    def block(self, seconds: float) -> None:
        self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)


class Provider:
    name = "base"

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Real adapters. Each is built per account key (never shared across accounts) and maps the provider's errors onto
# RateLimited / TransientError / FatalError, plus KeyRejected for a bad key (the run stops at once, no retries).
class KeyRejected(FatalError):
    """The provider refused the key itself (401/403) or the account behind it (no credit)."""

    def __init__(self, msg: str, reason: str = "rejected"):
        super().__init__(msg)
        self.reason = reason       # rejected | no_credit


def _openai_tools(req: LLMRequest) -> dict[str, Any]:
    if req.tools:
        return {"tools": [t.to_openai() for t in req.tools], "tool_choice": "auto"}
    return {"response_format": {"type": "json_object"}} if req.json_mode else {}


class GroqProvider(Provider):
    name = "groq"

    def __init__(self, api_key: str, http_client=None):
        from groq import AsyncGroq
        self.client = AsyncGroq(api_key=api_key, max_retries=0, timeout=90, http_client=http_client)

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        import groq

        kwargs: dict[str, Any] = dict(model=model, messages=req.messages, max_tokens=req.max_tokens)
        if req.tier != "guard":
            kwargs["temperature"] = req.temperature
        if model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = req.reasoning
        elif model.startswith("qwen/"):
            kwargs["reasoning_format"] = "hidden"  # keep thinking out of the JSON answer
        kwargs.update(_openai_tools(req))
        try:
            raw = await self.client.chat.completions.with_raw_response.create(**kwargs)
            r = await raw.parse()          # async client: parse() is a coroutine
        except (groq.AuthenticationError, groq.PermissionDeniedError) as e:
            raise KeyRejected(str(e)) from e
        except groq.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(str(e), float(ra) if ra else retry_hint(str(e)), daily=is_daily_limit(str(e))) from e
        except (groq.APIConnectionError, groq.APITimeoutError, groq.InternalServerError) as e:
            raise TransientError(str(e)) from e
        except groq.BadRequestError as e:
            # The model emitted an invalid tool call / JSON: a retry usually fixes it.
            if any(code in str(e) for code in ("tool_use_failed", "json_validate_failed", "output_parse_failed")):
                raise TransientError(str(e)) from e
            raise FatalError(str(e)) from e
        except groq.APIStatusError as e:
            if e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise FatalError(str(e)) from e      # includes 413: prompt + max_tokens above the model's per-minute limit
        msg = r.choices[0].message
        calls = [ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
                 for c in (msg.tool_calls or [])]
        u = r.usage
        rem = raw.headers.get("x-ratelimit-remaining-tokens")
        return RawResponse(msg.content, calls, u.prompt_tokens if u else 0, u.completion_tokens if u else 0,
                           remaining_tokens=float(rem) if rem else None,
                           reset_tokens_s=parse_duration(raw.headers.get("x-ratelimit-reset-tokens")))


class OpenAIProvider(Provider):
    """Chat Completions with JSON mode and tool calls. `base_url` lets the same code run against any
    OpenAI-compatible endpoint; it is exercised live against Groq's (https://api.groq.com/openai/v1)."""
    name = "openai"

    def __init__(self, api_key: str, base_url: str | None = None, http_client=None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0, timeout=90, http_client=http_client)

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        import openai

        # No temperature: current OpenAI reasoning models accept only the default. max_completion_tokens is the
        # current name of the output cap (it also bounds reasoning tokens).
        kwargs: dict[str, Any] = dict(model=model, messages=req.messages, max_completion_tokens=req.max_tokens)
        kwargs.update(_openai_tools(req))
        try:
            r = await self.client.chat.completions.create(**kwargs)
        except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
            raise KeyRejected(str(e)) from e
        except openai.RateLimitError as e:
            if getattr(e, "code", None) == "insufficient_quota" or "insufficient_quota" in str(e):   # billing, not speed
                raise KeyRejected(str(e), reason="no_credit") from e
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(str(e), float(ra) if ra else retry_hint(str(e))) from e
        except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as e:
            raise TransientError(str(e)) from e
        except openai.BadRequestError as e:
            if any(code in str(e) for code in ("tool_use_failed", "json_validate_failed")):
                raise TransientError(str(e)) from e
            raise FatalError(str(e)) from e
        except openai.APIStatusError as e:
            raise (TransientError if e.status_code >= 500 else FatalError)(str(e)) from e
        if not r.choices:
            raise TransientError("empty response")
        msg = r.choices[0].message
        calls = [ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
                 for c in (msg.tool_calls or []) if getattr(c, "function", None)]
        u = r.usage
        return RawResponse(msg.content, calls, u.prompt_tokens if u else 0, u.completion_tokens if u else 0)


def to_anthropic(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    """OpenAI-shaped history -> (system, Messages API messages). Tool calls become tool_use blocks; consecutive tool
    results are merged into one user message of tool_result blocks, which is what the Messages API expects."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system" and m.get("content"))
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m["role"]
        if role == "system":
            continue
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": m["tool_call_id"], "content": str(m.get("content") or "")}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) and \
                    all(b.get("type") == "tool_result" for b in out[-1]["content"]):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            for c in m["tool_calls"]:
                try:
                    args = json.loads(c["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                blocks.append({"type": "tool_use", "id": c["id"], "name": c["function"]["name"], "input": args})
            out.append({"role": "assistant", "content": blocks})
        else:
            out.append({"role": role, "content": str(m.get("content") or "") or "(empty)"})
    return system, out


class AnthropicProvider(Provider):
    """Messages API: system prompt as its own field, tool use blocks, max_tokens always set. No temperature (the API
    refuses sampling parameters) and no assistant prefill. Thinking is kept short: off where the model allows it."""
    name = "anthropic"

    def __init__(self, api_key: str, http_client=None):
        from anthropic import AsyncAnthropic
        self.client = AsyncAnthropic(api_key=api_key, max_retries=0, timeout=90, http_client=http_client)

    @staticmethod
    def _thinking(model: str) -> dict[str, Any]:
        # Sonnet 5.5 refuses {"type": "disabled"}; "between_tools" is its thinking-off mode. Haiku accepts disabled.
        return {"type": "between_tools"} if "sonnet" in model else {"type": "disabled"}

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        import anthropic

        system, messages = to_anthropic(req.messages)
        if req.json_mode and not req.tools:
            system += "\n\nReply with one JSON object only: no prose before or after it."
        kwargs: dict[str, Any] = dict(model=model, max_tokens=req.max_tokens, messages=messages,
                                      thinking=self._thinking(model),
                                      output_config={"effort": {"low": "low", "medium": "medium"}.get(req.reasoning, "high")})
        if system.strip():
            kwargs["system"] = system
        if req.tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters}
                               for t in req.tools]
            kwargs["tool_choice"] = {"type": "auto"}      # forced tool choice is refused by current models
        try:
            r = await self.client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise KeyRejected(str(e)) from e
        except anthropic.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(str(e), float(ra) if ra else None) from e
        except (anthropic.APIConnectionError, anthropic.APITimeoutError, anthropic.InternalServerError,
                anthropic.OverloadedError) as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 402 or getattr(e, "type", "") == "billing_error":
                raise KeyRejected(str(e), reason="no_credit") from e
            raise (TransientError if e.status_code >= 500 else FatalError)(str(e)) from e
        if r.stop_reason == "refusal":
            raise FatalError("the model declined this request")
        text = "".join(b.text for b in r.content if b.type == "text") or None
        calls = [ToolCall(id=b.id, name=b.name, arguments=json.dumps(b.input)) for b in r.content if b.type == "tool_use"]
        return RawResponse(text, calls, r.usage.input_tokens, r.usage.output_tokens)


def build_provider(provider: str, api_key: str, http_client=None) -> Provider:
    if provider == "groq":
        return GroqProvider(api_key, http_client=http_client)
    if provider == "openai":
        return OpenAIProvider(api_key, http_client=http_client)
    if provider == "anthropic":
        return AnthropicProvider(api_key, http_client=http_client)
    raise FatalError(f"unknown AI provider {provider!r}")


# ---------------------------------------------------------------------------
MockResponder = Callable[[LLMRequest], dict[str, Any]]
MOCK_RESPONDERS: dict[str, MockResponder] = {}


def register_mock(agent: str) -> Callable[[MockResponder], MockResponder]:
    def deco(fn: MockResponder) -> MockResponder:
        MOCK_RESPONDERS[agent] = fn
        return fn
    return deco


@dataclass
class MockConfig:
    latency_ms: tuple[int, int] = (0, 0)
    rate_limit_rate: float = 0.0     # fraction of calls answered with a 429
    transient_rate: float = 0.0      # fraction of calls failing with a 5xx
    seed: int | None = None
    calls: int = 0
    failures: dict[str, int] = field(default_factory=lambda: {"429": 0, "5xx": 0})


class MockProvider(Provider):
    """Deterministic offline provider. Agents register responders keyed by agent name.

    Latency and error injection make it usable for the stress test.
    """
    name = "mock"

    def __init__(self, config: MockConfig | None = None):
        self.config = config or MockConfig()
        self._rng = random.Random(self.config.seed)

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        cfg = self.config
        cfg.calls += 1
        lo, hi = cfg.latency_ms
        if hi:
            await asyncio.sleep(self._rng.uniform(lo, hi) / 1000)
        roll = self._rng.random()
        if roll < cfg.rate_limit_rate:
            cfg.failures["429"] += 1
            raise RateLimited("mock 429", retry_after=0.05)
        if roll < cfg.rate_limit_rate + cfg.transient_rate:
            cfg.failures["5xx"] += 1
            raise TransientError("mock 503")
        responder = MOCK_RESPONDERS.get(req.agent)
        if responder is None:
            raise FatalError(f"no mock responder registered for agent '{req.agent}'")
        out = responder(req)
        calls = [ToolCall(id=f"call_{uuid.uuid4().hex[:8]}", name=c["name"],
                          arguments=c["arguments"] if isinstance(c["arguments"], str) else json.dumps(c["arguments"]))
                 for c in out.get("tool_calls", [])]
        content = out.get("content")
        if content is not None and not isinstance(content, str):
            content = json.dumps(content)
        prompt_chars = sum(len(str(m.get("content") or "")) for m in req.messages)
        return RawResponse(content, calls, prompt_chars // 4 + 50, len(content or "") // 4 + 20 * len(calls) + 10)


class CircuitBreaker:
    """Opens after N consecutive failures; lets one trial call through after the cooldown."""

    def __init__(self, threshold: int, cooldown_s: float):
        self.threshold, self.cooldown_s = threshold, cooldown_s
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self.opened_at is None:
            return False
        if time.monotonic() - self.opened_at >= self.cooldown_s:
            return False  # half-open: allow a trial
        return True

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()
