"""rollout_transitions + Stage 6 grants in the running PostgreSQL.

Every test runs inside a transaction that is always rolled back (the table is append-only,
so a committed test row could never be removed again).
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

from sentinel.rollout.store import INSERT, LOCK, SELECT_LATEST, TRAFFIC_SQL, like_prefix

pytestmark = pytest.mark.integration

ADMIN_DSN = os.environ.get("SENTINEL_IT_ADMIN_DSN", "")
API_DSN = os.environ.get("SENTINEL_IT_API_DSN", "")
ROLLOUT_DSN = os.environ.get("SENTINEL_IT_ROLLOUT_DSN", "")

if not (ADMIN_DSN and API_DSN and ROLLOUT_DSN):
    pytest.skip("stack not configured (run via make test-integration)", allow_module_level=True)

FT = "it-type"  # never a real file type: the latest-row lookups of real rollouts are unaffected


def _row(rid: str, seq: int, frm: str | None, to: str) -> tuple[object, ...]:
    return (rid, seq, FT, "m", "2", "1", frm, to, "it", "integration test row", "{}")


def test_rollout_role_appends_and_reads_but_cannot_change() -> None:
    rid = f"it-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ROLLOUT_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(LOCK, (FT,))
        conn.execute(INSERT, _row(rid, 1, None, "APPROVED"))
        conn.execute(INSERT, _row(rid, 2, "APPROVED", "RING0"))
        latest = conn.execute(SELECT_LATEST, (FT,)).fetchone()
        assert latest is not None and (latest[0], latest[1], latest[7]) == (rid, 2, "RING0")
        for sql in (
            "UPDATE rollout_transitions SET reason = 'x' WHERE rollout_id = %s",
            "DELETE FROM rollout_transitions WHERE rollout_id = %s",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                conn.execute(sql, (rid,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM predictions WHERE false")
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            conn.execute(INSERT, _row(rid, 2, "APPROVED", "RING1"))  # one row per step


def test_even_the_owner_cannot_rewrite_rollout_history() -> None:
    rid = f"it-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT, _row(rid, 1, None, "APPROVED"))
        for sql, args in (
            ("UPDATE rollout_transitions SET to_state = 'COMPLETE' WHERE rollout_id = %s", (rid,)),
            ("DELETE FROM rollout_transitions WHERE rollout_id = %s", (rid,)),
            ("TRUNCATE rollout_transitions", ()),
        ):
            with (
                pytest.raises(psycopg.errors.RaiseException, match="append-only"),
                conn.transaction(),
            ):
                conn.execute(sql, args)


@pytest.mark.parametrize(
    ("seq", "frm", "to"),
    [(1, None, "RING0"), (1, "APPROVED", "APPROVED"), (2, None, "RING0"), (2, "RING0", "LIVE")],
)
def test_database_refuses_malformed_transitions(seq: int, frm: str | None, to: str) -> None:
    with (
        psycopg.connect(ROLLOUT_DSN) as conn,
        conn.transaction(force_rollback=True),
        pytest.raises(psycopg.errors.CheckViolation),
        conn.transaction(),
    ):
        conn.execute(INSERT, _row(f"it-{uuid.uuid4().hex[:12]}", seq, frm, to))


def test_api_may_record_a_kill_switch_rollback() -> None:
    rid = f"it-{uuid.uuid4().hex[:12]}"
    with psycopg.connect(API_DSN) as conn, conn.transaction(force_rollback=True):
        conn.execute(INSERT, _row(rid, 1, None, "APPROVED"))
        conn.execute(INSERT, _row(rid, 2, "APPROVED", "ROLLED_BACK"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "UPDATE rollout_transitions SET reason = 'x' WHERE rollout_id = %s", (rid,)
            )


def test_traffic_query_runs_as_the_rollout_role() -> None:
    with psycopg.connect(ROLLOUT_DSN) as conn:
        row = conn.execute(
            TRAFFIC_SQL,
            {
                "cand": "no-such-model/0",
                "prod": "no-such-model/1",
                "ring": "ring0",
                "since": "2000-01-01T00:00:00Z",
                "start": "2000-01-01T00:00:00Z",
                "canary": like_prefix("canary-"),
            },
        ).fetchone()
    assert row == (0, 0, 0, 0, 0, 0)
