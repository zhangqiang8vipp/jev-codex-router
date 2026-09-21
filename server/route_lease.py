"""Session-aware route leases for Codex agent continuity.

This module deliberately does not call Jev or any other model.  It answers the
cheaper question first: does this Responses call actually require a new routing
judgement?

Route Lease v1 is conservative:
- one meaningful user turn opens a new semantic decision;
- tool/background/compaction continuations reuse that decision;
- duplicate replays of the same user turn reuse it;
- repeated tool failures may raise the lease locally;
- policy-version changes invalidate persisted leases.

Only privacy-preserving hashes and bounded model/effort metadata are persisted.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

from routing_policy import ASTRA, EFFORTS, SOL, TIERS

LEASE_VERSION = 1
TIER_RANK = {name: index for index, name in enumerate(TIERS)}
EFFORT_RANK = {name: index for index, name in enumerate(EFFORTS)}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "\n".join(parts)


def _item_anchor(item: Dict[str, Any]) -> str:
    for key in ("id", "call_id"):
        value = item.get(key)
        if isinstance(value, str) and value:
            return f"{key}:{value}"
    role = item.get("role")
    kind = item.get("type")
    text = _content_text(item.get("content"))
    if not text and isinstance(item.get("output"), str):
        text = item["output"]
    if text:
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        return f"{role or kind or 'item'}:{digest}"
    # Hashing the local JSON shape is privacy-preserving because only the
    # digest is returned/persisted. This also covers image-only user items and
    # opaque compaction items that have no ordinary text or stable id.
    try:
        encoded = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(kind or role or "item")
    digest = hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()
    return f"{kind or role or 'item'}:{digest}"


def human_turn_key(
    payload: Dict[str, Any],
    *,
    task: str = "",
    session_key: Optional[str] = None,
) -> Optional[str]:
    """Privacy-preserving identity for the latest human turn.

    Prefer the user item's own id.  Otherwise bind the normalized current task
    to the immediately preceding assistant/compaction anchor.  That makes an
    exact replay stable while two identical user messages after different
    assistant states remain different turns.
    """
    inp = payload.get("input")
    last_user = None
    previous_anchor = ""

    if isinstance(inp, str):
        raw_user = inp
    elif isinstance(inp, list):
        user_index = None
        for index in range(len(inp) - 1, -1, -1):
            item = inp[index]
            if isinstance(item, dict) and item.get("role") == "user":
                last_user = item
                user_index = index
                break
        if last_user is None:
            return None
        item_id = last_user.get("id")
        if isinstance(item_id, str) and item_id:
            basis = f"user-id:{item_id}"
            seed = f"{session_key or ''}|{basis}"
            return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()

        raw_user = _content_text(last_user.get("content"))
        if user_index is not None:
            for item in reversed(inp[:user_index]):
                if not isinstance(item, dict):
                    continue
                if item.get("role") == "assistant" or item.get("type") == "compaction":
                    previous_anchor = _item_anchor(item)
                    if previous_anchor:
                        break
    else:
        return None

    normalized_task = " ".join((task or "").split())
    normalized_user = " ".join((raw_user or "").split())
    semantic = normalized_task or normalized_user
    if not semantic:
        # Image-only/user-item requests can still be replayed.  Use the hashed
        # local item shape rather than persisting the image URL/content.
        if isinstance(last_user, dict):
            semantic = _item_anchor(last_user)
        if not semantic:
            return None

    semantic_hash = hashlib.sha256(semantic.encode("utf-8", "replace")).hexdigest()
    seed = f"{session_key or ''}|user:{semantic_hash}|prev:{previous_anchor}"
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()


def contains_compaction(payload: Dict[str, Any]) -> bool:
    inp = payload.get("input")
    if not isinstance(inp, list):
        return False
    return any(isinstance(item, dict) and item.get("type") == "compaction" for item in inp)


def tool_step_key(
    payload: Dict[str, Any],
    *,
    session_key: Optional[str] = None,
) -> Optional[str]:
    """Privacy-preserving identity for the latest tool-result continuation.

    Codex can replay an identical Responses request after transport trouble.
    Failure streaks must advance for a new tool result, not for every replay of
    the same result. Only the digest is persisted.
    """
    inp = payload.get("input")
    if not isinstance(inp, list) or not inp:
        return None
    last = inp[-1]
    if not isinstance(last, dict):
        return None
    if last.get("type") not in ("function_call_output", "custom_tool_call_output"):
        return None
    try:
        encoded = json.dumps(last, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        encoded = str(last.get("call_id") or last.get("type") or "tool_step")
    digest = hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()
    seed = f"{session_key or ''}|tool:{digest}"
    return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class RouteLease:
    model: str
    effort: str
    turn_key: Optional[str]
    source: str
    policy_version: str


def read_lease(session: Dict[str, Any], policy_version: str) -> Optional[RouteLease]:
    if not isinstance(session, dict):
        return None
    if session.get("lease_version") != LEASE_VERSION:
        return None
    if session.get("lease_policy_version") != policy_version:
        return None
    model = session.get("lease_model")
    effort = session.get("lease_effort")
    if model not in TIER_RANK or effort not in EFFORT_RANK:
        return None
    turn_key = session.get("lease_turn_key")
    if turn_key is not None and not isinstance(turn_key, str):
        turn_key = None
    source = session.get("lease_source")
    source = source if isinstance(source, str) and source else "unknown"
    return RouteLease(model, effort, turn_key, source, policy_version)


def lease_fields(
    model: str,
    effort: str,
    *,
    turn_key: Optional[str],
    source: str,
    policy_version: str,
) -> Dict[str, Any]:
    if model not in TIER_RANK or effort not in EFFORT_RANK:
        raise ValueError("invalid route lease")
    return {
        "lease_version": LEASE_VERSION,
        "lease_policy_version": policy_version,
        "lease_model": model,
        "lease_effort": effort,
        "lease_turn_key": turn_key if isinstance(turn_key, str) else None,
        "lease_source": source,
    }


def route_action(
    *,
    step_type: str,
    meaningful_user_turn: bool,
    turn_key: Optional[str],
    lease: Optional[RouteLease],
    compacted: bool = False,
) -> tuple[str, str]:
    """Return (REPLAN|KEEP, reason) without calling any model."""
    if lease is None:
        return "REPLAN", "no_valid_lease"

    if step_type == "user_turn" and meaningful_user_turn:
        if turn_key and lease.turn_key == turn_key:
            return "KEEP", "same_user_turn_replay"
        return "REPLAN", "new_user_turn"

    if step_type == "tool_step":
        return "KEEP", "tool_continuation"

    if compacted:
        return "KEEP", "compaction_continuity"

    return "KEEP", "same_session_continuation"


def _max_model(model: str, floor: str) -> str:
    if model not in TIER_RANK:
        return floor
    return model if TIER_RANK[model] >= TIER_RANK[floor] else floor


def _max_effort(effort: str, floor: str) -> str:
    if effort not in EFFORT_RANK:
        return floor
    return effort if EFFORT_RANK[effort] >= EFFORT_RANK[floor] else floor


def apply_failure_escalation(
    model: str,
    effort: str,
    failure_streak: int,
) -> tuple[str, str, Optional[str]]:
    """Raise a leased route locally; never downgrade and never call a model."""
    if failure_streak >= 3:
        floor_model, floor_effort = ASTRA, "xhigh"
    elif failure_streak >= 2:
        floor_model, floor_effort = SOL, "high"
    else:
        return model, effort, None

    raised_model = _max_model(model, floor_model)
    raised_effort = _max_effort(effort, floor_effort)
    if (raised_model, raised_effort) == (model, effort):
        return model, effort, None
    return raised_model, raised_effort, f"failure_floor_{failure_streak}"


class RouteLeaseLocks:
    """Per-session single-flight lock for semantic route decisions."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: Dict[str, Dict[str, Any]] = {}

    @contextlib.contextmanager
    def hold(self, key: Optional[str]):
        if not key:
            yield
            return

        with self._guard:
            record = self._locks.get(key)
            if record is None:
                record = {"lock": threading.Lock(), "refs": 0}
                self._locks[key] = record
            record["refs"] += 1
            lock = record["lock"]

        lock.acquire()
        try:
            yield
        finally:
            lock.release()
            with self._guard:
                record["refs"] -= 1
                if record["refs"] <= 0 and self._locks.get(key) is record:
                    self._locks.pop(key, None)