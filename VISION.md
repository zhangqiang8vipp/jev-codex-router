# Vision — from Jev router to a session-aware Codex runtime

## Product thesis

This project should not end as “Jev chooses a model for every Codex call.”

The target is a **local, session-aware execution runtime for Codex** that keeps
track of what the coding task is doing, preserves continuity across long-running
tool loops and compaction, compiles only the context that the next phase needs,
and allocates model + reasoning capacity only when execution state actually
changes.

The runtime manages three coupled decisions:

1. **When to reconsider execution strategy.**
2. **What the next model call actually needs to see.**
3. **Which model + reasoning effort should execute that work.**

The default is continuity:

> **KEEP is the default. REPLAN requires evidence.**

A raw Responses request is not automatically a new task, and a tool result is
not automatically a new routing decision.

## User-facing goal

The UI should stay simple:

- **Auto OFF** — native Codex behaviour.
- **Auto ON** — adaptive session runtime.

The internal machinery may become sophisticated, but the user should not have
to manage leases, phases, compaction state or routing providers manually.

An advanced status view may eventually expose only useful diagnostics such as:

```text
phase       debugging
route       Sol / high
route action KEEP
context     126k -> 38k
reason      active tool loop
replans     2
compactions 1
continuity  healthy
```

## Core runtime model

```text
                    Codex Session
                         |
                         v
                Session State Engine
                         |
                 Boundary Detector
                    /         \
                 KEEP        REPLAN
                   |            |
                   |      Execution Planner
                   |         /        \
                   |  Context Lease  Route Lease
                   |         \        /
                   +----------\------/ 
                              v
                    Execution Directive
                              |
                      Reliability Plane
                              |
                              v
                  Luna / Terra / Sol / Astra
                              |
                         Codex tools
                              |
                              +---- outcome ----+
                                               |
                                      Session State Engine
```

The long-term state engine should distinguish:

- **Durable task ledger** — goal, hard constraints, accepted decisions,
  unresolved blockers and evidence references that must survive compaction.
- **Working state** — current phase, active files/entities, tool chain, current
  hypothesis, current error and verification state.
- **Context generation** — the current Codex context/compaction generation.

Memory is not the same thing as context. A fact can remain durable without being
included in every model prompt.

## The first production invariant: Route Lease

Before building a context compiler, the router must stop treating every
`/responses` call as a fresh semantic decision.

Route Lease v1 deliberately stays conservative:

```text
meaningful new user turn
        |
        v
     ask Jev once
        |
        v
 save model + effort lease
        |
        +---- tool continuation ----> KEEP
        |
        +---- background/compaction -> KEEP
        |
        +---- first tool failure ----> KEEP
        |
        +---- repeated failures -----> local deterministic escalation
        |
        +---- next meaningful user turn -> REPLAN
```

Important rules:

- A tool call/result loop reuses the physical route.
- Compaction does **not** invalidate the route lease.
- Repeated failures may raise the lease locally without another Jev call.
- A duplicate/replayed copy of the same user turn reuses the existing semantic
  decision.
- A service restart can recover the lease from bounded persisted session state.
- A policy-version change invalidates old leases.
- If no valid lease exists, the system may ask Jev once to recover continuity.

This first step changes **when** Jev is consulted, not how Jev ranks the model
pairs.

## Context safety comes after continuity

Context pruning is higher risk than model routing. Choosing the wrong model can
cost latency or quality for a turn; removing a hard constraint can silently
corrupt the entire task.

Context work therefore rolls out in this order:

1. observe context pressure and compaction;
2. maintain a small provenance-aware durable ledger;
3. compute a context plan in shadow mode;
4. remove only exact duplicates and obviously stale tool noise;
5. add dependency-aware eviction;
6. add rehydration from repository/session evidence;
7. jointly optimize context + route only after the separate systems are proven.

Codex remains the transport/session source, but Codex compaction is not assumed
to be a complete semantic memory.

## What we borrow instead of reinventing

### vLLM Semantic Router / SAAR

Reference:
<https://vllm.ai/blog/2026-06-02-session-aware-agentic-routing>

Borrow:

- router-owned session continuity;
- hard locks around tool loops;
- reset/safe boundaries;
- switch economics and cache-awareness later;
- replayable “why keep / why switch” traces.

Do **not** copy the entire gateway architecture. Our transport remains Codex
Router + native ChatGPT session; we are borrowing the session-control model.

### OpenClaw

References:

- <https://github.com/openclaw/openclaw/blob/main/docs/concepts/compaction.md>
- <https://github.com/openclaw/openclaw/blob/main/docs/concepts/memory.md>

Borrow:

- explicit compaction lifecycle;
- preserving tool-call/tool-result integrity across compaction boundaries;
- pre-compaction durable-memory flush as a safety pattern;
- the separation between active context and durable memory.

Adaptation for this project: deterministic facts (user constraints, test
results, route state, commits) should be written without an LLM whenever
possible. A model should only summarize genuinely semantic evidence.

