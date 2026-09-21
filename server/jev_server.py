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

Session-aware routing (v4): every request is classified as a meaningful user
turn, tool-step continuation, or other, but classification no longer implies a
new Jev judgement. A meaningful user turn establishes a model/effort Route
Lease. Tool loops, background calls and compaction continuations KEEP that
lease; repeated tool failures raise it locally. Jev is consulted again only
when a valid lease is missing or a meaningful user turn opens a new boundary.

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
native tiers (Luna, Terra, Sol, Astra). Terminal native quota failures are
carried across the generic-provider hop as a non-retryable Responses failure;
Jev never substitutes a third-party model.
"""
import codecs
import contextlib
import hashlib
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
from route_lease import (RouteLeaseLocks, apply_failure_escalation,
                         contains_compaction, human_turn_key, lease_fields,
                         read_lease, route_action)
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
ROUTE_LEASE_LOCKS = RouteLeaseLocks()
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
VERSION = "1.8"
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

# Codex can replay the exact same Responses request several times while handling
# a transient/terminal failure. A routing judgement is pure for that request, so
# paying TypeSafe again for every transport retry is waste. Cache only successful
# routing judgements, keyed by a digest of the original request bytes (never the
# request content itself), and coalesce concurrent identical calls.
# A judgement for an unanswered request stays cached until the request
# succeeds; this is only a safety cap to prevent unbounded growth.
ROUTE_CACHE_SAFETY_TTL_S = 600.0
ROUTE_CACHE_MAX_ENTRIES = 256
ROUTE_SINGLEFLIGHT_WAIT_S = 8.0

# Native ChatGPT usage exhaustion is special. If it crosses the generic jev
# provider boundary as HTTP 429, the outer Codex Router rewrites the body and
# Codex's HTTP transport spends its retry budget before it can classify the
# subscription error. For streaming turns we therefore carry terminal quota as
# a successful HTTP SSE envelope with a fatal Responses error code. Current
# Codex classifies insufficient_quota as terminal UsageLimitExceeded.
TERMINAL_QUOTA_TYPES = frozenset({"usage_limit_reached", "usage_not_included"})
TERMINAL_QUOTA_CODES = frozenset({
    "usage_limit_reached",
    "usage_not_included",
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
})
TERMINAL_QUOTA_HEADERS = frozenset({
    "workspace_owner_credits_depleted",
    "workspace_member_credits_depleted",
    "workspace_owner_usage_limit_reached",
    "workspace_member_usage_limit_reached",
})

# The events that close a Responses stream and repeat the response id it opened
# with. A relayed stream is rewritten onto that opening id.
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
_route_cache_lock = threading.Lock()
_route_cache = {}
_route_flights = {}


class ResponseCommittedError(Exception):
    """The downstream response has started, so no retry/second response is legal."""


def _bounded_timeout(deadline, cap):
    """Return a socket timeout capped by the remaining wall-clock budget."""
    if deadline is None:
        return cap
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("upstream deadline exceeded")
    return max(0.05, min(float(cap), remaining))


class _RouteFlight:
    def __init__(self):
        self.event = threading.Event()
        self.result = None
        self.error = None


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


def _route_cache_key(request_bytes):
    digest = hashlib.sha256()
    digest.update(POLICY_VERSION.encode("utf-8"))
    digest.update(b"\0")
    digest.update(request_bytes)
    return digest.hexdigest()


def _purge_route_cache(now):
    expired = [key for key, (until, _result) in _route_cache.items() if until <= now]
    for key in expired:
        _route_cache.pop(key, None)
    while len(_route_cache) > ROUTE_CACHE_MAX_ENTRIES:
        _route_cache.pop(next(iter(_route_cache)))


def call_jev_for_route(key, state, request_bytes, timeout=4.0):
    """One paid Jev route judgement per identical request within the short TTL.

    Returns (result, reuse), where reuse is miss, hit or coalesced. Failures are
    shared only with callers already waiting on the same in-flight judgement;
    they are never cached for later requests.
    """
    cache_key = _route_cache_key(request_bytes)
    now = time.monotonic()
    with _route_cache_lock:
        _purge_route_cache(now)
        cached = _route_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1], "hit"
        flight = _route_flights.get(cache_key)
        if flight is None:
            flight = _RouteFlight()
            _route_flights[cache_key] = flight
            owner = True
        else:
            owner = False

    if not owner:
        if not flight.event.wait(ROUTE_SINGLEFLIGHT_WAIT_S):
            # A wedged leader must not wedge the request forever. This bounded
            # escape is intentionally not cached; normal calls finish in <=4s.
            return call_jev_routed(key, state, timeout=timeout), "miss"
        if flight.error is not None:
            raise flight.error
        if flight.result is not None:
            return flight.result, "coalesced"
        return call_jev_routed(key, state, timeout=timeout), "miss"

    try:
        result = call_jev_routed(key, state, timeout=timeout)
        flight.result = result
        with _route_cache_lock:
            _route_cache[cache_key] = (time.monotonic() + ROUTE_CACHE_SAFETY_TTL_S, result)
            _purge_route_cache(time.monotonic())
        return result, "miss"
    except Exception as exc:
        flight.error = exc
        raise
    finally:
        with _route_cache_lock:
            _route_flights.pop(cache_key, None)
        flight.event.set()


def invalidate_route_cache(request_bytes):
    """Drop the cached judgement once the request has a successful response."""
    key = _route_cache_key(request_bytes)
    with _route_cache_lock:
        _route_cache.pop(key, None)


def _error_object(data):
    try:
        parsed = json.loads(data.decode("utf-8", "replace"))
    except ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    inner = parsed.get("error")
    return inner if isinstance(inner, dict) else parsed


def terminal_quota_error(status, headers, data):
    """Return a fatal Codex SSE error for native subscription exhaustion only."""
    if status != 429:
        return None

    inner = _error_object(data)
    error_type = str(inner.get("type") or "").strip().lower()
    code = str(inner.get("code") or "").strip().lower()
    reached = str(headers.get("x-codex-rate-limit-reached-type") or "").strip().lower()
    terminal = (
        error_type in TERMINAL_QUOTA_TYPES
        or code in TERMINAL_QUOTA_CODES
        or reached in TERMINAL_QUOTA_HEADERS
    )
    if not terminal:
        return None

    message = inner.get("message")
    if not isinstance(message, str) or not message.strip():
        message = "You have reached your Codex usage limit. Wait for the usage window to reset or check your ChatGPT plan."

    # usage_limit_reached is understood on Codex's HTTP error path but not by
    # its SSE response.failed parser. insufficient_quota is terminal on both
    # current Codex Desktop and CLI and maps to UsageLimitExceeded.
    fatal_code = "usage_not_included" if (
        error_type == "usage_not_included" or code == "usage_not_included"
    ) else "insufficient_quota"
    return {"code": fatal_code, "message": message.strip()}


def quota_failure_sse(error):
    event = {
        "type": "response.failed",
        "sequence_number": 0,
        "response": {
            "id": "resp_jev_quota",
            "object": "response",
            "created_at": int(time.time()),
            "status": "failed",
            "background": False,
            "error": error,
        },
    }
    return (
        "event: response.failed\n"
        + "data: "
        + json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        + "\n\n"
    ).encode("utf-8")


# The host's native redirect is deliberately all-or-nothing: it reroutes every
# native turn (including the concrete tier this router just selected) back to
# jev/auto. Until the caller edge has a scoped bypass, concrete forwards move
# that state file aside. The move is reference-counted so overlapping forwards
# do not restore it early; only the filesystem transitions are locked.
_NATIVE_REDIRECT_LOCK = threading.Lock()
_NATIVE_REDIRECT_NAME = "native-redirect.json"
_NATIVE_REDIRECT_HELD_NAME = "native-redirect.json.routing-held"
_NATIVE_REDIRECT_DEPTH = 0
_EXACT_NATIVE_ROUTE_ENV = "JEV_EXACT_NATIVE_ROUTE"
_EXACT_NATIVE_ROUTE_MARKER = "jev-exact-native-route.json"
_EXACT_NATIVE_ROUTE_CONDITION = b"if (!registeredRoute && requestedModel && !exactRouteProbe) {"
_EXACT_NATIVE_ROUTE_PROBE = b"const exactRouteProbe = exactRouteProbeRequested(request.headers);"
_EXACT_NATIVE_ROUTE_REDIRECT = b"const redirect = MODEL_BY_SLUG.get(readNativeRedirect());"
_exact_native_route_cache = None


def exact_native_route_supported():
    """Whether the supervised Node caller edge can bypass native redirect exactly.

    The supervisor opts in only after the guarded source patch has been loaded by
    a successful Codex Router restart. The arm marker binds that restart to the
    exact router.mjs bytes. If an external router update replaces the source,
    this check immediately falls back to legacy suppression instead of recursing.
    """
    global _exact_native_route_cache
    if os.environ.get(_EXACT_NATIVE_ROUTE_ENV) != "1" or not CODEX_ROUTER_DIR:
        return False

    path = os.path.join(CODEX_ROUTER_DIR, "src", "router.mjs")
    marker_path = os.path.join(STATE, _EXACT_NATIVE_ROUTE_MARKER)
    try:
        source_stat = os.stat(path)
        marker_stat = os.stat(marker_path)
    except OSError:
        return False

    cache_key = (
        path,
        source_stat.st_mtime_ns,
        source_stat.st_size,
        marker_path,
        marker_stat.st_mtime_ns,
        marker_stat.st_size,
    )
    if _exact_native_route_cache and _exact_native_route_cache[:6] == cache_key:
        return _exact_native_route_cache[6]

    try:
        with open(marker_path, encoding="utf-8") as fh:
            marker = json.load(fh)
        with open(path, "rb") as fh:
            data = fh.read()
    except (OSError, ValueError):
        supported = False
    else:
        marker_router = marker.get("router") if isinstance(marker, dict) else None
        same_router = (
            isinstance(marker_router, str)
            and os.path.normcase(os.path.realpath(marker_router))
            == os.path.normcase(os.path.realpath(path))
        )
        supported = (
            isinstance(marker, dict)
            and marker.get("version") == 1
            and same_router
            and marker.get("router_sha256") == hashlib.sha256(data).hexdigest()
            and _EXACT_NATIVE_ROUTE_CONDITION in data
            and _EXACT_NATIVE_ROUTE_PROBE in data
            and _EXACT_NATIVE_ROUTE_REDIRECT in data
        )
    _exact_native_route_cache = (*cache_key, supported)
    return supported


def _native_redirect_paths():
    return (
        os.path.join(STATE, _NATIVE_REDIRECT_NAME),
        os.path.join(STATE, _NATIVE_REDIRECT_HELD_NAME),
    )


def native_redirect_suppression_active():
    with _NATIVE_REDIRECT_LOCK:
        return _NATIVE_REDIRECT_DEPTH > 0


def held_redirect_model():
    """Return the temporarily-held redirect while a concrete forward is active."""
    _path, held = _native_redirect_paths()
    try:
        with open(held, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    model = value.get("model")
    return model.strip() if isinstance(model, str) and model.strip() else None


def recover_native_redirect():
    """Recover a redirect left aside by an interrupted previous process.

    If both files exist, the live path is newer/operator-owned and wins.
    """
    path, held = _native_redirect_paths()
    with _NATIVE_REDIRECT_LOCK:
        if _NATIVE_REDIRECT_DEPTH != 0:
            return False
        try:
            if os.path.exists(path):
                if os.path.exists(held):
                    os.remove(held)
                return False
            if os.path.exists(held):
                os.replace(held, path)
                return True
        except OSError:
            pass
    return False


@contextlib.contextmanager
def native_redirect_suppressed():
    """Move native-redirect.json aside for one or more concrete forwards.

    Restoration is atomic and never overwrites a redirect written by another
    actor while suppression was active.
    """
    global _NATIVE_REDIRECT_DEPTH
    path, held = _native_redirect_paths()
    with _NATIVE_REDIRECT_LOCK:
        if _NATIVE_REDIRECT_DEPTH == 0:
            try:
                # Crash recovery: a previous process may have died while the
                # redirect was held. A live path always wins over stale held
                # state because it may reflect a newer operator choice.
                if os.path.exists(path) and os.path.exists(held):
                    os.remove(held)
                elif not os.path.exists(path) and os.path.exists(held):
                    os.replace(held, path)
                if os.path.exists(path):
                    os.replace(path, held)
            except OSError:
                pass
        _NATIVE_REDIRECT_DEPTH += 1
    try:
        yield
    finally:
        with _NATIVE_REDIRECT_LOCK:
            _NATIVE_REDIRECT_DEPTH = max(0, _NATIVE_REDIRECT_DEPTH - 1)
            if _NATIVE_REDIRECT_DEPTH == 0:
                try:
                    if os.path.exists(path):
                        # A newer actor wrote redirect state while the forward
                        # was active. Do not clobber it with our saved copy.
                        if os.path.exists(held):
                            os.remove(held)
                    elif os.path.exists(held):
                        os.replace(held, path)
                except OSError:
                    pass


# Repair an interrupted suppression before the server starts answering status
# or forwarding requests.
recover_native_redirect()


@contextlib.contextmanager
def concrete_native_forward():
    """Prefer caller-edge exact routing; retain file suppression as safe fallback."""
    if exact_native_route_supported():
        yield
        return
    with native_redirect_suppressed():
        yield


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

# ---------------------------------------------------------------------------
# Circuit breaker: track per-model upstream failures so a rate-limited model
# stops being selected for a cooldown window instead of hammering it.
# ---------------------------------------------------------------------------
_BREAKER = {}            # model -> (fail_count, first_fail_ts, open_until, probe_in_flight)
_BREAKER_LOCK = threading.Lock()
_BREAKER_THRESHOLD = 2   # consecutive failures before opening
_BREAKER_OPEN_S = 60.0   # how long a model is skipped
RETRYABLE_UPSTREAM = frozenset({429, 500, 502, 503, 504})


def _breaker_unpack(entry):
    if entry is None:
        return 0, 0.0, 0.0, False
    if len(entry) == 3:  # tolerate state created by an older in-memory version
        count, first_ts, open_until = entry
        return count, first_ts, open_until, False
    return entry


def breaker_record(model, ok):
    """Record one real upstream attempt and close/re-open half-open probes."""
    now = time.time()
    with _BREAKER_LOCK:
        entry = _BREAKER.get(model)
        if ok:
            _BREAKER.pop(model, None)
            return True

        count, first_ts, open_until, probe_in_flight = _breaker_unpack(entry)
        if entry is None:
            count, first_ts = 1, now
        else:
            count += 1

        # A failed half-open probe immediately re-opens the circuit. Likewise,
        # a failure recorded after an expired open window must not silently
        # reset the model to closed.
        if probe_in_flight or (open_until and now >= open_until):
            open_until = now + _BREAKER_OPEN_S
            count = max(count, _BREAKER_THRESHOLD)
        elif count >= _BREAKER_THRESHOLD:
            open_until = now + _BREAKER_OPEN_S

        _BREAKER[model] = (count, first_ts or now, open_until, False)
        return open_until == 0.0


def breaker_available(model):
    """Claim this model if available; exactly one caller gets a half-open probe."""
    now = time.time()
    with _BREAKER_LOCK:
        entry = _BREAKER.get(model)
        if entry is None:
            return True
        count, first_ts, open_until, probe_in_flight = _breaker_unpack(entry)
        if open_until == 0.0:
            return True
        if now < open_until or probe_in_flight:
            return False
        _BREAKER[model] = (count, first_ts, open_until, True)
        return True


def breaker_release(model):
    """Release a claimed half-open probe without changing circuit health."""
    with _BREAKER_LOCK:
        entry = _BREAKER.get(model)
        if entry is None:
            return
        count, first_ts, open_until, probe_in_flight = _breaker_unpack(entry)
        if probe_in_flight:
            _BREAKER[model] = (count, first_ts, open_until, False)


def breaker_pick(preferred_model):
    """Claim preferred or the next available *higher* tier; never wrap downward."""
    if preferred_model not in TIERS:
        return preferred_model, False
    idx = TIERS.index(preferred_model)
    for candidate in TIERS[idx:]:
        if breaker_available(candidate):
            return candidate, candidate != preferred_model
    return None, True


def breaker_finish(model, status, quota_hit=False):
    """Finish a claimed circuit slot based only on model-health evidence."""
    if quota_hit:
        breaker_release(model)
    elif status == 200:
        breaker_record(model, True)
    elif status in RETRYABLE_UPSTREAM:
        breaker_record(model, False)
    else:
        # Request-specific 4xx/protocol rejections do not prove the physical
        # model is unhealthy, and must not strand a half-open probe.
        breaker_release(model)


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
    server_version = f"jev-router/{VERSION}"

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
        # Suppression temporarily moves the redirect file out of the normal
        # status path. Report the held state so the desktop badge does not
        # flicker OFF during every routed request.
        if native_redirect_suppression_active():
            held_model = held_redirect_model()
            if held_model:
                snapshot["redirect_model"] = held_model
                snapshot["auto"] = held_model == "jev/auto"
        snapshot["route"] = last_route_status() or None
        snapshot["policy_version"] = POLICY_VERSION
        snapshot["exact_native_route"] = exact_native_route_supported()
        return snapshot

    def _auto_control(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > CONTROL_MAX_BYTES:
            self.close_connection = True
            return self._json(413, {"error": {"message": "control body too large"}})
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            return self._json(400, {"error": {"message": "invalid json"}})
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return self._json(400, {"error": {"message": "enabled must be boolean"}})
        # Mutating native redirect while it is temporarily held would make an
        # OFF click look successful and then be undone when the forward exits.
        # Fail explicitly; the UI can retry after the in-flight turn completes.
        # Make the idle check and redirect mutation atomic against a new
        # suppression starting in another request. Holding this lock across the
        # control subprocess is acceptable: Auto toggles are rare, while routed
        # forwards only need the lock for their short file transition.
        busy = False
        with _NATIVE_REDIRECT_LOCK:
            if _NATIVE_REDIRECT_DEPTH > 0:
                busy = True
                result = None
            else:
                result = set_auto_enabled(STATE, CODEX_ROUTER_DIR, body["enabled"])
        if busy:
            payload = self._auto_status()
            payload["error"] = "routing request in flight; retry Auto toggle shortly"
            return self._json(503, payload)
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
            body = json.loads(raw.decode("utf-8-sig"))
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
        except (BrokenPipeError, ConnectionResetError, ResponseCommittedError):
            self.close_connection = True
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
            payload = json.loads(raw.decode("utf-8-sig"))
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
        thread_key = session_key(payload, cwd, self.headers)
        session = SESSION_STORE.get(thread_key)
        failure_streak = next_failure_streak(session, step)
        repo = REPO_PROFILER.snapshot(cwd)
        stream_requested = payload.get("stream") is True
        turn_id = new_turn_id()
        session_tag = thread_key[:16] if thread_key else None
        turn_key = human_turn_key(payload, task=task, session_key=thread_key)
        compacted = contains_compaction(payload)
        meaningful_user_turn = (
            step.get("step_type") == "user_turn"
            and (bool(task) or bool(signals.get("has_image")))
        )
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
        jev_cache = None
        decision = None
        jev_usage = None
        smart_gate = None
        breaker_blocked = False
        route_source = None
        lease_action = None
        lease_reason = None

        # Serialize only semantic route selection for this session. The lock is
        # released before the actual model call, so unrelated sessions and the
        # long upstream stream remain fully concurrent.
        with ROUTE_LEASE_LOCKS.hold(thread_key):
            # Another concurrent replay may have created the lease while this
            # request waited for the per-session decision lock.
            session = SESSION_STORE.get(thread_key)
            failure_streak = next_failure_streak(session, step)
            lease = read_lease(session, POLICY_VERSION)
            lease_action, lease_reason = route_action(
                step_type=step.get("step_type") or "other",
                meaningful_user_turn=meaningful_user_turn,
                turn_key=turn_key,
                lease=lease,
                compacted=compacted,
            )

            if os.path.exists(OFF_PATH):
                model, effort, speed, gate = ASTRA, None, "default", "off"
                route_source = "off"
                if thread_key:
                    SESSION_STORE.put(thread_key, failure_streak=failure_streak)
            elif lease_action == "KEEP" and lease is not None:
                model, effort, speed = lease.model, lease.effort, "default"
                model, effort, local_escalation = apply_failure_escalation(
                    model, effort, failure_streak
                )
                route_source = "lease_escalation" if local_escalation else "lease"
                smart_gate = local_escalation or "lease_keep"
                gate = f"lease:{lease_reason}"
                if local_escalation:
                    gate = f"{gate}+{local_escalation}"

                if thread_key:
                    SESSION_STORE.put(
                        thread_key,
                        failure_streak=failure_streak,
                        last_model=model,
                        last_effort=effort,
                        **lease_fields(
                            model,
                            effort,
                            turn_key=lease.turn_key or turn_key,
                            source=("local_escalation" if local_escalation else lease.source),
                            policy_version=POLICY_VERSION,
                        ),
                    )
            else:
                key = load_key()
                if key and (task or step.get("digest") or signals.get("has_image")):
                    jt0 = time.time()
                    state = jev_state(task, prev_assistant, signals, step)
                    state = enrich_jev_state(state, task, session, repo, failure_streak)
                    try:
                        result, jev_cache = call_jev_for_route(key, state, raw)
                        decision = decision_from_answers(result.get("answers"))
                        raw_usage = result.get("usage") or {}
                        if not isinstance(raw_usage, dict):
                            raw_usage = {}
                        jev_usage = (
                            {k: v for k, v in raw_usage.items()
                             if k in ("input_tokens", "output_tokens", "inputTokens", "outputTokens")
                             and isinstance(v, int) and not isinstance(v, bool) and v >= 0}
                            if jev_cache == "miss" else None
                        )
                        tier, depth, conf = (decision["model"], decision["effort"],
                                             decision["confidence"])
                        model, effort, speed, gate = route(tier, depth)
                        model, effort, smart_gate = apply_guardrails(
                            model, effort, task, step, session, failure_streak)
                        if smart_gate != "apply":
                            gate = f"{gate}+{smart_gate}"
                        route_source = "jev"
                    except Exception as exc:
                        model, effort, speed, gate = (
                            ASTRA, "medium", "default",
                            f"jev_error:{type(exc).__name__}"
                        )
                        route_source = "jev_error_fallback"
                    jev_ms = int((time.time() - jt0) * 1000)
                else:
                    model, effort, speed, gate = ASTRA, "medium", "default", "no_key_or_task"
                    route_source = "fallback"

                # Even a technical fallback becomes the continuity route for
                # this user turn. Otherwise every tool result after one Jev
                # outage would ask Jev again or accidentally resurrect the
                # previous task's lease.
                if thread_key and model in TIERS and effort in EFFORTS:
                    SESSION_STORE.put(
                        thread_key,
                        failure_streak=failure_streak,
                        last_model=model,
                        last_effort=effort,
                        **lease_fields(
                            model,
                            effort,
                            turn_key=turn_key,
                            source=route_source,
                            policy_version=POLICY_VERSION,
                        ),
                    )
                elif thread_key:
                    SESSION_STORE.put(thread_key, failure_streak=failure_streak)

        # Capture the production smart route before operational shadow/dry
        # overrides. The raw Jev route is populated only when this request
        # actually opened a semantic Jev decision.
        smart_model, smart_effort, smart_speed, smart_route_gate = (
            model, effort, speed, gate
        )

        would = None
        if os.path.exists(SHADOW_PATH):
            would = {"model": model, "effort": effort, "speed": speed, "gate": gate}
            model, effort, speed, gate = ASTRA, None, "default", "shadow(astra)"

        # Jev Auto is intentionally OpenAI-only. No quota or operational path
        # may substitute a third-party model.
        dry_reason = None
        native_model = model

        # Claim the circuit immediately before the actual forward, after shadow
        # and other operational overrides have chosen the model that will really
        # be served. This avoids reserving a half-open probe for a model that is
        # later replaced before any upstream attempt occurs.
        alt_model, breaker_rerouted = breaker_pick(model)
        if alt_model is None:
            breaker_blocked = True
            gate = f"{gate}+breaker_open"
        elif breaker_rerouted:
            model = alt_model
            gate = f"{gate}+breaker"

        # Display the model actually serving the request, including shadow,
        # breaker reroutes, and operational fallbacks.
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

        out_path = path if path.startswith("/v1") else "/v1" + path
        self._attempts = []
        # Hard wall-clock budget for the whole tier-escalation sequence.
        ESCALATION_DEADLINE = 75.0
        escalation_deadline = time.monotonic() + ESCALATION_DEADLINE
        attempt_model = model
        attempt_effort = effort
        status = 0
        out_kind = ctype = ""
        error_bytes = None
        escalated = False

        if stream_requested:
            # Tier-escalation loop: a failed model bumps up one tier and is
            # retried WITHOUT another Jev decision, until success, all usable
            # tiers are exhausted, or the global deadline is spent.
            if breaker_blocked:
                status = 503
                out_kind = "error"
                error_bytes = json.dumps({"error": {
                    "type": "server_error",
                    "message": "all eligible native model circuits are open",
                }}).encode("utf-8")
            while not breaker_blocked:
                if time.monotonic() >= escalation_deadline:
                    status = 504
                    out_kind = "error"
                    error_bytes = json.dumps({"error": {
                        "type": "server_error",
                        "message": "native model escalation deadline exceeded",
                    }}).encode("utf-8")
                    break

                apply_route(payload, attempt_model, attempt_effort)
                quota_hit = False
                with concrete_native_forward():
                    try:
                        status, out_kind, ctype, quota_hit, _u, _r, error_bytes = self._forward(
                            payload, out_path, stream_requested, debug, marker,
                            attempt_model, signature, deadline=escalation_deadline)
                    except (BrokenPipeError, ConnectionResetError):
                        breaker_release(attempt_model)
                        raise
                    except ResponseCommittedError:
                        breaker_record(attempt_model, False)
                        raise
                    except (http.client.HTTPException, ConnectionError, OSError) as exc:
                        status = 504 if time.monotonic() >= escalation_deadline else 502
                        if self._attempts:
                            self._attempts[-1]["status"] = status
                        error_bytes = json.dumps({"error": {"type": "server_error",
                            "message": f"router connection failed: {exc}"}}).encode("utf-8")

                # Terminal subscription exhaustion is carried to Codex as a
                # response.failed SSE. It is handled, not a healthy upstream
                # success and not a reason to probe another tier.
                breaker_finish(attempt_model, status, quota_hit)
                if status == 200:
                    break

                idx = TIERS.index(attempt_model) if attempt_model in TIERS else -1
                if (status in RETRYABLE_UPSTREAM and idx + 1 < len(TIERS)
                        and time.monotonic() < escalation_deadline):
                    next_model, skipped_open = breaker_pick(TIERS[idx + 1])
                    if next_model is not None:
                        attempt_model = next_model
                        escalated = True
                        if skipped_open and "+breaker" not in gate:
                            gate = f"{gate}+breaker"
                        continue
                break

            model = attempt_model
            effort = attempt_effort
            # All tiers failed before any downstream response started.
            if status != 200 and error_bytes is not None:
                self._write_error_response(status, error_bytes)
            if escalated and status == 200:
                gate = f"{gate}+escalated"
        else:
            if breaker_blocked:
                status = 503
                out_kind = "error"
                error_bytes = json.dumps({"error": {
                    "type": "server_error",
                    "message": "all eligible native model circuits are open",
                }}).encode("utf-8")
                self._write_error_response(status, error_bytes)
            else:
                apply_route(payload, model, effort)
                with concrete_native_forward():
                    try:
                        status, out_kind, ctype, quota_hit, _u, _r, error_bytes = self._forward(
                            payload, out_path, stream_requested, debug, marker, model, signature)
                    except (BrokenPipeError, ConnectionResetError):
                        breaker_release(model)
                        raise
                    except ResponseCommittedError:
                        breaker_record(model, False)
                        raise
                    except (http.client.HTTPException, ConnectionError, OSError):
                        breaker_record(model, False)
                        raise
                breaker_finish(model, status, quota_hit)

        retried = False
        fallback = None

        # Only a completed Responses terminal event proves the request really
        # finished. HTTP 200 alone may be response.failed, an interrupted SSE,
        # or the transport envelope used for terminal quota.
        final_attempt = self._attempts[-1] if self._attempts else {}
        request_completed = (
            status == 200
            and final_attempt.get("terminal_type") == "response.completed"
        )
        if request_completed:
            invalidate_route_cache(raw)
        total_ms = int((time.time() - t0) * 1000)
        finished_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        log_line({
            "at": finished_at,
            "policy_version": POLICY_VERSION,
            "route_probabilities": decision["probabilities"] if decision else None,
            "chosen_probability": decision["chosen_probability"] if decision else None,
            "jev_usage": jev_usage,
            "jev_cache": jev_cache,
            "route_source": route_source,
            "lease_action": lease_action,
            "lease_reason": lease_reason,
            "compacted": compacted,
            "turn": turn_key[:10] if turn_key else None,
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
                route_source=route_source,
                route_reason=lease_reason,
                jev_cache=jev_cache,
                dry_reason=dry_reason,
                fallback=fallback,
            ),
        )
        if thread_key:
            SESSION_STORE.put(thread_key, last_eval_id=turn_id)

    def _write_error_response(self, status, error_bytes):
        """Write a terminal JSON error for a streaming request that never got a stream."""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(error_bytes)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(error_bytes)

    def _forward(self, payload, out_path, stream_requested, debug, marker, model,
                 signature=None, deadline=None):
        """One relay attempt to the local caller edge, streamed straight back.

        Once downstream headers have been committed, failures are terminal for
        this client connection: callers must not escalate or write a second HTTP
        response. The deadline is monotonic and shared by all tier attempts.
        """
        body = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection(
            *ROUTER, timeout=_bounded_timeout(deadline, 30.0))
        status = 0
        out_kind = ""
        ctype = ""
        attempt = {"model": model, "effort": (payload.get("reasoning") or {}).get("effort"),
                   "speed": payload.get("service_tier"), "status": None,
                   "terminal_type": None, "usage": None}
        self._attempts.append(attempt)
        markerer = None
        cap = None
        response_started = False
        quota_hit = False
        status = 0
        error_bytes = None
        try:
            conn.request(
                "POST",
                f"/_codex-router/{caller_secret()}{out_path}",
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    # Codex Router already uses this authenticated-caller probe
                    # to mean "serve the requested route exactly". The local
                    # source hook extends that same meaning to native redirect,
                    # so a Jev-selected native tier cannot recurse to jev/auto.
                    "x-codex-router-exact-route": "1",
                },
            )
            resp = conn.getresponse()
            # Headers arrived: allow a longer window for the streamed body
            # (legitimately long generations), while a fully-dead read still
            # fails after 120s instead of hanging the slot for 300s+.
            if conn.sock is not None:
                conn.sock.settimeout(_bounded_timeout(deadline, 120.0))
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
                response_started = True
                markerer = SummaryMarker(marker, signature)
                while True:
                    if conn.sock is not None:
                        conn.sock.settimeout(_bounded_timeout(deadline, 120.0))
                    try:
                        chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                    except (BrokenPipeError, ConnectionResetError):
                        raise
                    except (http.client.HTTPException, OSError) as exc:
                        raise ResponseCommittedError(
                            f"upstream stream failed after response start: {exc}") from exc
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
                if markerer.terminal_type is None:
                    raise ResponseCommittedError(
                        "upstream SSE ended without a terminal Responses event")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                out_kind = "json"
                if conn.sock is not None:
                    conn.sock.settimeout(_bounded_timeout(deadline, 120.0))
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
                quota_error = terminal_quota_error(status, resp.headers, data)
                if quota_error is not None:
                    # Account/workspace exhaustion is terminal but not evidence
                    # that this physical tier is unhealthy. Streaming callers
                    # get the native-looking response.failed envelope below;
                    # non-stream callers keep the upstream HTTP error verbatim.
                    quota_hit = True
                if quota_error is not None and stream_requested:
                    # Terminal ChatGPT subscription/quota exhaustion: return a
                    # 200 SSE response.failed so Codex shows the native
                    # usage-limit message and treats it as terminal rather than
                    # burning its HTTP retry budget through the generic provider.
                    out_kind = "sse"
                    stream = quota_failure_sse(quota_error)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Content-Length", str(len(stream)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    response_started = True
                    self.wfile.write(stream)
                    self.wfile.flush()
                    attempt["terminal_type"] = "response.failed"
                    # Already handled for this client; preserve that this was
                    # terminal quota so breaker/cache health are not falsified.
                    quota_hit = True
                    status = 200
                    error_bytes = None
                else:
                    error_bytes = None
                    if status != 200 and stream_requested:
                        # Keep the native upstream JSON intact. Terminal quota
                        # was handled above; transient 429/5xx stays retryable
                        # and, if every tier fails, Codex receives the original
                        # error instead of a synthetic rate_limit_error.
                        out_kind = "error"
                        try:
                            parsed = json.loads(data.decode("utf-8", "replace"))
                        except (ValueError, UnicodeDecodeError):
                            parsed = None
                        if isinstance(parsed, dict):
                            error_bytes = data
                        else:
                            error_bytes = json.dumps({"error": {
                                "type": "server_error",
                                "message": "upstream returned a non-JSON error",
                            }}).encode("utf-8")
                    else:
                        # Ordinary non-stream / success: write straight through.
                        self.send_response(status)
                        self.send_header("Content-Type", out_ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
            return status, out_kind, ctype, quota_hit, None, None, error_bytes
        except (BrokenPipeError, ConnectionResetError):
            raise
        except ResponseCommittedError:
            raise
        except (http.client.HTTPException, OSError) as exc:
            if response_started:
                raise ResponseCommittedError(
                    f"upstream failed after downstream response start: {exc}") from exc
            raise
        finally:
            attempt["status"] = status
            if markerer is not None:
                attempt["usage"] = markerer.usage
                attempt["terminal_type"] = markerer.terminal_type
            if cap is not None:
                try:
                    cap.close()
                except OSError:
                    pass
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
                catalog = json.loads(raw.decode("utf-8-sig")) if resp.status == 200 else {}
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