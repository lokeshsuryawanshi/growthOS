"""Provider adapters. Each normalises errors into three classes the gateway understands."""
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
        self.req_capacity = float(requests_per_minute or 0)
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
class GroqProvider(Provider):
    name = "groq"

    def __init__(self, api_key: str):
        from groq import AsyncGroq
        self.client = AsyncGroq(api_key=api_key, max_retries=0, timeout=90)

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        import groq

        kwargs: dict[str, Any] = dict(model=model, messages=req.messages, max_tokens=req.max_tokens)
        if req.tier != "guard":
            kwargs["temperature"] = req.temperature
        if model.startswith("openai/gpt-oss"):
            kwargs["reasoning_effort"] = req.reasoning
        elif model.startswith("qwen/"):
            kwargs["reasoning_format"] = "hidden"  # keep thinking out of the JSON answer
        if req.tools:
            kwargs["tools"] = [t.to_openai() for t in req.tools]
            kwargs["tool_choice"] = "auto"
        elif req.json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            raw = await self.client.chat.completions.with_raw_response.create(**kwargs)
            r = raw.parse()
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


# ---------------------------------------------------------------------------
class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, api_key: str):
        from google import genai
        self.client = genai.Client(api_key=api_key)

    async def complete(self, model: str, req: LLMRequest) -> RawResponse:
        from google.genai import errors, types

        if req.tools:
            raise FatalError("gemini adapter is used for judging only; tools not supported here")
        system = "\n\n".join(m["content"] for m in req.messages if m["role"] == "system")
        contents = [types.Content(role="model" if m["role"] == "assistant" else "user",
                                  parts=[types.Part(text=m["content"] or "")])
                    for m in req.messages if m["role"] in ("user", "assistant")]
        config = types.GenerateContentConfig(
            system_instruction=system or None, temperature=req.temperature, max_output_tokens=req.max_tokens,
            response_mime_type="application/json" if req.json_mode else None)
        try:
            r = await asyncio.wait_for(self.client.aio.models.generate_content(
                model=model, contents=contents, config=config), timeout=60)
        except asyncio.TimeoutError as e:
            raise TransientError("gemini timeout") from e
        except errors.ClientError as e:
            if e.code == 429:
                raise RateLimited(str(e)[:300], retry_hint(str(e)), daily="perday" in str(e).lower().replace(" ", "")) from e
            raise FatalError(str(e)[:300]) from e
        except errors.ServerError as e:
            raise TransientError(str(e)[:300]) from e
        um = r.usage_metadata
        return RawResponse(r.text, [], (um.prompt_token_count or 0) if um else 0,
                           (um.candidates_token_count or 0) if um else 0)

    async def grounded_search(self, model: str, query: str) -> dict[str, Any]:
        """Google-Search-grounded answer with source URLs."""
        from google.genai import errors, types
        try:
            r = await asyncio.wait_for(self.client.aio.models.generate_content(
                model=model, contents=query,
                config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])),
                timeout=45)
        except asyncio.TimeoutError as e:
            raise TransientError("gemini search timeout") from e
        except errors.ClientError as e:
            if e.code == 429:
                raise RateLimited(str(e)[:300]) from e
            raise FatalError(str(e)[:300]) from e
        except errors.ServerError as e:
            raise TransientError(str(e)[:300]) from e
        gm = r.candidates[0].grounding_metadata if r.candidates else None
        sources = [{"title": c.web.title, "url": c.web.uri} for c in (gm.grounding_chunks or [])
                   if c.web] if gm else []
        return {"answer": r.text or "", "sources": sources}


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
