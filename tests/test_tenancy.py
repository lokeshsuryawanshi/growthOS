import asyncio
from datetime import date

import pytest
from sqlalchemy import func, select, text, update

from app.core.tenancy import TenantScopeError, system_scope, tenant_scope
from app.db.models import Lead, Tenant


async def _seed(db):
    async with db.session() as s:
        s.add_all([Tenant(id="ta", slug="a", name="A"), Tenant(id="tb", slug="b", name="B")])
    for tid in ("ta", "tb"):
        with tenant_scope(tid):
            async with db.session() as s:
                for i in range(3):
                    s.add(Lead(week_of=date(2026, 11, 2), source_id=f"{tid}{i}", dedupe_key=f"{tid}{i}",
                               full_name=f"P{i}", title="CFO", company=f"Co{tid}{i}"))


async def test_unscoped_query_hard_fails(db):
    await _seed(db)
    async with db.reader() as s:
        with pytest.raises(TenantScopeError):
            await s.execute(select(Lead))
        with pytest.raises(TenantScopeError):
            await s.execute(select(func.count()).select_from(Lead))
        with pytest.raises(TenantScopeError):
            await s.execute(select(Lead.id))


async def test_scoped_query_only_sees_own_rows(db):
    await _seed(db)
    with tenant_scope("ta"):
        async with db.reader() as s:
            rows = (await s.execute(select(Lead))).scalars().all()
            assert {r.tenant_id for r in rows} == {"ta"} and len(rows) == 3
            # an explicit filter for another tenant still returns nothing
            other = (await s.execute(select(Lead).where(Lead.tenant_id == "tb"))).scalars().all()
            assert other == []
            n = (await s.execute(select(func.count(Lead.id)))).scalar_one()
            assert n == 3
            # shapes the criteria can't filter are refused instead of leaking
            with pytest.raises(TenantScopeError):
                await s.execute(select(func.count()).select_from(Lead))


async def test_bulk_update_is_scoped(db):
    await _seed(db)
    with tenant_scope("ta"):
        async with db.session() as s:
            await s.execute(update(Lead).values(status="contacted"))
    with system_scope("test verification"):
        async with db.reader() as s:
            rows = (await s.execute(select(Lead.tenant_id, Lead.status))).all()
    assert all((st == "contacted") == (tid == "ta") for tid, st in rows)


async def test_cross_tenant_write_blocked(db):
    await _seed(db)
    with tenant_scope("ta"):
        with pytest.raises(TenantScopeError):
            async with db.session() as s:
                s.add(Lead(tenant_id="tb", week_of=date(2026, 11, 2), source_id="x", dedupe_key="x",
                           full_name="X", title="CEO", company="X"))
    with tenant_scope("ta"):
        with pytest.raises(TenantScopeError):
            async with db.session() as s:
                lead = (await s.execute(select(Lead))).scalars().first()
                lead.tenant_id = "tb"


async def test_unscoped_write_blocked(db):
    await _seed(db)
    with pytest.raises(TenantScopeError):
        async with db.session() as s:
            s.add(Lead(tenant_id="ta", week_of=date(2026, 11, 2), source_id="y", dedupe_key="y",
                       full_name="Y", title="CEO", company="Y"))


async def test_scope_propagates_into_concurrent_tasks(db):
    """contextvars must reach asyncio fan-out tasks and SQLAlchemy's greenlet bridge."""
    await _seed(db)

    async def count_for(tid):
        with tenant_scope(tid):
            await asyncio.sleep(0)
            async with db.reader() as s:
                rows = (await s.execute(select(Lead))).scalars().all()
                return {r.tenant_id for r in rows}

    results = await asyncio.gather(*[count_for(t) for t in ["ta", "tb"] * 10])
    assert results == [{"ta"}, {"tb"}] * 10


async def test_wal_pragmas_applied(db):
    if not db.url.startswith("sqlite"):
        pytest.skip("SQLite-only settings")
    async with db.reader() as s:
        mode = (await s.execute(text("PRAGMA journal_mode"))).scalar_one()
        timeout = (await s.execute(text("PRAGMA busy_timeout"))).scalar_one()
    assert mode == "wal" and timeout == 30000
