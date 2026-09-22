#!/usr/bin/env python3
"""Per-session Route Lease readout for Jev Codex Router.

This answers the operational question Route Lease v1 was built for:

    "For each Codex session, how many times did we actually ask Jev,
     how many turns reused the lease (KEEP), and how many escalated locally?"

It is a deterministic aggregation over the router live log. It calls no model
and persists nothing beyond the optional report files. The headline metric is
Jev decisions per human turn; a healthy session should be close to one (or
fewer) Jev decision per human turn, with tool/compaction continuations KEEP.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
from typing import Any

DEFAULT_LOG = os.path.expanduser("~/.codex/codex-router/jev-router-live.jsonl")


def _iter_records(path: str):
    """Yield decoded log records, skipping corrupt/partial lines."""
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(record, dict):
                yield record


def _empty_stats() -> dict[str, Any]:
    return {
        "calls": 0,
        "user_turns": 0,
        "tool_steps": 0,
        "other_steps": 0,
        "jev_decisions": 0,
        "jev_error_fallbacks": 0,
        "lease_keeps": 0,
        "replans": 0,
        "local_escalations": 0,
        "off_turns": 0,
        "statuses": collections.Counter(),
        "input_tokens": 0,
        "output_tokens": 0,
        "first_at": None,
        "last_at": None,
        "last_model": None,
        "last_effort": None,
    }


def aggregate(path: str) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}

    for record in _iter_records(path):
        session_id = record.get("session") or "(unknown)"
        stats = sessions.setdefault(str(session_id), _empty_stats())

        stats["calls"] += 1
        step = record.get("step")
        if step == "user_turn":
            stats["user_turns"] += 1
        elif step == "tool_step":
            stats["tool_steps"] += 1
        else:
            stats["other_steps"] += 1

        source = record.get("route_source")
        if source == "jev":
            stats["jev_decisions"] += 1
        elif source == "jev_error_fallback":
            stats["jev_error_fallbacks"] += 1
        elif source == "lease_escalation":
            stats["local_escalations"] += 1
        elif source == "off":
            stats["off_turns"] += 1

        action = record.get("lease_action")
        if action == "KEEP":
            stats["lease_keeps"] += 1
        elif action == "REPLAN":
            stats["replans"] += 1

        status = record.get("status")
        if status is not None:
            stats["statuses"][str(status)] += 1

        usage = record.get("jev_usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if isinstance(inp, int) and inp >= 0:
                stats["input_tokens"] += inp
            if isinstance(out, int) and out >= 0:
                stats["output_tokens"] += out

        at = record.get("at")
        if at:
            if stats["first_at"] is None:
                stats["first_at"] = at
            stats["last_at"] = at

        if record.get("model"):
            stats["last_model"] = record.get("model")
            stats["last_effort"] = record.get("effort")

    return sessions


def _jev_per_turn(stats: dict[str, Any]) -> str:
    turns = stats["user_turns"]
    if turns == 0:
        return "n/a"
    return f"{stats['jev_decisions'] / turns:.2f}"


def _keep_ratio(stats: dict[str, Any]) -> str:
    decided = stats["lease_keeps"] + stats["replans"]
    if decided == 0:
        return "n/a"
    return f"{stats['lease_keeps'] / decided * 100:.0f}%"


def render_text(sessions: dict[str, dict[str, Any]]) -> str:
    if not sessions:
        return "No session records yet; the first routed request will create them."

    # Most recently active sessions first.
    ordered = sorted(
        sessions.items(),
        key=lambda item: item[1]["last_at"] or item[1]["first_at"] or "",
        reverse=True,
    )

    header = (
        "session      calls turns tools | Jev KEEP REPLAN esc | "
        "Jev/turn keep% | last route        | statuses"
    )
    lines = [header, "-" * len(header)]

    for session_id, stats in ordered:
        statuses = ",".join(
            f"{code}:{count}" for code, count in sorted(stats["statuses"].items())
        )
        last_route = stats["last_model"] or "-"
        if stats["last_effort"]:
            last_route += f":{stats['last_effort']}"
        lines.append(
            f"{session_id[:10]:<10} "
            f"{stats['calls']:>5} {stats['user_turns']:>5} {stats['tool_steps']:>5} | "
            f"{stats['jev_decisions']:>3} {stats['lease_keeps']:>4} {stats['replans']:>6} "
            f"{stats['local_escalations']:>3} | "
            f"{_jev_per_turn(stats):>8} {_keep_ratio(stats):>5} | "
            f"{last_route[:17]:<17} | {statuses}"
        )

    # Totals across sessions.
    totals = _empty_stats()
    for _, stats in ordered:
        totals["calls"] += stats["calls"]
        totals["user_turns"] += stats["user_turns"]
        totals["tool_steps"] += stats["tool_steps"]
        totals["jev_decisions"] += stats["jev_decisions"]
        totals["lease_keeps"] += stats["lease_keeps"]
        totals["replans"] += stats["replans"]
        totals["local_escalations"] += stats["local_escalations"]
        totals["statuses"].update(stats["statuses"])
        totals["input_tokens"] += stats["input_tokens"]
        totals["output_tokens"] += stats["output_tokens"]

    lines.append("-" * len(header))
    lines.append(
        f"TOTAL sessions={len(sessions)} calls={totals['calls']} "
        f"human_turns={totals['user_turns']} jev={totals['jev_decisions']} "
        f"keep={totals['lease_keeps']} replan={totals['replans']} "
        f"local_escalations={totals['local_escalations']} "
        f"jev_per_human_turn={_jev_per_turn(totals)} keep_ratio={_keep_ratio(totals)} "
        f"tokens_in={totals['input_tokens']} tokens_out={totals['output_tokens']}"
    )
    return "\n".join(lines)


def _sessions_to_jsonable(sessions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for session_id, stats in sessions.items():
        copied = dict(stats)
        copied["statuses"] = dict(stats["statuses"])
        copied["jev_per_user_turn"] = _jev_per_turn(stats)
        copied["keep_ratio"] = _keep_ratio(stats)
        out[session_id] = copied
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default=DEFAULT_LOG, help="router live log path")
    parser.add_argument("--json-out", help="write aggregated JSON to this path")
    parser.add_argument("--text-out", help="write text report to this path")
    args = parser.parse_args(argv)

    sessions = aggregate(args.log)
    text = render_text(sessions)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump(_sessions_to_jsonable(sessions), handle, indent=2, sort_keys=True)
    if args.text_out:
        with open(args.text_out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")

    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
