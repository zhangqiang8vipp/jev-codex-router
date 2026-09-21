#!/usr/bin/env python3
"""Rolling Shadow Eval report for Jev Codex Router.

This report is deliberately observational:
- the smart route is the only route actually executed;
- the raw Jev route is a counterfactual route choice;
- counterfactual *cost* can be estimated from the observed token volume;
- counterfactual *quality* is never invented.

The Pareto table therefore compares observed smart-route pairs using an
operational quality proxy (successful Responses transport, penalized when the
next tool-result turn reports an error) against measured/estimated credits.
Task mix is not randomized, so the frontier is descriptive, not causal.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import statistics

from report_routing import CREDIT_RATES

DEFAULT_LOG = os.path.expanduser("~/.codex/codex-router/jev-shadow-eval.jsonl")
DEFAULT_JSON = os.path.expanduser("~/.codex/codex-router/jev-shadow-eval-7d.json")
DEFAULT_TEXT = os.path.expanduser("~/.codex/codex-router/jev-shadow-eval-7d.txt")


def parse_at(value):
    try:
        return datetime.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def pair(route):
    if not isinstance(route, dict) or not route.get("model"):
        return "(none)"
    return f"{route['model']}:{route.get('effort') or 'default'}"


def credit_estimate(model, usage):
    """Published-rate estimate from observed tokens; cache writes stay diagnostic."""
    rates = CREDIT_RATES.get(model)
    if not rates or not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    cached = usage.get("cached_input_tokens", 0)
    out = usage.get("output_tokens")
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (inp, cached, out)):
        return None
    cached = min(cached, inp)
    p_in, p_cached, p_out = rates
    return ((inp - cached) * p_in + cached * p_cached + out * p_out) / 1e6


def actual_credits(event):
    total = 0.0
    priced = 0
    unpriced = 0
    for attempt in event.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        value = credit_estimate(attempt.get("model"), attempt.get("usage"))
        if value is None:
            unpriced += 1
        else:
            total += value
            priced += 1
    return (total if priced else None), priced, unpriced


def median(values):
    vals = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return statistics.median(vals) if vals else None


def load_events(path, days, now=None):
    now = now or datetime.datetime.now()
    cutoff = now - datetime.timedelta(days=days)
    turns = []
    feedback = {}
    stats = {"lines": 0, "bad_json": 0, "bad_date": 0, "old": 0}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            stats["lines"] += 1
            try:
                event = json.loads(raw)
            except ValueError:
                stats["bad_json"] += 1
                continue
            at = parse_at(event.get("at"))
            if at is None:
                stats["bad_date"] += 1
                continue
            if at < cutoff:
                stats["old"] += 1
                continue
            event["_at"] = at
            if event.get("event") == "turn" and event.get("turn_id"):
                turns.append(event)
            elif event.get("event") == "tool_feedback" and event.get("turn_id"):
                current = feedback.get(event["turn_id"])
                # Any observed error wins; duplicated successful feedback cannot hide it.
                if current is None or event.get("tool_error"):
                    feedback[event["turn_id"]] = {
                        "tool_error": bool(event.get("tool_error")),
                        "tool_success": not bool(event.get("tool_error")),
                        "at": at,
                    }
    turns.sort(key=lambda e: e["_at"])
    return turns, feedback, stats


def observed_success(event, feedback):
    if not event.get("success"):
        return False
    item = feedback.get(event.get("turn_id"))
    return not (item and item.get("tool_error"))


def summarize(turns, feedback, stats, days, min_turns):
    groups = {}
    transitions = {}
    route_changes = retry_turns = immediate_failures = 0
    tool_feedback_turns = tool_errors = 0
    total_input = total_cached = 0
    latency = []
    actual_credit_total = 0.0
    actual_credit_turns = 0
    unpriced_attempts = 0

    for event in turns:
        smart = pair(event.get("smart_route"))
        raw = pair(event.get("jev_route"))
        served = pair(event.get("served_route"))
        if event.get("route_changed"):
            route_changes += 1
        if (event.get("retry_count") or 0) > 0:
            retry_turns += 1
        if not event.get("success"):
            immediate_failures += 1
        fb = feedback.get(event.get("turn_id"))
        if fb:
            tool_feedback_turns += 1
            if fb.get("tool_error"):
                tool_errors += 1

        usage = event.get("usage") or {}
        inp = usage.get("input_tokens")
        cached = usage.get("cached_input_tokens")
        if isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0:
            total_input += inp
            if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                total_cached += min(cached, inp)

        success = observed_success(event, feedback)
        credits, _priced, unpriced = actual_credits(event)
        unpriced_attempts += unpriced
        if credits is not None:
            actual_credit_total += credits
            actual_credit_turns += 1

        row = groups.setdefault(smart, {
            "pair": smart,
            "turns": 0,
            "successes": 0,
            "immediate_failures": 0,
            "tool_feedback_turns": 0,
            "tool_errors": 0,
            "retry_turns": 0,
            "retry_count": 0,
            "latency": [],
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "actual_credits": 0.0,
            "priced_turns": 0,
            "served_pairs": {},
        })
        row["turns"] += 1
        row["successes"] += int(success)
        row["immediate_failures"] += int(not event.get("success"))
        if fb:
            row["tool_feedback_turns"] += 1
            row["tool_errors"] += int(bool(fb.get("tool_error")))
        retries = int(event.get("retry_count") or 0)
        row["retry_turns"] += int(retries > 0)
        row["retry_count"] += retries
        if isinstance(event.get("total_ms"), (int, float)):
            row["latency"].append(event["total_ms"])
            latency.append(event["total_ms"])
        if isinstance(inp, int) and not isinstance(inp, bool) and inp >= 0:
            row["input_tokens"] += inp
            if isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0:
                row["cached_input_tokens"] += min(cached, inp)
        if credits is not None:
            row["actual_credits"] += credits
            row["priced_turns"] += 1
        row["served_pairs"][served] = row["served_pairs"].get(served, 0) + 1

        if raw != "(none)" and smart != "(none)":
            key = f"{raw} -> {smart}"
            tr = transitions.setdefault(key, {
                "from": raw,
                "to": smart,
                "turns": 0,
                "successes": 0,
                "raw_estimated_credits": 0.0,
                "smart_estimated_credits": 0.0,
                "priced_turns": 0,
            })
            tr["turns"] += 1
            tr["successes"] += int(success)
            raw_model = (event.get("jev_route") or {}).get("model")
            smart_model = (event.get("smart_route") or {}).get("model")
            raw_cost = credit_estimate(raw_model, usage)
            smart_cost = credit_estimate(smart_model, usage)
            if raw_cost is not None and smart_cost is not None:
                tr["raw_estimated_credits"] += raw_cost
                tr["smart_estimated_credits"] += smart_cost
                tr["priced_turns"] += 1

    pair_rows = []
    for row in groups.values():
        turns_n = row["turns"]
        input_n = row["input_tokens"]
        success_rate = row["successes"] / turns_n if turns_n else 0.0
        avg_credits = row["actual_credits"] / row["priced_turns"] if row["priced_turns"] else None
        cost_per_success = row["actual_credits"] / row["successes"] if row["successes"] and row["priced_turns"] else None
        pair_rows.append({
            "pair": row["pair"],
            "turns": turns_n,
            "observed_success_rate": round(success_rate, 4),
            "immediate_failure_rate": round(row["immediate_failures"] / turns_n, 4) if turns_n else None,
            "tool_feedback_turns": row["tool_feedback_turns"],
            "tool_error_rate": (
                round(row["tool_errors"] / row["tool_feedback_turns"], 4)
                if row["tool_feedback_turns"] else None
            ),
            "retry_turn_rate": round(row["retry_turns"] / turns_n, 4) if turns_n else None,
            "avg_retries": round(row["retry_count"] / turns_n, 3) if turns_n else None,
            "median_latency_ms": median(row["latency"]),
            "cache_hit_ratio": (
                round(row["cached_input_tokens"] / input_n, 4) if input_n else None
            ),
            "avg_actual_credits": round(avg_credits, 6) if avg_credits is not None else None,
            "credits_per_observed_success": (
                round(cost_per_success, 6) if cost_per_success is not None else None
            ),
            "priced_turns": row["priced_turns"],
            "served_pairs": dict(sorted(row["served_pairs"].items(), key=lambda kv: -kv[1])),
        })

    eligible = [
        row for row in pair_rows
        if row["turns"] >= min_turns and row["avg_actual_credits"] is not None
    ]
    frontier = []
    for row in eligible:
        dominated = False
        for other in eligible:
            if other is row:
                continue
            cheaper_or_equal = other["avg_actual_credits"] <= row["avg_actual_credits"]
            better_or_equal = other["observed_success_rate"] >= row["observed_success_rate"]
            strictly_better = (
                other["avg_actual_credits"] < row["avg_actual_credits"]
                or other["observed_success_rate"] > row["observed_success_rate"]
            )
            if cheaper_or_equal and better_or_equal and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(row["pair"])

    transition_rows = []
    for row in transitions.values():
        raw_avg = row["raw_estimated_credits"] / row["priced_turns"] if row["priced_turns"] else None
        smart_avg = row["smart_estimated_credits"] / row["priced_turns"] if row["priced_turns"] else None
        transition_rows.append({
            "from": row["from"],
            "to": row["to"],
            "turns": row["turns"],
            "observed_success_rate": round(row["successes"] / row["turns"], 4) if row["turns"] else None,
            "raw_estimated_credits": round(raw_avg, 6) if raw_avg is not None else None,
            "smart_estimated_credits": round(smart_avg, 6) if smart_avg is not None else None,
            "estimated_credit_delta": (
                round(smart_avg - raw_avg, 6)
                if raw_avg is not None and smart_avg is not None else None
            ),
        })
    transition_rows.sort(key=lambda row: (-row["turns"], row["from"], row["to"]))
    pair_rows.sort(key=lambda row: (-row["turns"], row["pair"]))

    total = len(turns)
    if turns:
        observed_span_days = max(
            0.0,
            (turns[-1]["_at"] - turns[0]["_at"]).total_seconds() / 86400.0,
        )
    else:
        observed_span_days = 0.0

    return {
        "schema_version": 1,
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "window_days": days,
        "observed_span_days": round(observed_span_days, 3),
        "warmup_complete": observed_span_days >= min(7, days) - 0.05,
        "turns": total,
        "route_changes": route_changes,
        "route_change_rate": round(route_changes / total, 4) if total else None,
        "immediate_failures": immediate_failures,
        "immediate_failure_rate": round(immediate_failures / total, 4) if total else None,
        "tool_feedback_turns": tool_feedback_turns,
        "tool_errors": tool_errors,
        "tool_error_rate": (
            round(tool_errors / tool_feedback_turns, 4) if tool_feedback_turns else None
        ),
        "retry_turns": retry_turns,
        "retry_turn_rate": round(retry_turns / total, 4) if total else None,
        "cache_hit_ratio": round(total_cached / total_input, 4) if total_input else None,
        "median_latency_ms": median(latency),
        "actual_credits": round(actual_credit_total, 6),
        "actual_credits_priced_turns": actual_credit_turns,
        "unpriced_attempts": unpriced_attempts,
        "pairs": pair_rows,
        "pareto_frontier": frontier,
        "transitions": transition_rows,
        "log_stats": stats,
        "notes": [
            "Only the smart route is executed; Jev-route quality is counterfactual and is not claimed.",
            "Counterfactual route cost holds observed token volume constant.",
            "Observed success is an operational proxy, not semantic correctness.",
            "Pair comparisons are selection-biased because the router chooses different pairs for different task mixes.",
        ],
    }


def pct(value):
    return "—" if value is None else f"{value * 100:.1f}%"


def num(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def render_text(report):
    lines = [
        "Jev Codex Router — Shadow Eval",
        f"window: {report['window_days']}d · turns: {report['turns']} · observed span: {report['observed_span_days']}d",
    ]
    if not report["warmup_complete"]:
        lines.append("status: WARMING UP — the rolling 7-day window is not complete yet")
    if not report["turns"]:
        lines.append("no shadow-eval turns in this window")
        return "\n".join(lines)

    lines += [
        "",
        "Overall",
        f"  route changed: {report['route_changes']} ({pct(report['route_change_rate'])})",
        f"  immediate failures: {report['immediate_failures']} ({pct(report['immediate_failure_rate'])})",
        f"  tool errors: {report['tool_errors']}/{report['tool_feedback_turns']} ({pct(report['tool_error_rate'])})",
        f"  retry turns: {report['retry_turns']} ({pct(report['retry_turn_rate'])})",
        f"  cache hit ratio: {pct(report['cache_hit_ratio'])}",
        f"  median latency: {num(report['median_latency_ms'], 0)} ms",
        f"  observed native credits: {num(report['actual_credits'], 6)} over {report['actual_credits_priced_turns']} priced turns",
        "",
        "Observed smart-route pairs",
        "pair | turns | success | tool err | retry | cache | med ms | avg credits | credits/success",
        "--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---:",
    ]
    frontier = set(report["pareto_frontier"])
    for row in report["pairs"]:
        marker = " *" if row["pair"] in frontier else ""
        lines.append(
            f"{row['pair']}{marker} | {row['turns']} | {pct(row['observed_success_rate'])} | "
            f"{pct(row['tool_error_rate'])} | {pct(row['retry_turn_rate'])} | "
            f"{pct(row['cache_hit_ratio'])} | {num(row['median_latency_ms'], 0)} | "
            f"{num(row['avg_actual_credits'], 6)} | {num(row['credits_per_observed_success'], 6)}"
        )
    lines += [
        "",
        "Pareto frontier (*)",
        "  " + (", ".join(report["pareto_frontier"]) if report["pareto_frontier"] else "insufficient priced samples"),
        "",
        "Jev -> Smart disagreements / confirmations",
        "from | to | turns | observed success | raw est credits | smart est credits | delta",
        "--- | --- | ---: | ---: | ---: | ---: | ---:",
    ]
    for row in report["transitions"]:
        lines.append(
            f"{row['from']} | {row['to']} | {row['turns']} | {pct(row['observed_success_rate'])} | "
            f"{num(row['raw_estimated_credits'], 6)} | {num(row['smart_estimated_credits'], 6)} | "
            f"{num(row['estimated_credit_delta'], 6)}"
        )
    lines += [
        "",
        "Interpretation",
        "  * quality is an observed operational proxy, not a semantic judge;",
        "  * raw Jev quality is not estimated because that route was not executed;",
        "  * cost counterfactuals reuse the real token volume;",
        "  * pair comparisons are descriptive because task mix differs by route.",
    ]
    return "\n".join(lines)


def write_file(path, text):
    path = os.path.realpath(os.path.expanduser(path))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--log", default=DEFAULT_LOG)
    parser.add_argument("--min-turns", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    parser.add_argument("--write", action="store_true", help="write rolling JSON and text reports")
    parser.add_argument("--json-out", default=DEFAULT_JSON)
    parser.add_argument("--text-out", default=DEFAULT_TEXT)
    args = parser.parse_args()
    if args.days < 1 or args.min_turns < 1:
        raise SystemExit("--days and --min-turns must be positive")
    try:
        turns, feedback, stats = load_events(os.path.expanduser(args.log), args.days)
    except OSError as exc:
        raise SystemExit(f"cannot read shadow eval log {args.log}: {exc}")
    report = summarize(turns, feedback, stats, args.days, args.min_turns)
    text_report = render_text(report)
    if args.write:
        write_file(args.json_out, json.dumps(report, ensure_ascii=False, indent=2))
        write_file(args.text_out, text_report)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(text_report)


if __name__ == "__main__":
    main()
