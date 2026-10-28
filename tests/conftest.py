"""Test fixtures. No Ollama: every model call goes to the deterministic FakeProvider.

Needs the compose Postgres (`make up`). A fresh database `relay_test` is created
once per session and truncated before each test.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import pytest

from relay.config import load_settings
from relay.contracts import Ticket
from relay.db import connect, migrate
from relay.fake_model import FakeProvider
from relay.gateway import CircuitBreaker, Gateway
from relay.runs import create_run
from relay.seed import seed

TEST_DB = "relay_test"
TABLES = "kb_articles, kb_fingerprints, runs, tickets, decisions, approvals, outbox, cases, audit_log, model_calls, jobs, rejected_inputs"


def _swap_db(url: str, name: str) -> str:
    return url.rsplit("/", 1)[0] + "/" + name


@pytest.fixture(scope="session")
def settings():
    base = load_settings()
    s = dataclasses.replace(
        base,
        database_admin_url=_swap_db(base.database_admin_url, TEST_DB),
        database_url=_swap_db(base.database_url, TEST_DB),
        model_provider="fake",
        chaos_fail_rate=0.0,
        token_budget_ticket=8000,
        token_budget_run=400000,
        backoff_base_s=0.0,
        backoff_max_s=0.0,
        max_attempts=3,
        lease_s=60,
        min_score=0.05,
        category_margin=1.0,  # fake embeddings are crude; keep the model's category in tests
    )
    with psycopg.connect(_swap_db(base.database_admin_url, "postgres"), autocommit=True) as c:
        c.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        c.execute(f"CREATE DATABASE {TEST_DB}")
    migrate(s)
    return s


@pytest.fixture
def admin(settings):
    with psycopg.connect(settings.database_admin_url, autocommit=True) as c:
        yield c


@pytest.fixture
def conn(settings, admin):
    admin.execute(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE")
    with connect(settings) as c:
        yield c


@pytest.fixture
def fake():
    return FakeProvider()


@pytest.fixture
def gw(settings, conn, fake):
    return Gateway(settings, fake, conn, breaker=CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_s))


@pytest.fixture
def seeded(conn, gw):
    seed(conn, gw)
    return conn


@pytest.fixture
def run_id(conn, settings):
    return create_run(conn, "test", settings.token_budget_run)


def make_ticket(id: str, tenant: str, body: str, subject: str = "Help", customer_ref: str = "C-1") -> Ticket:
    return Ticket(id=id, tenant=tenant, customer_ref=customer_ref, subject=subject, body=body,
                  received_at=datetime(2026, 10, 20, tzinfo=timezone.utc))


def write_inbox(path: Path, tickets: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(t) for t in tickets) + "\n", encoding="utf-8")
    return path


def ticket_dict(id: str, tenant: str, body: str, subject: str = "Help", customer_ref: str = "C-1") -> dict:
    return {"id": id, "tenant": tenant, "customer_ref": customer_ref, "subject": subject, "body": body,
            "received_at": "2026-10-20T09:00:00+05:30"}
