# Decisions and trade-offs

## 1. Plain Python orchestration, no agent framework

**Chosen:** a lead agent written as ordinary functions (`relay/lead.py`) with Pydantic contracts, and a
Postgres-backed queue.
**Rejected:** LangGraph or CrewAI.
**Why:** the hard parts here are a durable handoff, a retry, a resume after a crash and an approval
pause. In this design each of those is a row in a table plus one transaction, and anyone can read it. A
framework would add its own checkpoint store next to Postgres, so we would have two sources of truth for
"where is this ticket". The cost: there's no graph visualisation, and adding a branch means editing `route()`.

## 2. The model proposes; deterministic rules can only make the outcome safer

**Chosen:** triage (qwen2.5:1.5b) proposes category, priority, action and flags. Code in `policy.py` and
`injection.py` can override in one direction only: escalate instead of reply, ask instead of guess, raise
priority. Code never turns a model escalation into a reply.
**Rejected:** (a) trusting the model's action directly; (b) a fully rule-based router.
**Why:** on the first real run the 1.5B model put almost everything at "normal" priority, labelled Harbor
billing questions "clinical", and missed half the injection probes. Pure rules wouldn't generalise to
wording we haven't seen. The hybrid keeps the model's language understanding (including Hinglish) and makes
the safety rules from the brief impossible to talk out of. The cost: the rules are a second place where
behaviour lives, so `docs/ARCHITECTURE.md` lists them in order and `tests/test_routing.py` pins them down.

## 3. Retrieval doubles as a second classifier, with thresholds set on the visible data

**Chosen:** each KB article maps to a category (`kb_categories` in the tenant policy). If the best match
beats the runner-up by at least `RETRIEVAL_CATEGORY_MARGIN` (0.04), its category wins.
**Rejected:** a bigger triage model (qwen2.5:3b for triage roughly doubles triage latency), or more
few-shot examples.
**Why:** retrieval top-1 was right on about 22 of 24 visible tickets, while the 1.5B model's category was
right on about 18. **Honest caveat:** I chose the margin (0.04) and `RETRIEVAL_MIN_SCORE` (0.55) by looking
at the visible tickets' scores (nomic-embed-text scores sit in a narrow 0.5 to 0.9 band). They are tuned to
the data you gave us, so your unseen tickets are the real test. Both thresholds are environment variables,
and the internal report shows the scores in each decision's trace.

## 4. Injection detection: structural scorer plus model flag, measured on probes

