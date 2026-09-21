"""Bounded session/repo context and evidence-based routing guards for Jev Codex Router.

This module intentionally keeps semantic model selection in Jev. It only adds:
- bounded, non-code repo metadata;
- small persistent per-thread memory;
- deterministic escalation after observed tool failures; and
- one-rung downgrade hysteresis for very short continuation turns.

No source file contents, filenames, command output, or absolute cwd are sent to Jev.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

LUNA = "gpt-5.6-luna"
TERRA = "gpt-5.6-terra"
SOL = "gpt-5.6-sol"
ASTRA = "gpt-6-astra"
TIERS = (LUNA, TERRA, SOL, ASTRA)
TIER_RANK = {name: index for index, name in enumerate(TIERS)}
EFFORTS = ("low", "medium", "high", "xhigh", "max")
EFFORT_RANK = {name: index for index, name in enumerate(EFFORTS)}

CWD_RX = re.compile(r"<cwd>\s*(.*?)\s*</cwd>", re.S | re.I)
GOAL_RX = re.compile(
    r'<codex_internal_context(?:\s[^<>]*)?source=["\']goal["\'][^<>]*>(.*?)'
    r'</codex_internal_context\s*>',
    re.S | re.I,
)
SHORT_FOLLOWUP_CHARS = 64
SESSION_TTL_S = 12 * 60 * 60
SESSION_MAX_ENTRIES = 256
REPO_CACHE_S = 2.0
GIT_TIMEOUT_S = 0.35


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") not in ("input_text", "output_text", "text"):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _user_texts(payload: Dict[str, Any]):
    inp = payload.get("input")
    if isinstance(inp, str):
        return [inp]
    if not isinstance(inp, list):
        return []
    out = []
    for item in inp:
        if isinstance(item, dict) and item.get("role") == "user":
            text = _content_text(item.get("content"))
            if text:
                out.append(text)
    return out


def extract_cwd(payload: Dict[str, Any]) -> Optional[str]:
    """Read Codex's environment envelope without sending its absolute path to Jev."""
    direct = payload.get("cwd")
    if isinstance(direct, str) and direct.strip():
        candidate = direct.strip()
        return os.path.realpath(os.path.expanduser(candidate))
    matches = []
    for text in _user_texts(payload)[-4:]:
        matches.extend(CWD_RX.findall(text))
    if not matches:
        return None
    candidate = matches[-1].strip()
    if not candidate:
        return None
    return os.path.realpath(os.path.expanduser(candidate))


def is_short_followup(task: str) -> bool:
    """A shape signal only; this does not choose a model by itself."""
    compact = " ".join((task or "").strip().split())
    return bool(compact) and len(compact) <= SHORT_FOLLOWUP_CHARS and len(compact.split()) <= 10


