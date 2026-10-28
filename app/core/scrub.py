"""Remove API keys from any text that might be shown, stored or logged (provider errors can echo the key they got)."""
from __future__ import annotations

import logging
import re

MASK = "***"
KEY_PATTERNS = [
    re.compile(r"gsk_[A-Za-z0-9]{12,}"),                          # Groq
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}"),                    # Anthropic
    re.compile(r"sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_\-]{16,}"),  # OpenAI
    re.compile(r"tvly-[A-Za-z0-9_\-]{10,}"),                      # Tavily
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                       # Google API keys
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)((?:x-api-key|api[_-]?key|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"'&,}]{8,}"),
    re.compile(r"\b[0-9a-f]{40}\b"),                              # Hunter keys are 40 hex characters
]


def scrub(text: object, *secrets: str | None) -> str:
    out = str(text)
    for s in secrets:
        if s and len(s) >= 8:
            out = out.replace(s, MASK)
    for pat in KEY_PATTERNS:
        out = pat.sub(lambda m: (m.group(1) if m.re.groups and m.group(1) else "") + MASK, out)
    return out


class ScrubFilter(logging.Filter):
    """Installed on the root handlers at startup: nothing that looks like a key reaches the logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string must not lose the log line
            msg = str(record.msg)
        clean = scrub(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def install_log_scrubber() -> None:
    root = logging.getLogger()
    for h in root.handlers or []:
        if not any(isinstance(f, ScrubFilter) for f in h.filters):
            h.addFilter(ScrubFilter())
