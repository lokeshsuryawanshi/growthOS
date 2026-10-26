"""Fair scheduler: a global worker cap plus a per-tenant cap, dispatched round-robin across tenants.

One tenant with a huge backlog can't starve the others: each free slot goes to
the next tenant in rotation that has queued work and is under its own cap.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from app.core.tenancy import tenant_scope


class FairScheduler:
    def __init__(self, global_cap: int, per_tenant_cap: int):
        self.global_cap, self.per_tenant_cap = global_cap, per_tenant_cap
        self.queues: dict[str, deque] = defaultdict(deque)
        self.running: dict[str, int] = defaultdict(int)
        self.total_running = 0
        self.rotation: deque[str] = deque()
        self.waits: dict[str, list[float]] = defaultdict(list)  # queue wait per tenant (seconds)
        self.peak_running = 0
        self._tasks: set[asyncio.Task] = set()

    async def run(self, tenant_id: str, factory: Callable[[], Awaitable[Any]]) -> Any:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if tenant_id not in self.rotation:
            self.rotation.append(tenant_id)
        self.queues[tenant_id].append((factory, fut, time.monotonic()))
        self._dispatch()
        return await fut

    def _dispatch(self) -> None:
        while self.total_running < self.global_cap:
            picked = None
            for _ in range(len(self.rotation)):
                t = self.rotation[0]
                self.rotation.rotate(-1)
                if self.queues[t] and self.running[t] < self.per_tenant_cap:
                    picked = t
                    break
            if picked is None:
                return
            factory, fut, enq = self.queues[picked].popleft()
            self.waits[picked].append(time.monotonic() - enq)
            self.running[picked] += 1
            self.total_running += 1
            self.peak_running = max(self.peak_running, self.total_running)
            task = asyncio.create_task(self._run_one(picked, factory, fut))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _run_one(self, tenant_id: str, factory, fut: asyncio.Future) -> None:
        try:
            with tenant_scope(tenant_id):  # never inherit whichever scope called _dispatch
                result = await factory()
            if not fut.done():
                fut.set_result(result)
        except BaseException as e:  # noqa: BLE001 - surfaced to the awaiting caller
            if not fut.done():
                fut.set_exception(e)
        finally:
            self.running[tenant_id] -= 1
            self.total_running -= 1
            self._dispatch()
