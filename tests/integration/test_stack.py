"""Against the running compose stack (make up && make test-integration).

Real models (sentinel-win64 / sentinel-win32 @ production), real PostgreSQL overrides and
lineage. Request bodies come from `make sample-request` ($SENTINEL_DATA_DIR/samples/Win64).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest

pytestmark = pytest.mark.integration

API = os.environ.get("SENTINEL_API_URL", "")
ADMIN_DSN = os.environ.get("SENTINEL_IT_ADMIN_DSN", "")
API_DSN = os.environ.get("SENTINEL_IT_API_DSN", "")
SAMPLES = Path(os.environ.get("SENTINEL_DATA_DIR", "/data")) / "samples" / "Win64"
WAIT_SECONDS = 20.0  # > overrides.refresh_seconds and lineage.flush_seconds

if not (API and ADMIN_DSN and API_DSN):
    pytest.skip("stack not configured (run via make test-integration)", allow_module_level=True)


def _sample(name: str) -> dict[str, Any]:
    path = SAMPLES / f"{name}.json"
    if not path.exists():
        pytest.skip("no sample requests: run `make sample-request` first")
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return body


@pytest.fixture(scope="module")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=API, timeout=30) as c:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if c.get("/readyz").status_code == 200:
                    break
            except httpx.TransportError:
                pass
            time.sleep(1)
        assert c.get("/readyz").status_code == 200, "api never became ready"
        yield c


@pytest.fixture()
def tenant() -> Iterator[str]:
    t = f"it-{uuid.uuid4().hex[:12]}"
    yield t
    with psycopg.connect(ADMIN_DSN) as conn:
        conn.execute("DELETE FROM tenant_overrides WHERE tenant_id = %s", (t,))


def _override(tenant: str, file_id: str, action: str) -> None:
    with psycopg.connect(ADMIN_DSN) as conn:
        conn.execute(
            "INSERT INTO tenant_overrides (tenant_id, file_id, action, reason, created_by) "
            "VALUES (%s, %s, %s, 'integration test', 'pytest')",
            (tenant, file_id, action),
        )


def _score_until(http: httpx.Client, body: dict[str, Any], want_override: bool) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        r = http.post("/v1/score", json=body)
        assert r.status_code == 200, r.text
        out: dict[str, Any] = r.json()
        if out["override_applied"] == want_override or time.monotonic() > deadline:
            return out
        time.sleep(0.5)


def _lineage_row(request_id: str) -> tuple[Any, ...] | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with psycopg.connect(ADMIN_DSN) as conn:
            row = conn.execute(
                "SELECT file_id, tenant_id, model_version, feature_schema_version, threshold, "
                "score, verdict, override_applied FROM predictions WHERE request_id = %s",
                (request_id,),
            ).fetchone()
        if row is not None:
            return tuple(row)
        time.sleep(0.5)
    return None


def test_both_real_models_are_served(http: httpx.Client) -> None:
    # Stage 6: one entry per (ring, file type); each ring's alias, production as fallback
    models = {(m["ring"], m["file_type"]): m for m in http.get("/v1/models").json()}
    rings = ("ring0", "ring1", "ring2")
    assert set(models) == {(r, ft) for r in rings for ft in ("Win64", "Win32")}
    for (ring, ft), m in models.items():
        cur = m["current"]
        assert cur is not None, m["last_error"]
        assert cur["model"] == f"sentinel-{ft.lower()}"
        assert m["aliases"] == [f"{ring}_model", "production"]


def test_score_writes_full_lineage(http: httpx.Client, tenant: str) -> None:
    body = {**_sample("benign_1"), "tenant_id": tenant}
    out = http.post("/v1/score", json=body).json()
    row = _lineage_row(out["request_id"])
    assert row == (
        body["file_id"],
        tenant,
        out["model_version"],
        body["feature_schema_version"],
        out["threshold"],
        out["score"],
        out["verdict"],
        out["override_applied"],
    )


def test_schema_mismatch_is_409(http: httpx.Client) -> None:
    body = {**_sample("benign_1"), "feature_schema_version": "not-a-real-schema"}
    r = http.post("/v1/score", json=body)
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "feature_schema_mismatch"


def test_allow_listed_file_is_never_blocked(http: httpx.Client, tenant: str) -> None:
    body = {**_sample("malicious_1"), "tenant_id": tenant}
    before = http.post("/v1/score", json=body).json()
    assert before["verdict"] == "malicious"  # the real model flags this record
    _override(tenant, body["file_id"], "allow")
    out = _score_until(http, body, want_override=True)
    assert (out["verdict"], out["override_applied"]) == ("benign", True)
    assert out["score"] == pytest.approx(before["score"])  # the model is unchanged
    row = _lineage_row(out["request_id"])
    assert row is not None
    assert (row[6], row[7]) == ("benign", True)


def test_block_list_wins_over_benign_model(http: httpx.Client, tenant: str) -> None:
    body = {**_sample("benign_1"), "tenant_id": tenant}
    assert http.post("/v1/score", json=body).json()["verdict"] == "benign"
    _override(tenant, body["file_id"], "block")
    out = _score_until(http, body, want_override=True)
    assert (out["verdict"], out["override_applied"]) == ("malicious", True)


def test_api_role_is_least_privilege() -> None:
    with psycopg.connect(API_DSN, autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM tenant_overrides").fetchone() is not None
        for stmt in (
            "UPDATE predictions SET verdict = 'benign'",
            "DELETE FROM predictions",
            "INSERT INTO tenant_overrides VALUES ('t', 'f', 'allow', 'x', 'y', now())",
            "DROP TABLE predictions",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(stmt)
