#!/usr/bin/env python3
"""Jev Codex Router — local server on 127.0.0.1:4319 for the Codex Router.

Receives Responses requests destined for the "jev/auto" model (the Codex
Router's "jev" generic provider), asks Jev (TypeSafe System One) for a tier
and a thinking depth, applies the routing policy, then relays to the Codex
Router's local caller edge (native session sharing enabled) — with no format
conversion: Responses in, Responses out, SSE relayed verbatim.

Routing policy: Jev chooses one (model, thinking effort) pair for every call.
Every pair uses standard speed. Confidence is logged without changing the chosen
model. There are no keyword/scenario overrides or target model proportions.
Technical Jev failures remain fail-open to astra @medium and are logged separately.

Per-call awareness (v2): every request is classified as a fresh user turn, a
tool-step continuation, or other. Tool-steps carry a digest of the last tool
output and its tool name into the Jev state, so Jev routes THIS step
(mechanical continuation, standard next action, or frontier-worthy) instead
of re-judging the session's original prompt. On live sessions (7 days):
~92% of model calls are tool-steps — ~74% of the money weight.

Input handling (v3): Jev sees the current ask, never the thread — the task is
Codex's last user text, with Codex's own machine-generated envelopes stripped
(goal context, plugin catalog, environment, skills) and clipped to the
calibrated 500 chars as head+tail, because Codex appends the real tail after
its own blocks. Thread length never reaches the judge: extract() keeps one tail
item, the assistant side is bounded to 240 chars, and the only thread-sized
number (n_items) stays in the local log instead of the Jev state.
Live data (4 516 calls): 1 291 (29%) had sent Jev nothing but a
`<codex_internal_context source="goal">` block (~6.4k chars, p50) and 23 more
only a `<recommended_plugins>` catalog — the head-clip stopped inside the
envelope, so the user's actual request was never judged.

Fail-open: any Jev error → astra @medium. Kill switch: file
~/.codex/codex-router/jev-router.off → relay astra without a decision.
Shadow: file ~/.codex/codex-router/jev-router.shadow → decide and log the
route, but serve plain astra (quality-neutral data collection).
Debug: file ~/.codex/codex-router/jev-router.debug → dump request shapes
(jev-router-debug.jsonl) and raw response streams (jev-router-debug-stream.log).
Display: streamed reasoning summaries get the routed tag appended in place
( · 🧠sol:low · ) so the Codex thread shows the picked model per call.
The same rewriter keeps one response id across a relayed stream: the response
has already crossed the local edge once, so its terminal event can carry a
re-encrypted id, and the Responses consumer in front of us refuses a completion
whose id differs from the one `response.created` announced.
Non-stream callers (auto-compaction checkpoints, litellm non-stream path)
receive the SSE stream reassembled into a single JSON response object.
Balance: sufficient capability and effort for the next decision, including
compaction. Capability profiles are priors; outcome quality requires evaluation.
Log: ~/.codex/codex-router/jev-router-live.jsonl

Auto routing is OpenAI-only: the served model must remain one of the four
native tiers (Luna, Terra, Sol, Astra). Native quota failures are returned to
the caller as-is; Jev never substitutes a third-party model.
"""
import codecs
import http.client
import json
import os
import re
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from auto_control import set_enabled as set_auto_enabled
from auto_control import status as auto_status
from routing_policy import (ASTRA, EFFORTS, LUNA, POLICY_VERSION, QUESTIONS, SOL,
                            TERRA, TIERS, decision_from_answers, route)
from smart_context import (RepoProfiler, SessionStore, apply_guardrails,
                           enrich_jev_state, extract_cwd, next_failure_streak,
                           session_key)
from shadow_eval import (append_event as append_shadow_event,
                         build_tool_feedback, build_turn_event, new_turn_id)

HOME = os.path.expanduser("~")
CODEX_HOME = os.path.realpath(os.path.expanduser(
    os.environ.get("CODEX_HOME", os.path.join(HOME, ".codex"))))
STATE = os.path.realpath(os.path.expanduser(
    os.environ.get("CODEX_ROUTER_STATE_DIR", os.path.join(CODEX_HOME, "codex-router"))))
CODEX_ROUTER_DIR = os.environ.get("CODEX_ROUTER_DIR", "").strip()
ENV_PATH = os.path.join(HOME, ".hermes", ".env")
LEGACY_ENV_PATH = os.path.join(HOME, ".jev.env")
CALLER_SECRET_PATH = os.path.join(STATE, "caller-secret")
OFF_PATH = os.path.join(STATE, "jev-router.off")
SHADOW_PATH = os.path.join(STATE, "jev-router.shadow")
DEBUG_PATH = os.path.join(STATE, "jev-router.debug")
# Opt-in route header on each assistant text message. Presentation metadata is
# removed from replayed history, including legacy trailing signatures.
SIGNATURE_PATH = os.path.join(STATE, "jev-router.signature")
LOG_PATH = os.path.join(STATE, "jev-router-live.jsonl")
SHADOW_EVAL_PATH = os.path.join(STATE, "jev-shadow-eval.jsonl")
SESSION_PATH = os.path.join(STATE, "jev-router-sessions.json")
SESSION_STORE = SessionStore(SESSION_PATH)
REPO_PROFILER = RepoProfiler()

def _port_from_env(*names, default):
    for name in names:
        value = os.environ.get(name, "").strip()
        if not value:
            continue
        try:
            port = int(value)
        except ValueError:
            continue
        if 1 <= port <= 65535:
            return port
    return default


LISTEN = ("127.0.0.1", _port_from_env("JEV_ROUTER_PORT", default=4319))
ROUTER = ("127.0.0.1", _port_from_env(
    "MODEL_ROUTER_PORT", "CODEX_ROUTER_PORT", default=4202))

DISPLAY_NAME = "Jev Codex Router"
VERSION = "1.6"
VIRTUAL_MODEL_ID = "auto"
VIRTUAL_MODEL_SLUG = "jev/auto"
VIRTUAL_CONTEXT_WINDOW = 1_050_000
VIRTUAL_MAX_OUTPUT_TOKENS = 128_000

API = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"


