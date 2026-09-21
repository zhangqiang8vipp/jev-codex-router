# Operations runbook

`jev_server.py` listens on `127.0.0.1:4319` and receives Responses requests for
the `jev/auto` model. For each turn it asks Jev for a route
(tier + thinking depth), applies the routing policy, and relays the request to
the Codex Router's local caller edge, which serves native GPT models from the
shared ChatGPT session.

## One-command local setup

### Windows

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup-local.ps1 -RouterDir "C:\absolute\path\to\codex-router"
```

The Windows installer registers three per-user Scheduled Tasks:

- `Jev Codex Router`: hidden background service, starts at logon and has a
  one-minute heartbeat with `IgnoreNew`.
- `Jev Codex Router Shadow Eval`: daily 03:15 rolling 7-day report with
  `StartWhenAvailable`.
- `Jev Codex Auto Toggle`: a small WPF/UIAutomation overlay anchored beside
  Codex's native reasoning control.

It then registers the loopback generic Responses provider, discovers/curates
`jev/auto`, enables Codex Router signed routing, and leaves Codex's native
model + reasoning picker as the manual UI. The Auto button only changes Codex
Router's `native-redirect`: OFF restores the previous redirect (normally
native ChatGPT), ON sends native GPT calls to `jev/auto`.

### macOS

```bash
bash setup-local.sh /absolute/path/to/codex-router
```

## Lifecycle

### Windows

| Action | Command |
|---|---|
| Full readiness | `py -3 server\jev_server.py --check` |
| Shadow report now | `py -3 server\report_shadow_eval.py --days 7 --write` |
| Service status | `Get-ScheduledTask -TaskName "Jev Codex Router"` |
| Eval status | `Get-ScheduledTask -TaskName "Jev Codex Router Shadow Eval"` |
| Auto toggle status | `Get-ScheduledTask -TaskName "Jev Codex Auto Toggle"` |
| Auto router status | `Invoke-RestMethod http://127.0.0.1:4319/control/status` |
| Restart service | `Stop-ScheduledTask -TaskName "Jev Codex Router"; Start-ScheduledTask -TaskName "Jev Codex Router"` |
| Uninstall all Jev tasks | `.\server\uninstall-service.ps1` |

### macOS

| Action | Command |
|---|---|
| Decision log | `tail -f ~/.codex/codex-router/jev-router-live.jsonl` |
| Readiness check | `python3 server/jev_server.py --check` |
| Install launchd | `bash server/install-service.sh` |
| Service status | `launchctl print gui/$(id -u)/com.thibaultsaintjean.jev-router` |

## Auto toggle behavior

The overlay does not change Codex's picker and does not simulate clicks.

- Auto OFF: Jev Auto is off. If there was no pre-existing Codex Router native
  redirect, the native model and reasoning effort selected in Codex apply.
- Auto ON: Codex Router sets `native-redirect=jev/auto`; Jev chooses the
  model/effort pair independently on each Responses call.
- Auto OFF after that restores the native redirect that existed immediately
  before Auto was enabled.
- If some other operator changes the native redirect while Auto is on, disabling
  Auto will not overwrite that newer choice.
- Native redirect is router-wide for native GPT traffic. Background native GPT
  turns that reach the router are also redirected while Auto is on.

The local control API is:

```powershell
Invoke-RestMethod http://127.0.0.1:4319/control/status

Invoke-RestMethod http://127.0.0.1:4319/control/auto -Method Post -ContentType "application/json" -Body (@{ enabled = $true } | ConvertTo-Json -Compress)
```

## Key setup

Both platforms default to `~/.hermes/.env`. The secret stays in that file;
Windows Scheduled Tasks receive only its path, never the key value.

Windows:

```powershell
New-Item -ItemType Directory -Force (Join-Path $HOME ".hermes") | Out-Null
Set-Content -Path (Join-Path $HOME ".hermes\.env") -Value "TYPESAFE_API_KEY=YOUR_KEY"
```

macOS:

```bash
mkdir -p ~/.hermes
printf '%s\n' 'TYPESAFE_API_KEY=YOUR_KEY' > ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

To use another file, set `JEV_ENV_FILE=/path/to/env` when running
`server/install-service.sh`; the installer persists only that path in the
launchd plist, never the key itself.

The Windows Auto overlay also needs the .NET 8 SDK during installation; it is
published self-contained after setup.

Run `py -3 server\jev_server.py --check` on Windows or
`python3 server/jev_server.py --check` on macOS at any time. It checks the key,
TypeSafe API reachability, the protected Codex Router caller secret, the router
health, and whether `jev/auto` is actually loaded in the authenticated model
catalog, without printing either credential. During first-time bootstrap,
`server/install-service.sh` uses `--check-core` before the model is curated.

## After a Codex Router update

Provider and model state live outside the router checkout, so updates should not
touch them. Verify anyway:

1. `./bin/model-router codex providers generic list` → should show `SHOW jev`.
2. `cat ~/.codex/codex-router/model-picker.json` → `jev/auto` under `visible`.
3. `curl -s http://127.0.0.1:4319/health` → `{"ok": true...}`.
4. `python3 server/jev_server.py --check` → all five checks should be `OK`, including `jev_model`.
5. If needed: `./bin/model-router codex refresh-catalog` and `./bin/control service restart`, then fully restart Codex.

