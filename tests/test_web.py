"""The UI over a seeded world: every page renders, decisions work, and another account's ids are 404."""
import re

import pytest
from sqlalchemy import select

from app.agents.orchestrator.engine import Orchestrator, set_orchestrator
from app.core.tenancy import tenant_scope
from app.db.models import OutreachMessage, Post
from app.fixtures.seed import seed
from app.llm.gateway import LLMGateway, set_gateway
from app.main import app
from tests.helpers import add_client, client_for, make_sendable, sign_in, weekly

OPERATOR = "operator@growthos.example"


@pytest.fixture
async def world(db):
    set_gateway(LLMGateway())
    set_orchestrator(orch := Orchestrator())
    ids = await seed(db)
    tid = ids["ledgerloop"]
    rid = await orch.create_run(tid, "discovery", {"start_date": "2026-11-02", "auto_approve_gates": True})
    await orch.execute(tid, rid)
    await weekly(orch, tid)
    async with client_for(app) as c:
        await sign_in(c, OPERATOR)
        yield c, ids, db


async def first(db, tid, model, *where):
    with tenant_scope(tid):
        async with db.reader() as s:
            return (await s.execute(select(model).where(*where))).scalars().first()


async def test_every_page_renders_with_data(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    for path in ["/clients", "/account", *[f"/portal/{tid}?tab={t}" for t in
                 ("overview", "content", "calendar", "leads", "research", "activity", "settings")]]:
        r = await client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
    run = re.search(r'/ui/[^/]+/runs/([0-9a-f]+)/progress', (await client.get(f"/portal/{tid}?tab=activity")).text)
    if run:
        assert (await client.get(f"/ui/{tid}/runs/{run.group(1)}/progress")).status_code == 200


async def test_another_account_gets_404_for_every_id(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    post = await first(db, tid, Post, Post.status == "pending_approval")
    msg = await first(db, tid, OutreachMessage)
    async with client_for(app) as other:
        await sign_in(other, "stranger@example.com")
        mine = await add_client(other)
        for path in (f"/portal/{tid}", f"/portal/{tid}?tab=leads", f"/ui/{tid}/runs/x/progress"):
            assert (await other.get(path)).status_code == 404, path
        for path, data in ((f"/ui/{tid}/posts/{post.id}", {"action": "approve"}), (f"/ui/{tid}/outreach/{msg.id}", {"action": "approve"}),
                           (f"/ui/{tid}/runs", {"kind": "discovery"}), (f"/ui/{tid}/settings", {}), (f"/ui/{tid}/delete", {"confirm": "LedgerLoop"}),
                           (f"/ui/{tid}/outreach/{msg.id}/sent", {}), (f"/ui/{tid}/posts/{post.id}/posted", {})):
            assert (await other.post(path, data=data)).status_code == 404, path
        # A's post id under B's own client path is not found either (ids are not global capabilities)
        assert (await other.post(f"/ui/{mine}/posts/{post.id}", data={"action": "approve"})).status_code == 404
    assert (await first(db, tid, Post, Post.id == post.id)).status == "pending_approval"     # nothing changed


async def test_unknown_ids_are_404_not_500(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    for path, data in ((f"/ui/{tid}/posts/nope", {"action": "approve"}), (f"/ui/{tid}/outreach/nope", {"action": "edit"}),
                       (f"/ui/{tid}/outreach/nope/sent", {}), (f"/ui/{tid}/posts/nope/posted", {}),
                       (f"/ui/{tid}/runs/nope/approve/strategy_review", {}), (f"/ui/{tid}/runs/nope/rerun/market", {})):
        assert (await client.post(path, data=data)).status_code == 404, path
    assert (await client.get(f"/ui/{tid}/runs/nope/progress")).status_code == 404
    assert (await client.post(f"/ui/{tid}/posts/x", data={"action": "explode"})).status_code == 400


async def test_post_approval_and_guardrail_on_a_leaky_edit(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    post = await first(db, tid, Post, Post.status == "pending_approval")
    r = await client.post(f"/ui/{tid}/posts/{post.id}", data={"action": "edit", "text": post.text + "\nCall me on 555-201-0199."})
    assert r.status_code == 200 and "Not saved" in r.text
    r = await client.post(f"/ui/{tid}/posts/{post.id}", data={"action": "approve"})
    assert r.status_code == 200 and "Copy post" in r.text
    r = await client.post(f"/ui/{tid}/posts/{post.id}/posted")
    assert "Marked as posted" in r.text
    assert (await first(db, tid, Post, Post.id == post.id)).status == "published"


async def test_blocked_drafts_are_never_shown(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    blocked = await first(db, tid, Post, Post.status == "blocked")
    if blocked:
        assert blocked.text[:60] not in (await client.get(f"/portal/{tid}?tab=content")).text


async def test_outreach_buttons_follow_approval(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    await make_sendable(db, tid)
    msg = await first(db, tid, OutreachMessage, OutreachMessage.status == "pending_approval")
    page = (await client.get(f"/portal/{tid}?tab=leads")).text
    card = page[page.index(f'id="dm-{msg.id}"'):]
    card = card[:card.index("</article>")]
    assert "Open in Gmail" in card and "Copy email" in card and 'aria-disabled="true"' in card and "mail.google.com" not in card
    r = await client.post(f"/ui/{tid}/outreach/{msg.id}", data={"action": "approve"})
    assert "https://mail.google.com/mail/?view=cm&amp;fs=1" in r.text and "data-copy=" in r.text and "I sent it" in r.text
    r = await client.post(f"/ui/{tid}/outreach/{msg.id}/sent")
    assert "Marked as sent" in r.text
    assert (await first(db, tid, OutreachMessage, OutreachMessage.id == msg.id)).status == "sent"


async def test_every_calendar_card_opens_its_post_or_its_plan(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    page = (await client.get(f"/portal/{tid}?tab=calendar")).text
    links = re.findall(rf'<a class="slot link" href="(/portal/{tid}/(?:posts|slots)/[0-9a-f]+)"', page)
    from app.db.models import CalendarSlot
    with tenant_scope(tid):
        async with db.reader() as s:
            n_slots = len((await s.execute(select(CalendarSlot))).scalars().all())
    assert len(links) == n_slots >= 40                                     # every slot on the 8-week calendar is a link
    posts = [u for u in links if "/posts/" in u]
    planned = [u for u in links if "/slots/" in u]
    assert posts and planned
    for url in posts:
        r = await client.get(url)
        assert r.status_code == 200 and "Back to the calendar" in r.text and 'class="li"' in r.text, url
        assert "Why this post" in r.text and "Editor's check (automatic)" in r.text and "Your rating" in r.text
    r = await client.get(planned[0])
    assert r.status_code == 200 and "planned, not written yet" in r.text and "Brief" in r.text
    # a slot that has a post redirects to the post
    post_slot = (await first(db, tid, Post, Post.status == "pending_approval")).slot_id
    r = await client.get(f"/portal/{tid}/slots/{post_slot}")
    assert r.status_code == 303 and "/posts/" in r.headers["location"]


async def test_the_post_page_takes_decisions_in_place(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    post = await first(db, tid, Post, Post.status == "pending_approval")
    page = (await client.get(f"/portal/{tid}/posts/{post.id}")).text
    assert f'hx-post="/ui/{tid}/posts/{post.id}"' in page and "Approve" in page and "Edit the text" in page
    r = await client.post(f"/ui/{tid}/posts/{post.id}", data={"action": "approve"})
    assert "Copy post" in r.text and "I posted it" in r.text


async def test_posts_and_slots_of_another_account_are_404(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    post = await first(db, tid, Post, Post.status == "pending_approval")
    async with client_for(app) as other:
        await sign_in(other, "stranger2@example.com")
        mine = await add_client(other)
        for url in (f"/portal/{tid}/posts/{post.id}", f"/portal/{tid}/slots/{post.slot_id}",
                    f"/portal/{mine}/posts/{post.id}", f"/portal/{mine}/slots/{post.slot_id}"):
            assert (await other.get(url)).status_code == 404, url
    assert (await client.get(f"/portal/{tid}/posts/nope")).status_code == 404
    assert (await client.get(f"/portal/{tid}/slots/nope")).status_code == 404


async def test_the_operator_rates_a_draft_and_the_rating_is_stored(world):
    client, ids, db = world
    tid = ids["ledgerloop"]
    post = await first(db, tid, Post, Post.status == "pending_approval")
    r = await client.post(f"/ui/{tid}/posts/{post.id}/rating", data={"voice": "2", "template": "yes"})
    assert r.status_code == 200 and "voice 2/5" in r.text and "reads like a template" in r.text
    p = await first(db, tid, Post, Post.id == post.id)
    assert (p.rating_voice, p.rating_would_post, p.rating_template) == (2, False, True) and p.rated_at
    assert "Thanks" not in (await client.post(f"/ui/{tid}/posts/{post.id}/rating", data={"voice": "9"})).text