# Generic ask surface (POST /ask): a thin typed pass-through to System One for
# callers that own their question set — the in-app browser chooser is the first
# one. No routing policy, no logging of the caller's state.
ASK_PATHS = ("/ask", "/v1/ask")
ASK_MAX_BYTES = 256 * 1024
ASK_MAX_STATE_CHARS = 120_000
ASK_MAX_QUESTIONS = 40
ASK_TIMEOUT = 15.0
ASK_TYPES = ("noul", "choice", "score")
CONTROL_MAX_BYTES = 4096
# The live Codex catalog contains rich metadata for every routed/native model
# and can exceed 512 KiB. Keep the readiness read bounded, but large enough for
# a realistic merged catalog so the checker never parses a deliberately
# truncated JSON document.
MODEL_CATALOG_MAX_BYTES = 16 * 1024 * 1024

# The events that close a Responses stream and repeat the response id it opened
# with. A relayed (tandem) stream is rewritten onto that opening id.
TERMINAL_EVENT_TYPES = ("response.completed", "response.incomplete", "response.failed")

ERROR_RX = re.compile(
    r"(?i)(traceback|error|failed|exit code [1-9]|assertion|exception|fatal|panic)")
DIGEST_CHARS = 520

# The task budget Jev was calibrated on (500 chars), spent as a head+tail window
# so the tail survives: Codex puts its own blocks around the user's text, and the
# ask itself can be the last thing in a long paste. 320 + marker + 171 <= 500.
TASK_CHARS = 500
TASK_HEAD_CHARS = 320
TASK_TAIL_CHARS = TASK_CHARS - TASK_HEAD_CHARS - 9
TASK_CLIP_MARK = "\n[...]\n"

# Codex wraps every turn in machine-generated blocks (goal context, plugin
# catalog, environment, skills, mode notices). They are the longest part of a
# turn and they describe the harness, not the work, so they are removed before
# the clip instead of eating the budget. A block that is never closed simply
# does not match and is left to the clip.
ENVELOPE_TAGS = (
    "codex_internal_context", "recommended_plugins", "environment_context",
    "skills_instructions", "plugins_instructions", "apps_instructions",
    "app-context", "collaboration_mode", "model_switch", "multi_agent_mode",
    "permissions instructions", "memory_instructions",
)
ENVELOPE_RX = re.compile(
    r"<(%s)(?:\s[^<>]*)?>.*?</\1\s*>" % "|".join(re.escape(t) for t in ENVELOPE_TAGS),
    re.S)
# `<codex_internal_context source="goal">` is the one envelope whose body is a
# work objective ("Continue working toward the active thread goal. / The
# objective below is ..."), so it is what an envelope-only turn falls back to.
GOAL_BODY_RX = re.compile(
    r'<codex_internal_context(?:\s[^<>]*)?>(.*?)</codex_internal_context\s*>', re.S)
# A single envelope has been seen at 950k chars. Past this size only the two ends
# are scanned, which is where the user's text sits anyway.
ENVELOPE_SCAN_CHARS = 200_000

_log_lock = threading.Lock()
_route_status_lock = threading.Lock()
_last_route_status = {}


def update_last_route_status(model, effort, gate, at):
    with _route_status_lock:
        _last_route_status.clear()
        _last_route_status.update({
            "model": model,
            "effort": effort,
            "gate": gate,
            "at": at,
        })


def last_route_status():
    with _route_status_lock:
        return dict(_last_route_status)


def key_paths():
    """Key files in precedence order; JEV_ENV_FILE is an explicit override."""
    paths = []
    override = os.environ.get("JEV_ENV_FILE", "").strip()
    if override:
        paths.append(os.path.realpath(os.path.expanduser(override)))
    paths.extend((ENV_PATH, LEGACY_ENV_PATH))
    out = []
    for path in paths:
        if path and path not in out:
            out.append(path)
    return tuple(out)


def load_key():
    """TYPESAFE_API_KEY: key files win; process env is a last resort."""
    for path in key_paths():
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("TYPESAFE_API_KEY="):
                        value = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if value:
                            return value
        except OSError:
            continue
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def caller_secret():
    with open(CALLER_SECRET_PATH, encoding="utf-8") as fh:
        return fh.read().strip()


