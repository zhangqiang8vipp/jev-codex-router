#!/usr/bin/env python3
"""Privacy-preserving Shadow Eval primitives for Jev Codex Router.

The live request is executed exactly once.  The raw Jev route is recorded as a
counterfactual choice, while the smart route and actually served route record
what production used.  No prompt text or tool payload is written here.

A later tool-result request can append feedback for the previous turn in the
same hashed session.  This gives the evaluator an observed tool-error signal
without replaying side effects or paying for a second model call.
"""
from __future__ import annotations

import json
import os
import threading
import uuid

SCHEMA_VERSION = 2
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)

_lock = threading.Lock()


def new_turn_id() -> str:
    return uuid.uuid4().hex


def route_value(model, effort):
    if not model:
        return None
    return {"model": model, "effort": effort}


def aggregate_usage(attempts):
    """Sum known counters across every real upstream attempt.

    Unknown usage is kept explicit instead of being converted to zero.
    """
    totals = {}
    known_attempts = 0
    unknown_attempts = 0
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        usage = attempt.get("usage")
        if not isinstance(usage, dict):
            unknown_attempts += 1
            continue
        known_attempts += 1
        for name in USAGE_FIELDS:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[name] = totals.get(name, 0) + value
    return (totals or None), known_attempts, unknown_attempts


def cache_metrics(usage):
    if not isinstance(usage, dict):
        return {"cached_input_tokens": None, "input_tokens": None, "cache_hit_ratio": None}
    inp = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens")
    if not isinstance(inp, int) or isinstance(inp, bool) or inp < 0:
        inp = None
    if not isinstance(cached, int) or isinstance(cached, bool) or cached < 0:
        cached = None
    ratio = None
    if inp and cached is not None:
        ratio = min(max(cached / inp, 0.0), 1.0)
    return {
        "cached_input_tokens": cached,
        "input_tokens": inp,
        "cache_hit_ratio": ratio,
    }


def classify_outcome(status, attempts):
    """Deterministic transport/Responses outcome; not a semantic correctness claim."""
    terminal = None
    for attempt in reversed(attempts or []):
        if isinstance(attempt, dict) and attempt.get("terminal_type"):
            terminal = attempt.get("terminal_type")
            break
    if not isinstance(status, int) or isinstance(status, bool):
        return "unknown", False
    if status >= 400:
        return "http_error", False
    if terminal == "response.failed":
        return "failed", False
    if terminal == "response.incomplete":
        return "incomplete", False
    if terminal == "response.completed":
        return "completed", True
    if 200 <= status < 300:
        return "http_ok_no_terminal", True
    return "unknown", False


def normalized_attempts(attempts):
    """Keep only eval-safe attempt metadata; never request/response bodies."""
    out = []
    for attempt in attempts or []:
        if not isinstance(attempt, dict):
            continue
        row = {}
        for key in ("model", "effort", "speed", "status", "terminal_type", "usage"):
            value = attempt.get(key)
            if value is not None:
                row[key] = value
        out.append(row)
    return out


def build_turn_event(
    *,
    at,
    turn_id,
    session,
    policy_version,
    jev_model,
    jev_effort,
    smart_model,
    smart_effort,
    smart_gate,
    served_model,
    served_effort,
    status,
    attempts,
    total_ms,
    jev_ms,
    step_type,
    route_source=None,
    route_reason=None,
    jev_cache=None,
    dry_reason=None,
    fallback=None,
):
    usage, known_usage, unknown_usage = aggregate_usage(attempts)
    cache = cache_metrics(usage)
    outcome, success = classify_outcome(status, attempts)
    jev_route = route_value(jev_model, jev_effort)
    smart_route = route_value(smart_model, smart_effort)
    served_route = route_value(served_model, served_effort)
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "turn",
        "at": at,
        "turn_id": turn_id,
        "session": session,
        "policy_version": policy_version,
        "jev_route": jev_route,
        "smart_route": smart_route,
        "served_route": served_route,
        "route_changed": bool(jev_route and smart_route and jev_route != smart_route),
        "smart_gate": smart_gate,
        "outcome": outcome,
        "success": success,
        "status": status,
        "attempt_count": len(attempts or []),
        "retry_count": max(0, len(attempts or []) - 1),
        "attempts_with_usage": known_usage,
        "attempts_without_usage": unknown_usage,
        "usage": usage,
        **cache,
        "total_ms": total_ms,
        "jev_ms": jev_ms,
        "jev_cache": jev_cache,
        "step_type": step_type,
        "route_source": route_source,
        "route_reason": route_reason,
        "dry": dry_reason,
        "fallback": fallback,
        "attempts": normalized_attempts(attempts),
    }


def build_tool_feedback(*, at, turn_id, session, errored):
    return {
        "schema_version": SCHEMA_VERSION,
        "event": "tool_feedback",
        "at": at,
        "turn_id": turn_id,
        "session": session,
        "tool_error": bool(errored),
        "tool_success": not bool(errored),
    }


def append_event(path, event):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with _lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return True
    except OSError:
        return False