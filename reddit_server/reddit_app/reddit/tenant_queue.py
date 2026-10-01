"""Per-tenant round-robin admission for outbound Reddit fetches.

Same idea as the sentiment service's tenant-fair gate: when more than one
tenant is waiting, the next free slot goes to the next tenant in rotation,
not to whoever queued first. Callers wait until they are admitted. Nothing
here rejects, rate-limits, or gives up — the HTTP handler stays open until
the Reddit response comes back.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

UNKNOWN_TENANT = "unknown"

tenant_key_var: ContextVar[str] = ContextVar("reddit_tenant_key", default=UNKNOWN_TENANT)


class TenantRoundRobin:
    def __init__(self, *, size: int = 64) -> None:
        self.size = max(1, int(size))
        self._cond = asyncio.Condition()
        self._in_flight = 0
        self._rotation: list[str] = []
        self._known: set[str] = set()
        self._waiting: dict[str, int] = {}
        self._cursor = 0

    def _register(self, key: str) -> None:
        if key in self._known:
            return
        self._known.add(key)
        self._rotation.append(key)

    def _next_ready(self) -> str | None:
        n = len(self._rotation)
        if n == 0:
            return None
        for i in range(n):
            candidate = self._rotation[(self._cursor + i) % n]
            if self._waiting.get(candidate, 0) > 0:
                return candidate
        return None

    def _advance_past(self, key: str) -> None:
        try:
            idx = self._rotation.index(key)
        except ValueError:
            return
        self._cursor = (idx + 1) % len(self._rotation)

    async def acquire(self, tenant_key: str | None = None) -> None:
        key = (tenant_key or UNKNOWN_TENANT).strip() or UNKNOWN_TENANT
        async with self._cond:
            self._register(key)
            self._waiting[key] = self._waiting.get(key, 0) + 1
            try:
                while True:
                    if self._in_flight < self.size:
                        turn = self._next_ready()
                        if turn == key:
                            self._in_flight += 1
                            self._waiting[key] -= 1
                            self._advance_past(key)
                            return
                    await self._cond.wait()
            except BaseException:
                self._waiting[key] = max(0, self._waiting.get(key, 1) - 1)
                raise

    async def release(self) -> None:
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()
