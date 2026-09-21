# Jev Codex Router

[![ci](https://github.com/zhangqiang8vipp/jev-codex-router/actions/workflows/ci.yml/badge.svg)](https://github.com/zhangqiang8vipp/jev-codex-router/actions/workflows/ci.yml)

**Per-turn model routing for Codex, driven by [Jev](https://docs.typesafe.ai) (TypeSafe System One).**

Jev chooses a model and thinking effort together for each model call, including
continuations after tools. Every route uses standard speed. The objective is
sufficient capability for the next decision with no unnecessary quota consumption.

**Historical simulation: ≈ −60 % vs full Astra** on 237 turns under the old
policy. This is not measured Codex quota saved, nor evidence for the current
policy — protocol and limitations in [BACKTEST.md](BACKTEST.md).
Installing with an AI agent? Hand it [AGENTS.md](AGENTS.md).

This is not a fork of any router: it plugs into an existing local
**Codex Router** installation through its official extension points
(a *generic provider* + a *curated model*), so router updates never overwrite it.

**Fork it. Change the policy. Keep your own tandem.** MIT. No permission needed.
See [Fork and customize](#fork-and-customize--允许自己改) below.

## Auto toggle on Windows

Codex already owns the model picker and reasoning-effort control. This project
does **not** duplicate them. On Windows it adds one small WPF/UIAutomation
`Auto` pill next to Codex's native reasoning control.

- **Auto OFF**: Jev Auto is disabled. On a normal installation with no prior
  native redirect, Codex's own model + reasoning choices go straight through
  the native ChatGPT path.
- **Auto ON**: Codex Router's `native-redirect` is set to `jev/auto`.
  Every native GPT Responses call that reaches the router is dynamically
  classified by Jev and served by the selected model/effort pair.
- Turning Auto OFF restores the redirect that existed before Auto was enabled,
  if there was one. It never destroys a pre-existing operator redirect.
- The overlay does not click or rewrite Codex's model picker. It only calls the
  loopback Jev control endpoint, which delegates the actual redirect mutation to
  Codex Router's own `native-redirect` control path.

Because Codex Router's native redirect is deliberately all-or-nothing, **Auto
ON also applies to background native GPT turns that reach the router**, not only
the visible composer turn. The native picker remains visible while Auto is on,
but its model/effort choice takes effect again only after Auto is turned off.

The button is an independent .NET/WPF overlay anchored with Windows
UIAutomation; Codex Desktop itself is not patched.

## How it works

```
Codex native model + reasoning controls
                    │
                    ▼
          signed Codex Router (:4202)
                    │
          ┌─────────┴──────────┐
          │                    │
      Auto OFF              Auto ON
          │                    │
 native GPT path       native-redirect=jev/auto
          │                    │
          │             jev_server.py (:4319)
          │                    │
          │                   Jev
          │                    │
          │             smart guardrails
          │                    │
          │       Luna / Terra / Sol / Astra
          │                    │
          └──────────┬─────────┘
                     ▼
            ChatGPT backend (your plan)
```

- **Responses in, Responses out** — no format conversion; the SSE stream is
  relayed verbatim, so tool calls, reasoning and compaction behave natively.
- **Fail-open** — any Jev error keeps the turn alive (safe fallback route).
- **Kill switch** — a sentinel file routes without Jev, instantly.
- **Codex-dry tandem** — when native (ChatGPT) usage is exhausted (sentinel
  file, or an observed 429 / usage-limit response), the four-tier native set is replaced:
  GLM (`opencode-go/glm-5.3-flash`) for frontier-tier steps, deepseek
  (`opencode-go/deepseek-v4.1-flash`) for everything else. The failed call is
  retried on the tandem, at the thinking depth Jev decided, mapped onto the Go
  models' own ladder; a tandem call that comes back retryable is tried once on
  the sibling model before the turn is lost. The next successful native call
  clears an auto flip.
- **Decision log** — every routed turn is logged locally for calibration
  (`~/.codex/codex-router/jev-router-live.jsonl`), never published.

## Routing policy

The shared contract in `server/routing_policy.py` gives Jev 20 explicit pairs:
Luna, Terra, Sol or Astra × low, medium, high, xhigh or max thinking. Jev chooses
the pair in one Choice question using capability profiles, the current request,
recent assistant intent, the latest tool evidence, and bounded session/repository
signals. Every pair uses standard speed, overriding an incoming Fast setting,
including retries and bypass modes.

There is no preferred model, target distribution, keyword-to-model rule,
low-confidence fallback to Sol, mechanical-step exception, or compaction pin.
A valid decision is applied unchanged even when several pairs are close. Jev's
confidence and full choice distribution are logged separately; neither is a
measured probability that the selected model will successfully finish the task.

The model descriptions are capability priors, not calibrated success rates.
The policy must be evaluated on completed tasks, corrections, tokens and quota,
not on a desired share of Luna calls or artificially high confidence. Schema
checks and synthetic routing samples establish wiring, not equal-quality savings.
A missing/invalid Jev response or a provider error still uses the separately
logged technical fail-open route (Astra at medium); the manual kill switch and
native-quota exhaustion are operational bypasses, not Jev decisions.

### Smart continuity guardrails

The smart-router branch keeps Jev as the semantic chooser but adds bounded local
evidence and deterministic recovery rules:

- only a hashed thread key, previous model/effort, failure streak, project basename,
  dirty-file count and aggregate diff-line count are retained;
- source code, filenames and absolute paths are not sent to Jev;
- one failed tool call cannot immediately downgrade model or reasoning effort;
- two consecutive failed tool steps floor the next call at Sol + high;
- three floor it at Astra + xhigh, while max remains a Jev decision;
- a very short continuation can lower model and effort by at most one rung, while
  a successful mechanical tool continuation may still fall directly to Luna + low.

These are recovery/continuity floors, not keyword-based task classification.

### Codex-dry tandem — only while native usage is exhausted

The four-tier native set is the policy **unless** the ChatGPT usage window is exhausted
(manual sentinel file, or an automatic flip on a 429 / usage-limit response,
which also retries the failed call on the tandem). While dry:

| Native tier | Dry substitute |
|---|---|
| `gpt-6-astra` (frontier) | `opencode-go/glm-5.3-flash` |
| `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna` | `opencode-go/deepseek-v4.1-flash` |

An automatic flip lasts until the instant the edge announced for the window
reset, so the first call after the quota returns is served by the native four-tier set
again; when a refusal announces no instant it falls back to a 30-minute
re-probe, and a week is the ceiling on anything a refusal claims. It is cleared
by the first successful native call, and the manual sentinel file is never
auto-cleared.

Two details keep the substitute transparent. The decided depth travels with the
call, mapped onto the Go ladder — `low` stays `low`, `medium` and `high` become
`high`, `xhigh` or above become `max` — because those models declare three rungs
where the native models expose five, and the API forwarder clamps the value once more
onto the route's own ladder. And a tandem call that comes back retryable
(429/5xx) is tried once on the sibling model: opencode Go meters the two Go
models against separate allowances and reports a spent one the same way it
reports a transient outage. If both refuse, the caller receives that refusal
rather than a request nobody answers.

A third detail keeps the relay legal for the Responses consumer in front of it.
A dry turn crosses the local edge, which encodes response ids, so the terminal
event of the stream the relay receives repeats the id under a fresh encoding.
Read as-is, that is a completion that renamed its own response, and the consumer
replaces the finished turn with an `invalid_responses_stream` error; the relay
therefore rewrites the terminal id onto the one `response.created` announced.
Native turns are untouched — their ids already match.

## Measuring what it served

The router logs one JSON line per decision (`~/.codex/codex-router/jev-router-live.jsonl`).
`server/report_routing.py` turns that log into the routing/savings report — the
table a third party can reproduce on their own machine:

```bash
python3 server/report_routing.py --days 7          # text tables (default window)
python3 server/report_routing.py --days 30 --json  # machine-readable
```

It prints the served model distribution (luna/terra/sol/astra, plus the Codex-dry
tandem when it took over: turns + %), the share of turns served by the cheapest
tier, the share of turns held below the confidence gate, the gates encountered,
median latency (end-to-end and Jev's own decision time), and an estimate of the
real cost against two counterfactuals — every turn on `gpt-6-astra`, and every
turn on `gpt-5.6-sol`.

New log entries record a versioned decision and each upstream attempt's model,
effort, standard speed, terminal event and token usage when the provider reports
it. Only numeric usage counters are retained. Unknown usage is not counted as
zero, retries are retained, and reasoning tokens are already included in output.
The report estimates standard ChatGPT credits from these observed tokens against
all-Sol and all-Astra counterfactuals. External fallback calls are excluded from
that comparison. These are published-rate estimates, not observed account debits;
counterfactual token volumes and task quality have not been experimentally measured.

Historical entries without usage keep a separate fixed-volume API-rate proxy.
Their logged Fast speed retains its surcharge instead of being repriced by the
new policy. The old backtest is clearly labelled as a simulation. Current replay
scripts share the live decision contract and reject a cache from another policy.

## Shadow Eval (production)

Every real Codex call now produces a second, privacy-reduced evaluation event in
`~/.codex/codex-router/jev-shadow-eval.jsonl`. The request is still executed
**once**. Shadow Eval records three distinct routes:

- **Jev route** — the raw typed Choice result before local guardrails;
- **smart route** — the production route after continuity/recovery guardrails;
- **served route** — the model that actually answered after quota fallback/retry.

It also records the Responses outcome, per-attempt token usage, cached-input
tokens, cache-write counters when reported, end-to-end/Jev latency, retry count,
and the next tool-result success/error when one arrives. Prompt text, tool
arguments, command output and response bodies are not copied into the Shadow
Eval log.

Generate the rolling report manually:

```powershell
py -3 server\report_shadow_eval.py --days 7 --write
```

or on macOS/Linux:

```bash
python3 server/report_shadow_eval.py --days 7 --write
```

The report writes `jev-shadow-eval-7d.txt` and `jev-shadow-eval-7d.json` in
the router state directory. Windows installation registers a daily 03:15
Scheduled Task with `StartWhenAvailable`, so after the first week the rolling
7-day report is maintained automatically.

The Pareto frontier is intentionally **observational**. Quality is an
operational proxy from the route that really ran (Responses completion plus
observed tool-result errors). The raw Jev route is used only for a
same-token-volume cost counterfactual; the report never invents quality for a
model/effort pair that was not executed. Pair comparisons are selection-biased
because different tasks are routed to different pairs, so use the frontier to
find candidates for calibration, not as causal proof.

## Ask surface (`POST /ask`)

The server also answers typed questions directly, for local callers that bring
their own question set. The `jev-browser-choice` skill is the first one: it
turns an in-app-browser accessibility dump into one Jev `choice` question and
acts only on the validated element index, so the page never enters the model's
context.

```sh
curl -s http://127.0.0.1:4319/ask -X POST -H 'Content-Type: application/json' \
  -d '{"state":{"goal":"open the docs"},"questions":{"next":{"type":"choice","instructions":"Which element advances the goal?","criteria":{"e5":"link Documentation"}}}}'
# → {"model":"jev-1.13.0","answers":{"next":{...}},"usage":{...},"ms":612}
```

Validation is the whole contract: a JSON-serialisable `state` under 120k chars,
at most 40 questions, each a `noul`, `choice` or `score` with its instructions
and criteria. The caller's state is never logged. `502` surfaces an upstream Jev
failure — `402` means the TypeSafe account is out of credits — and `503` means
no key is configured.

## Repository layout

```
BACKTEST.md  Savings backtest — protocol, tables, limitations (the "proof")
AGENTS.md    Autonomous install & operations playbook (for AI agents)
poc/         Tiering POC, shadow replay, and the backtest tool
server/      The live server + service install (this is what runs)
desktop/     Windows WPF Auto toggle anchored beside Codex native controls
hook/        Explored alternative (LiteLLM callback tap) — kept for reference
```

## Quickstart

### Windows (recommended for this fork)

Prerequisites: Windows 10/11, Codex Desktop or CLI signed in with ChatGPT, and
PowerShell in FullLanguage mode. The bootstrap automatically installs a managed
[Codex Router](https://github.com/duolahypercho/codex-router) checkout under
`%LOCALAPPDATA%\codex-router` when none is found. It can also install missing
Git, Node.js, Python 3 and .NET 8 SDK through `winget` after asking first. If
the TypeSafe/Jev key is not already stored, it prompts for it locally with
hidden input.

Recommended install/update command:

```powershell
irm https://raw.githubusercontent.com/zhangqiang8vipp/jev-codex-router/main/install.ps1 | iex
```

This is the PowerShell equivalent of `curl ... | sh`: it downloads the latest
`main` source into `%LOCALAPPDATA%\JevCodexRouter\source`, finds or
automatically installs the local Codex Router checkout, installs/updates the Jev
service and Auto overlay, and runs the readiness checks. Re-running the same
command upgrades the tool. The base Codex Router is first installed in its
valid idle mode (no provider, credential discovery disabled). The Jev setup
then explicitly enables credential discovery before authorizing the existing
local Codex ChatGPT session for signed routing. No external upstream provider
is selected during that bootstrap transition.

If Codex Router is in a non-standard folder, set it before the same command:

```powershell
$env:CODEX_ROUTER_DIR = "C:\absolute\path\to\codex-router"
irm https://raw.githubusercontent.com/zhangqiang8vipp/jev-codex-router/main/install.ps1 | iex
```

For users who prefer not to pipe remote code directly into PowerShell:

```powershell
$p = Join-Path $env:TEMP "jev-install.ps1"
irm https://raw.githubusercontent.com/zhangqiang8vipp/jev-codex-router/main/install.ps1 -OutFile $p
powershell.exe -NoProfile -ExecutionPolicy Bypass -File $p
```

The lower-level `setup-local.ps1` remains available for development checkouts.
The Windows setup is idempotent. It verifies Codex Router and the shared ChatGPT
session, creates/updates the loopback `jev` generic Responses provider,
installs Jev as a hidden per-user Scheduled Task, registers the daily rolling
Shadow Eval task, discovers and curates `jev/auto`, enables Codex Router's
signed ChatGPT transport, builds/installs the small `Jev Codex Auto Toggle`
WPF overlay, and runs the full readiness check.

A successful install ends with `READY`. Fully quit and reopen Codex Desktop
once. **Do not select Jev Codex Router manually.** Keep using Codex's own model
and reasoning controls; click the small **Auto** pill beside the reasoning
control when you want dynamic routing.

Useful Windows checks:

```powershell
py -3 server\jev_server.py --check
Get-ScheduledTask -TaskName "Jev Codex Router"
Get-ScheduledTask -TaskName "Jev Codex Router Shadow Eval"
Get-ScheduledTask -TaskName "Jev Codex Auto Toggle"
Invoke-RestMethod http://127.0.0.1:4319/control/status
Get-Content "$HOME\.codex\codex-router\jev-shadow-eval-7d.txt"
```

To remove this project's background tasks after bootstrap installation:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\JevCodexRouter\source\server\uninstall-service.ps1"
```

### macOS

The existing macOS flow remains available:

```bash
bash setup-local.sh /absolute/path/to/codex-router
```

Both platforms use the same `jev_server.py`, routing policy, Shadow Eval log,
and report format.

## Operations

### Windows

| Action | Command |
|---|---|
| Full readiness | `py -3 server\jev_server.py --check` |
| Watch live routing log | `Get-Content "$HOME\.codex\codex-router\jev-router-live.jsonl" -Wait` |
| Watch Shadow Eval log | `Get-Content "$HOME\.codex\codex-router\jev-shadow-eval.jsonl" -Wait` |
| Generate rolling 7-day eval now | `py -3 server\report_shadow_eval.py --days 7 --write` |
| Read latest 7-day report | `Get-Content "$HOME\.codex\codex-router\jev-shadow-eval-7d.txt"` |
| Service task status | `Get-ScheduledTask -TaskName "Jev Codex Router"` |
| Eval task status | `Get-ScheduledTask -TaskName "Jev Codex Router Shadow Eval"` |
| Auto toggle task status | `Get-ScheduledTask -TaskName "Jev Codex Auto Toggle"` |
| Auto status | `Invoke-RestMethod http://127.0.0.1:4319/control/status` |
| Restart Jev task | `Stop-ScheduledTask -TaskName "Jev Codex Router"; Start-ScheduledTask -TaskName "Jev Codex Router"` |
| Uninstall Jev tasks | `.\server\uninstall-service.ps1` |

The existing sentinel files are platform-independent and live under the router
state directory. On Windows, for example:

```powershell
$state = Join-Path $HOME ".codex\codex-router"
New-Item (Join-Path $state "jev-router.signature") -ItemType File -Force | Out-Null
New-Item (Join-Path $state "jev-router.off") -ItemType File -Force | Out-Null
Remove-Item (Join-Path $state "jev-router.off") -ErrorAction SilentlyContinue
```

### macOS/Linux

The existing `tail -f`, `touch`, launchd/watchdog and
`bin/model-router codex ...` operations remain supported. The data and report
files are the same names under `~/.codex/codex-router/`.

## Notes & quirks

- The router's local edge requires `stream: true` — the server always forces it.
- The edge returns SSE with **no Content-Type header**; the server re-emits
  `text/event-stream` because the API forwarder picks its parser from it
  (otherwise it tries to JSON-parse the stream and fails with
  `invalid_responses_response`).
- The shared ChatGPT session authorization has a validity window; re-run
  `chatgpt-session enable` if native routing stops after a while.
- Code comments are in French for now (author's working language) — PRs welcome.

## Security

- **No secrets in this repository.** The server reads `TYPESAFE_API_KEY` from an
  env file or the process environment; everything else stays on your machine.
- The server binds `127.0.0.1` only, talks to your local Codex Router only, and
  never logs prompt content beyond a short task excerpt used for calibration.
- The Auto control endpoint is loopback-only and accepts one boolean. The overlay
  never receives the TypeSafe key or Codex Router caller secret.
- Local decision logs and replay data are git-ignored by default.

## Status

The router now includes production Shadow Eval plus a Windows-native Auto
toggle that leaves Codex's own model/reasoning UI intact. Auto is implemented
with Codex Router native redirect, not simulated picker clicks. The 7-day
Pareto report remains observational: it measures the route that actually ran
and does not claim counterfactual model quality.

## Fork and customize / 允许自己改

This tree is MIT. Fork it, strip it, or replace the policy. You do not need to
ask. Keep the original copyright notice in copies of the Software.

Suggested local edit points:

| Want | File |
|---|---|
| Change model + effort choices | `server/routing_policy.py` |
| Change session/repo guardrails | `server/smart_context.py` |
| Bump the logged policy id | `POLICY_VERSION` in `server/routing_policy.py` |
| Swap Codex-dry substitutes | tandem tables in `server/jev_server.py` |
| Recalibrate after changes | `python3 server/report_routing.py --days 7` |

Do not reuse a replay cache built under another `POLICY_VERSION`.

Upstream origin: [0xNatoshi/jev-codex-router](https://github.com/0xNatoshi/jev-codex-router).

## License

MIT