def _stable_explicit_id(
    payload: Dict[str, Any],
    headers: Optional[Any] = None,
) -> Optional[str]:
    if headers is not None:
        try:
            header_map = {str(k).lower(): v for k, v in headers.items()}
        except (AttributeError, TypeError, ValueError):
            header_map = {}
        for key in ("thread-id", "session-id", "session_id"):
            value = header_map.get(key)
            if isinstance(value, str) and value:
                return f"header.{key}:{value}"

    for key in ("conversation_id", "thread_id", "session_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    conversation = payload.get("conversation")
    if isinstance(conversation, str) and conversation:
        return f"conversation:{conversation}"
    if isinstance(conversation, dict):
        value = conversation.get("id")
        if isinstance(value, str) and value:
            return f"conversation:{value}"
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        for key in ("conversation_id", "thread_id", "session_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value:
                return f"metadata.{key}:{value}"
    return None


def session_key(
    payload: Dict[str, Any],
    cwd: Optional[str] = None,
    headers: Optional[Any] = None,
) -> Optional[str]:
    """Best-effort stable thread key, always returned as a one-way hash.

    Prefer explicit conversation/session identifiers, including the protected
    caller headers Codex Router preserves. Raw identifiers are never persisted.
    """
    basis = _stable_explicit_id(payload, headers)
    inp = payload.get("input")
    if basis is None and isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            value = item.get("id")
            if isinstance(value, str) and value:
                basis = f"item:{value}"
                break
    if basis is None:
        texts = _user_texts(payload)
        goal = None
        for text in texts[:2]:
            match = GOAL_RX.search(text)
            if match:
                goal = " ".join(match.group(1).split())[:1000]
                break
        seed = goal or (" ".join(texts[0].split())[:1000] if texts else "")
        if not seed:
            return None
        basis = f"fallback:{cwd or ''}:{seed}"
    return hashlib.sha256(basis.encode("utf-8", "replace")).hexdigest()


class SessionStore:
    """Small JSON store for bounded routing observations."""

    def __init__(self, path: str, ttl_s: int = SESSION_TTL_S, max_entries: int = SESSION_MAX_ENTRIES):
        self.path = os.path.expanduser(path)
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def _read(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _prune(self, data: Dict[str, Dict[str, Any]], now: float) -> Dict[str, Dict[str, Any]]:
        live = {
            key: value
            for key, value in data.items()
            if isinstance(value, dict) and now - float(value.get("seen_at") or 0) <= self.ttl_s
        }
        if len(live) <= self.max_entries:
            return live
        ordered = sorted(live.items(), key=lambda kv: float(kv[1].get("seen_at") or 0), reverse=True)
        return dict(ordered[: self.max_entries])

    def get(self, key: Optional[str]) -> Dict[str, Any]:
        if not key:
            return {}
        now = time.time()
        with self._lock:
            data = self._prune(self._read(), now)
            value = data.get(key)
            return dict(value) if isinstance(value, dict) else {}

    def put(self, key: Optional[str], **fields: Any) -> None:
        if not key:
            return
        now = time.time()
        with self._lock:
            data = self._prune(self._read(), now)
            current = data.get(key)
            current = dict(current) if isinstance(current, dict) else {}
            current.update(fields)
            current["seen_at"] = now
            data[key] = current
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self.path)


@dataclass
class RepoSnapshot:
    project: Optional[str] = None
    dirty_files: Optional[int] = None
    diff_lines: Optional[int] = None
    available: bool = False

    def as_signal(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"available": self.available}
        if self.project:
            out["project"] = self.project
        if self.dirty_files is not None:
            out["dirty_files"] = self.dirty_files
        if self.diff_lines is not None:
            out["diff_lines"] = self.diff_lines
        return out


class RepoProfiler:
    """Read-only, bounded git metadata with a short cache."""

    def __init__(self, cache_s: float = REPO_CACHE_S, timeout_s: float = GIT_TIMEOUT_S):
        self.cache_s = cache_s
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self._cache: Dict[str, tuple[float, RepoSnapshot]] = {}

    def _run(self, cwd: str, *args: str) -> str:
        env = dict(os.environ)
        env["GIT_OPTIONAL_LOCKS"] = "0"
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=self.timeout_s,
            check=False,
            env=env,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    @staticmethod
    def _shortstat(text: str):
        files = insertions = deletions = 0
        match = re.search(r"(\d+) files? changed", text)
        if match:
            files = int(match.group(1))
        match = re.search(r"(\d+) insertions?\(\+\)", text)
        if match:
            insertions = int(match.group(1))
        match = re.search(r"(\d+) deletions?\(-\)", text)
        if match:
            deletions = int(match.group(1))
        return files, insertions + deletions

    def snapshot(self, cwd: Optional[str]) -> RepoSnapshot:
        if not cwd or not os.path.isdir(cwd):
            return RepoSnapshot()
        now = time.time()
        with self._lock:
            cached = self._cache.get(cwd)
            if cached and now - cached[0] <= self.cache_s:
                return cached[1]
        try:
            root = self._run(cwd, "rev-parse", "--show-toplevel")
            if not root:
                snap = RepoSnapshot()
            else:
                shortstat = self._run(cwd, "diff", "--shortstat", "HEAD", "--")
                dirty_files, diff_lines = self._shortstat(shortstat)
                snap = RepoSnapshot(
                    project=os.path.basename(root.rstrip(os.sep)) or None,
                    dirty_files=dirty_files,
                    diff_lines=diff_lines,
                    available=True,
                )
        except (OSError, subprocess.SubprocessError, ValueError):
            snap = RepoSnapshot()
        with self._lock:
            self._cache[cwd] = (now, snap)
        return snap


def next_failure_streak(previous: Dict[str, Any], step: Dict[str, Any]) -> int:
    prior = previous.get("failure_streak", 0)
    prior = prior if isinstance(prior, int) and prior >= 0 else 0
    kind = step.get("step_type")
    if kind == "user_turn":
        return 0
    if kind == "tool_step":
        return min(prior + 1, 9) if step.get("errored") else 0
    return prior


def enrich_jev_state(
    state: Dict[str, Any],
    task: str,
    previous: Dict[str, Any],
    repo: RepoSnapshot,
    failure_streak: int,
) -> Dict[str, Any]:
    """Add bounded evidence. These are observations, not routing rules."""
    out = dict(state)
    signals = dict(out.get("signals") or {})
    signals["short_followup"] = is_short_followup(task)
    out["signals"] = signals

    session: Dict[str, Any] = {"failure_streak": failure_streak}
    previous_model = previous.get("last_model")
    previous_effort = previous.get("last_effort")
    if previous_model in TIERS:
        session["previous_model"] = previous_model
    if previous_effort in EFFORT_RANK:
        session["previous_effort"] = previous_effort
    out["session"] = session

    repo_signal = repo.as_signal()
    if repo_signal.get("available"):
        out["repo"] = repo_signal
    return out


def _max_tier(a: str, b: str) -> str:
    if a not in TIER_RANK:
        return b
    if b not in TIER_RANK:
        return a
    return a if TIER_RANK[a] >= TIER_RANK[b] else b


def _max_effort(a: str, b: str) -> str:
    if a not in EFFORT_RANK:
        return b
    if b not in EFFORT_RANK:
        return a
    return a if EFFORT_RANK[a] >= EFFORT_RANK[b] else b


def _one_step_below(previous: str, ordered, ranks):
    if previous not in ranks:
        return None
    return ordered[max(0, ranks[previous] - 1)]


def apply_guardrails(
    model: str,
    effort: str,
    task: str,
    step: Dict[str, Any],
    previous: Dict[str, Any],
    failure_streak: int,
):
    """Apply bounded, evidence-based floors after Jev's joint decision.

    Rules:
    - one failed tool result cannot downgrade model or effort below the prior route;
    - two consecutive failures use at least Sol + high;
    - three use at least Astra + xhigh; max remains a Jev choice;
    - short continuations can drop model and effort by at most one rung.
    """
    if model not in TIER_RANK or effort not in EFFORT_RANK:
        return model, effort, "apply"

    chosen_model = model
    chosen_effort = effort
    reasons = []
    previous_model = previous.get("last_model")
    previous_effort = previous.get("last_effort")

    if step.get("step_type") == "tool_step" and step.get("errored"):
        if previous_model in TIER_RANK:
            held_model = _max_tier(chosen_model, previous_model)
            if held_model != chosen_model:
                chosen_model = held_model
                reasons.append("failure_hold_model")
        if previous_effort in EFFORT_RANK:
            held_effort = _max_effort(chosen_effort, previous_effort)
            if held_effort != chosen_effort:
                chosen_effort = held_effort
                reasons.append("failure_hold_effort")

    floor_model = floor_effort = None
    if failure_streak >= 3:
        floor_model, floor_effort = ASTRA, "xhigh"
    elif failure_streak >= 2:
        floor_model, floor_effort = SOL, "high"
    if floor_model:
        raised_model = _max_tier(chosen_model, floor_model)
        raised_effort = _max_effort(chosen_effort, floor_effort)
        if raised_model != chosen_model or raised_effort != chosen_effort:
            chosen_model, chosen_effort = raised_model, raised_effort
            reasons.append(f"failure_floor_{failure_streak}")

    if step.get("step_type") == "user_turn" and is_short_followup(task):
        if previous_model in TIER_RANK and TIER_RANK[previous_model] - TIER_RANK[chosen_model] > 1:
            chosen_model = _one_step_below(previous_model, TIERS, TIER_RANK)
            reasons.append("continuation_model_hysteresis")
        if previous_effort in EFFORT_RANK and EFFORT_RANK[previous_effort] - EFFORT_RANK[chosen_effort] > 1:
            chosen_effort = _one_step_below(previous_effort, EFFORTS, EFFORT_RANK)
            reasons.append("continuation_effort_hysteresis")

    return chosen_model, chosen_effort, "+".join(reasons) if reasons else "apply"
