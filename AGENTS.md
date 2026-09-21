# AGENTS.md — install, verify, and operate Jev Codex Router

This file is for coding agents installing or maintaining this repository on a
user's machine. Discover what you can locally. Never ask the user to paste a
secret into chat.

## What this project does

`jev/auto` is a local Codex model backed by Jev (TypeSafe System One). Jev
chooses one of 20 model/effort pairs:

- Luna / Terra / Sol / Astra
- low / medium / high / xhigh / max

Local smart guardrails may raise or hold that route after observed tool
failures or across short continuations. The request is executed once.

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

## Prerequisites

Common:

- Codex Desktop or CLI signed in with ChatGPT.
- Current Codex Router checkout.
- Python 3.11+.
- Node.js required by Codex Router.
- TypeSafe key stored in `~/.hermes/.env` or a file named by
  `JEV_ENV_FILE`. Ask for the file path if missing, never the value.

Windows is first-class. Codex Router's current Windows entrypoint is
`model-router.ps1`; do not substitute old `bin/codex-router` commands.

## Preferred install

### Windows

From this repository:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-local.ps1 \
  -RouterDir "C:\absolute\path\to\codex-router"
```

The script must complete all of these stages:

1. Codex Router status + doctor.
2. Shared ChatGPT session authorization.
3. `jev` generic provider on `http://127.0.0.1:4319/v1`, adapter
   `openai-responses`, private-loopback explicitly allowed.
4. `Jev Codex Router` hidden Scheduled Task plus one-minute heartbeat.
5. `Jev Codex Router Shadow Eval` daily Scheduled Task.
6. Generic provider live test.
7. Curation of `jev/auto` with
   `low,medium,high,xhigh,max` and apply/restart.
8. `py -3 server\jev_server.py --check` with all checks OK.

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

.\model-router.ps1 codex providers generic add jev \
  --name "Jev Router" \
  --base-url http://127.0.0.1:4319/v1 \
  --adapter openai-responses \
  --allow-private
```

If the provider exists, use `generic edit jev` with the same options.

Install the Jev service:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\server\install-service.ps1
```

Curate using Codex Router's current curation source:

```powershell
node C:\path\to\codex-router\src\curate-models.mjs jev \
  --models auto \
  --efforts low,medium,high,xhigh,max \
  --apply
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

## Shadow Eval

Raw eval events:

```text
~/.codex/codex-router/jev-shadow-eval.jsonl
```

Each production turn records:

- raw Jev model + effort;
- smart model + effort after local guardrails;
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

## Tests

Run:

```text
python -m compileall -q server poc
python -m unittest discover -s server -p "test_*.py" -v
```

On Windows also parse every `.ps1` file with PowerShell's language parser.
CI contains a `windows-syntax` job for this.

## Current known limitations

- Shadow Eval measures operational success, not semantic task correctness.
- Counterfactual quality requires a controlled eval dataset or safe replay, not
  live dual execution.
- ChatGPT credit figures are published-rate estimates, not observed account
  debits.
- Model-pair comparisons are observational because the router chooses which
  tasks each pair sees.