def call_jev(key, state, questions=None, timeout=4.0):
    body = json.dumps({"model": MODEL, "state": state,
                       "questions": QUESTIONS if questions is None else questions}).encode()
    req = urllib.request.Request(
        API,
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_jev_routed(key, state, questions=None, timeout=4.0):
    """One System One call, on the direct TypeSafe API."""
    return call_jev(key, state, questions, timeout=timeout)


def validate_ask(body):
    """Check a POST /ask body. Returns (state, questions, error) — error is None when valid.

    Bounds are the whole point: the endpoint spends TypeSafe credits on loopback,
    so it accepts only a JSON-serialisable state under the size cap and a small
    set of well-formed typed questions.
    """
    if not isinstance(body, dict):
        return None, None, "json object expected"
    state = body.get("state")
    if not isinstance(state, (str, dict, list)):
        return None, None, "state must be a string, object or array"
    try:
        size = len(json.dumps(state, ensure_ascii=False))
    except (TypeError, ValueError):
        return None, None, "state is not JSON-serialisable"
    if size > ASK_MAX_STATE_CHARS:
        return None, None, f"state too large ({size} > {ASK_MAX_STATE_CHARS} chars)"
    questions = body.get("questions")
    if not isinstance(questions, dict) or not questions:
        return None, None, "questions must be a non-empty object"
    if len(questions) > ASK_MAX_QUESTIONS:
        return None, None, f"too many questions ({len(questions)} > {ASK_MAX_QUESTIONS})"
    for name, question in questions.items():
        if not isinstance(name, str) or not name:
            return None, None, "question names must be non-empty strings"
        if not isinstance(question, dict):
            return None, None, f"question {name} must be an object"
        qtype = question.get("type")
        if qtype not in ASK_TYPES:
            return None, None, f"question {name} has unsupported type {qtype!r}"
        if not isinstance(question.get("instructions"), (str, dict, list)):
            return None, None, f"question {name} needs instructions"
        criteria = question.get("criteria")
        if qtype == "choice" and (not isinstance(criteria, dict) or not criteria):
            return None, None, f"question {name} needs a non-empty criteria map"
        if qtype == "score" and not isinstance(criteria, list):
            return None, None, f"question {name} needs a criteria list"
    return state, questions, None


def _content_text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") in ("input_text", "output_text", "text"):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def strip_envelopes(text):
    """Codex's own wrapper blocks, out of the text the judge reads.

    Whole blocks go: their bodies describe the harness, not the work, and they
    are by far the longest part of a turn. When a turn holds nothing else, the
    goal block's body is salvaged -- it carries the thread objective, and an
    empty task would push the call onto the caller's fail-open path. A catalog-
    or environment-only turn holds no request at all, so it keeps nothing.
    """
    if len(text) <= ENVELOPE_SCAN_CHARS:
        scanned = text
    else:
        scanned = text[:ENVELOPE_SCAN_CHARS] + "\n" + text[-ENVELOPE_SCAN_CHARS:]
    stripped = ENVELOPE_RX.sub("\n", scanned).strip()
    if stripped:
        return stripped
    goal = GOAL_BODY_RX.search(scanned)
    return goal.group(1).strip() if goal else ""


def clip_task(text):
    """Bound the task to Jev's calibrated budget, keeping head and tail."""
    text = text.strip()
    if len(text) <= TASK_CHARS:
        return text
    return text[:TASK_HEAD_CHARS] + TASK_CLIP_MARK + text[-TASK_TAIL_CHARS:]


def task_for_jev(text):
    """The current ask, out of Codex's envelopes, in <= TASK_CHARS characters.

    Empty means the turn carried no request (a catalog- or environment-only
    turn), which the caller reads as "no judgement to make here".
    """
    return clip_task(strip_envelopes(text))


def extract(payload):
    """Last user message (envelope-free, clipped) + last assistant message + small stats."""
    inp = payload.get("input")
    last_user = last_assistant = ""
    n_items = 0
    has_image = False
    tool_tail = False
    if isinstance(inp, str):
        last_user = inp
        n_items = 1
    elif isinstance(inp, list):
        n_items = len(inp)
        tail = inp[-6:]
        for item in tail:
            if isinstance(item, dict) and item.get("type") == "function_call_output":
                tool_tail = True
            if isinstance(item, dict) and isinstance(item.get("content"), list):
                for part in item["content"]:
                    if isinstance(part, dict) and part.get("type") in ("input_image", "image_url"):
                        has_image = True
        for item in reversed(inp):
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role == "user" and not last_user:
                last_user = _content_text(item.get("content"))
            elif role == "assistant" and not last_assistant:
                last_assistant = _content_text(item.get("content"))
            if last_user and last_assistant:
                break
    return task_for_jev(last_user), last_assistant.strip(), {
        "n_items": n_items,
        "has_image": has_image,
        "tool_history": tool_tail,
    }


def _output_text(output):
    """Best-effort text of a tool output item (str, list of parts, or dict)."""
    if isinstance(output, str):
        return output
    if isinstance(output, list):
        parts = []
        for item in output:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "output", "content"):
                    value = item.get(key)
                    if isinstance(value, str):
                        parts.append(value)
                        break
        return "\n".join(parts)
    if isinstance(output, dict):
        for key in ("text", "output", "content"):
            value = output.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(output)[:4000]
    return ""


def classify(payload):
    """What this model call is for, read off the input tail: user turn / tool step."""
    inp = payload.get("input")
    detail = {"step_type": "other", "digest": "", "errored": False, "n_items": 0}
    if isinstance(inp, str):
        detail["step_type"] = "user_turn"
        return detail
    if not isinstance(inp, list):
        return detail
    detail["n_items"] = len(inp)
    last = inp[-1] if inp else None
    if isinstance(last, dict):
        ltype = last.get("type")
        if ltype in ("function_call_output", "custom_tool_call_output"):
            text = _output_text(last.get("output"))
            detail["step_type"] = "tool_step"
            detail["digest"] = text.strip()[-DIGEST_CHARS:] if text else ""
            detail["errored"] = bool(ERROR_RX.search(text[-4000:]))
            call_id = last.get("call_id")
            if call_id:
                for item in reversed(inp[:-1]):
                    if (isinstance(item, dict) and item.get("call_id") == call_id
                            and item.get("type") in ("function_call", "custom_tool_call")):
                        detail["tool_call"] = {"name": str(item.get("name") or "")[:160]}
                        break
        elif last.get("role") == "user":
            detail["step_type"] = "user_turn"
    return detail


def jev_state(task, prev_assistant, signals, step):
    """The state sent to Jev: the current ask plus signals that do not grow.

    `n_items` is deliberately left out. It is the one number that scales with the
    thread, and the calibrated shapes (backtest, shadow replay) never carried it,
    so letting it travel would make the same last exchange judge differently in a
    long thread than in a short one — the opposite of per-call routing. It stays
    in the local decision log.
    """
    state = {
        "task": task,
        "signals": {k: v for k, v in signals.items() if k != "n_items"},
        "step": {"type": step["step_type"]},
    }
    if prev_assistant:
        state["previous_assistant"] = prev_assistant[-240:]
    if step["step_type"] == "tool_step":
        state["step"]["last_tool_output_tail"] = step["digest"]
        if step.get("tool_call"):
            state["step"]["tool_call"] = step["tool_call"]
    return state


def _debug_shape(payload):
    """Bounded request shape for wire debugging (jev-router.debug flag)."""
    inp = payload.get("input")
    items = inp if isinstance(inp, list) else []
    tail = []
    for item in items[-8:]:
        if isinstance(item, dict):
            tail.append(item.get("type") or item.get("role"))
    names = []
    for tool in (payload.get("tools") or [])[:10]:
        if isinstance(tool, dict):
            names.append(tool.get("name") or (tool.get("function") or {}).get("name"))
    return {
        "keys": sorted(payload.keys()),
        "model": payload.get("model"),
        "reasoning": payload.get("reasoning"),
        "include": payload.get("include"),
        "stream": payload.get("stream"),
        "store": payload.get("store"),
        "tool_choice": payload.get("tool_choice"),
        "parallel_tool_calls": payload.get("parallel_tool_calls"),
        "instructions_head": (payload.get("instructions") or "")[:200],
        "n_input": len(items),
        "tail": tail,
        "tools": names,
    }


ROUTE_GLYPHS = {
    "gpt-5.6-luna": ("luna", "⚡"),      # cheap tier, adaptive thinking
    "gpt-5.6-sol": ("sol", "🧠"),        # reasoning workhorse
    "gpt-6-astra": ("astra", "🚀"),      # frontier
    TERRA: ("terra", "🌍"),
}
def route_label(model):
    """(short name, glyph) of a routed call — the vocabulary of both tags."""
    short, glyph = ROUTE_GLYPHS.get(model, (None, None))
    if not short:
        leaf = (model or "?").split("/")[-1]
        short, glyph = leaf, "⚡"
    return short, glyph


