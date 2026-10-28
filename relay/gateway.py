"""Model gateway: the only module that talks to a model.

Responsibilities, in call order:
  1. route by task (small model for triage, larger model for drafting)
  2. enforce the per-ticket and per-run token budgets *before* the call
  3. skip models whose circuit breaker is open, fall back to the second model
  4. optionally fail on purpose (CHAOS_FAIL_RATE) to exercise recovery
  5. meter every attempt (model, tokens in/out, latency, ok/error) in model_calls
  6. for structured tasks: validate against a Pydantic contract, one repair attempt
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

import httpx
import psycopg
from pydantic import BaseModel, ValidationError

from relay.config import Settings
from relay.db import tenant_tx

T = TypeVar("T", bound=BaseModel)

# Upper bound on generated tokens per task; also used for the pre-call budget estimate.
MAX_OUTPUT_TOKENS = {"triage": 256, "draft": 400}


class ModelUnavailable(Exception):
    """Transient: provider down, timeout, model missing, chaos. The queue retries these."""

    def __init__(self, message: str, error_class: str = "unavailable"):
        super().__init__(message)
        self.error_class = error_class


class InvalidOutput(Exception):
    """Permanent for this ticket: the model's output failed validation twice."""

    def __init__(self, message: str, error_class: str):
        super().__init__(message)
        self.error_class = error_class


class BudgetExceeded(Exception):
    def __init__(self, scope: str, used: int, limit: int):
        super().__init__(f"{scope} token budget exhausted ({used}/{limit})")
        self.scope = scope


@dataclass
class ChatResult:
    content: str
    model: str
    tokens_in: int
    tokens_out: int
    latency_ms: int


@dataclass
class CallContext:
    """Who a call is for. One per ticket attempt; carries the ticket's token tally."""

    tenant_id: str
    run_id: str | None
    ticket_id: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    models: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


class Provider(Protocol):
    name: str

    def chat(self, model: str, messages: list[dict], schema: dict | None, options: dict) -> tuple[str, int, int]: ...

    def embed(self, model: str, texts: list[str]) -> tuple[list[list[float]], int]: ...


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, timeout_s: float):
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s)

    def _post(self, path: str, body: dict) -> dict:
        try:
            resp = self._client.post(path, json=body)
        except httpx.TimeoutException as e:
            raise ModelUnavailable(f"timeout calling {path}", "timeout") from e
        except httpx.HTTPError as e:
            raise ModelUnavailable(f"cannot reach Ollama: {e}", "connection") from e
        if resp.status_code == 404:
            raise ModelUnavailable(f"model not found: {resp.text[:200]}", "model_missing")
        if resp.status_code >= 400:
            raise ModelUnavailable(f"Ollama HTTP {resp.status_code}: {resp.text[:200]}", "http_error")
        return resp.json()

    def chat(self, model: str, messages: list[dict], schema: dict | None, options: dict) -> tuple[str, int, int]:
        body: dict = {"model": model, "messages": messages, "stream": False, "options": options}
        if schema is not None:
            body["format"] = schema
        data = self._post("/api/chat", body)
        return data["message"]["content"], int(data.get("prompt_eval_count", 0)), int(data.get("eval_count", 0))

    def embed(self, model: str, texts: list[str]) -> tuple[list[list[float]], int]:
        data = self._post("/api/embed", {"model": model, "input": texts})
        return data["embeddings"], int(data.get("prompt_eval_count", 0))


class CircuitBreaker:
    """Per-model breaker: opens after N consecutive failures, half-opens after a cooldown."""

    def __init__(self, threshold: int, cooldown_s: float, clock=time.monotonic):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def allow(self, model: str) -> bool:
        opened = self._opened_at.get(model)
        if opened is None:
            return True
        # Half-open: after the cooldown one call is let through to probe.
        return self._clock() - opened >= self.cooldown_s

    def success(self, model: str) -> None:
        self._failures.pop(model, None)
        self._opened_at.pop(model, None)

    def failure(self, model: str) -> None:
        n = self._failures.get(model, 0) + 1
        self._failures[model] = n
        if n >= self.threshold:
            self._opened_at[model] = self._clock()

    def state(self, model: str) -> str:
        if model not in self._opened_at:
            return "closed"
        return "half_open" if self.allow(model) else "open"


