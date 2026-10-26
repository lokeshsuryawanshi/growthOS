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
    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


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


def retry_hint(message: str) -> float | None:
    """Groq puts the wait in the message: 'Please try again in 10.14s' / '450ms'."""
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)(ms|s)", message)
    if not m:
        return None
    secs = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return secs + 60 * int(m.group(1) or 0)


class TokenBucket:
    """Client-side tokens-per-minute pacing so we queue instead of collecting 429s."""

    def __init__(self, tokens_per_minute: int):
        self.capacity = float(tokens_per_minute)
        self.tokens = float(tokens_per_minute)
        self.rate = tokens_per_minute / 60.0
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    async def acquire(self, amount: int) -> float:
        amount = min(float(amount), self.capacity)
        waited = 0.0
        async with self.lock:  # FIFO: one waiter refills at a time
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= amount:
                    self.tokens -= amount
                    return waited
                need = (amount - self.tokens) / self.rate
                waited += need
                await asyncio.sleep(need)


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
            r = await self.client.chat.completions.create(**kwargs)
        except groq.RateLimitError as e:
            ra = e.response.headers.get("retry-after") if e.response is not None else None
            raise RateLimited(str(e), float(ra) if ra else retry_hint(str(e))) from e
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
            raise FatalError(str(e)) from e
        msg = r.choices[0].message
        calls = [ToolCall(id=c.id, name=c.function.name, arguments=c.function.arguments or "{}")
                 for c in (msg.tool_calls or [])]
        u = r.usage
        return RawResponse(msg.content, calls, u.prompt_tokens if u else 0, u.completion_tokens if u else 0)


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
                raise RateLimited(str(e)[:300]) from e
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