def route_marker(model, effort):
    """Visible tag for a routed call, separators on both sides: ' · 🧠sol:low · '.

    The client concatenates reasoning summary parts with no separator, so the
    tag has to carry its own trailing one (" · ") or it glues to the next part.
    """
    short, glyph = route_label(model)
    return f" · {glyph} {short}" + (f":{effort}" if effort else "") + " · "


def answer_signature(shown):
    """Leading model/thinking label for each assistant message, when enabled."""
    if not os.path.exists(SIGNATURE_PATH):
        return None
    short, glyph = route_label(shown.get("model"))
    effort = shown.get("effort") or "non spécifié"
    return f"**{glyph} {short} · thinking: {effort}**\n\n"


# Only our exact presentation forms, at the boundaries of assistant text.
# Retain the trailing form solely for old transcripts.
HEADER_RX = re.compile(
    r"\A\*\*(?:⚡|🧠|🚀|🌍|🐳|✨) [A-Za-z0-9._/-]+ · thinking: "
    r"(?:low|medium|high|xhigh|max|non spécifié)\*\*\r?\n\r?\n")
SIGNATURE_RX = re.compile(
    r"\s*\n*—\s+(?:⚡|🧠|🚀|🌍|🐳|✨)\s+[A-Za-z0-9._/-]+"
    r"(?:\s+·\s+(?:low|medium|high|xhigh|max))?\s*$")


def strip_signatures(payload):
    """Remove route annotations before classification and forwarding, even if disabled."""
    items = payload.get("input")
    if not isinstance(items, list):
        return 0
    removed = 0
    for item in items:
        if not isinstance(item, dict) or item.get("role") != "assistant":
            continue
        content = item.get("content")
        if isinstance(content, str):
            cleaned = SIGNATURE_RX.sub("", HEADER_RX.sub("", content))
            if cleaned != content:
                item["content"] = cleaned
                removed += 1
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str) or not text:
                continue
            cleaned = SIGNATURE_RX.sub("", HEADER_RX.sub("", text))
            if cleaned != text:
                block["text"] = cleaned
                removed += 1
    return removed


