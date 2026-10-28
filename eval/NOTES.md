### How we got here

The first end-to-end run used the 1.5B triage model with only a few deterministic overrides. In a
single-run eval it scored **83% action accuracy**:
- Harbor billing questions were labelled "clinical" and escalated.
- An off-topic bulk-order request got an acknowledgement draft.
- Almost every priority was "normal".

Three changes moved it to 96% on one run:
- Retrieval as a second classifier (DECISIONS §3).
- The "other + asks something → escalate" rule.
- Priority rules taken from the brief's definitions.

**These changes were made by looking at the visible golden tickets**, so the numbers above are a training-set
score in all but name. Expect unseen tickets to score lower. Unseen tickets that are worded like the samples
should hold up best. Tickets whose topic spans two KB articles are the weakest case: retrieval isn't decisive
there, and the 1.5B model's category decides alone.

Injection detector history:
- **First measurement, before any tuning:** 6/8 probes strongly flagged, 1 weak hit (PR-14), 1 miss (PR-16),
  and 1 weak false positive (PR-03, "show me the instructions for the pedals").
- **What changed:** I added persona and authority-claim features and narrowed "reveal" targets to hidden or
  internal material.
- **Since then** the probes are not a held-out test.

### Known remaining errors

- **HD-T12 ("thank you")** is escalated when the model labels it clinical and no KB article is a decisive
  match. This is deliberate: the safe failure (DECISIONS, "Where I disagree").
- **NW-T08 priority:** the model says urgent or high. The cap brings urgent down to high, but the golden label
  is low. The "low = general information" rule does not fire because the ticket carries an order ID.

### Reproducibility

- Temperature 0 and seed 7 make Ollama on CPU close to deterministic on one machine. The stability rows above
  show how close.
- On a different CPU, Ollama version or quantisation, small differences in triage output are expected. The
  deterministic layers (detector, policy rules, retrieval classifier) reduce how much they change the final
  action: retrieval scores on the same embedding model barely move.
- If your re-run differs, the per-ticket table shows exactly where.

### What I would do next

1. Write 50 held-out tickets (by someone else) to measure overfitting honestly.
2. Run qwen2.5:3b for triage with 3-sample majority voting and compare accuracy, stability and latency.
3. Calibrate `RETRIEVAL_CATEGORY_MARGIN` on held-out data rather than the visible tickets, and report the
   accuracy of the retrieval classifier and the model separately.
4. Add Hindi and Hinglish legal-threat and clinical vocabularies, and probes for each.
