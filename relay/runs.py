from __future__ import annotations

import secrets
from datetime import datetime, timezone

import psycopg

from relay.db import global_tx


def new_run_id(prefix: str = "") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{prefix}{stamp}-{secrets.token_hex(2)}"


def create_run(conn: psycopg.Connection, kind: str, token_budget: int, run_id: str | None = None) -> str:
    run_id = run_id or new_run_id("eval-" if kind == "eval" else "")
    with global_tx(conn) as cur:
        cur.execute("INSERT INTO runs (run_id, kind, token_budget) VALUES (%s, %s, %s)", (run_id, kind, token_budget))
    return run_id


def finish_run(conn: psycopg.Connection, run_id: str) -> None:
    with global_tx(conn) as cur:
        cur.execute("UPDATE runs SET finished_at = now() WHERE run_id = %s", (run_id,))
