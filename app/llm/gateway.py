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

from app.core.config import MODEL_PRICES, Settings, get_settings
from app.core.tenancy import tenant_scope
from app.db.audit import incident
from app.db.models import LLMCache, LLMCall, Tenant
from app.db.session import get_db
from app.llm.providers import (
    CircuitBreaker, FatalError, GeminiProvider, GroqProvider, MockConfig, MockProvider, Provider,
    RateLimited, TokenBucket, TransientError,
)
from app.integrations.errors import NOT_CONFIGURED, IntegrationError
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
            self.providers["mock"] = MockProvider(mock_config)   # settings only allow this outside production
        elif self.mode == "live":
            self.providers["groq"] = GroqProvider(self.s.groq_api_key)
            if self.s.google_api_key:
                self.providers["gemini"] = GeminiProvider(self.s.google_api_key)
        # mode "unconfigured": no providers. Every call fails with a clear error instead of faking output.
        self.breakers = {n: CircuitBreaker(self.s.circuit_failure_threshold, self.s.circuit_cooldown_s)
                         for n in self.providers}
        self.semaphores = {n: asyncio.Semaphore(self.s.provider_max_concurrency) for n in self.providers}
        self.limits = self.s.model_limits()
        self.buckets: dict[str, TokenBucket] = {m: TokenBucket(v["tpm"], v.get("rpm")) for m, v in self.limits.items()}
        self.exhausted_until: dict[str, float] = {}             # model -> monotonic time its daily quota frees up
        self._tenants: dict[str, _TenantState] = {}
        self._tenant_lock = asyncio.Lock()
        self._global: tuple[str, float, float] | None = None   # (utc day, spend, fetched_at)

    # -- routing ---------------------------------------------------------------
    def route(self, tier: Tier) -> list[tuple[str, str]]:
        if self.mode == "mock":
            return [("mock", f"mock-{tier}")]
        return [(p, m) for p, m in self.s.route_chain(tier) if p in self.providers]

    def plan(self, req: LLMRequest) -> tuple[list[tuple[str, str]], list[str]]:
        """Order the tier's chain for this request.

        Limits are per model, so a busy primary should not make a call wait while another model in the same chain is
        free. Candidates the request is too large for, or whose daily quota is used up, are dropped with a note. The
        first candidate stays first unless its expected wait exceeds route_spill_wait_s and another one is sooner."""
        need = self._estimate_tokens(req)
        now = time.monotonic()
        usable, notes = [], []
        for pname, model in self.route(req.tier):
            cap = self.limits.get(model, {}).get("max_request")
            if cap and need > cap:
                notes.append(f"{model}: request of ~{need} tokens exceeds its {cap}-token limit")
                continue
            if self.exhausted_until.get(model, 0) > now:
                notes.append(f"{model}: daily quota used up")
                continue
            usable.append((pname, model))
        if len(usable) > 1:
            waits = {m: (self.buckets[m].estimate_wait(need) if m in self.buckets else 0.0) for _, m in usable}
            first = usable[0][1]
            if waits[first] > self.s.route_spill_wait_s:
                best = min(usable, key=lambda c: waits[c[1]])
                if waits[best[1]] + 1.0 < waits[first]:
                    usable.remove(best)
                    usable.insert(0, best)
                    notes.append(f"spilled from {first} (~{waits[first]:.0f}s wait) to {best[1]}")
        return usable, notes

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

    async def _global_spend(self) -> float:
        """Service-wide spend today (UTC), so one open signup page cannot drain the provider account."""
        from app.core.tenancy import system_scope
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._global and self._global[0] == day and time.monotonic() - self._global[2] < 10:
            return self._global[1]
        start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with system_scope("gateway: service-wide daily budget"):
            async with get_db().reader() as s:
                spent = float((await s.execute(select(func.coalesce(func.sum(LLMCall.cost_usd), 0.0))
                                               .where(LLMCall.created_at >= start))).scalar_one())
        self._global = (day, spent, time.monotonic())
        return spent

    def require_configured(self) -> None:
        if self.mode == "unconfigured":
            raise IntegrationError("ai", NOT_CONFIGURED, "No AI provider is configured on this server",
                                   "Set GROQ_API_KEY in the server environment (free key: console.groq.com) and restart.")

    async def _preflight(self, ctx: CallContext) -> None:
        self.require_configured()
        if ctx.cancel is not None and ctx.cancel.is_set():
            raise BudgetExceeded("run cancelled")
        if self.mode == "live" and await self._global_spend() >= self.s.global_daily_budget_usd:
            raise BudgetExceeded(f"service-wide daily AI budget ${self.s.global_daily_budget_usd:.2f} reached; try again tomorrow")
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
                      error: str = "", model: str = "", waits: tuple[int, int] = (0, 0)) -> None:
        with tenant_scope(ctx.tenant_id):
            async with get_db().session() as s:
                s.add(LLMCall(
                    run_id=ctx.run_id, node_key=ctx.node_key, agent=req.agent, tier=req.tier,
                    model=resp.model if resp else model, prompt_tokens=resp.prompt_tokens if resp else 0,
                    completion_tokens=resp.completion_tokens if resp else 0, cost_usd=resp.cost_usd if resp else 0.0,
                    latency_ms=resp.latency_ms if resp else 0, retries=resp.retries if resp else 0,
                    pace_wait_ms=resp.pace_wait_ms if resp else waits[0],
                    retry_wait_ms=resp.retry_wait_ms if resp else waits[1],
                    route_note=(resp.route_note if resp else "")[:300],
                    cached=resp.cached if resp else False, status=status, error=error[:500]))

    # -- the call ------------------------------------------------------------------
    async def complete(self, req: LLMRequest, ctx: CallContext) -> LLMResponse:
        await self._preflight(ctx)

        key = self._cache_key(req) if req.cacheable else None
        if key:
            hit = await self._cache_get(ctx.tenant_id, key)
            if hit:
                hit.cached, hit.cost_usd, hit.latency_ms, hit.retries = True, 0.0, 0, 0
                hit.pace_wait_ms = hit.retry_wait_ms = 0
                await self._ledger(ctx, req, hit, "ok")
                return hit

        chain, notes = self.plan(req)
        if not chain:
            msg = "; ".join(notes) or f"no provider configured for tier {req.tier}"
            await self._ledger(ctx, req, None, "error", msg, model=(self.route(req.tier) or [("", "")])[0][1])
            raise LLMUnavailable(msg)
        errors: list[str] = []
        waited = [0.0, 0.0]          # pacing, retry sleeps: across every model tried for this call
        for idx, (pname, model) in enumerate(chain):
            breaker = self.breakers[pname]
            if breaker.is_open:
                errors.append(f"{pname}: circuit open")
                continue
            try:
                resp = await self._call_with_retries(pname, model, req, waited, last=idx == len(chain) - 1)
            except FatalError as e:
                breaker.record_failure()
                errors.append(f"{pname}/{model}: {e}")
                continue
            except RateLimited as e:
                if not e.daily:                      # a used-up daily quota says nothing about the provider's health
                    breaker.record_failure()
                errors.append(f"{pname}/{model}: rate limited: {str(e)[:200]}")
                continue
            except TransientError as e:
                breaker.record_failure()
                errors.append(f"{pname}/{model}: retries exhausted: {e}")
                continue
            breaker.record_success()
            resp.fallback_used = idx > 0
            resp.pace_wait_ms, resp.retry_wait_ms = int(waited[0] * 1000), int(waited[1] * 1000)
            resp.route_note = "; ".join(notes + errors)
            await self._account(ctx, resp.cost_usd)
            await self._ledger(ctx, req, resp, "fallback" if (idx or errors) else "ok")
            if key:
                await self._cache_put(ctx.tenant_id, key, resp)
            return resp

        msg = " | ".join(notes + errors)
        await self._ledger(ctx, req, None, "error", msg, model=chain[0][1],
                           waits=(int(waited[0] * 1000), int(waited[1] * 1000)))
        raise LLMUnavailable(msg)

    @staticmethod
    def _estimate_tokens(req: LLMRequest) -> int:
        # Providers reserve prompt + max_tokens against the per-minute budget.
        chars = sum(len(str(m.get("content") or "")) for m in req.messages)
        chars += sum(len(str(t.parameters)) for t in req.tools or [])
        return chars // 4 + req.max_tokens

    async def _call_with_retries(self, pname: str, model: str, req: LLMRequest, waited: list[float],
                                 last: bool = True) -> LLMResponse:
        """Call one model. 429s are waited out only when no other model in the chain could take the call sooner;
        otherwise the model is marked busy (or exhausted for the day) and the caller moves on."""
        provider = self.providers[pname]
        retries = rate_limited = 0
        bucket = self.buckets.get(model)
        while True:
            if bucket is not None:
                waited[0] += await bucket.acquire(self._estimate_tokens(req))
            t0 = time.monotonic()
            try:
                async with self.semaphores[pname]:
                    raw = await provider.complete(model, req)
                if bucket is not None:
                    bucket.sync(raw.remaining_tokens, raw.reset_tokens_s)
                return LLMResponse(
                    content=raw.content, tool_calls=raw.tool_calls, provider=pname, model=model,
                    prompt_tokens=raw.prompt_tokens, completion_tokens=raw.completion_tokens,
                    cost_usd=price(model, raw.prompt_tokens, raw.completion_tokens),
                    latency_ms=int((time.monotonic() - t0) * 1000), retries=retries)
            except RateLimited as e:
                if e.daily:
                    self.exhausted_until[model] = time.monotonic() + (e.retry_after or 3600)
                    raise
                wait = e.retry_after if e.retry_after else min(20.0, 0.5 * 2 ** (rate_limited + 1))
                if bucket is not None:
                    bucket.block(wait)
                if rate_limited >= self.s.llm_rate_limit_retries or (not last and wait > self.s.route_spill_wait_s):
                    raise
                rate_limited += 1
                retries += 1
                pause = min(self.s.llm_rate_limit_max_wait_s, wait) * random.uniform(1.0, 1.2)
                waited[1] += pause
                await asyncio.sleep(pause)
            except TransientError:
                if retries - rate_limited >= self.s.llm_max_retries:
                    raise
                retries += 1
                pause = min(20.0, 0.5 * 2 ** (retries - rate_limited)) * random.uniform(0.7, 1.3)
                waited[1] += pause
                await asyncio.sleep(pause)


_gateway: LLMGateway | None = None


def get_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
    return _gateway


def set_gateway(gw: LLMGateway) -> None:
    global _gateway
    _gateway = gw
