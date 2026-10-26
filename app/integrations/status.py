"""What is connected, what is missing, and how to fix it. Shown on the Integrations page and /healthz."""
from __future__ import annotations

from app.core.config import get_settings
from app.integrations import gmail
from app.integrations.hunter import HunterClient

OK, MISSING, DEGRADED = "connected", "not_configured", "degraded"


async def integration_status(tenant_id: str | None = None) -> list[dict]:
    s = get_settings()
    out: list[dict] = []

    mode = s.effective_llm_mode
    out.append({"key": "ai", "name": "AI models (Groq)", "required": True,
                "state": OK if mode == "live" else (MISSING if mode == "unconfigured" else DEGRADED),
                "detail": {"live": "Writing, strategy and scoring run on Groq models.",
                           "mock": "MOCK MODELS: output is synthetic (demo/test only).",
                           "unconfigured": "No GROQ_API_KEY: nothing can be researched or drafted."}[mode],
                "how": "Create a free key at console.groq.com, set GROQ_API_KEY, restart."})
    out.append({"key": "judge", "name": "Quality judge (Gemini)", "required": False,
                "state": OK if s.google_api_key and mode == "live" else MISSING,
                "detail": "Scores drafts with a different model family than the writer." if s.google_api_key
                else "Not set: a Groq Qwen model judges drafts instead.",
                "how": "Get a key at aistudio.google.com/apikey and set GOOGLE_API_KEY."})
    search_ok = bool(s.google_api_key or s.tavily_api_key)
    search_state = OK if s.tavily_api_key else (DEGRADED if s.google_api_key else MISSING)   # Gemini grounding needs billing; unverified
    out.append({"key": "search", "name": "Web search", "required": True, "state": search_state,
                "detail": ("Providers: " + ", ".join(p for p, on in (("Gemini Google Search grounding", s.google_api_key),
                                                                   ("Tavily", s.tavily_api_key)) if on)
                           + (". Only Gemini grounding is configured: it needs billing enabled and may be out of quota, in which case research stops "
                              "with that message. Add TAVILY_API_KEY for a dependable provider." if not s.tavily_api_key else "."))
                if search_ok else "No search provider: market, competitor and trend research cannot run.",
                "how": "Set TAVILY_API_KEY (free tier at tavily.com), or enable billing on the Google key."})
    hunter = HunterClient()
    out.append({"key": "hunter", "name": "Hunter.io (business emails)", "required": False,
                "state": OK if hunter.configured else MISSING,
                "detail": "Finds and verifies decision-maker emails for each lead." if hunter.configured
                else "Not set: leads are found as companies without contacts, and outreach cannot be emailed.",
                "how": "Create a key at hunter.io (free plan available) and set HUNTER_API_KEY."})
    g: dict = {"key": "gmail", "name": "Gmail (send approved outreach)", "required": False,
               "how": "Add Google OAuth client credentials (see docs/DEPLOY.md), then click Connect Gmail."}
    if not gmail.configured():
        g.update(state=MISSING, detail="Server has no Google OAuth client, so Gmail cannot be connected yet.")
    elif tenant_id is None:
        g.update(state=OK, detail="Ready to connect per workspace.")
    else:
        cred = await gmail.connection(tenant_id)
        if cred and cred.status == "connected":
            g.update(state=OK, detail=f"Connected as {cred.account_email}. Emails send only after you approve each one.")
        elif cred:
            g.update(state=DEGRADED, detail=f"Connection {cred.status}: {cred.last_error or 'reconnect required'}")
        else:
            g.update(state=MISSING, detail="Not connected. Approved emails cannot be sent until you connect Gmail.")
    out.append(g)
    out.append({"key": "publishing", "name": "LinkedIn publishing", "required": False, "state": MISSING,
                "detail": "Not integrated. Approved posts are copied to LinkedIn by the founder, then marked as posted.",
                "how": "Intentionally out of scope for now."})
    return out
