"""Tenant isolation: RLS fails closed, tenants cannot read or write each other's rows."""

from __future__ import annotations

import psycopg
import pytest

from relay.agents import retrieve
from relay.db import global_tx, tenant_tx
from relay.gateway import CallContext
from relay.ingest import ingest_file

from .conftest import make_ticket, ticket_dict, write_inbox

TENANT_TABLES = ["kb_articles", "tickets", "decisions", "approvals", "outbox", "cases", "audit_log", "model_calls", "jobs"]


def test_app_role_cannot_bypass_rls(admin):
    role = admin.execute("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = 'relay_app'").fetchone()
    assert role == (False, False)
    forced = dict(admin.execute(
        "SELECT relname, relforcerowsecurity FROM pg_class WHERE relname = ANY(%s)", (TENANT_TABLES,)
    ).fetchall())
    assert forced == {t: True for t in TENANT_TABLES}


def test_missing_tenant_setting_returns_zero_rows(seeded, settings, run_id):
    from relay.ingest import store_ticket

    store_ticket(seeded, settings, make_ticket("T1", "northwind", "hello"), run_id)
    with tenant_tx(seeded, "northwind") as cur:
        cur.execute("SELECT count(*) AS n FROM kb_articles")
        assert cur.fetchone()["n"] == 7
    # Same connection, new transaction, no tenant set: nothing is visible.
    with global_tx(seeded) as cur:
        for table in TENANT_TABLES:
            cur.execute(f"SELECT count(*) AS n FROM {table}")
            assert cur.fetchone()["n"] == 0, table


def test_tenant_setting_does_not_leak_into_next_transaction(seeded):
    with tenant_tx(seeded, "harbor") as cur:
        cur.execute("SELECT count(*) AS n FROM kb_articles")
        assert cur.fetchone()["n"] == 7
    with global_tx(seeded) as cur:
        cur.execute("SELECT current_setting('app.tenant_id', true) AS t")
        assert cur.fetchone()["t"] in (None, "")


def test_tenant_sees_only_its_own_rows(seeded):
    with tenant_tx(seeded, "northwind") as cur:
        cur.execute("SELECT DISTINCT tenant_id FROM kb_articles")
        assert [r["tenant_id"] for r in cur.fetchall()] == ["northwind"]
        cur.execute("SELECT count(*) AS n FROM kb_articles WHERE id LIKE 'HD-%%'")
        assert cur.fetchone()["n"] == 0


def test_cannot_write_rows_for_another_tenant(seeded):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(seeded, "northwind") as cur:
            cur.execute(
                """INSERT INTO tickets (tenant_id, ticket_id, customer_ref, subject, body, received_at)
                   VALUES ('harbor', 'X1', 'P-1', 's', 'b', now())"""
            )


def test_fingerprints_unreadable_but_foreign_hashes_available(seeded):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with tenant_tx(seeded, "northwind") as cur:
            cur.execute("SELECT count(*) AS n FROM kb_fingerprints")
    with tenant_tx(seeded, "northwind") as cur:
        cur.execute("SELECT count(*) AS n FROM relay_foreign_fingerprints()")
        assert cur.fetchone()["n"] > 0
    with pytest.raises(psycopg.errors.RaiseException):
        with global_tx(seeded) as cur:
            cur.execute("SELECT * FROM relay_foreign_fingerprints()")


def test_retrieval_is_tenant_bound(seeded, gw):
    t = make_ticket("T1", "harbor", "Are you open on Sunday? What about my appointment?")
    passages = retrieve(seeded, gw, t, CallContext("harbor", None, "T1"), 7)
    assert passages and all(p.kb_id.startswith("HD-KB-") for p in passages)


def test_unknown_tenant_is_rejected_and_logged(seeded, settings, run_id, tmp_path):
    inbox = write_inbox(tmp_path / "in.jsonl", [
        ticket_dict("Z1", "zenith", "hello"),
        ticket_dict("Z2", "", "hello"),
        ticket_dict("N1", "northwind", "Where is my order?"),
    ])
    stats = ingest_file(seeded, settings, inbox, run_id)
    assert stats.accepted == 1
    assert [r["ticket_id"] for r in stats.rejected] == ["Z1", "Z2"]
    with global_tx(seeded) as cur:
        cur.execute("SELECT ticket_id, reason FROM rejected_inputs ORDER BY id")
        rows = cur.fetchall()
    assert rows[0]["ticket_id"] == "Z1" and "unknown tenant" in rows[0]["reason"]
    for tenant in ("northwind", "harbor"):
        with tenant_tx(seeded, tenant) as cur:
            cur.execute("SELECT ticket_id FROM tickets")
            assert {r["ticket_id"] for r in cur.fetchall()} <= {"N1"}
