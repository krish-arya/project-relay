# Runbook

Commands assume the repo root. `make X` and `uv run relay X` are interchangeable.
Two commands give you the current picture:

```bash
uv run relay report --view internal   # runs, model calls and failures, decisions by status, queue, cases
uv run relay list                     # approval queue and open cases
```

## Ollama is down

**Symptoms:** `relay doctor` prints `[fail] ollama`. Worker log lines say `connection -> retry`, or
`... -> dead` after `QUEUE_MAX_ATTEMPTS`. `model_calls` rows show `ok=false, error_class=connection`.

**What the system does:**
- The gateway tries the fallback model first; with Ollama down that fails too.
- The job is re-queued with exponential backoff and jitter (`QUEUE_BACKOFF_BASE_S`, `QUEUE_BACKOFF_MAX_S`).
- After `QUEUE_MAX_ATTEMPTS` the job is dead-lettered: a `cases` row (source `dead_letter`, owner
  `platform-oncall`), a `failed` decision, and ticket state `dead_lettered`/`failed`.

Nothing is sent and nothing is lost.

**Operator steps:**
1. `ollama serve` (or restart the Ollama app), then `relay doctor` until every check says `[ok]`.
2. Any missing model: `ollama pull <model>`.
3. Tickets still waiting out their backoff resume on their own: run `relay worker`.
4. Dead-lettered tickets have a `failed` decision. To reprocess one, delete its decision and re-enqueue it
   as the superuser:
   ```sql
   DELETE FROM decisions WHERE tenant_id = '<t>' AND ticket_id = '<id>';
   UPDATE jobs SET status = 'queued', attempts = 0, run_after = now()
    WHERE tenant_id = '<t>' AND ticket_id = '<id>' AND kind = 'process';
   UPDATE cases SET status = 'resolved' WHERE tenant_id = '<t>' AND ticket_id = '<id>' AND source = 'dead_letter';
   ```
   Then run `relay worker`. (A `relay retry-dead` command is on the LIMITATIONS list.)

## The worker dies (killed, crashed, laptop closed)

**What the system does:** nothing is half-written. A ticket's decision, approval, case, audit row and job
completion commit in one transaction. The dead worker's job stays `leased` until `lease_until`
(`QUEUE_LEASE_S`, default 300 s), then any worker can reclaim it. If the old worker comes back, it can't
complete the job, because completion requires still holding the lease.

**Operator steps:**
1. Start a worker: `relay worker`. Several at once are safe.
2. To reclaim sooner than the lease, as the superuser:
   `UPDATE jobs SET lease_until = now() WHERE status = 'leased' AND leased_by = '<dead worker>';`
   (`leased_by` is `hostname:pid`.)
3. Check that nothing was duplicated: `relay report --view internal` shows `QUEUE: {'done': N}`.
   `out/decisions.jsonl` has one row per ticket by construction.

## A budget runs out

**Symptoms:** decisions with `status = held_budget` (the internal report's "DECISIONS BY STATUS"). The
trace says whether it was the `ticket` or the `run` budget. The run does not crash; other tickets carry on
until the run budget is spent.

**Operator steps:**
1. Decide whether the spend is legitimate. Check tokens per ticket in `relay report --view internal`. A
   very long ticket body is truncated to 4,000 characters but can still be expensive.
2. Raise the limit in `.env`: `TOKEN_BUDGET_TICKET` or `TOKEN_BUDGET_RUN`.
3. `relay requeue-held` deletes the held decisions and re-queues those tickets (audited as
   `requeued_after_budget`), then run `relay worker`.

## The database is unreachable

**Symptoms:** `relay doctor` prints `[fail] postgres`. Every command exits with a psycopg connection error
before doing anything.

**What the system does:**
- Because all state lives in Postgres, nothing proceeds and nothing is half-written.
- Model calls already made for an in-flight ticket are lost. The job is still leased in the database, so it
  is retried after the lease expires.

**Operator steps:**
1. `docker compose ps`, then `docker compose up -d --wait`. Check `RELAY_PG_PORT` (default 5433) is free.
2. `relay doctor`, then `relay worker` to resume.
3. If the volume was lost, recover from backup. In the demo, `make clean-db && make demo` rebuilds from the
   inbox files: ingestion is idempotent, but approvals would need redoing.

## A draft is rejected twice

**What the system does:**
- First rejection: the reason is stored and the ticket goes to `redrafting`. A `redraft` job reruns the
  lead agent with the reviewer's reason as feedback, and a v2 draft lands in the approval queue (when the CLI
  runs it inline, you see it immediately).
- Second rejection: the ticket and decision become `escalated`, and a case with source `rejected_twice` is
  opened for the tenant's default owner, quoting the last reason. No third draft is attempted.

**Operator steps:**
1. `relay show <ticket>` shows both drafts, both reasons, the case and the full audit trail.
2. The case owner replies to the customer outside the system. If the drafter keeps making the same mistake,
   the KB article it cites is usually missing a fact, so fix the KB (`data/<tenant>/kb.jsonl`, then
   `relay seed` re-embeds only changed articles).

## Unknown tenant or malformed tickets

These are rejected at ingest, recorded in `rejected_inputs`, and written to `out/rejected_inputs.jsonl`
(reason only, a hash instead of the text). They are never processed. Fix the source system and re-send.
