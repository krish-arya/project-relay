"""Deterministic stand-in for Ollama, used by the test suite (MODEL_PROVIDER=fake).

It is a test double, not a classifier we evaluate: a handful of keyword rules
so the pipeline produces plausible, repeatable output without a model server.
Tests can queue raw responses per task (e.g. invalid JSON) to exercise repair
and failure paths, or make calls fail to exercise retries and the breaker.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict

from relay.gateway import ModelUnavailable

DIM = 768

_CATEGORY_RULES = [
    ("clinical", r"pain|swell|bleed|ibuprofen|medic"),
    ("records_privacy", r"records?\b"),
    ("appointments", r"appointment|reschedul|cancel|open on|sunday|slot"),
    ("warranty_repair", r"warranty|crack|defect|broken|worn"),
    ("order_change", r"pincode|change (my|the) (address|size)|wrong address"),
    ("assembly_setup", r"assembl"),
    ("returns_refunds", r"return|refund"),
    ("shipping_delivery", r"deliver|arriv|where is|kab|shipping"),
    ("billing_payments", r"emi|cash on delivery|invoice|insur|price|cost|charged|payment"),
]


def _ticket_text(messages: list[dict]) -> str:
    joined = "\n".join(m["content"] for m in messages if m["role"] == "user")
    m = re.search(r"<ticket>(.*?)</ticket>", joined, re.S)
    return (m.group(1) if m else joined).lower()


def _tokens(text: str) -> int:
    return max(1, len(text.split()))


class FakeProvider:
    name = "fake"

    def __init__(self) -> None:
        self.scripted: dict[str, list[str]] = defaultdict(list)
        self.fail_next: dict[str, int] = defaultdict(int)
        self.calls: list[tuple[str, str]] = []

    # -- test hooks -------------------------------------------------------------

    def script(self, task: str, *responses: str) -> None:
        self.scripted[task].extend(responses)

    def fail(self, model: str, times: int) -> None:
        self.fail_next[model] += times

    # -- provider API -----------------------------------------------------------

    def chat(self, model: str, messages: list[dict], schema: dict | None, options: dict) -> tuple[str, int, int]:
        task = "triage" if schema and schema.get("title") == "TriageResult" else "draft"
        self.calls.append((task, model))
        if self.fail_next[model] > 0:
            self.fail_next[model] -= 1
            raise ModelUnavailable(f"fake failure for {model}", "connection")
        if self.scripted[task]:
            content = self.scripted[task].pop(0)
        else:
            content = json.dumps(self._triage(messages) if task == "triage" else self._draft(messages))
        prompt = sum(_tokens(m["content"]) for m in messages)
        return content, prompt, _tokens(content)

    def embed(self, model: str, texts: list[str]) -> tuple[list[list[float]], int]:
        self.calls.append(("embed", model))
        if self.fail_next[model] > 0:
            self.fail_next[model] -= 1
            raise ModelUnavailable(f"fake failure for {model}", "connection")
        return [embed_text(t) for t in texts], sum(_tokens(t) for t in texts)

    # -- behaviour --------------------------------------------------------------

    def _triage(self, messages: list[dict]) -> dict:
        text = _ticket_text(messages)
        category = next((c for c, pat in _CATEGORY_RULES if re.search(pat, text)), "other")
        injection = bool(re.search(r"ignore (all )?previous|system:|assistant:|admin mode", text))
        money = bool(re.search(r"overcharged|court|promised", text))
        priority = "urgent" if re.search(r"court|worse|legal", text) else "high" if money or injection else "normal"
        action = "escalate" if injection or money or category == "clinical" or "bulk" in text else "draft_reply"
        return {
            "category": category, "priority": priority, "injection_suspected": injection,
            "money_dispute": money, "proposed_action": action, "missing_details": [], "reason": "fake triage",
        }

    def _draft(self, messages: list[dict]) -> dict:
        user = messages[-1]["content"]
        m = re.search(r"Passages: (\[.*?\])\n", user, re.S)
        passages = json.loads(m.group(1)) if m else []
        ask = re.search(r"missing details before we can help: (.*?)\.\n", user)
        if ask:
            return {"reply": f"Thanks for reaching out. Could you please share {ask.group(1)}?", "cited_kb_ids": []}
        if "no question to answer" in user:
            return {"reply": "Thank you for your kind words. We are glad to hear it.", "cited_kb_ids": []}
        if not passages:
            return {"reply": "Thanks for reaching out. A colleague will get back to you.", "cited_kb_ids": []}
        p = passages[0]
        first = re.split(r"(?<=\.)\s", p["text"])[0]
        return {"reply": f"Thanks for reaching out. {first} [{p['id']}]", "cited_kb_ids": [p["id"]]}


def embed_text(text: str) -> list[float]:
    """Hashed bag of word prefixes: similar wording gives similar vectors, deterministically."""
    text = re.sub(r"^search_(query|document):", "", text)
    vec = [0.0] * DIM
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if len(w) < 3:
            continue
        h = int(hashlib.md5(w[:5].encode()).hexdigest(), 16)
        vec[h % DIM] += 1.0 if (h >> 10) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]
