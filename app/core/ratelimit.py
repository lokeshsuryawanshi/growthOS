"""In-memory sliding-window rate limiter (per process). Protects login, signup and expensive actions.

Single-process by design, like the orchestrator; behind several instances, move the counters to Redis.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, window_s: float) -> None:
        now = time.monotonic()
        q = self._hits[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            retry = max(1, int(window_s - (now - q[0])))
            raise HTTPException(429, f"Too many attempts. Try again in {retry} seconds.",
                                headers={"Retry-After": str(retry)})
        q.append(now)

    def reset(self) -> None:
        self._hits.clear()


limiter = RateLimiter()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"
