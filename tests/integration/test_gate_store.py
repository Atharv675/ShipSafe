"""gate_decisions in the running PostgreSQL: append-only for everyone, hash checked by the DB.

Every test runs inside a transaction that is always rolled back: the table is append-only,
so a committed test row could never be removed again.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from sentinel.gate.evidence import bundle_sha256, canonical_json

pytestmark = pytest.mark.integration

ADMIN_DSN = os.environ.get("SENTINEL_IT_ADMIN_DSN", "")
GATE_DSN = os.environ.get("SENTINEL_IT_GATE_DSN", "")

if not (ADMIN_DSN and GATE_DSN):
    pytest.skip("stack not configured (run via make test-integration)", allow_module_level=True)

INSERT = (
    "INSERT INTO gate_decisions (decision_id, file_type, policy_version, production_version, "
    "candidate_version, decision, reason, bundle_sha256, bundle_json) "
    "VALUES (%s, 'Win64', 'test', 'm/1', 'm/2', 'REJECT', 'integration test row', %s, %s)"
)


def _row() -> tuple[str, str, str]:
    did = f"it-{uuid.uuid4().hex[:12]}"
    text = canonical_json({"decision_id": did, "decision": "REJECT"})
    return did, bundle_sha256(text), text


def test_gate_role_appends_and_reads_but_cannot_change() -> None:
    did, sha, text = _row()
    with psycopg.connect(GATE_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT, (did, sha, text))
        got = conn.execute(
            "SELECT bundle_sha256, bundle_json FROM gate_decisions WHERE decision_id = %s", (did,)
        ).fetchone()
        assert got == (sha, text)
        for sql in (
            "UPDATE gate_decisions SET decision = 'PROMOTE' WHERE decision_id = %s",
            "DELETE FROM gate_decisions WHERE decision_id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql, (did,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM shadow_predictions WHERE false")
        assert conn.execute("SELECT count(*) FROM predictions").fetchone() is not None


def test_even_the_owner_cannot_update_delete_or_truncate() -> None:
    did, sha, text = _row()
    with psycopg.connect(ADMIN_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT, (did, sha, text))
        for sql, args in (
            ("UPDATE gate_decisions SET reason = 'edited' WHERE decision_id = %s", (did,)),
            ("DELETE FROM gate_decisions WHERE decision_id = %s", (did,)),
            ("TRUNCATE gate_decisions", ()),
        ):
            with (
                pytest.raises(psycopg.errors.RaiseException, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(sql, args)
        assert conn.execute(
            "SELECT reason FROM gate_decisions WHERE decision_id = %s", (did,)
        ).fetchone() == ("integration test row",)


def test_database_refuses_a_hash_that_does_not_match_the_bundle() -> None:
    did, _, text = _row()
    with (
        psycopg.connect(GATE_DSN) as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(INSERT, (did, "0" * 64, text))
