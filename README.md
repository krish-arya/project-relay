# Project Relay

A small multi-agent support desk for two tenants (Northwind Cycles, Harbor Dental), running on
local Ollama models, isolated with Postgres row-level security, gated by a human approver.

> Work in progress. Plan and assumptions below; full quickstart and status table to follow.

## Plan

1. Schema, migrations, RLS with a non-superuser role, KB seeding with embeddings, pgvector retrieval.
2. Lead agent + triage / retrieval / drafter sub-agents with Pydantic contracts; deterministic checker.
3. Approval CLI with an exactly-once outbox and a full audit trail; `out/decisions.jsonl`.
4. Model gateway (routing, metering, budgets, breaker, chaos), durable queue with leases and retries.
5. Tests with a fake model; evaluation over the golden set; docs.

## Assumptions

- Python 3.11+, managed with uv. Postgres runs from `docker-compose.yml` on host port **5433**
  (not 5432) so it does not collide with a local Postgres or the setup container from the brief.
