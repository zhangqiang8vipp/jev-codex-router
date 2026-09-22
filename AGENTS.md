# AGENTS.md — install, verify, and operate Jev Codex Router

This file is for coding agents installing or maintaining this repository on a
user's machine. Discover what you can locally. Never ask the user to paste a
secret into chat.

## What this project does

`jev/auto` is a local Codex routing runtime backed by Jev (TypeSafe System
One) for semantic REPLAN decisions. Jev chooses one of 20 model/effort pairs:

- Luna / Terra / Sol / Astra
- low / medium / high / xhigh / max

A meaningful user turn establishes a persisted Route Lease. Tool,
background and compaction continuations KEEP that route without another Jev
call. Repeated tool failures may raise the lease locally. The request is
executed once. See `VISION.md` for the session-aware runtime direction.

Production Shadow Eval records the raw Jev route, smart route, actually served
route, numeric usage/cache counters, latency, retries, terminal outcome and
next-turn tool success/error. It never replays a second model route.

## Hard rules

1. Never print, log, commit, or transmit the TypeSafe API key, Codex Router
   `caller-secret`, ChatGPT tokens, or raw credentials.
2. Keep the Jev server bound to `127.0.0.1`.
3. Do not hand-edit generated Codex Router artifacts. Use the current
   `model-router` / generic-provider / curation interfaces.
4. Treat `jev-router-live.jsonl` as private because it contains a short task
   excerpt. Shadow Eval is intentionally privacy-reduced but is still local
   telemetry; do not republish it without permission.
5. Do not dual-execute counterfactual routes in production. Tool calls can have
   side effects. Counterfactual quality is unknown.
6. TypeSafe probabilities/confidence are diagnostic evidence, not proof of
   correctness. Keep deterministic policy and exact calculations in code.
7. **Do not simulate picker clicks for Auto.** Codex owns its model/reasoning UI.
   Auto is implemented only through Codex Router `native-redirect=jev/auto`.
8. Auto OFF must restore the redirect that existed immediately before Auto was
   enabled. Never clear or overwrite a newer operator redirect.
9. Concrete native tiers selected by Jev must re-enter Codex Router with its
   authenticated `x-codex-router-exact-route: 1` probe. The managed install
   applies guarded caller-edge hooks so that exact probe bypasses only
   `native-redirect` for that request and hard native Codex account/workspace
   quota is restored as a canonical HTTP 429 after the local Jev provider hop.
   Keep file suppression / terminal-SSE handling only as compatibility
   fallbacks when those scoped hooks are unavailable.
10. **Do not call Jev merely because Codex emitted another Responses call.**
    A valid Route Lease must be reused for tool/background/compaction
    continuations. A meaningful new user turn or missing/invalid lease may
    REPLAN.
11. Compaction is a context lifecycle event, not a route boundary. It must not
    invalidate a valid Route Lease.
12. Repeated tool failures escalate locally (two → at least Sol/high; three →
    at least Astra/xhigh) without an extra Jev decision.

## Prerequisites

Common:

- Codex Desktop or CLI signed in with ChatGPT.
- Current Codex Router checkout.
- Python 3.11+.
- Node.js required by Codex Router.
- TypeSafe key stored in `~/.hermes/.env` or a file named by
  `JEV_ENV_FILE`. Ask for the file path if missing, never the value.
- .NET 8 SDK on Windows to publish the self-contained WPF Auto overlay.

Windows is first-class. Codex Router's current Windows entrypoint is
`model-router.ps1`; do not substitute old `bin/codex-router` commands.

## Preferred install

### Windows

From this repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-local.ps1 -RouterDir "C:\absolute\path\to\codex-router"
```

The script must complete all of these stages:

1. Codex Router status + doctor.
2. Shared ChatGPT session authorization.
3. `jev` generic provider on `http://127.0.0.1:4319/v1`, adapter
   `openai-responses`, private-loopback explicitly allowed.
4. `Jev Codex Router` hidden Scheduled Task plus one-minute heartbeat.
5. `Jev Codex Router Shadow Eval` daily Scheduled Task.
6. Generic provider live test and curation of `jev/auto` with
   `low,medium,high,xhigh,max`.
7. Codex Router signed routing enabled so native GPT requests reach the local
   router while ChatGPT authentication stays active.
8. `Jev Codex Auto Toggle` WPF Scheduled Task installed. It is anchored beside
   Codex's own reasoning control and exposes one Auto button only.
9. `py -3 server\jev_server.py --check` with all checks OK.

Windows background tasks are installed by
`server/install-service.ps1` and removed by
`server/uninstall-service.ps1`.

### macOS

```bash
bash setup-local.sh /absolute/path/to/codex-router
```

The macOS launchd flow remains supported.

## Manual Windows wiring

Use the Codex Router wrapper from its checkout:

```powershell
.\model-router.ps1 codex chatgpt-session enable

.\model-router.ps1 codex providers generic add jev --name "Jev Router" --base-url http://127.0.0.1:4319/v1 --adapter openai-responses --allow-private
```

If the provider exists, use `generic edit jev` with the same options.

Install the Jev service:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\server\install-service.ps1
```

Curate using Codex Router's current curation source:

```powershell
node C:\path\to\codex-router\src\curate-models.mjs jev --models auto --efforts low,medium,high,xhigh,max --apply
```

Then:

```powershell
py -3 server\jev_server.py --check
```

Do not hand-author `user-models.json` for normal installation.

## Verification

The full readiness check verifies:

- TypeSafe key is configured.
- TypeSafe API is reachable.
- protected Codex Router caller capability exists.
- Codex Router is healthy.
- authenticated router catalog actually contains `jev/auto`.

The health-only endpoint is not enough to claim end-to-end readiness.

Auto control readiness additionally requires `CODEX_ROUTER_DIR` to point to the
current Codex Router checkout. Managed installs should report
`exact_native_route: true`; `false` means the server is using the legacy
router-wide suppression fallback. Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:4319/control/status
Get-ScheduledTask -TaskName "Jev Codex Auto Toggle"
```