def usage_counts(usage):
    """Allowlist token counters; absent usage stays unknown, never zero."""
    if not isinstance(usage, dict):
        return None
    out = {}
    for name in ("input_tokens", "output_tokens", "total_tokens",
                 "cache_write_input_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[name] = value
    for group, name in (("input_tokens_details", "cached_tokens"),
                        ("output_tokens_details", "reasoning_tokens")):
        details = usage.get(group)
        value = details.get(name) if isinstance(details, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out["cached_input_tokens" if name == "cached_tokens" else name] = value
    return out or None


class SummaryMarker:
    """Append the routed tag to reasoning summaries (the thread's thinking blocks).

    The last delta of each summary part is held back by one event so the tag can
    be appended in place to it and to the matching done events: no fabricated
    events, no sequence-number surgery, byte-exact pass-through everywhere else.

    A ``signature`` now means a leading route header on every assistant text
    message, including commentary and messages without a phase. The first text
    delta receives it immediately; done events and full items carry the same
    prefix. Tool arguments and reasoning content never receive this header.

    The same pass keeps a relayed stream's response id consistent. A Codex-dry
    call crosses the local edge, which encrypts response ids, so the terminal
    event of the stream we receive carries a freshly encoded id; the Responses
    transform in front of the router reads that as a completion that renamed
    itself and replaces the whole turn with an error event. The id announced by
    `response.created` is the one that travels, so a terminal event is rewritten
    onto it before the block leaves.
    """

    def __init__(self, marker, signature=None):
        self.marker = marker
        self.signature = signature or None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf = ""
        self._block = []
        self._held = None  # (key, block_lines)
        self._headed = set()  # messages whose streamed text already received a header
        self._header_parts = {}  # first nonempty text part of each message
        self._response_id = None  # the id this stream's completion must repeat
        self.usage = None
        self.terminal_type = None

    @staticmethod
    def _emit(lines):
        return "".join(line + "\n" for line in lines) + "\n"

    @staticmethod
    def _event_type(block):
        for line in block:
            if line.startswith("event: "):
                return line[7:].strip()
        return ""

    @staticmethod
    def _data(block):
        for line in block:
            if line.startswith("data: "):
                try:
                    return json.loads(line[6:])
                except ValueError:
                    return None
        return None

    @staticmethod
    def _rebuild(block, data):
        return [
            f"data: {json.dumps(data, ensure_ascii=False)}" if line.startswith("data: ") else line
            for line in block
        ]

    def _tag(self, value):
        if not isinstance(value, str) or not value or self.marker in value:
            return value
        return value + self.marker

    def _sign(self, value):
        """Prefix a full text representation once, preserving an empty output."""
        if not self.signature or not isinstance(value, str) or not value:
            return value
        return value if value.startswith(self.signature) else self.signature + value

    @staticmethod
    def _message_key(data):
        return data.get("item_id") or ("output", data.get("output_index", 0))

    def _sign_done_block(self, block):
        data = self._data(block)
        if not isinstance(data, dict):
            return block
        target = data.get("part") if isinstance(data.get("part"), dict) else data
        text = target.get("text")
        if isinstance(text, str) and text:
            key = self._message_key(data)
            index = data.get("content_index", 0)
            if self._header_parts.setdefault(key, index) == index:
                target["text"] = self._sign(text)
        return self._rebuild(block, data)

    def _sign_message_item(self, item):
        """One header on the first nonempty text part of an assistant message."""
        if (not self.signature or not isinstance(item, dict)
                or item.get("type") != "message" or item.get("role") not in (None, "assistant")):
            return
        for index, part in enumerate(item.get("content") or []):
            if (isinstance(part, dict) and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str) and part["text"]):
                part["text"] = self._sign(part["text"])
                self._header_parts.setdefault(item.get("id"), index)
                return

    def _flush_held(self, out):
        if self._held is not None:
            out.append(self._emit(self._held[1]))
            self._held = None

    def _tag_delta_block(self, block):
        data = self._data(block)
        if not isinstance(data, dict):
            return block
        data["delta"] = self._tag(data.get("delta"))
        return self._rebuild(block, data)

    def _tag_done_block(self, block):
        data = self._data(block)
        if not isinstance(data, dict):
            return block
        if "text" in data:
            data["text"] = self._tag(data.get("text"))
        part = data.get("part")
        if isinstance(part, dict) and "text" in part:
            part["text"] = self._tag(part.get("text"))
        return self._rebuild(block, data)

    def _tag_item_block(self, block):
        data = self._data(block)
        if not isinstance(data, dict):
            return block
        item = data.get("item")
        if isinstance(item, dict) and item.get("type") == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict) and "text" in part:
                    part["text"] = self._tag(part.get("text"))
        self._sign_message_item(item)
        response = data.get("response")
        if isinstance(response, dict):
            for item in response.get("output") or []:
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    for part in item.get("summary") or []:
                        if isinstance(part, dict) and "text" in part:
                            part["text"] = self._tag(part.get("text"))
                self._sign_message_item(item)
        return self._rebuild(block, data)

    def _process_block(self, block):
        out = []
        data = self._data(block)
        if not isinstance(data, dict):
            self._flush_held(out)
            out.append(self._emit(block))
            return out
        dtype = data.get("type")
        if dtype == "response.created":
            response = data.get("response")
            if isinstance(response, dict) and isinstance(response.get("id"), str):
                self._response_id = response["id"]
        if self.signature and dtype == "response.output_item.added":
            item = data.get("item")
            if isinstance(item, dict) and item.get("type") == "message":
                self._sign_message_item(item)
                if item.get("id") in self._header_parts:
                    self._headed.add(item["id"])
                self._flush_held(out)
                out.append(self._emit(self._rebuild(block, data)))
                return out
        if self.signature and dtype == "response.output_text.delta":
            key = self._message_key(data)
            delta = data.get("delta")
            if isinstance(delta, str) and delta and key not in self._headed:
                self._header_parts.setdefault(key, data.get("content_index", 0))
                data["delta"] = self._sign(delta)
                self._headed.add(key)
                block = self._rebuild(block, data)
            self._flush_held(out)
            out.append(self._emit(block))
            return out
        if self.signature and dtype in ("response.output_text.done", "response.content_part.done"):
            self._flush_held(out)
            out.append(self._emit(self._sign_done_block(block)))
            return out
        if self.signature and dtype == "response.content_part.added":
            part = data.get("part") or {}
            if part.get("type") == "output_text" and part.get("text"):
                block = self._sign_done_block(block)
                self._headed.add(self._message_key(data))
            self._flush_held(out)
            out.append(self._emit(block))
            return out
        if dtype == "response.reasoning_summary_text.delta":
            if self._held is not None:
                out.append(self._emit(self._held[1]))
            key = (data.get("item_id"), data.get("summary_index"))
            self._held = (key, block)
            return out
        if dtype == "response.reasoning_summary_text.done":
            if self._held is not None:
                key = (data.get("item_id"), data.get("summary_index"))
                held_block = self._held[1]
                if self._held[0] == key:
                    held_block = self._tag_delta_block(held_block)
                out.append(self._emit(held_block))
                self._held = None
            out.append(self._emit(self._tag_done_block(block)))
            return out
        if dtype == "response.reasoning_summary_part.done":
            if self._held is not None:
                key = (data.get("item_id"), data.get("summary_index"))
                held_block = self._held[1]
                if self._held[0] == key:
                    held_block = self._tag_delta_block(held_block)
                out.append(self._emit(held_block))
                self._held = None
            out.append(self._emit(self._tag_done_block(block)))
            return out
        if dtype == "response.output_item.done":
            item = data.get("item") or {}
            if item.get("type") == "reasoning":
                if self._held is not None:
                    held_block = self._held[1]
                    if self._held[0][0] == item.get("id"):
                        held_block = self._tag_delta_block(held_block)
                    out.append(self._emit(held_block))
                    self._held = None
                out.append(self._emit(self._tag_item_block(block)))
                return out
            if self.signature and item.get("type") == "message":
                self._flush_held(out)
                out.append(self._emit(self._tag_item_block(block)))
                return out
        if dtype in TERMINAL_EVENT_TYPES:
            response = data.get("response")
            self.terminal_type = dtype
            self.usage = usage_counts(response.get("usage")) if isinstance(response, dict) else None
            if (
                self._response_id
                and isinstance(response, dict)
                and response.get("id") != self._response_id
            ):
                # Two encodings of one response id, not two responses: the
                # consumer in front of us only accepts a completion that repeats
                # the id it already saw.
                response["id"] = self._response_id
                block = self._rebuild(block, data)
            out.append(self._emit(self._tag_item_block(block)))
            return out
        self._flush_held(out)
        out.append(self._emit(block))
        return out

    def feed(self, raw):
        self._buf += self._decoder.decode(raw)
        out = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.endswith("\r"):
                line = line[:-1]
            if line == "":
                if self._block:
                    out.extend(self._process_block(self._block))
                    self._block = []
                out.append("\n")
            else:
                self._block.append(line)
        return "".join(out)

    def flush(self):
        out = []
        self._flush_held(out)
        if self._block:
            out.append(self._emit(self._block))
            self._block = []
        out.append(self._buf)
        self._buf = ""
        return "".join(out)


def assemble_sse(raw):
    """Rebuild the final response object from an SSE stream (non-stream requests)."""
    final = None
    error = None
    items = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except ValueError:
            continue
        etype = event.get("type") if isinstance(event, dict) else None
        if etype == "response.output_item.done" and isinstance(event.get("item"), dict):
            items[event.get("output_index") or 0] = event["item"]
        elif etype == "response.completed":
            final = event.get("response")
        elif isinstance(etype, str) and etype in ("response.failed", "error"):
            error = event
    if final is not None:
        if not final.get("output") and items:
            final["output"] = [items[i] for i in sorted(items)]
        return final
    if error is not None:
        return {"error": error}
    return None


