"""Database access.

All tenant data is read and written inside `tenant_tx`, which sets the
transaction-local `app.tenant_id` that the RLS policies check. There is no
helper that runs a tenant query without it: forgetting the tenant gives zero
rows, not every tenant's rows.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from relay.config import ROOT, Settings

MIGRATIONS = ROOT / "migrations"


class UnknownTenant(Exception):
    pass


def connect(settings: Settings, *, admin: bool = False) -> psycopg.Connection:
    url = settings.database_admin_url if admin else settings.database_url
    return psycopg.connect(url, row_factory=dict_row, autocommit=False)


@contextmanager
def tenant_tx(conn: psycopg.Connection, tenant_id: str) -> Iterator[psycopg.Cursor]:
    """Open a transaction bound to one tenant. Commits on success, rolls back on error."""
    if not tenant_id:
        raise UnknownTenant("empty tenant id")
    with conn.transaction():
        with conn.cursor() as cur:
            # set_config(..., true) is SET LOCAL: it ends with the transaction,
            # so a pooled connection can never carry a tenant into the next one.
            cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
            yield cur


@contextmanager
def global_tx(conn: psycopg.Connection) -> Iterator[psycopg.Cursor]:
    """Transaction with no tenant: only tenant-free tables are visible."""
    with conn.transaction():
        with conn.cursor() as cur:
            yield cur


def known_tenants(conn: psycopg.Connection) -> dict[str, dict]:
    with global_tx(conn) as cur:
        cur.execute("SELECT id, name, aliases FROM tenants ORDER BY id")
        return {r["id"]: r for r in cur.fetchall()}


def migrate(settings: Settings, *, admin_url: str | None = None) -> list[str]:
    """Create the app role and apply pending migrations in order. Idempotent."""
    applied: list[str] = []
    with psycopg.connect(admin_url or settings.database_admin_url, autocommit=True) as conn:
        conn.execute(
            """
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'relay_app') THEN
                    CREATE ROLE relay_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
                END IF;
            END $$;
            """
        )
        conn.execute(
            sql.SQL("ALTER ROLE relay_app WITH LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD {}").format(
                sql.Literal(settings.app_password)
            )
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for path in sorted(Path(MIGRATIONS).glob("*.sql")):
            if path.name in done:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            applied.append(path.name)
    return applied


def audit(
    cur: psycopg.Cursor,
    tenant_id: str,
    ticket_id: str | None,
    actor: str,
    action: str,
    *,
    from_state: str | None = None,
    to_state: str | None = None,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    from psycopg.types.json import Jsonb

    cur.execute(
        """INSERT INTO audit_log (tenant_id, ticket_id, actor, action, from_state, to_state, reason, details)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (tenant_id, ticket_id, actor, action, from_state, to_state, reason, Jsonb(details or {})),
    )
