"""Check a deployed Growth OS before trusting it. Run after every deploy.

  python scripts/check_deploy.py https://growthos-b0t9.onrender.com

Fails (exit 1) unless /healthz says production, Postgres, not ephemeral, no fixtures and not mock models, the public
pages answer, and the test-only sign-in does not exist. Reads nothing private; sends no credentials.
"""
import sys

import httpx


def main(base: str) -> int:
    base = base.rstrip("/")
    problems, notes = [], []
    with httpx.Client(timeout=60, follow_redirects=False) as c:     # free hosting may need ~50 s to wake up
        try:
            h = c.get(base + "/healthz").json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"FAIL: /healthz did not answer with JSON ({type(e).__name__})")
            return 1
        expect = {"ok": True, "env": "production", "database": "postgresql", "ephemeral_storage": False, "fixtures": False}
        for k, v in expect.items():
            if h.get(k) != v:
                problems.append(f"/healthz {k} = {h.get(k)!r}, expected {v!r}")
        if h.get("llm_mode") == "mock":
            problems.append("/healthz llm_mode is mock")
        for path, must in (("/login", "Continue with Google"), ("/termsofservice", "Terms of service"),
                           ("/login/policy", "Privacy policy")):
            r = c.get(base + path)
            if r.status_code != 200 or must not in r.text:
                problems.append(f"{path}: HTTP {r.status_code}" + ("" if r.status_code != 200 else f", missing '{must}'"))
            elif "DEMO MODE" in r.text or "Test sign-in" in r.text:
                problems.append(f"{path} shows demo or test sign-in content")
        r = c.post(base + "/auth/test-login", data={"email": "probe@example.com"})
        if r.status_code not in (404, 405):
            problems.append(f"/auth/test-login answered HTTP {r.status_code}: the test sign-in must not exist")
        r = c.get(base + "/auth/google/start")
        loc = r.headers.get("location", "")
        if r.status_code != 303 or not loc.startswith("https://accounts.google.com/"):
            problems.append(f"/auth/google/start: HTTP {r.status_code}, not a redirect to Google")
        elif httpx.URL(loc).params.get("redirect_uri") != base + "/auth/google/callback":
            notes.append("the Google redirect_uri does not match this URL; check APP_BASE_URL")
        r = c.get(base + "/clients")
        if r.status_code != 303 or r.headers.get("location") != "/login":
            problems.append(f"/clients without a session: HTTP {r.status_code} (want 303 to /login)")
    for n in notes:
        print("WARN:", n)
    if problems:
        for p in problems:
            print("FAIL:", p)
        return 1
    print(f"OK: {base} is production on Postgres (persistent), no fixtures, models live, public pages up, no test sign-in.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
