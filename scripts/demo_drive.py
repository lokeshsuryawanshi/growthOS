"""Drive a running Growth OS server through a demo: discovery -> gate approval -> weekly cycles -> some founder decisions.

Usage: python scripts/demo_drive.py [base_url]   (uses the seeded local demo accounts)
"""
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8010"
PW = "growthos-demo"


def login(c, email):
    r = c.post(f"{BASE}/api/auth/login", json={"email": email, "password": PW})
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['token']}"}


def wait(c, h, tid, rid, timeout=600):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = c.get(f"{BASE}/api/tenants/{tid}/runs/{rid}", headers=h).json()["status"]
        if st not in ("pending", "running"):
            return st
        time.sleep(1)
    return "timeout"


with httpx.Client(timeout=60) as c:
    am = login(c, "am.sam@growthos.example")
    tenants = c.get(f"{BASE}/api/tenants", headers=am).json()
    runs = {}
    for t in tenants:
        runs[t["id"]] = c.post(f"{BASE}/api/tenants/{t['id']}/runs", json={"kind": "discovery"}, headers=am).json()["run_id"]
    for t in tenants:
        st = wait(c, am, t["id"], runs[t["id"]])
        print(t["slug"], "discovery:", st)
        if st == "waiting_human":
            c.post(f"{BASE}/api/tenants/{t['id']}/runs/{runs[t['id']]}/gates/strategy_review/approve", headers=am).raise_for_status()
            print(t["slug"], "gate approved ->", wait(c, am, t["id"], runs[t["id"]]))
    for cycle in range(2):
        started = c.post(f"{BASE}/api/weekly-cycle", headers=am).json()["started"]
        for s in started:
            tid = next(t["id"] for t in tenants if t["slug"] == s["tenant"])
            print(f"weekly {cycle + 1}", s["tenant"], wait(c, am, tid, s["run_id"]))
    # founder decisions for LedgerLoop: approve two as-is, edit one, reject one
    priya = login(c, "priya@ledgerloop.example")
    tid = next(t["id"] for t in tenants if t["slug"] == "ledgerloop")
    posts = c.get(f"{BASE}/api/tenants/{tid}/posts?status=pending_approval", headers=priya).json()
    c.post(f"{BASE}/api/tenants/{tid}/posts/{posts[0]['id']}/decision", json={"action": "approve"}, headers=priya)
    c.post(f"{BASE}/api/tenants/{tid}/posts/{posts[1]['id']}/decision", json={"action": "approve"}, headers=priya)
    short = "\n\n".join(posts[2]["text"].split("\n\n")[:3])
    c.post(f"{BASE}/api/tenants/{tid}/posts/{posts[2]['id']}/decision", json={"action": "approve", "text": short}, headers=priya)
    c.post(f"{BASE}/api/tenants/{tid}/posts/{posts[3]['id']}/decision", json={"action": "reject", "note": "Too generic for our buyers"}, headers=priya)
    leads = [l for l in c.get(f"{BASE}/api/tenants/{tid}/leads", headers=priya).json() if l["message_id"]]
    c.post(f"{BASE}/api/tenants/{tid}/outreach/{leads[0]['message_id']}/decision", json={"action": "approve"}, headers=priya)
    print("founder decisions recorded for ledgerloop")
