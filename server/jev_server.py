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