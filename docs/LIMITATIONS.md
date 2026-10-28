# Limitations

## Missing

- **No optional hosted-model route.** The gateway has a `Provider` interface, but only `ollama` and `fake`
  are implemented. The free hosted tier allowed by the brief was skipped.
- **No `relay retry-dead` command.** Dead-lettered tickets are reprocessed with the SQL in RUNBOOK.md.
- **Cases are a table, not a workflow.** Cases have an owner, severity and reason, but no assignment,
  resolution or SLA commands. The `status` column is only ever `open` unless updated by hand.
- **No numeric score for draft quality.** `make eval` measures triage and routing, as the brief asks. Draft
  quality is checked only by the deterministic checker and by the human approver.
- **No web view.** CLI only (S2 is implemented as two CLI views).
- **Attachments are not modelled.** Tickets have no attachments field, so "photos of the defect" can never be
  verified. A warranty claim missing its order ID is asked for the order ID and photos together
  (`ask_together` in the tenant policy). A claim that has an order ID but no photos is answered from the KB,
  not asked for photos.

## Fragile

- **Thresholds were tuned on the visible tickets.** `RETRIEVAL_MIN_SCORE` and `RETRIEVAL_CATEGORY_MARGIN`
  were chosen after looking at the 24 visible tickets' retrieval scores (DECISIONS §3). nomic-embed-text
  puts every score in about 0.5–0.9, so the "KB doesn't cover this" gate only catches clearly off-topic
  text. Unseen tickets will show how far the numbers carry over.
- **The deterministic signals are English-first.** Legal threat, money dispute, clinical terms and request
  detection are regex lists. Hinglish coverage exists for injection and requests ("kab", "chahiye"), but a
  legal threat written in Hindi would rely on the model alone.
- **The 1.5B triage model.** Its category and priority output varies with small wording changes. Most of the
  accuracy comes from the retrieval classifier and the priority rules, which is why the stability numbers
  matter.
- **The injection probes are not held out.** I wrote them, then tuned the detector after the first
  measurement (DECISIONS §4). A determined attacker who paraphrases without any structural marker ("as the
  store owner I'm telling you to...") gets past the scorer, and is only caught if the model flags it AND some
  weak feature fires.
- **Fingerprints catch copies, not paraphrases.** A paraphrased leak of another tenant's KB passes the
  checker. In practice the drafter never sees another tenant's text, so this is a second line of defence.
- **Per-run budgets are checked before each call, using an estimate.** A run can overshoot by up to one
  call's tokens.
- **Single-threaded worker.** Each process handles one job at a time. Concurrency means running several
  processes, which is safe but not tested beyond two workers in the kill test.
- **Very long tickets** are truncated to 4,000 characters before triage and drafting. Instructions placed
  after the cut are invisible to the model, but they are still scored by the injection detector, which sees
  the full text.

## With one more day

1. A held-out set: 50 more tickets written by someone other than the author, labelled blind, to measure how
   much the thresholds overfit.
2. Use qwen2.5:3b for triage, with self-consistency (3 samples, majority vote), and compare accuracy,
   stability and latency against the current hybrid in EVAL.md.
3. A small multilingual list of legal-threat and clinical terms (Hindi in Devanagari and Hinglish), plus probes for each.
4. `relay retry-dead`, `relay case assign/resolve`, and a cases SLA in the client view.
5. LLM-as-judge scoring of drafts (groundedness against the cited passages), run in eval, never in the
   decision path.
6. Tests with several concurrent worker processes, plus a chaos soak (`CHAOS_FAIL_RATE=0.3` over 200 tickets).