## Troubleshooting

- **Auto button does not appear**: verify
  `Get-ScheduledTask -TaskName "Jev Codex Auto Toggle"`, then verify
  `Invoke-RestMethod http://127.0.0.1:4319/control/status`. If both work,
  Codex may have changed the UIAutomation name/layout of its reasoning control;
  the overlay intentionally hides rather than attaching to an uncertain target.
- **Auto is green but a native picker choice seems ignored**: this is expected.
  While Auto is ON, native GPT traffic is redirected to `jev/auto`. Turn Auto
  OFF before using Codex's native model/reasoning selection manually.
- **Auto says unavailable**: the Jev service needs the Codex Router checkout
  path in `CODEX_ROUTER_DIR`; rerun `setup-local.ps1 -RouterDir ...`.

- **`invalid_responses_response` in router logs / “unavailable right now” in
  Codex**: the API forwarder parsed our reply as JSON instead of SSE. The server
  forces `Content-Type: text/event-stream` on streamed replies for exactly this
  reason; make sure you run the current `jev_server.py`.
- **401 / route refused by the edge**: the shared ChatGPT session expired —
  re-run `./bin/model-router codex chatgpt-session enable`.
- **Every turn routes to astra**: check the decision log (`gate` field) — the
  kill switch may be on, or the TypeSafe key is unreadable (look for
  `jev_error` / `no_key_or_task` gates).
- **Model missing from the picker**: re-run `./bin/curate-models jev --models auto --efforts low,medium,high,xhigh,max --apply`, then `./bin/control service restart` and fully restart Codex.

### Model visible but rejected by ChatGPT

`The 'jev/auto' model is not supported when using Codex with a ChatGPT account`
can mean the model is selected while the OpenAI provider still points directly
at OpenAI. Listing a model in a catalog, or declaring `[model_providers.jev]`,
does not associate an existing task with that provider.

1. Inspect `./bin/model-router codex status`: check `model_provider` and the redacted
   `openai_base_url`, not just whether the service is running.
2. Verify the main router has the enabled `jev` generic provider and the
   `jev/auto` entry in `user-models.json`. A direct Codex provider declaration
   is a separate configuration. Reload the router after restoring its routes;
   its startup regenerates the gateway configuration from source.
3. Preserve a user-owned `model_catalog_json`. With the built-in `openai`
   provider, Codex supports a user-level `openai_base_url` pointing to the
   router's authenticated loopback Responses entry. Use Codex's
   `config/value/write` API for this setting; resolve the caller capability
   locally from its protected file, never print it or put it in command
   arguments. Leave other provider definitions and model defaults intact.
4. Verify a small request through **4202 → Jev 4319 → native 4202**, then through
   an ephemeral Codex invocation reading the saved configuration. Checking
   Jev's health alone does not exercise the client transport.
5. Fully quit and reopen Codex Desktop on the host OS so it reloads the
   configuration before retrying the existing task.

The built-in OpenAI transport override was verified with Codex
`0.155.0-alpha.9.2`; no switch to a different provider or catalog was needed.
See the [official configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced)
for the distinction between the built-in endpoint override and custom providers.

## Shadow Eval operations

The production evaluator writes
`~/.codex/codex-router/jev-shadow-eval.jsonl` and never copies prompt text,
tool arguments, command output, or response bodies. It records the raw Jev
choice, the smart route, the actually served route, completion outcome, numeric
usage/cache counters, latency, retries, and next-turn tool success/error.

Windows keeps these rolling outputs current automatically:

- `jev-shadow-eval-7d.txt`
- `jev-shadow-eval-7d.json`

The report's quality value is an operational proxy for the route that actually
ran. The raw Jev route receives a counterfactual cost estimate only; it is never
assigned invented counterfactual quality.

## Design notes

- The edge emits SSE with no Content-Type; we always re-emit
  `text/event-stream; charset=utf-8` on stream relays.
- `stream: true` is forced upstream (the edge requires it); non-stream callers
  get the final response object assembled from the SSE stream.
- One Jev decision per request (≈0.6 s, included in total latency). Tool-loop
  continuations are re-classified on the same last-user text; they land on the
  same tier in practice, and everything is logged for tuning.
