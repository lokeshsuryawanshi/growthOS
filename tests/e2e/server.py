"""Test harness only: the real app in APP_ENV=test (mock models, the test-only sign-in), with outbound HTTP answered
by a mock so a fake API key passes its check. Usage: python tests/e2e/server.py <port> <sqlite path>"""
import os
import sys

os.environ.update(APP_ENV="test", LLM_MODE="mock", DATABASE_URL=f"sqlite+aiosqlite:///{sys.argv[2]}",
                  APP_BASE_URL=f"http://127.0.0.1:{sys.argv[1]}", GOOGLE_OAUTH_CLIENT_ID="", GOOGLE_OAUTH_CLIENT_SECRET="")
from app.core.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None

import httpx  # noqa: E402
import httpx2  # noqa: E402
import uvicorn  # noqa: E402

from app.integrations import http  # noqa: E402

http.set_transport(httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [], "account": {}})),
                   httpx2.MockTransport(lambda r: httpx2.Response(200, json={"data": [], "has_more": False})))
uvicorn.run("app.main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