### Hermes Agent

References:

- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/features/memory.md>
- <https://github.com/NousResearch/hermes-agent/blob/main/website/docs/user-guide/sessions.md>

Borrow:

- durable session storage;
- SQLite + FTS5 retrieval of real historical messages without an LLM call;
- separation of small persistent memory from on-demand session retrieval.

Adaptation for this project: avoid duplicating Codex into a second canonical
chat system. Persist structured runtime state and privacy-bounded evidence
indexes, not another authoritative conversation transcript.

### Switchcraft

Reference:
<https://www.microsoft.com/en-us/research/publication/switchcraft-ai-model-router-for-agentic-tool-calling/>

Borrow later:

- a small local classifier can make routing decisions under a tight latency
  budget;
- the router itself does not need to be a large generative model.

Long-term path:

```text
deterministic KEEP / boundary rules
          |
          v
small local router
     /         \
confident     uncertain
   |             |
 route           Jev
```

Jev then becomes a hard-case judge/teacher rather than the mandatory online
classifier.

### RouteLLM

Reference:
<https://arxiv.org/abs/2406.18665>

Borrow later:

- learn routing from preference/outcome data rather than hand-growing keyword
  rules;
- evaluate the router on quality/cost trade-offs, not desired model shares.

### ACON

Reference:
<https://www.microsoft.com/en-us/research/publication/acon-optimizing-context-compression-for-long-horizon-llm-agents/>

Borrow for the context phase:

- optimize compression using full-context-success / compressed-context-failure
  evidence;
- distill a capable compressor into a smaller model only after the policy is
  proven.

## Which layers should call a model API?

Most control-plane work should **not** call a generative model.

| Layer | Model API? | Preferred technique |
|---|---:|---|
| HTTP/SSE, quota, breaker, deadlines | No | deterministic code |
| session identity and event capture | No | hashes + structured state |
| tool-loop continuity | No | state machine / lease |
| compaction detection | No | request/event shape |
| Route Lease KEEP decision | No | deterministic rules |
| repeated-failure escalation | No | deterministic floors |
| session retrieval | No | SQLite/FTS5, git, ripgrep/symbol index |
| durable user constraints | Usually no | provenance-aware structured facts |
| semantic phase change | Eventually, sometimes | local classifier; Jev fallback |
| new route selection | Only at a real boundary | local router / Jev |
| semantic context summarization | Occasionally | small/local auxiliary model |
| coding execution | Yes | native Luna/Terra/Sol/Astra |
| online evaluation | No extra execution | passive telemetry |
| offline learning/judging | Optional | controlled eval only |

The desired steady-state is therefore not “Codex calls + one router call for
every Codex call.” It is closer to:

```text
30 Codex execution calls
 1-3 semantic route decisions
 0-few auxiliary memory/context calls
```

depending on how many real execution boundaries the task crosses.

## Roadmap

### Phase 1 — Route Lease

Status: current implementation target.

- stable privacy-preserving session/turn identity;
- one Jev decision per meaningful user turn in v1;
- tool/background/compaction continuity;
- local failure escalation;
- semantic single-flight for concurrent duplicate decisions;
- persisted lease recovery;
- telemetry for Jev decisions vs lease reuse.

### Phase 2 — Boundary Engine

- distinguish “continue” from a genuinely new task;
- detect verification/replan/implementation boundaries without fragile keyword
  routing;
- only reopen the planner at safe boundaries.

### Phase 3 — Durable task ledger + compaction sentinel

- provenance-aware constraints/decisions/evidence;
- compaction generation;
- continuity reconciliation after compaction.

### Phase 4 — Context compiler in shadow mode

- PINNED / ACTIVE / SUMMARY / OMIT plan;
- no production pruning yet;
- measure what would have been removed and what later became necessary.

### Phase 5 — Conservative context execution

- safe eviction;
- FTS/repository rehydration;
- context correctness metrics.

### Phase 6 — Local routing and joint optimization

- train/calibrate a small local router from real route/outcome data;
- Jev as fallback/teacher;
- switch economics, cache locality and context pressure;
- optimize task quality first, then cost/latency/tokens.

## Evaluation contract

The runtime is not successful because Jev calls decrease.

A change is acceptable only if it keeps or improves task quality while reducing
unnecessary work.

Primary measurements should include:

- completed/failed Responses outcome;
- observed next-tool error;
- retries and corrections;
- route KEEP / REPLAN / local escalation;
- Jev decision count and paid Jev call count;
- model switches;
- total and cached Codex tokens;
- latency;
- compaction count and post-compaction recovery;
- later, context kept/omitted/rehydrated.

The long-term gate is:

```text
first:  quality >= baseline
then:   router calls, model cost, latency, context tokens and switching decrease
```

This repository should remain able to fail open safely even when every
“intelligent” layer is uncertain or unavailable.
