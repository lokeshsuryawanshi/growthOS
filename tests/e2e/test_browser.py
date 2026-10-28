"""A real browser (Chromium via Playwright) through the operator's whole journey, on a local server with mock
models: sign in, add a client, save a key, run research, open the calendar, click a card, approve a post, approve an
email and use Open in Gmail and Copy (marking it sent is refused: mock leads are synthetic), delete the client.

  .venv/bin/python -m pytest -m e2e tests/e2e         # needs: pip install playwright && playwright install chromium
"""
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e
ROOT = Path(__file__).resolve().parents[2]
FORM = {"company_name": "Brightwave Analytics", "industry": "B2B SaaS: analytics for retailers",
        "company": "Brightwave turns point-of-sale data into weekly demand forecasts for independent retailers.",
        "target_customers": "Owners and operations leads at independent retailers with 5 to 50 stores.",
        "founder_name": "Alex Rivera",
        "sample_1": "Retail forecasting is not about the algorithm. It is about knowing which Tuesday matters. We learned that the hard way.",
        "sample_2": "Independent shops do not need dashboards. They need one number on Monday morning that tells them what to order."}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    db = tmp_path_factory.mktemp("e2e") / "e2e.db"
    proc = subprocess.Popen([sys.executable, str(ROOT / "tests/e2e/server.py"), str(port), str(db)], cwd=ROOT,
                            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(base + "/healthz", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.fail(proc.stdout.read() if proc.stdout else "server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


async def test_the_operator_journey_in_a_real_browser(server, tmp_path):
    from playwright.async_api import async_playwright
    errors: list[str] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(viewport={"width": 390, "height": 844})        # a phone-width screen
        await ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin=server)
        page = await ctx.new_page()
        page.on("response", lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 500 else None)

        await page.goto(server + "/login")
        await page.get_by_text("Test sign-in").click()
        await page.fill("input[name=email]", "e2e@example.com")
        await page.get_by_role("button", name="Sign in without Google").click()
        await page.wait_for_url(re.compile(r"/clients$"))
        assert await page.get_by_text("No clients yet").is_visible()

        await page.get_by_role("link", name="Add client").first.click()
        for name, value in FORM.items():                                           # the stepper shows one step at a time
            field = page.locator(f"[name={name}]")
            while not await field.is_visible():
                await page.locator("#nextbtn").click()
            await field.fill(value)
        while not await page.locator("#submitbtn").is_visible():
            await page.locator("#nextbtn").click()
        await page.locator("#submitbtn").click()
        await page.wait_for_url(re.compile(r"/portal/[0-9a-f]+$"))
        tid = page.url.rsplit("/", 1)[1]

        await page.goto(server + "/account")
        await page.locator("form[action='/account/keys']:has(input[value=llm]) input[name=key]").fill("gsk_" + "f" * 52)
        await page.locator("form[action='/account/keys']:has(input[value=llm]) button").click()
        assert await page.get_by_text("checked and saved").is_visible()
        assert "f" * 52 not in await page.content()                                 # never shown again

        await page.goto(f"{server}/portal/{tid}")
        await page.get_by_role("button", name="Start research").click()
        for _ in range(60):                                                          # mock models: seconds
            await page.goto(f"{server}/portal/{tid}?tab=research")
            if await page.get_by_role("button", name="Approve strategy").count():
                break
            time.sleep(0.5)
        else:
            await page.goto(f"{server}/portal/{tid}?tab=activity")
            pytest.fail("research did not reach the approval step: " + (await page.inner_text("main"))[-1500:])
        await page.get_by_role("button", name="Approve strategy").click()
        await page.goto(f"{server}/portal/{tid}?tab=overview")
        await page.get_by_role("button", name="Write this week's posts").first.click()
        for _ in range(60):
            await page.goto(f"{server}/portal/{tid}?tab=calendar")
            if await page.locator("a.slot.link[href*='/posts/']").count():
                break
            time.sleep(0.5)
        await page.locator("a.slot.link[href*='/posts/']").first.click()               # a calendar card opens its post
        await page.wait_for_url(re.compile(r"/posts/[0-9a-f]+$"))
        assert await page.get_by_text("Back to the calendar").is_visible()
        await page.get_by_role("button", name="Approve").first.click()
        await page.get_by_role("button", name="Copy post").wait_for()

        await page.goto(f"{server}/portal/{tid}?tab=overview")
        await page.get_by_role("button", name="Find leads").first.click()
        for _ in range(60):
            await page.goto(f"{server}/portal/{tid}?tab=leads")
            if await page.locator("article[id^=dm-]").count():
                break
            time.sleep(0.5)
        card = page.locator("article[id^=dm-]").first
        assert await card.get_by_text("Open in Gmail").is_visible() and await card.get_by_text("Copy email").is_visible()
        await card.get_by_role("button", name="Approve").click()
        card = page.locator("article[id^=dm-]").first
        await card.locator("a[data-gmail]").wait_for()
        href = await card.locator("a[data-gmail]").get_attribute("href")
        assert href.startswith("https://mail.google.com/mail/?view=cm&fs=1")
        async with ctx.expect_page() as popup:                                       # opens in a new tab
            await page.route("https://mail.google.com/**", lambda r: r.fulfill(status=200, body="gmail"))
            await ctx.route("https://mail.google.com/**", lambda r: r.fulfill(status=200, body="gmail"))
            await card.locator("a[data-gmail]").click()
        assert (await popup.value).url.startswith("https://mail.google.com/mail/")
        await card.get_by_role("button", name="Copy email").click()
        await card.get_by_role("button", name="Copied").wait_for()
        assert (await page.evaluate("navigator.clipboard.readText()")).startswith("Subject: ")
        await card.get_by_role("button", name="I sent it").click()
        await page.get_by_text("synthetic demo data and must never be contacted").wait_for()   # mock leads are fake

        await page.goto(f"{server}/portal/{tid}?tab=settings")
        await page.fill("input[name=confirm]", FORM["company_name"])
        await page.get_by_role("button", name="Delete client").click()
        await page.wait_for_url(re.compile(r"/clients$"))
        assert await page.get_by_text("No clients yet").is_visible()
        await page.screenshot(path=str(tmp_path / "end.png"))
        await browser.close()
    assert errors == [], errors
