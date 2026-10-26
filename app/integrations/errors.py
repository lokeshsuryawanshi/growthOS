"""One error type for every external dependency, carrying what the user should do about it."""
from __future__ import annotations

NOT_CONFIGURED = "not_configured"
AUTH_FAILED = "auth_failed"
QUOTA = "quota_exceeded"
RATE_LIMITED = "rate_limited"
UNAVAILABLE = "unavailable"
TIMEOUT = "timeout"
BAD_RESPONSE = "bad_response"
BLOCKED = "blocked"

# Failures that retrying immediately cannot fix; the orchestrator fails fast instead of burning attempts.
NON_RETRYABLE = {NOT_CONFIGURED, AUTH_FAILED, QUOTA, BLOCKED}


class IntegrationError(Exception):
    def __init__(self, integration: str, code: str, message: str, recovery: str = ""):
        super().__init__(message)
        self.integration = integration
        self.code = code
        self.message = message
        self.recovery = recovery

    @property
    def retryable(self) -> bool:
        return self.code not in NON_RETRYABLE

    def as_dict(self) -> dict[str, str]:
        return {"integration": self.integration, "code": self.code, "message": self.message,
                "recovery": self.recovery}

    def __str__(self) -> str:
        return f"[{self.integration}:{self.code}] {self.message}" + (f" Fix: {self.recovery}" if self.recovery else "")
