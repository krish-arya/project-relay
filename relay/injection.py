"""Prompt-injection detection.

Two independent signals, combined by the lead agent:

1. A structural scorer (this module). It does not look for "bad words"; it
   scores *features of text addressed to a machine*: fake role headers,
   instruction overrides, persona switches, requests to reveal hidden data,
   commands to perform privileged actions, chat-template tokens and
   separator fences. Each feature has a weight; the score is the sum.
2. The triage model's `injection_suspected` flag.

Flag rule: score >= STRONG, or (model flag and score >= WEAK). The model alone
cannot flag (small models over-flag ordinary complaints) and cannot unflag a
strong structural hit. Recall and false positives are measured by `make eval`
on the golden tickets plus data/probes/injection_probes.jsonl.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from relay.text import normalize

STRONG = 3
WEAK = 1

# (name, weight, pattern over normalized text)
FEATURES: list[tuple[str, int, re.Pattern[str]]] = [
    ("role_header", 3, re.compile(r"(^|[\s.:;!?\-*#>])(system|assistant|developer|admin|ai|bot|model)\s*:")),
    ("chat_template_token", 3, re.compile(r"<\|im_start\|>|<\|system\|>|\[/?inst\]|<<sys>>|</?s>")),
    (
        "instruction_override",
        3,
        re.compile(
            r"\b(ignore|disregard|forget|override|bypass)\b.{0,30}\b(previous|prior|above|earlier|all|any|your|the)\b"
            r".{0,20}\b(instructions?|rules|prompts?|guidelines|polic(y|ies)|directives)\b"
        ),
    ),
    # Hinglish: "pichle saare instructions bhool jao", "rules ko ignore karo"
    ("instruction_override_hinglish", 3, re.compile(r"\b(instructions?|rules|niyam)\b.{0,25}\b(bhool|bhul|ignore kar|mat maano)")),
    (
        "persona_switch",
        2,
        re.compile(
            r"\byou are now\b|\bact as (an? )?(admin|administrator|developer|system)|\b(admin|developer|god|debug|jailbreak) mode\b"
            r"|\bnew instructions\b|\bpretend (you are|to be)\b|\bwithout (any )?(restrictions|filters|rules)\b|\bfrom now on you\b"
        ),
    ),
    (
        "reveal_hidden",
        2,
        re.compile(
            r"\b(print|reveal|show|tell|include|output|repeat|dump|leak|paste|share|list)\b.{0,40}"
            r"\b(system prompt|your (instructions|prompt|rules)|internal|staff[- ]only|staff notes|confidential|hidden|secret"
            r"|other (customer|patient)s?'?|previous (customer|patient)|all (customers|patients))"
        ),
    ),
    (
        "privileged_action",
        2,
        re.compile(r"\b(approve|issue|grant|authori[sz]e|transfer|send)\b.{0,25}\b(full )?(refund|credit|payment|money|discount)\b.{0,40}\b(to|into)\b"),
    ),
    # Claims of authority the sender cannot have: "as authorised by your admin team".
    ("claimed_authority", 1, re.compile(r"\b(authori[sz]ed|approved|instructed|ordered) by (your|the) (admin|manager|supervisor|system|developer)")),
    ("other_party_records", 1, re.compile(r"\b(details|records|data)\b.{0,20}\b(of|for)\b.{0,10}\b(patient|customer|user)\s+[a-z]{1,4}-\d{3,}")),
    ("separator_fence", 1, re.compile(r"(-{4,}|={4,}|#{3,}|`{3,})")),
]


@dataclass
class InjectionReport:
    score: int
    features: list[str] = field(default_factory=list)

    @property
    def strong(self) -> bool:
        return self.score >= STRONG


def score(text: str) -> InjectionReport:
    norm = normalize(text)
    hits = [(name, w) for name, w, pat in FEATURES if pat.search(norm)]
    return InjectionReport(score=sum(w for _, w in hits), features=[n for n, _ in hits])


def decide(report: InjectionReport, model_flag: bool) -> bool:
    return report.score >= STRONG or (model_flag and report.score >= WEAK)
