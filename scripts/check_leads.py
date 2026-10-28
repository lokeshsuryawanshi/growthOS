"""Open every lead's evidence: does the page the lead came from name the company, and does the contact page name the
person (or show the email)? Prints one line per lead for a human to judge relevance; writes the facts to JSON.

  python scripts/check_leads.py --db run.db [--db ...] --out report.json

Mechanical checks only (pages fetched with the app's own safe fetcher; linkedin.com is never fetched, a LinkedIn
route is checked as "URL came from search"). Whether a lead is a sensible prospect is a judgement the reader makes.
"""
import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def check(db: str) -> dict:
    from app.integrations.errors import IntegrationError
    from app.integrations.fetch import fetch_page
    con = sqlite3.connect(db)
    client = con.execute("select name from tenants").fetchone()[0]
    rows = con.execute("select company, company_domain, full_name, title, email, contact_source, contact_url, "
                       "linkedin_url, source_url, fit_score, signal, evidence from leads").fetchall()
    con.close()
    cache: dict[str, str | None] = {}

    async def text(url: str) -> str | None:
        if not url or "linkedin.com" in url:
            return None
        if url not in cache:
            try:
                cache[url] = (await fetch_page(url)).text.lower()
            except IntegrationError:
                cache[url] = None
        return cache[url]
    out = []
    for company, domain, person, title, email, route, curl, li, surl, fit, signal, evidence in rows:
        src = await text(surl)
        home = await text("https://" + domain)
        contact = await text(curl) if curl and curl != surl else src
        first = person.split()[0].lower() if person else ""
        out.append({
            "company": company, "domain": domain, "fit": fit, "route": route, "person": person, "title": title,
            "email": email, "source_url": surl, "contact_url": curl or li,
            "source_names_company": bool(src and (company.lower() in src or domain.split(".")[0] in src)),
            "source_readable": src is not None, "company_site_up": home is not None,
            "person_on_contact_page": (bool(contact and first and first in contact) if route in ("team_page", "site_email") else None),
            "email_on_site": (bool(email and contact and email.lower() in contact) or bool(email and home and email.lower() in home))
            if email and route in ("site_email", "role_inbox") else None,
            "linkedin_from_search": bool(li) if route == "linkedin_search" else None,
            "signal": signal, "evidence": json.loads(evidence) if evidence else []})
    return {"client": client, "leads": out}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    import os
    os.environ.setdefault("APP_ENV", "production")
    os.environ.setdefault("JWT_SECRET", "x" * 40)
    reports = [asyncio.run(check(d)) for d in a.db]
    Path(a.out).write_text(json.dumps(reports, indent=1))
    for r in reports:
        print(f"\n== {r['client']}: {len(r['leads'])} leads")
        for l in r["leads"]:
            flags = " ".join(k for k in ("source_names_company", "company_site_up", "person_on_contact_page",
                                         "email_on_site", "linkedin_from_search") if l[k])
            print(f"  {l['fit']:>3} {l['company'][:28]:<28} {l['domain'][:26]:<26} {l['route']:<15} "
                  f"{(l['person'] or l['email'])[:24]:<24} [{flags}] {re.sub(r'\\s+', ' ', (l['evidence'] or [''])[0])[:70]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
