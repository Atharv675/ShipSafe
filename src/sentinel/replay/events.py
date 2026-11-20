"""The `file-events` message: one file seen at one moment, plus stable tenant and ring.

- event_id = "<file type>:<sha256>" (deterministic: replaying a week again produces the SAME
  ids, which is what makes processing idempotent downstream).
- tenant_id = stable hash(file_id) % tenants; ring = stable hash(tenant_id) bucket 0..99:
  ring0 (first 5%), ring1 (next 25%), ring2 (rest). blake2b, never Python's salted hash().
- The label is NOT in the event: at scoring time nobody knows it yet.
- Wire format: one orjson object; features as base64 of little-endian float32 (exact, ~4x
  smaller than JSON numbers).
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import orjson

from sentinel.config import RingsConfig

EVENT_VERSION = 1
RING0, RING1, RING2 = "ring0", "ring1", "ring2"
_BUCKETS = 100


def _stable_int(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode(), digest_size=8).digest(), "big")


def make_event_id(file_type: str, file_id: str) -> str:
    return f"{file_type}:{file_id}"


def tenant_for(file_id: str, tenants: int) -> str:
    return f"tenant-{_stable_int('tenant|' + file_id) % tenants:04d}"


def ring_for(tenant_id: str, rings: RingsConfig) -> str:
    bucket = _stable_int("ring|" + tenant_id) % _BUCKETS
    if bucket < rings.ring0_percent:
        return RING0
    if bucket < rings.ring0_percent + rings.ring1_percent:
        return RING1
    return RING2


@dataclass(frozen=True, eq=False)
class FileEvent:
    event_id: str
    file_id: str
    file_type: str
    tenant_id: str
    ring: str
    week: int
    first_seen: str  # ISO-8601 UTC
    feature_schema_version: str
    features: npt.NDArray[np.float32]

    def encode(self) -> bytes:
        return orjson.dumps(
            {
                "v": EVENT_VERSION,
                "event_id": self.event_id,
                "file_id": self.file_id,
                "file_type": self.file_type,
                "tenant_id": self.tenant_id,
                "ring": self.ring,
                "week": self.week,
                "first_seen": self.first_seen,
                "feature_schema_version": self.feature_schema_version,
                "features_f32le": base64.b64encode(
                    np.ascontiguousarray(self.features, dtype="<f4").tobytes()
                ).decode(),
            }
        )

    @classmethod
    def decode(cls, payload: bytes) -> FileEvent:
        d: dict[str, Any] = orjson.loads(payload)
        if d.get("v") != EVENT_VERSION:
            raise ValueError(f"unsupported event version {d.get('v')!r}")
        feats = np.frombuffer(base64.b64decode(d["features_f32le"]), dtype="<f4").astype(np.float32)
        return cls(
            event_id=str(d["event_id"]),
            file_id=str(d["file_id"]),
            file_type=str(d["file_type"]),
            tenant_id=str(d["tenant_id"]),
            ring=str(d["ring"]),
            week=int(d["week"]),
            first_seen=str(d["first_seen"]),
            feature_schema_version=str(d["feature_schema_version"]),
            features=feats,
        )