class Gateway:
    def __init__(
        self,
        settings: Settings,
        provider: Provider,
        conn: psycopg.Connection | None,
        *,
        rng: random.Random | None = None,
        breaker: CircuitBreaker | None = None,
    ):
        self.settings = settings
        self.provider = provider
        self.conn = conn
        self.rng = rng or random.Random()
        self.breaker = breaker or CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_s)
        self.routes = {
            "triage": [settings.model_triage, settings.model_triage_fallback],
            "draft": [settings.model_draft, settings.model_draft_fallback],
            "embed": [settings.model_embed],
        }

    # -- budgets --------------------------------------------------------------

    def _check_budget(self, ctx: CallContext, estimate: int) -> None:
        limit = self.settings.token_budget_ticket
        if ctx.ticket_id is not None and ctx.tokens + estimate > limit:
            raise BudgetExceeded("ticket", ctx.tokens, limit)
        if ctx.run_id is None or self.conn is None:
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute("SELECT tokens_used, token_budget FROM runs WHERE run_id = %s", (ctx.run_id,))
            row = cur.fetchone()
        if row and row["tokens_used"] + estimate > row["token_budget"]:
            raise BudgetExceeded("run", row["tokens_used"], row["token_budget"])

    def _charge(self, ctx: CallContext, tokens_in: int, tokens_out: int, model: str) -> None:
        ctx.tokens_in += tokens_in
        ctx.tokens_out += tokens_out
        if model not in ctx.models:
            ctx.models.append(model)
        if ctx.run_id is None or self.conn is None:
            return
        with self.conn.transaction(), self.conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET tokens_used = tokens_used + %s WHERE run_id = %s",
                (tokens_in + tokens_out, ctx.run_id),
            )

    # -- metering -------------------------------------------------------------

    def _meter(self, ctx: CallContext, task: str, model: str, tin: int, tout: int, ms: int, err: str | None) -> None:
        if self.conn is None:
            return
        with tenant_tx(self.conn, ctx.tenant_id) as cur:
            cur.execute(
                """INSERT INTO model_calls (tenant_id, run_id, ticket_id, task, model, provider,
                                            tokens_in, tokens_out, latency_ms, ok, error_class)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (ctx.tenant_id, ctx.run_id, ctx.ticket_id, task, model, self.provider.name,
                 tin, tout, ms, err is None, err),
            )

    # -- calls ----------------------------------------------------------------

    def _models_for(self, task: str) -> list[str]:
        seen: list[str] = []
        for m in self.routes[task]:
            if m and m not in seen:
                seen.append(m)
        return seen

    def _attempt(self, task: str, ctx: CallContext, fn):
        """Try each model for the task in order, honouring the breaker. Returns (model, result, ms)."""
        last: ModelUnavailable | None = None
        for model in self._models_for(task):
            if not self.breaker.allow(model):
                last = ModelUnavailable(f"circuit open for {model}", "circuit_open")
                continue
            started = time.perf_counter()
            try:
                if self.settings.chaos_fail_rate > 0 and self.rng.random() < self.settings.chaos_fail_rate:
                    raise ModelUnavailable("chaos: injected failure", "chaos")
                result = fn(model)
            except ModelUnavailable as e:
                ms = int((time.perf_counter() - started) * 1000)
                self.breaker.failure(model)
                self._meter(ctx, task, model, 0, 0, ms, e.error_class)
                last = e
                continue
            self.breaker.success(model)
            return model, result, int((time.perf_counter() - started) * 1000)
        assert last is not None
        raise last

    def chat(self, task: str, messages: list[dict], ctx: CallContext, schema: dict | None = None) -> ChatResult:
        max_out = MAX_OUTPUT_TOKENS.get(task, 256)
        estimate = sum(len(m["content"]) for m in messages) // 4 + max_out
        self._check_budget(ctx, estimate)
        options = {"temperature": self.settings.temperature, "seed": self.settings.seed, "num_predict": max_out}
        model, (content, tin, tout), ms = self._attempt(
            task, ctx, lambda m: self.provider.chat(m, messages, schema, options)
        )
        self._meter(ctx, task, model, tin, tout, ms, None)
        self._charge(ctx, tin, tout, model)
        return ChatResult(content, model, tin, tout, ms)

    def embed(self, texts: list[str], ctx: CallContext) -> list[list[float]]:
        estimate = sum(len(t) for t in texts) // 4
        self._check_budget(ctx, estimate)
        model, (vectors, tin), ms = self._attempt("embed", ctx, lambda m: self.provider.embed(m, texts))
        self._meter(ctx, "embed", model, tin, 0, ms, None)
        self._charge(ctx, tin, 0, model)
        return vectors

    def structured(self, task: str, messages: list[dict], contract: type[T], ctx: CallContext) -> T:
        """Schema-constrained call validated by Pydantic. One repair attempt, then a classified failure."""
        schema = contract.model_json_schema()
        first = self.chat(task, messages, ctx, schema)
        try:
            return contract.model_validate_json(first.content)
        except ValidationError as e:
            error = _describe(e, first.content)
        repair = messages + [
            {"role": "assistant", "content": first.content},
            {
                "role": "user",
                "content": f"That output was invalid: {error}. Reply again with only a JSON object "
                f"that matches this schema exactly: {json.dumps(schema)}",
            },
        ]
        second = self.chat(task, repair, ctx, schema)
        try:
            return contract.model_validate_json(second.content)
        except ValidationError as e:
            raise InvalidOutput(
                f"{contract.__name__} invalid after repair: {_describe(e, second.content)}",
                _classify(second.content),
            ) from e


def _classify(content: str) -> str:
    try:
        json.loads(content)
    except ValueError:
        return "invalid_json"
    return "schema_violation"


def _describe(e: ValidationError, content: str) -> str:
    if _classify(content) == "invalid_json":
        return "not valid JSON"
    return "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()[:5])


def make_provider(settings: Settings) -> Provider:
    if settings.model_provider == "fake":
        from relay.fake_model import FakeProvider

        return FakeProvider()
    if settings.model_provider == "ollama":
        return OllamaProvider(settings.ollama_url, settings.model_timeout_s)
    raise ValueError(f"unknown MODEL_PROVIDER {settings.model_provider!r}")