Do not instruct the user to select `jev/auto` manually after Windows setup.
They should keep using Codex's native model/reasoning UI and toggle Auto.

## Shadow Eval

Raw eval events:

```text
~/.codex/codex-router/jev-shadow-eval.jsonl
```

Each production turn records:

- route source/reason (Jev REPLAN, lease KEEP, local escalation, fallback);
- raw Jev model + effort when a Jev decision actually occurred;
- smart model + effort after local continuity/guardrails;
- actually served model + effort after operational fallback;
- Responses terminal outcome and HTTP status;
- upstream attempt count and retry count;
- numeric input/output/cached/cache-write/reasoning counters when available;
- Jev and total latency;
- hashed/bounded session identity;
- next-turn tool-result success/error when available.

It does **not** copy prompt text, tool arguments, command output, or response
bodies.

Manual rolling report:

```powershell
py -3 server\report_shadow_eval.py --days 7 --write
```

Outputs:

- `jev-shadow-eval-7d.txt`
- `jev-shadow-eval-7d.json`

The Windows Scheduled Task refreshes them daily at 03:15 and uses
`StartWhenAvailable`.

Interpret the report carefully:

- `observed_success_rate` is an operational proxy for the smart route that
  actually ran.
- raw Jev route cost may be estimated using the same observed token volume.
- raw Jev route quality is never estimated.
- the pair Pareto frontier is selection-biased because task mix differs by
  route. It identifies calibration candidates, not causal winners.

## Operations

Windows examples:

```powershell
Get-ScheduledTask -TaskName "Jev Codex Router"
Get-ScheduledTask -TaskName "Jev Codex Router Shadow Eval"
Get-Content "$HOME\.codex\codex-router\jev-router-live.jsonl" -Wait
Get-Content "$HOME\.codex\codex-router\jev-shadow-eval.jsonl" -Wait
Get-Content "$HOME\.codex\codex-router\jev-shadow-eval-7d.txt"
```

### Auto routing control

Auto ON invokes Codex Router's validated control surface to set
`native-redirect=jev/auto`. Auto OFF restores the immediately preceding
redirect. Native redirect is all-or-nothing for native GPT traffic reaching the
router, so background native turns are included while Auto is on.

The overlay must not read or write prompt text and must not read credentials.
Its only service calls are loopback `GET /control/status` and
`POST /control/auto` with a boolean.

Sentinel files are platform-independent in the router state directory:

- `jev-router.off`: skip Jev and use frontier fail-open.
- `jev-router.shadow`: legacy serve-Astra shadow mode.
- `jev-router.debug`: bounded debug capture.
- `jev-router.signature`: show route signature in assistant text.
- `jev-router.codex-dry`: manual native-quota fallback.

Production Shadow Eval is always passive and does not require
`jev-router.shadow`.

## Tuning policy

Do not add routing heuristics just because one 7-day aggregate looks cheaper.
Before changing `server/routing_policy.py` or `server/smart_context.py`:

1. inspect sample counts per pair;
2. inspect route-change transitions;
3. inspect tool-error and retry rates;
4. inspect cache-hit ratio and latency;
5. separate operational fallback/dry turns;
6. identify pairs on the observed Pareto frontier;
7. make one bounded policy change;
8. bump `POLICY_VERSION`;
9. collect a fresh window.

Keep raw Jev judgments in telemetry so thresholds and policy composition can be
reanalyzed without rerunning inference.

## Route Lease verification

Before changing routing behaviour, preserve these invariants:

- one meaningful user turn can create at most one semantic Jev decision after
  duplicate/concurrent replay coalescing;
- tool continuations reuse the current lease;
- compaction/background calls reuse the current lease;
- repeated failure escalation does not call Jev;
- a new meaningful user turn invalidates the prior semantic lease;
- a policy-version mismatch invalidates persisted leases;
- no raw prompt, tool output or absolute path is persisted in lease metadata.

Use the Shadow Eval report to inspect `route_sources`, `lease_reuse_turns`,
`jev_decision_turns`, `jev_error_turns`, `jev_cache_miss_turns` and
`jev_cache_reuse_turns`.

## Tests

Run:

```text
python -m compileall -q server poc
python -m unittest discover -s server -p "test_*.py" -v
```

On Windows also parse every `.ps1` file with PowerShell's language parser and
build `desktop/JevAutoToggle/JevAutoToggle.csproj` with .NET 8. CI contains a
`windows-syntax` job for both checks.

## Current known limitations

- Shadow Eval measures operational success, not semantic task correctness.
- Counterfactual quality requires a controlled eval dataset or safe replay, not
  live dual execution.
- ChatGPT credit figures are published-rate estimates, not observed account
  debits.
- Model-pair comparisons are observational because the router chooses which
  tasks each pair sees.
- Current upstream Codex Router does not make its exact-route probe bypass
  `native-redirect`, and its generic-provider error translation cannot preserve
  native ChatGPT usage-limit semantics across the Jev hop. The managed service
  therefore applies two guarded caller-edge hooks (exact native route + native
  quota pass-through) and arms them only after a successful router restart. If
  a future upstream update changes either source shape, Jev falls back to the
  compatibility paths instead of guessing at a rewrite.