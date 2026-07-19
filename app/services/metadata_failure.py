"""Failure classification + attempt recording for metadata resolution.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): split a non-success ResourceMetadata outcome into
transient / non_work / not_found buckets (so the cache only retains definitive
outcomes and the backfill knows what to retry), and stamp retry-state columns
on the resource after each evaluation.
"""
from __future__ import annotations

from typing import Any

from app.utils.time import utcnow

# Substrings of ``ResourceMetadata.reason`` / ``search_error`` that indicate
# an infra failure (not a real "no match"). These must NOT be cached, because
# re-running later will very likely succeed. HTTP failures from the external
# source (billing/credits exhausted, rate limit, auth, server errors) belong
# here: they are failures of the source, not a definitive "no match".
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "timed out", "timeout", "connection error", "did not call finalize",
    "401", "402", "403", "429", "payment required", "accountoverdue",
    "unauthorized", "rate limit", "service unavailable", "overloaded",
    "500", "502", "503", "504", "bad gateway", "server error",
    "api key not configured",
    # Agent-level failures: ``_run_react`` wraps any ReAct invocation
    # exception (LangGraph recursion-limit, LLM provider 4xx/5xx, unhandled
    # tool error) as ``reason="Agent error: {e}"``. These are NEVER a
    # definitive "no match" - they are infra/agent failures that should retry,
    # not be cached as a permanent not_found. Without these markers the
    # recursion-limit and LLM-400 cases were misclassified as not_found and
    # cached for the full TTL, condemning retryable resources.
    "agent error", "recursion limit",
    # Wikipedia infra failures surfaced by _wiki_call (connection, timeout,
    # rate limit, non-200, invalid JSON from wikipediaapi). A real "no match"
    # returns success=True with an empty data list (or "Page not found"), so
    # this marker only appears on retryable failures.
    "wikipedia request failed",
    # Exa web-search fallback failures (network, rate limit, API key/usage,
    # unparseable judge JSON). These are not definitive "no match" outcomes -
    # retry later when the service is healthy.
    "exa search failed", "exa judge",
)

# Substrings indicating the entry is genuinely not a TV/movie work (music,
# ASMR, theme songs). Re-running will not change the outcome.
_NON_WORK_MARKERS: tuple[str, ...] = (
    "music album", "music single", "music release", "mini-album", "mini album",
    "asmr", "opening theme", "ending theme", "theme song",
    "not a tv", "not a movie", "not an anime",
)


def _classify_failure(meta: Any) -> str | None:
    """Classify a ``ResourceMetadata`` outcome for retry/cache decisions.

    Returns ``None`` on success (``meta.found`` truthy AND a ``matched_entity``
    was produced). Otherwise one of:
      * ``"transient"``  — retryable infra failure; never cached.
      * ``"non_work"``   — correctly identified as non-TV/movie; never retried.
      * ``"not_found"``  — source had no match; retried after a long TTL.
    """
    if getattr(meta, "found", False):
        # found=True but no matched_entity -> the agent claimed success yet
        # produced nothing to link (LLM finalization gap). Treat as transient
        # so it retries instead of being cached as a fake "match" and leaving
        # the resource permanently unparsed.
        if not getattr(meta, "matched_entity", None):
            return "transient"
        return None
    haystack = " ".join(filter(None, (
        str(getattr(meta, "reason", "") or ""),
        str(getattr(meta, "search_error", "") or ""),
    ))).lower()
    if any(m in haystack for m in _TRANSIENT_MARKERS):
        return "transient"
    if any(m in haystack for m in _NON_WORK_MARKERS):
        return "non_work"
    return "not_found"


def _record_metadata_attempt(resource: Any, meta: Any) -> None:
    """Stamp retry-state columns on ``resource`` after an evaluation.

    ``metadata_matched_at`` only records successes, so this tracks *attempts*
    (count + timestamp + failure type) so the backfill can tell "never tried"
    from "tried and failed transiently" from "definitively not found".
    ``metadata_failure_type`` is set to ``None`` on success, which also clears
    any stale failure marker left by a previous attempt.
    """
    resource.metadata_attempts = int(getattr(resource, "metadata_attempts", 0) or 0) + 1
    resource.last_metadata_attempt_at = utcnow()
    resource.metadata_failure_type = _classify_failure(meta)
