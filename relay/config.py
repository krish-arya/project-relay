"""Configuration from environment variables, with an optional .env file.

Every variable has a safe default so `make demo` works from a clean clone.
.env.example documents each one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    # Minimal .env reader: KEY=VALUE lines, real environment wins.
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_admin_url: str
    database_url: str
    app_password: str
    ollama_url: str
    model_provider: str
    model_triage: str
    model_draft: str
    model_embed: str
    model_triage_fallback: str
    model_draft_fallback: str
    temperature: float
    seed: int
    model_timeout_s: float
    token_budget_ticket: int
    token_budget_run: int
    lease_s: int
    max_attempts: int
    backoff_base_s: float
    backoff_max_s: float
    chaos_fail_rate: float
    breaker_threshold: int
    breaker_cooldown_s: float
    top_k: int
    min_score: float
    category_margin: float
    operator: str


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    port = os.environ.get("RELAY_PG_PORT", "5433")
    pg_password = os.environ.get("POSTGRES_PASSWORD", "relay")
    app_password = os.environ.get("RELAY_APP_PASSWORD", "relay_app")
    return Settings(
        database_admin_url=os.environ.get(
            "DATABASE_ADMIN_URL", f"postgresql://postgres:{pg_password}@localhost:{port}/relay"
        ),
        database_url=os.environ.get(
            "DATABASE_URL", f"postgresql://relay_app:{app_password}@localhost:{port}/relay"
        ),
        app_password=app_password,
        ollama_url=os.environ.get("OLLAMA_URL", "http://localhost:11434"),
        model_provider=os.environ.get("MODEL_PROVIDER", "ollama"),
        model_triage=os.environ.get("MODEL_TRIAGE", "qwen2.5:1.5b"),
        model_draft=os.environ.get("MODEL_DRAFT", "qwen2.5:3b"),
        model_embed=os.environ.get("MODEL_EMBED", "nomic-embed-text"),
        model_triage_fallback=os.environ.get("MODEL_TRIAGE_FALLBACK", "qwen2.5:3b"),
        model_draft_fallback=os.environ.get("MODEL_DRAFT_FALLBACK", "qwen2.5:1.5b"),
        temperature=_float("MODEL_TEMPERATURE", 0.0),
        seed=_int("MODEL_SEED", 7),
        model_timeout_s=_float("MODEL_TIMEOUT_S", 120.0),
        token_budget_ticket=_int("TOKEN_BUDGET_TICKET", 8000),
        token_budget_run=_int("TOKEN_BUDGET_RUN", 400000),
        lease_s=_int("QUEUE_LEASE_S", 300),
        max_attempts=_int("QUEUE_MAX_ATTEMPTS", 4),
        backoff_base_s=_float("QUEUE_BACKOFF_BASE_S", 2.0),
        backoff_max_s=_float("QUEUE_BACKOFF_MAX_S", 60.0),
        chaos_fail_rate=_float("CHAOS_FAIL_RATE", 0.0),
        breaker_threshold=_int("BREAKER_FAILURE_THRESHOLD", 3),
        breaker_cooldown_s=_float("BREAKER_COOLDOWN_S", 30.0),
        top_k=_int("RETRIEVAL_TOP_K", 3),
        min_score=_float("RETRIEVAL_MIN_SCORE", 0.55),
        category_margin=_float("RETRIEVAL_CATEGORY_MARGIN", 0.04),
        operator=os.environ.get("RELAY_OPERATOR", "operator"),
    )
