"""The single door to every model. Agents never hold a provider client.

Per call: kill-switch + budget checks -> cache lookup -> route by tier through
a fallback chain (circuit breaker, concurrency cap, retry with backoff that
honours retry-after) -> cost accounting -> ledger row.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.core.config import MODEL_PRICES, MODEL_TPM, Settings, get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import incident
from app.db.models import LLMCache, LLMCall, Tenant
from app.db.session import get_db
from app.llm.providers import (
    CircuitBreaker, FatalError, GeminiProvider, GroqProvider, MockConfig, MockProvider, Provider,
    RateLimited, TokenBucket, TransientError,
)
from app.llm.types import BudgetExceeded, CallContext, LLMRequest, LLMResponse, LLMUnavailable, Tier


class TenantPaused(Exception):
    pass


def price(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pin, pout = MODEL_PRICES.get(model, (0.5, 1.5))  # unknown model: assume pricey
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000


@dataclass
class _TenantState:
    month: str
    spent_usd: float
    budget_usd: float
    kill_switch: bool
    fetched_at: float
    alerted: bool = False


class LLMGateway:
    def __init__(self, settings: Settings | None = None, mock_config: MockConfig | None = None):
        self.s = settings or get_settings()
        self.mode = self.s.effective_llm_mode
        self.providers: dict[str, Provider] = {}
        if self.mode == "mock":
            self.providers["mock"] = MockProvider(mock_config)
        else:
            self.providers["groq"] = GroqProvider(self.s.groq_api_key)
            if self.s.google_api_key:
                self.providers["gemini"] = GeminiProvider(self.s.google_api_key)
        self.breakers = {n: CircuitBreaker(self.s.circuit_failure_threshold, self.s.circuit_cooldown_s)
                         for n in self.providers}
        self.semaphores = {n: asyncio.Semaphore(self.s.provider_max_concurrency) for n in self.providers}
        self.buckets: dict[str, TokenBucket] = {m: TokenBucket(tpm) for m, tpm in MODEL_TPM.items()}
        self._tenants: dict[str, _TenantState] = {}
        self._tenant_lock = asyncio.Lock()

    # -- routing ---------------------------------------------------------------
    def route(self, tier: Tier) -> list[tuple[str, str]]:
        if self.mode == "mock":
            return [("mock", f"mock-{tier}")]
        chains: dict[str, list[tuple[str, str]]] = {
            "big": [("groq", self.s.groq_model)],
            "small": [("groq", self.s.small_model), ("groq", self.s.groq_model)],
            "judge": [("gemini", self.s.judge_model), ("groq", self.s.judge_fallback_model)],
            "guard": [("groq", self.s.guard_model)],
        }
        return [(p, m) for p, m in chains[tier] if p in self.providers]

    # -- tenant budget / kill switch ----------------------------------------------
    async def _tenant_state(self, tenant_id: str) -> _TenantState:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        st = self._tenants.get(tenant_id)
        if st and st.month == month and time.monotonic() - st.fetched_at < 15:
            return st
        async with self._tenant_lock:
            month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            async with get_db().reader() as s:
                tenant = await s.get(Tenant, tenant_id)
                with tenant_scope(tenant_id):
                    spent = (await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                             .where(LLMCall.created_at >= month_start))).scalar_one()
            if tenant is None:
                raise TenantPaused(f"unknown tenant {tenant_id}")
            alerted = bool(st and st.month == month and st.alerted)
            st = _TenantState(month, float(spent), tenant.monthly_budget_usd, tenant.kill_switch, time.monotonic(), alerted)
            self._tenants[tenant_id] = st
            return st

    async def _preflight(self, ctx: CallContext) -> None:
        if ctx.cancel is not None and ctx.cancel.is_set():
            raise BudgetExceeded("run cancelled")
        st = await self._tenant_state(ctx.tenant_id)
        if st.kill_switch:
            raise TenantPaused("tenant kill switch is on")
        if st.spent_usd >= st.budget_usd:
            raise BudgetExceeded(f"tenant monthly budget ${st.budget_usd:.2f} exhausted")
        if ctx.budget is not None:
            ctx.budget.check()

    async def _account(self, ctx: CallContext, cost: float) -> None:
        if ctx.budget is not None:
            ctx.budget.charge(cost)
        st = self._tenants.get(ctx.tenant_id)
        if st is None:
            return
        st.spent_usd += cost
        if not st.alerted and st.spent_usd >= st.budget_usd * self.s.budget_alert_ratio:
            st.alerted = True
            with tenant_scope(ctx.tenant_id):
                async with get_db().session() as s:
                    incident(s, "cost", "budget_80pct", "medium",
                             f"monthly LLM spend ${st.spent_usd:.2f} of ${st.budget_usd:.2f}", run_id=ctx.run_id)

    # -- cache -------------------------------------------------------------------
    @staticmethod
    def _cache_key(req: LLMRequest) -> str:
        blob = json.dumps({"tier": req.tier, "agent": req.agent, "m": req.messages,
                           "t": [t.model_dump() for t in req.tools or []], "j": req.json_mode},
                          sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    async def _cache_get(self, tenant_id: str, key: str) -> LLMResponse | None:
        with tenant_scope(tenant_id):
            async with get_db().reader() as s:
                row = (await s.execute(select(LLMCache).where(LLMCache.key == key))).scalars().first()
        return LLMResponse(**row.response) if row else None

    async def _cache_put(self, tenant_id: str, key: str, resp: LLMResponse) -> None:
        with tenant_scope(tenant_id):
            async with get_db().session() as s:
                exists = (await s.execute(select(LLMCache.id).where(LLMCache.key == key))).first()
                if not exists:
                    s.add(LLMCache(key=key, response=resp.model_dump()))

    # -- ledger --------------------------------------------------------------------
    async def _ledger(self, ctx: CallContext, req: LLMRequest, resp: LLMResponse | None, status: str,
                      error: str = "", model: str = "") -> None:
        with tenant_scope(ctx.tenant_id):
            async with get_db().session() as s:
                s.add(LLMCall(
                    run_id=ctx.run_id, node_key=ctx.node_key, agent=req.agent, tier=req.tier,
                    model=resp.model if resp else model, prompt_tokens=resp.prompt_tokens if resp else 0,
                    completion_tokens=resp.completion_tokens if resp else 0, cost_usd=resp.cost_usd if resp else 0.0,
                    latency_ms=resp.latency_ms if resp else 0, retries=resp.retries if resp else 0,
                    cached=resp.cached if resp else False, status=status, error=error[:500]))

    # -- the call ------------------------------------------------------------------
    async def complete(self, req: LLMRequest, ctx: CallContext) -> LLMResponse:
        await self._preflight(ctx)

        key = self._cache_key(req) if req.cacheable else None
        if key:
            hit = await self._cache_get(ctx.tenant_id, key)
            if hit:
                hit.cached, hit.cost_usd, hit.latency_ms, hit.retries = True, 0.0, 0, 0
                await self._ledger(ctx, req, hit, "ok")
                return hit

        chain = self.route(req.tier)
        if not chain:
            raise LLMUnavailable(f"no provider configured for tier {req.tier}")
        errors: list[str] = []
        for idx, (pname, model) in enumerate(chain):
            breaker = self.breakers[pname]
            if breaker.is_open:
                errors.append(f"{pname}: circuit open")
                continue
            try:
                resp = await self._call_with_retries(pname, model, req)
            except FatalError as e:
                breaker.record_failure()
                errors.append(f"{pname}/{model}: {e}")
                continue
            except (RateLimited, TransientError) as e:
                breaker.record_failure()
                errors.append(f"{pname}/{model}: retries exhausted: {e}")
                continue
            breaker.record_success()
            resp.fallback_used = idx > 0
            await self._account(ctx, resp.cost_usd)
            await self._ledger(ctx, req, resp, "fallback" if idx else "ok")
            if key:
                await self._cache_put(ctx.tenant_id, key, resp)
            return resp

        msg = " | ".join(errors)
        await self._ledger(ctx, req, None, "error", msg, model=chain[0][1])
        raise LLMUnavailable(msg)

    @staticmethod
    def _estimate_tokens(req: LLMRequest) -> int:
        # Providers reserve prompt + max_tokens against the per-minute budget.
        chars = sum(len(str(m.get("content") or "")) for m in req.messages)
        chars += sum(len(str(t.parameters)) for t in req.tools or [])
        return chars // 4 + req.max_tokens

    async def _call_with_retries(self, pname: str, model: str, req: LLMRequest) -> LLMResponse:
        provider = self.providers[pname]
        retries = rate_limited = 0
        bucket = self.buckets.get(model)
        while True:
            if bucket is not None:
                await bucket.acquire(self._estimate_tokens(req))
            t0 = time.monotonic()
            try:
                async with self.semaphores[pname]:
                    raw = await provider.complete(model, req)
                return LLMResponse(
                    content=raw.content, tool_calls=raw.tool_calls, provider=pname, model=model,
                    prompt_tokens=raw.prompt_tokens, completion_tokens=raw.completion_tokens,
                    cost_usd=price(model, raw.prompt_tokens, raw.completion_tokens),
                    latency_ms=int((time.monotonic() - t0) * 1000), retries=retries)
            except RateLimited as e:
                # Expected under load: wait as long as the provider asks (plus jitter), within limits.
                if rate_limited >= self.s.llm_rate_limit_retries:
                    raise
                rate_limited += 1
                retries += 1
                wait = e.retry_after if e.retry_after else min(20.0, 0.5 * 2 ** rate_limited)
                await asyncio.sleep(min(self.s.llm_rate_limit_max_wait_s, wait) * random.uniform(1.0, 1.2))
            except TransientError:
                if retries - rate_limited >= self.s.llm_max_retries:
                    raise
                retries += 1
                await asyncio.sleep(min(20.0, 0.5 * 2 ** (retries - rate_limited)) * random.uniform(0.7, 1.3))


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def set_gateway(gw: LLMGateway) -> None:
    global _gateway
    _gateway = gw