def log_line(record):
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "jev-router/1.6"

    def log_message(self, *args):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _auto_status(self):
        snapshot = auto_status(STATE, CODEX_ROUTER_DIR).as_dict()
        snapshot["route"] = last_route_status() or None
        snapshot["policy_version"] = POLICY_VERSION
        return snapshot

    def _auto_control(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > CONTROL_MAX_BYTES:
            self.close_connection = True
            return self._json(413, {"error": {"message": "control body too large"}})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._json(400, {"error": {"message": "invalid json"}})
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return self._json(400, {"error": {"message": "enabled must be boolean"}})
        result = set_auto_enabled(STATE, CODEX_ROUTER_DIR, body["enabled"])
        payload = result.as_dict()
        payload["route"] = last_route_status() or None
        payload["policy_version"] = POLICY_VERSION
        code = 200 if result.available and result.error is None else 503
        return self._json(code, payload)

    def _ask(self):
        """Typed pass-through to System One for local callers (:4319, loopback only).

        No policy, no logging of the caller's state: the body is validated,
        forwarded as-is and only the typed answers come back.
        """
        length = int(self.headers.get("Content-Length") or 0)
        if length > ASK_MAX_BYTES:
            # Do not drain an oversized body: answer and close so a wrong or
            # hostile Content-Length cannot make the server buffer it.
            self.close_connection = True
            return self._json(413, {"error": {"message": f"body too large ({length} bytes)"}})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._json(400, {"error": {"message": "invalid json"}})
        state, questions, error = validate_ask(body)
        if error:
            return self._json(400, {"error": {"message": error}})
        key = load_key()
        if not key:
            return self._json(503, {"error": {"message": "TYPESAFE_API_KEY is not configured"}})
        t0 = time.time()
        try:
            answer = call_jev_routed(key, state, questions, timeout=ASK_TIMEOUT)
        except Exception as exc:
            return self._json(502, {"error": {"message": f"jev: {exc}"[:300]}})
        return self._json(200, {
            "model": answer.get("model"),
            "answers": answer.get("answers") or {},
            "usage": answer.get("usage") or {},
            "ms": int((time.time() - t0) * 1000),
        })

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path in ("/v1/models", "/models"):
            self._json(200, {
                "object": "list",
                "data": [{
                    "id": VIRTUAL_MODEL_ID,
                    "object": "model",
                    "created": 1758000000,
                    "owned_by": "jev",
                    "name": DISPLAY_NAME,
                    "display_name": DISPLAY_NAME,
                    "context_length": VIRTUAL_CONTEXT_WINDOW,
                    "max_output_tokens": VIRTUAL_MAX_OUTPUT_TOKENS,
                    "input_modalities": ["text", "image"],
                    "output_modalities": ["text"],
                    "supports_tools": True,
                    "supports_reasoning": True,
                    "supports_vision": True,
                }],
            })
        elif path in ("/control/status", "/v1/control/status"):
            self._json(200, self._auto_status())
        elif path in ("/health", ""):
            self._json(200, {"ok": True, "service": "jev-router", "version": VERSION,
                             "policy_version": POLICY_VERSION,
                             "auto": self._auto_status()["auto"]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        try:
            self._post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:  # fail-open at the response level only
            try:
                self._json(502, {"error": {"message": f"jev-router: {exc}"}})
            except Exception:
                pass

    def _post(self):
        path = self.path.split("?", 1)[0]
        if path.rstrip("/") in ASK_PATHS:
            return self._ask()
        if path.rstrip("/") in ("/control/auto", "/v1/control/auto"):
            return self._auto_control()
        if "/responses" not in path:
            return self._json(404, {"error": {"message": f"unsupported path {path}"}})

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._json(400, {"error": {"message": "invalid json"}})
        if not isinstance(payload, dict):
            return self._json(400, {"error": {"message": "json object expected"}})
        # Our own answer signatures never travel back upstream (see
        # strip_signatures): the model must not read its own route tag.
        stripped = strip_signatures(payload)

        t0 = time.time()
        debug = os.path.exists(DEBUG_PATH)
        if debug:
            try:
                with open(os.path.join(STATE, "jev-router-debug.jsonl"), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(
                        {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "shape": _debug_shape(payload)},
                        ensure_ascii=False) + "\n")
            except OSError:
                pass
        task, prev_assistant, signals = extract(payload)
        step = classify(payload)
        cwd = extract_cwd(payload)
        thread_key = session_key(payload, cwd)
        session = SESSION_STORE.get(thread_key)
        failure_streak = next_failure_streak(session, step)
        repo = REPO_PROFILER.snapshot(cwd)
        stream_requested = payload.get("stream") is True
        turn_id = new_turn_id()
        session_tag = thread_key[:16] if thread_key else None
        previous_eval_id = session.get("last_eval_id")
        if (step.get("step_type") == "tool_step"
                and isinstance(previous_eval_id, str) and previous_eval_id):
            append_shadow_event(
                SHADOW_EVAL_PATH,
                build_tool_feedback(
                    at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                    turn_id=previous_eval_id,
                    session=session_tag,
                    errored=bool(step.get("errored")),
                ),
            )

        tier = depth = conf = None
        jev_ms = None
        decision = None
        jev_usage = None
        smart_gate = None
        if os.path.exists(OFF_PATH):
            model, effort, speed, gate = ASTRA, None, "default", "off"
        else:
            key = load_key()
            if key and (task or step.get("digest") or signals.get("has_image")):
                jt0 = time.time()
                state = jev_state(task, prev_assistant, signals, step)
                state = enrich_jev_state(state, task, session, repo, failure_streak)
                try:
                    result = call_jev_routed(key, state)
                    decision = decision_from_answers(result.get("answers"))
                    raw_usage = result.get("usage") or {}
                    if not isinstance(raw_usage, dict):
                        raw_usage = {}
                    jev_usage = {k: v for k, v in raw_usage.items()
                                 if k in ("input_tokens", "output_tokens", "inputTokens", "outputTokens")
                                 and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                    tier, depth, conf = (decision["model"], decision["effort"],
                                         decision["confidence"])
                    model, effort, speed, gate = route(tier, depth)
                    model, effort, smart_gate = apply_guardrails(
                        model, effort, task, step, session, failure_streak)
                    if smart_gate != "apply":
                        gate = f"{gate}+{smart_gate}"
                except Exception as exc:
                    model, effort, speed, gate = ASTRA, "medium", "default", f"jev_error:{type(exc).__name__}"
                jev_ms = int((time.time() - jt0) * 1000)
            else:
                model, effort, speed, gate = ASTRA, "medium", "default", "no_key_or_task"

        # Capture the production smart route before operational shadow/dry
        # overrides. The raw Jev route remains tier/depth above.
        smart_model, smart_effort, smart_speed, smart_route_gate = (
            model, effort, speed, gate
        )

        # Remember only bounded routing state. Operational fail-open paths do
        # not overwrite the last healthy semantic route.
        if thread_key:
            remembered = {"failure_streak": failure_streak}
            if decision is not None and model in TIERS:
                remembered.update(last_model=model, last_effort=effort)
            SESSION_STORE.put(thread_key, **remembered)

        would = None
        if os.path.exists(SHADOW_PATH):
            would = {"model": model, "effort": effort, "speed": speed, "gate": gate}
            model, effort, speed, gate = ASTRA, None, "default", "shadow(astra)"

        # Jev Auto is intentionally OpenAI-only. No quota or operational path
        # may substitute a third-party model.
        dry_reason = None
        native_model = model

        # Display the model actually serving the request, including shadow and
        # operational fallbacks, rather than a hypothetical classification.
        shown = {"model": model, "effort": effort or (payload.get("reasoning") or {}).get("effort")}
        marker = route_marker(shown["model"], shown["effort"])
        signature = answer_signature(shown)

        def apply_route(payload, model, effort):
            payload["model"] = model
            if effort:
                reasoning = payload.get("reasoning")
                reasoning = dict(reasoning) if isinstance(reasoning, dict) else {}
                reasoning["effort"] = effort
                payload["reasoning"] = reasoning
            # Explicitly override any Fast preference inherited from the client,
            # including kill-switch, shadow, and retried fallback requests.
            payload["service_tier"] = "default"
            payload["stream"] = True  # the local caller edge requires streaming
            return payload

        apply_route(payload, model, effort)

        out_path = path if path.startswith("/v1") else "/v1" + path
        self._attempts = []
        status, out_kind, ctype, _quota_hit, _unwritten, _resets_at = self._forward(
            payload, out_path, stream_requested, debug, marker, model, signature)
        retried = False
        fallback = None

        finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        total_ms = int((time.time() - t0) * 1000)
        log_line({
            "at": finished_at,
            "policy_version": POLICY_VERSION,
            "route_probabilities": decision["probabilities"] if decision else None,
            "chosen_probability": decision["chosen_probability"] if decision else None,
            "jev_usage": jev_usage,
            "attempts": self._attempts,
            "gate": gate,
            "tier": tier,
            "conf": conf,
            "depth": depth,
            "model": model,
            "effort": effort,
            "speed": speed,
            "native": native_model,
            "dry": dry_reason,
            "retried": retried,
            "fallback": fallback,
            "jev_ms": jev_ms,
            "total_ms": total_ms,
            "status": status,
            "stream": stream_requested,
            "out": out_kind,
            "uctype": ctype,
            "n_items": signals.get("n_items"),
            "img": signals.get("has_image"),
            "step": step["step_type"],
            "errored": step["errored"],
            "digest_len": len(step["digest"]),
            "smart_gate": smart_gate,
            "failure_streak": failure_streak,
            "session": thread_key[:10] if thread_key else None,
            "repo": repo.as_signal() if repo.available else None,
            "stripped": stripped,
            "would": would,
            "task": task[:110],
        })
        update_last_route_status(model, effort, gate, finished_at)
        append_shadow_event(
            SHADOW_EVAL_PATH,
            build_turn_event(
                at=finished_at,
                turn_id=turn_id,
                session=session_tag,
                policy_version=POLICY_VERSION,
                jev_model=tier,
                jev_effort=depth,
                smart_model=smart_model,
                smart_effort=smart_effort,
                smart_gate=smart_route_gate,
                served_model=model,
                served_effort=effort,
                status=status,
                attempts=self._attempts,
                total_ms=total_ms,
                jev_ms=jev_ms,
                step_type=step["step_type"],
                dry_reason=dry_reason,
                fallback=fallback,
            ),
        )
        if thread_key:
            SESSION_STORE.put(thread_key, last_eval_id=turn_id)

    def _forward(self, payload, out_path, stream_requested, debug, marker, model, signature=None):
        """One relay attempt to the local caller edge, streamed straight back.

        Returns (status, out_kind, ctype, quota_hit, unwritten, resets_at).
        The last three fields are retained as false/None placeholders for call
        site and logging compatibility. This OpenAI-only router never retries a
        quota failure on a third-party model.
        """
        body = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection(*ROUTER, timeout=900)
        status = 0
        out_kind = ""
        ctype = ""
        attempt = {"model": model, "effort": (payload.get("reasoning") or {}).get("effort"),
                   "speed": payload.get("service_tier"), "status": None,
                   "terminal_type": None, "usage": None}
        self._attempts.append(attempt)
        markerer = None
        try:
            conn.request(
                "POST",
                f"/_codex-router/{caller_secret()}{out_path}",
                body=body,
                headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
            )
            resp = conn.getresponse()
            status = resp.status
            ctype = (resp.getheader("Content-Type") or "").strip()
            # The local caller edge sets NO Content-Type on SSE streams. For a
            # streaming request, a 200 response IS an SSE stream: force the
            # outgoing header, because the forwarder picks its parser from it
            # (text/event-stream → SSE relay, application/json → JSON parse).
            is_sse = ("text/event-stream" in ctype) or (status == 200 and stream_requested)
            out_kind = ""

            if is_sse and stream_requested:
                out_kind = "sse"
                cap = None
                if debug:
                    try:
                        cap = open(os.path.join(STATE, "jev-router-debug-stream.log"), "a", encoding="utf-8")
                        cap.write(f"\n===== {time.strftime('%H:%M:%S')} model={model} =====\n")
                    except OSError:
                        cap = None
                self.send_response(status)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                markerer = SummaryMarker(marker, signature)
                while True:
                    chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    if not chunk:
                        break
                    if cap is not None:
                        try:
                            cap.write(chunk.decode("utf-8", "replace"))
                            cap.flush()
                        except OSError:
                            cap = None
                    piece = markerer.feed(chunk).encode("utf-8")
                    if piece:
                        self.wfile.write(f"{len(piece):X}\r\n".encode("ascii") + piece + b"\r\n")
                        self.wfile.flush()
                piece = markerer.flush().encode("utf-8")
                if piece:
                    self.wfile.write(f"{len(piece):X}\r\n".encode("ascii") + piece + b"\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
                if cap is not None:
                    cap.close()
            else:
                out_kind = "json"
                data = resp.read()
                out_ctype = ctype or "application/json"
                head = data[:64].lstrip()
                # The caller edge always streams; rebuild a proper single JSON
                # object for non-stream callers (compactions, litellm's
                # non-stream provider path) instead of forwarding raw SSE bytes.
                if status == 200 and (head.startswith(b"event:") or head.startswith(b"data:")):
                    assembled = assemble_sse(data)
                    if assembled is not None:
                        attempt["usage"] = usage_counts(assembled.get("usage"))
                        response_status = assembled.get("status")
                        if response_status in ("completed", "incomplete", "failed"):
                            attempt["terminal_type"] = f"response.{response_status}"
                        headerer = SummaryMarker("", signature)
                        for item in assembled.get("output") or []:
                            headerer._sign_message_item(item)
                        data = json.dumps(assembled).encode("utf-8")
                        out_ctype = "application/json"
                self.send_response(status)
                self.send_header("Content-Type", out_ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            return status, out_kind, ctype, False, None, None
        finally:
            attempt["status"] = status
            if markerer is not None:
                attempt["usage"] = markerer.usage
                attempt["terminal_type"] = markerer.terminal_type
            conn.close()


CHECK_QUESTIONS = {
    "ready": {
        "type": "noul",
        "instructions": "Does the state field named probe have the exact value ready?",
    },
}


def read_bounded_response(resp, limit):
    """Read one local HTTP body without silently truncating it."""
    advertised = resp.getheader("Content-Length")
    if advertised:
        try:
            size = int(advertised)
        except (TypeError, ValueError):
            size = None
        if size is not None and size > limit:
            raise ValueError(f"response body too large ({size} > {limit} bytes)")

    raw = resp.read(limit + 1)
    if len(raw) > limit:
        raise ValueError(f"response body exceeds {limit} bytes")
    return raw


def installation_check(require_model=True):
    """Return bounded readiness checks without ever exposing credentials."""
    checks = []

    key = load_key()
    checks.append({
        "name": "typesafe_key",
        "ok": bool(key),
        "detail": "configured" if key else "missing TYPESAFE_API_KEY",
    })

    if key:
        started = time.time()
        try:
            answer = call_jev_routed(
                key,
                {"probe": "ready"},
                CHECK_QUESTIONS,
                timeout=6.0,
            )
            answers = answer.get("answers") if isinstance(answer, dict) else None
            ok = isinstance(answers, dict) and "ready" in answers
            checks.append({
                "name": "typesafe_api",
                "ok": ok,
                "detail": f"reachable ({int((time.time() - started) * 1000)} ms)"
                          if ok else "response missing typed answer",
            })
        except Exception as exc:
            checks.append({
                "name": "typesafe_api",
                "ok": False,
                "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
            })
    else:
        checks.append({"name": "typesafe_api", "ok": False, "detail": "skipped: key missing"})

    secret = ""
    try:
        secret = caller_secret()
        checks.append({
            "name": "caller_secret",
            "ok": bool(secret),
            "detail": "present" if secret else "file is empty",
        })
    except OSError:
        checks.append({
            "name": "caller_secret",
            "ok": False,
            "detail": f"missing {CALLER_SECRET_PATH}",
        })

    conn = http.client.HTTPConnection(*ROUTER, timeout=3)
    router_ok = False
    try:
        conn.request("GET", "/health", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        body = resp.read(2048)
        router_ok = resp.status == 200
        checks.append({
            "name": "codex_router",
            "ok": router_ok,
            "detail": f"http {resp.status}" + ("" if router_ok else f": {body[:160].decode('utf-8', 'replace')}"),
        })
    except Exception as exc:
        checks.append({
            "name": "codex_router",
            "ok": False,
            "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
        })
    finally:
        conn.close()

    if require_model:
        if router_ok and secret:
            conn = http.client.HTTPConnection(*ROUTER, timeout=3)
            try:
                conn.request(
                    "GET",
                    f"/_codex-router/{secret}/v1/models",
                    headers={"Accept": "application/json"},
                )
                resp = conn.getresponse()
                raw = read_bounded_response(resp, MODEL_CATALOG_MAX_BYTES)
                catalog = json.loads(raw.decode("utf-8")) if resp.status == 200 else {}
                rows = catalog.get("data") if isinstance(catalog, dict) else None
                ids = {
                    row.get("id")
                    for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
                } if isinstance(rows, list) else set()
                loaded = VIRTUAL_MODEL_SLUG in ids
                checks.append({
                    "name": "jev_model",
                    "ok": loaded,
                    "detail": "jev/auto loaded"
                              if loaded
                              else "jev/auto missing; run setup-local.sh or curate-models, then restart Codex Router",
                })
            except Exception as exc:
                checks.append({
                    "name": "jev_model",
                    "ok": False,
                    "detail": f"{type(exc).__name__}: {str(exc)[:180]}",
                })
            finally:
                conn.close()
        else:
            checks.append({
                "name": "jev_model",
                "ok": False,
                "detail": "skipped: Codex Router or caller secret not ready",
            })

    return {
        "ok": all(item["ok"] for item in checks),
        "service": "jev-router",
        "version": VERSION,
        "policy_version": POLICY_VERSION,
        "checks": checks,
    }


def print_installation_check(result):
    for item in result["checks"]:
        mark = "OK" if item["ok"] else "FAIL"
        print(f"[{mark:4}] {item['name']}: {item['detail']}")
    print("READY" if result["ok"] else "NOT READY")
    return 0 if result["ok"] else 1


def main(argv=None):
    argv = list(argv if argv is not None else __import__("sys").argv[1:])
    if argv == ["--check"]:
        return print_installation_check(installation_check(require_model=True))
    if argv == ["--check-core"]:
        return print_installation_check(installation_check(require_model=False))
    if argv:
        print("usage: python3 server/jev_server.py [--check|--check-core]", flush=True)
        return 2

    server = ThreadingHTTPServer(LISTEN, Handler)
    server.daemon_threads = True
    os.makedirs(STATE, exist_ok=True)
    for path in (LOG_PATH, SHADOW_EVAL_PATH, SESSION_PATH):
        try:
            if os.path.exists(path):
                os.chmod(path, 0o600)
        except OSError:
            pass
    print(f"[jev-router] ready on {LISTEN[0]}:{LISTEN[1]}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
