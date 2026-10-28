"""Speed under free-tier limits: per-model pacing, spillover to a free model, request-size limits, daily quotas,
and an honest ledger of where the time went."""
import time

import pytest

from tests.helpers import bare_tenants, give_key
from sqlalchemy import select

from app.core.config import Settings
from app.core.tenancy import tenant_scope
from app.db.models import LLMCall, Tenant
from app.llm.gateway import LLMGateway
from app.llm.providers import Provider, RateLimited, RawResponse, TokenBucket, parse_duration
from app.llm.types import CallContext, LLMRequest, LLMUnavailable


class FakeProvider(Provider):
    """Answers instantly; can be told to fail per model. Records the key it was built with."""

    def __init__(self, name, key="", fail: dict | None = None):
        self.name, self.key, self.fail, self.calls = name, key, fail or {}, []

    async def complete(self, model, req):
        self.calls.append(model)
        if model in self.fail:
            raise self.fail[model]
        return RawResponse('{"ok": true}', [], 100, 20, remaining_tokens=None)


def live_gateway(**overrides) -> LLMGateway:
    s = Settings(_env_file=None, app_env="test", llm_mode="live", **overrides)
    built: list[FakeProvider] = []

    def factory(provider, key):
        built.append(FakeProvider(provider, key))
        return built[-1]
    gw = LLMGateway(settings=s, provider_factory=factory)
    gw.built = built
    return gw


def req(tier="big", chars=400, max_tokens=500):
    return LLMRequest(agent="t", tier=tier, messages=[{"role": "user", "content": "x" * chars}], max_tokens=max_tokens)


@pytest.fixture
async def tenant(db):
    await bare_tenants(db, "t1")
    await give_key("u-test", "groq")
    return "t1"


def test_each_provider_has_a_model_for_every_tier_and_the_critic_differs_from_the_writer():
    s = Settings(_env_file=None, app_env="test")
    for provider in ("groq", "openai", "anthropic"):
        for tier in ("big", "research", "small", "extract", "judge", "guard"):
            assert s.route_chain(provider, tier), (provider, tier)
        assert s.route_chain(provider, "judge")[0] != s.route_chain(provider, "big")[0], provider
    assert s.route_chain("groq", "big")[0] == "openai/gpt-oss-120b"
    assert s.route_chain("groq", "small")[0] == "openai/gpt-oss-20b"
    assert s.route_chain("groq", "judge")[0] == "qwen/qwen3.8-27b"      # a different family than the writer
    assert s.route_chain("anthropic", "big") == ["claude-sonnet-5-5"]


def test_chains_are_configurable_for_development(monkeypatch):
    monkeypatch.setenv("MODEL_ROUTES_JSON", '{"groq": {"big": ["qwen/qwen3.8-27b"]}}')
    s = Settings(_env_file=None, app_env="test")
    assert s.route_chain("groq", "big") == ["qwen/qwen3.8-27b"] and s.route_chain("groq", "small")[0] == "openai/gpt-oss-20b"


async def test_busy_primary_spills_to_a_free_model_in_the_same_tier(tenant):
    gw = live_gateway()
    lane = await gw.lane(tenant)
    chain, _ = gw.plan(lane, req("big"))
    assert chain[0] == "openai/gpt-oss-120b"                 # idle: primary first
    gw._bucket(lane, "openai/gpt-oss-120b").tokens = -16000   # two minutes of queued work on the primary
    chain, notes = gw.plan(lane, req("big"))
    assert chain[0] == "qwen/qwen3.8-27b" and any("spilled" in n for n in notes)


async def test_request_too_large_for_a_models_limit_skips_that_model(tenant):
    gw = live_gateway()
    lane = await gw.lane(tenant)
    big = req("extract", chars=60000, max_tokens=2000)        # ~17k tokens: above Groq's 8k per-minute limit
    chain, notes = gw.plan(lane, big)
    assert chain == [] and sum("exceeds" in n for n in notes) == 3


async def test_oversize_request_with_no_capable_model_fails_honestly(db, tenant):
    gw = live_gateway()
    with pytest.raises(LLMUnavailable, match="exceeds"):
        await gw.complete(req("big", chars=60000), CallContext(tenant_id=tenant))


async def test_daily_quota_moves_on_to_the_next_model_and_is_remembered(db, tenant):
    gw = live_gateway()
    lane = await gw.lane(tenant)
    lane.client.fail["openai/gpt-oss-120b"] = RateLimited("tokens per day (TPD) limit", retry_after=900, daily=True)
    resp = await gw.complete(req("big"), CallContext(tenant_id=tenant))
    assert resp.model == "qwen/qwen3.8-27b" and resp.fallback_used
    assert gw.exhausted_until[(lane.fingerprint, "openai/gpt-oss-120b")] > time.monotonic() + 800
    chain, notes = gw.plan(lane, req("big"))
    assert "openai/gpt-oss-120b" not in chain and any("daily quota" in n for n in notes)
    with tenant_scope(tenant):
        async with db.reader() as s:
            row = (await s.execute(select(LLMCall))).scalars().one()
    assert row.model == "qwen/qwen3.8-27b" and "rate limited" in row.route_note


async def test_pacing_wait_is_recorded_separately_from_latency(db, tenant, monkeypatch):
    monkeypatch.setenv("MODEL_ROUTES_JSON", '{"groq": {"big": ["openai/gpt-oss-120b"]}}')
    gw = live_gateway()
    lane = await gw.lane(tenant)
    gw.buckets[(lane.fingerprint, "openai/gpt-oss-120b")] = TokenBucket(tokens_per_minute=60000)   # 1000 tokens/s
    gw.buckets[(lane.fingerprint, "openai/gpt-oss-120b")].tokens = -400                            # ~0.4 s of debt
    await gw.complete(req("big", chars=400, max_tokens=100), CallContext(tenant_id=tenant))
    with tenant_scope(tenant):
        async with db.reader() as s:
            row = (await s.execute(select(LLMCall))).scalars().one()
    assert row.pace_wait_ms >= 300 and row.retry_wait_ms == 0


async def test_bucket_requests_per_minute_and_provider_sync():
    b = TokenBucket(tokens_per_minute=60000, requests_per_minute=120)   # 2 requests/s, burst of 60
    for _ in range(int(b.req_capacity)):
        await b.acquire(1)
    assert b.estimate_wait(1) > 0.3                                       # the request budget is spent
    b2 = TokenBucket(tokens_per_minute=8000)
    b2.sync(remaining=500, reset_s=4)                                     # the provider counted more than we did
    assert b2.estimate_wait(1000) > 3


def test_reset_header_durations():
    assert parse_duration("600ms") == pytest.approx(0.6)
    assert parse_duration("1m26.4s") == pytest.approx(86.4)
    assert parse_duration("7.66s") == pytest.approx(7.66)
    assert parse_duration(None) is None
