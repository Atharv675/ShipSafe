"""Per-tenant allow/block lists, applied OUTSIDE the model. Overrides always win.

Stored in PostgreSQL `tenant_overrides(tenant_id, file_id, action, reason, created_by,
created_at)`. The API keeps an in-memory snapshot refreshed every few seconds, so scoring
never waits on the database. Until the first snapshot has loaded the service is NOT ready
(otherwise an allow-listed file could be blocked); after that a database outage keeps the
last good snapshot.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from sentinel.logging import get_logger

log = get_logger(__name__)


class Action(StrEnum):
    ALLOW = "allow"  # never flag this file for this tenant
    BLOCK = "block"  # always flag this file for this tenant


@dataclass(frozen=True)
class Override:
    tenant_id: str
    file_id: str
    action: Action
    reason: str


def apply_override(model_malicious: bool, override: Override | None) -> tuple[bool, bool]:
    """(final_malicious, override_applied). An override always wins over the model."""
    if override is None:
        return model_malicious, False
    return override.action is Action.BLOCK, True


class OverrideStore(Protocol):
    @property
    def loaded(self) -> bool: ...

    def lookup(self, tenant_id: str, file_id: str) -> Override | None: ...

    def refresh(self) -> int: ...


class InMemoryOverrideStore:
    """For tests and the demo: same behaviour as the PostgreSQL store, no database."""

    def __init__(self, overrides: list[Override] | None = None, loaded: bool = True) -> None:
        self._items = {(o.tenant_id, o.file_id): o for o in overrides or []}
        self._loaded = loaded

    @property
    def loaded(self) -> bool:
        return self._loaded

    def put(self, override: Override) -> None:
        self._items = {**self._items, (override.tenant_id, override.file_id): override}

    def lookup(self, tenant_id: str, file_id: str) -> Override | None:
        return self._items.get((tenant_id, file_id))

    def refresh(self) -> int:
        self._loaded = True
        return len(self._items)


SELECT_OVERRIDES = "SELECT tenant_id, file_id, action, reason FROM tenant_overrides"


class PostgresOverrideStore:
    def __init__(self, dsn: str, connect_timeout: float = 5.0) -> None:
        self._dsn = dsn
        self._timeout = connect_timeout
        self._items: dict[tuple[str, str], Override] = {}
        self._loaded = False
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._loaded

    def lookup(self, tenant_id: str, file_id: str) -> Override | None:
        return self._items.get((tenant_id, file_id))

    def refresh(self) -> int:
        """Reload the whole snapshot; on any database error keep the previous one."""
        import psycopg

        with self._lock:
            try:
                with psycopg.connect(self._dsn, connect_timeout=int(self._timeout)) as conn:
                    rows = conn.execute(SELECT_OVERRIDES).fetchall()
                items = {
                    (str(t), str(f)): Override(str(t), str(f), Action(str(a)), str(r))
                    for t, f, a, r in rows
                }
            except Exception as exc:
                log.error("overrides_refresh_failed", error=f"{type(exc).__name__}: {exc}")
                raise
            self._items = items  # atomic rebind
            self._loaded = True
            return len(items)
