"""Search helpers (Phase 4.1): pure functions for query handling.

``build_fts_query`` converts free user text into a safe FTS5 MATCH query:
every whitespace-separated token is double-quoted (FTS5 syntax escaped) and
given a prefix star, so ``data fix`` matches documents containing tokens
starting with ``data`` AND tokens starting with ``fix``. Mid-word substring
matches (``tabase`` → ``database``) are intentionally not supported in FTS
mode; the substring fallback covers those.
"""
from __future__ import annotations

SEARCH_MODES = ("fts", "substring")
MIN_QUERY_LENGTH = 2


def validate_query(query: str) -> str:
    """Strip and validate a search query; raises ValueError when too short."""
    q = (query or "").strip()
    if len(q) < MIN_QUERY_LENGTH:
        raise ValueError(
            f"query must be at least {MIN_QUERY_LENGTH} characters"
        )
    return q


def build_fts_query(text: str) -> str:
    """Escape + prefix-quote every token for an FTS5 MATCH expression."""
    tokens = [t for t in text.split() if t]
    return " ".join('"' + t.replace('"', '""') + '"*' for t in tokens)


__all__ = ["MIN_QUERY_LENGTH", "SEARCH_MODES", "build_fts_query", "validate_query"]
