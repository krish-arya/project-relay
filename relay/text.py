"""Small deterministic text helpers shared by the detector, the policy and the checker.

These run over *ticket input and draft text*, never over model output that
drives a decision: model decisions come back as validated JSON.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Customer, order and patient references look like C-1001, NW-55821, HD-2231, P-2231.
REFERENCE_ID = re.compile(r"\b[A-Z]{1,4}-\d{3,}\b")
KB_ID = re.compile(r"\b[A-Z]{2,4}-KB-\d+\b")
CANARY = re.compile(r"CANARY-[A-Z0-9-]+", re.IGNORECASE)
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


def normalize(text: str) -> str:
    """NFKC, drop zero-width characters, lowercase, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)
    return re.sub(r"\s+", " ", text).strip().lower()


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).split()


def word_count(text: str) -> int:
    return len(text.split())


def shingles(text: str, n: int = 5) -> set[str]:
    """md5 of each n-word window. Must match relay_shingles() in migrations/002_rls.sql."""
    w = words(text)
    return {hashlib.md5(" ".join(w[i : i + n]).encode()).hexdigest() for i in range(len(w) - n + 1)}


def reference_ids(text: str) -> set[str]:
    return set(REFERENCE_ID.findall(text))


def any_phrase(text: str, phrases: list[str]) -> list[str]:
    """Return the phrases (regexes) that match the normalized text."""
    norm = normalize(text)
    return [p for p in phrases if re.search(p, norm)]
