"""Seed each tenant's KB with embeddings. Idempotent: unchanged articles are not re-embedded."""

from __future__ import annotations

import json
from pathlib import Path

import psycopg

from relay.config import ROOT
from relay.db import known_tenants, tenant_tx
from relay.gateway import CallContext, Gateway

DATA = ROOT / "data"


def document_text(article: dict) -> str:
    # nomic-embed-text expects task prefixes: search_document for corpus, search_query for queries.
    return f"search_document: {article['title']}. {article['body']}"


def seed(conn: psycopg.Connection, gw: Gateway, run_id: str | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for tenant in known_tenants(conn):
        path = DATA / tenant / "kb.jsonl"
        if not path.exists():
            continue
        articles = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for a in articles:
            if a["tenant"] != tenant:
                raise ValueError(f"{path}: article {a['id']} claims tenant {a['tenant']!r}")
        with tenant_tx(conn, tenant) as cur:
            cur.execute("SELECT id, body, title, internal, embed_model FROM kb_articles")
            current = {r["id"]: r for r in cur.fetchall()}
        stale = [
            a for a in articles
            if a["id"] not in current
            or (current[a["id"]]["body"], current[a["id"]]["title"], current[a["id"]]["internal"], current[a["id"]]["embed_model"])
            != (a["body"], a["title"], a["internal"], gw.settings.model_embed)
        ]
        if stale:
            ctx = CallContext(tenant_id=tenant, run_id=run_id)
            vectors = gw.embed([document_text(a) for a in stale], ctx)
            with tenant_tx(conn, tenant) as cur:
                for a, v in zip(stale, vectors):
                    cur.execute(
                        """INSERT INTO kb_articles (tenant_id, id, title, body, internal, embedding, embed_model)
                           VALUES (%s, %s, %s, %s, %s, %s::vector, %s)
                           ON CONFLICT (tenant_id, id) DO UPDATE
                              SET title = EXCLUDED.title, body = EXCLUDED.body, internal = EXCLUDED.internal,
                                  embedding = EXCLUDED.embedding, embed_model = EXCLUDED.embed_model""",
                        (tenant, a["id"], a["title"], a["body"], bool(a.get("internal", False)),
                         "[" + ",".join(f"{x:.7f}" for x in v) + "]", gw.settings.model_embed),
                    )
        counts[tenant] = len(stale)
    return counts
