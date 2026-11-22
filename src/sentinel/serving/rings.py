"""Ring routing (Stage 6): each tenant's ring decides which registry alias scores its files.

- The ring is computed HERE from tenant_id (the same stable hash as the replay producer), so a
  client cannot pick its own ring.
- One ModelManager per ring, aliases `ring<k>_model` with `production` as the fallback: a ring
  whose alias is missing serves production. Rollout = moving ring aliases; rollback = moving
  them back; each manager hot-reloads exactly as in Stage 3.
- `SharedModelSource` makes the three managers share loaded models: a version is loaded once
  and kept while any ring (current or previous slot) still holds it, so moving a ring back to
  a version another ring serves is instant and memory does not triple.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Mapping
from typing import Any

from sentinel.config import RING_ORDER, RingsConfig, RolloutConfig
from sentinel.monitoring.metrics import MetricsRecorder
from sentinel.replay.events import ring_for
from sentinel.serving.models import LoadedModel, ModelManager, ModelSource


class SharedModelSource:
    """Wraps a ModelSource; `load` returns the already-loaded object for a version if any
    ring still holds it (weak cache: unused versions are freed)."""

    def __init__(self, source: ModelSource) -> None:
        self._source = source
        self._cache: weakref.WeakValueDictionary[tuple[str, str], LoadedModel] = (
            weakref.WeakValueDictionary()
        )
        self._lock = threading.Lock()

    def resolve(self, name: str, alias: str) -> str:
        return self._source.resolve(name, alias)

    def load(self, name: str, version: str) -> LoadedModel:
        with self._lock:  # loads are rare and sequential (one refresh at a time)
            cached = self._cache.get((name, version))
            if cached is not None:
                return cached
            model = self._source.load(name, version)
            self._cache[(name, version)] = model
            return model


class RingRouter:
    """Picks the ring's model for a request; `refresh` hot-reloads every ring."""

    def __init__(self, managers: Mapping[str, ModelManager], rings: RingsConfig) -> None:
        if set(managers) != set(RING_ORDER):
            raise ValueError(f"need one manager per ring {RING_ORDER}")
        self._managers = dict(managers)
        self._rings = rings
        self._refreshing = threading.Lock()

    @classmethod
    def build(
        cls,
        source: ModelSource,
        names: Mapping[str, str],
        rollout: RolloutConfig,
        rings: RingsConfig,
        metrics: MetricsRecorder | None = None,
    ) -> RingRouter:
        shared = SharedModelSource(source)
        return cls(
            {
                r: ModelManager(
                    shared, names, (rollout.ring_aliases[r], rollout.fallback_alias), metrics
                )
                for r in RING_ORDER
            },
            rings,
        )

    @classmethod
    def single(cls, manager: ModelManager, rings: RingsConfig) -> RingRouter:
        """Every ring served by one manager (no rollout possible): Stage 3 behaviour."""
        return cls(dict.fromkeys(RING_ORDER, manager), rings)

    def ring_of(self, tenant_id: str) -> str:
        return ring_for(tenant_id, self._rings)

    def get(self, tenant_id: str, feature_schema_version: str) -> tuple[str, LoadedModel | None]:
        ring = self.ring_of(tenant_id)
        return ring, self._managers[ring].get(feature_schema_version)

    @property
    def ready(self) -> bool:
        return all(m.ready for m in self._managers.values())

    def expected_schemas(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for m in self._managers.values():
            out.update(m.expected_schemas())
        return out

    def refresh(self) -> dict[str, dict[str, str]]:
        with self._refreshing:
            done: dict[int, dict[str, str]] = {}
            out: dict[str, dict[str, str]] = {}
            for r in RING_ORDER:
                m = self._managers[r]
                if id(m) not in done:  # `single`: one shared manager refreshed once
                    done[id(m)] = m.refresh()
                out[r] = done[id(m)]
            return out

    def status(self) -> list[dict[str, Any]]:
        return [{"ring": r, **s} for r in RING_ORDER for s in self._managers[r].status()]

    def serving_versions(self) -> dict[str, dict[str, str | None]]:
        """{ring: {file_type: version or None}} — what each ring serves right now."""
        return {
            r: {
                s["file_type"]: (s["current"] or {}).get("version")
                for s in self._managers[r].status()
            }
            for r in RING_ORDER
        }