**Chosen:** a weighted scorer over *features of text addressed to a machine*: fake role headers
(`SYSTEM:`, `assistant:`), instruction overrides (English and Hinglish), persona switches, requests to reveal
hidden or internal data, privileged-action commands, chat-template tokens, separator fences and claimed
authority. The text is normalised first (NFKC, zero-width characters removed). A ticket is flagged if
score ≥ 3, or if the model flag is set and score ≥ 1.
**Rejected:** a keyword list (the brief calls it weak, and words like "ignore" and "system" occur in real
complaints) and the model flag alone.
**Why:** on the probe set the model flag alone caught 4 of 8 injections; the combination caught 8 of 8,
with 0 false positives on 8 benign look-alikes ("I tried to ignore the brake noise", "your system shows
delivered"). See `docs/EVAL.md`. **Caveat:** I wrote the probes myself and then widened the detector after
the first measurement (first run: 6/8 strong, 1 weak false positive). They are no longer held out.
**Defence in depth:** a flagged ticket is never drafted. Even when an injection is missed, the drafter only
sees public passages, so the staff-only text isn't available to leak, and the checker blocks canary codes,
internal-text shingles and money promises.

## 5. Exactly-once comes from database constraints, not careful code

**Chosen:**
- `decisions` primary key (tenant, ticket) and `outbox` UNIQUE (tenant, ticket) and UNIQUE idempotency_key.
- A job is completed only if this worker still holds the lease, **in the same transaction** that writes the
  decision, approval, case and audit rows.
- `out/*.jsonl` files are regenerated from the tables, never appended to.

**Rejected:** checking "already processed?" in application code before writing; appending to the JSONL files.
**Why:** application checks race between two workers, and appends duplicate rows on a retry. The cost: if a
worker dies mid-ticket, the model calls it already made are paid for again by the next worker (visible in
`model_calls`).

## 6. Queue in Postgres, not Redis or Celery

**Chosen:** a `jobs` table and `relay_claim_job()` (`FOR UPDATE SKIP LOCKED`, lease, attempts), with
backoff using full jitter and dead letters into `cases`.
**Rejected:** Redis or RabbitMQ plus Celery.
**Why:** one less service to run from a clean clone, and the job state commits in the same transaction as
the decision. That is what makes resume after a kill duplicate-free. It is fine for this volume. Section 9
says when to move off it.

## 7. The checker sees other tenants only as hashes

**Chosen:** a trigger stores md5 hashes of each KB body's 5-word shingles in `kb_fingerprints`, which the
app role can't read. The checker gets *other* tenants' hashes through a `SECURITY DEFINER` function and
compares them with the draft's shingles.
**Rejected:** letting the checker read every tenant's KB (that breaks isolation for a safety feature), or
checking only tenant names.
**Why:** "no other tenant's content" can then be enforced without any code path that holds another tenant's
text. The cost: paraphrased leaks are not caught, only copied runs of five or more words.

## 8. Escalations draft nothing

**Chosen:** `escalate` produces a case with an owner, a severity and a reason, and no customer text.
**Rejected:** an automatic "we've passed this to a specialist" holding reply.
**Why:** the brief says to draft nothing that promises money, outcomes or dates. A holding reply would also
need approval anyway. The cost: the customer hears nothing until a human picks up the case.

## 9. Scaling note (S4): 50 tenants

What changes:
- **Tenants as data.** Tenant rows and `policy.json` move into a `tenant_policies` table with an admin CLI.
  RLS stays exactly as it is, because the policy doesn't change with the number of tenants.
- **Workers.** Several worker processes per host, plus per-tenant fairness: claim round-robin by tenant so
  one noisy inbox can't starve the others, and set per-tenant budgets in `runs`/`tenant_budgets`.
- **Models.** One small GPU box running Ollama or vLLM (for example an L4 24 GB) serves qwen2.5 3B/7B for
  all tenants. Triage at about 0.5 s per ticket on a GPU, versus 3.5 s on this laptop CPU, handles roughly
  5,000 tickets/hour. Keep the free hosted route off. If volume justifies it, add a paid route behind the
  same gateway.
- **Database.** Managed Postgres with pgvector (one primary, about 2 vCPU / 8 GB to start), PgBouncer in
  transaction mode (safe, because the tenant setting is transaction-local), and an HNSW index on
  `kb_articles.embedding` once KBs exceed a few thousand articles.
- **Retention and audit.** Partition `audit_log` and `model_calls` by month, and ship them to cold storage.
- **Rough cost** (Indian region, on-demand, before discounts): about USD 250–450/month for the GPU node,
  about USD 100–150 for managed Postgres, and about USD 50 for workers, logs and backups. Total about
  **USD 400–650/month**, or roughly USD 8–13 per tenant. CPU-only inference on two 8-vCPU nodes would cost
  about USD 150 instead of the GPU, at around 10x the latency.

## Where I disagree with the brief or the golden labels

The rules are implemented as written. These are notes, not deviations.

- **HD-T04 (wife asks for husband's records).** The scenario table says Harbor records "may only go to the
  patient", but HD-KB-04 allows anyone with the patient's written consent. The golden label (`ask_for_info`)
  follows the KB. So does the system: it asks for written consent and never sends records. I think the KB
  should win, because it is the tenant's own policy, but the scenario text should be fixed.
- **HD-T12 ("Thank you").** The golden label `draft_reply` is right. When the 1.5B model calls it "clinical"
  and no KB match is decisive, the system **keeps the clinical label and escalates**. That is a deliberate
  choice: when the model says clinical and we can't disprove it, a clinician spending ten seconds on a thank
  you note is better than an agent answering a symptom. It costs one action error on the visible set, shown in EVAL.md.
- **NW-T08 (tyre warranty, priority `low`).** The customer gives an order ID, so it isn't purely general
  information. `normal` is defensible. The system's rule (low only when no order or patient reference)
  produces `high` or `normal` here, and the eval counts it as an error. I left the rule as it is rather than
  special-casing warranty questions to match one label.
