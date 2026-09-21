# Operations runbook

`jev_server.py` listens on `127.0.0.1:4319` and receives Responses requests for
the `jev/auto` model. For each turn it asks Jev for a route
(tier + thinking depth), applies the routing policy, and relays the request to
the Codex Router's local caller edge, which serves native GPT models from the
shared ChatGPT session.

## One-command local setup

Prefer the repository-level installer when wiring a fresh Mac:

```bash
bash setup-local.sh /absolute/path/to/codex-router
```

It uses the current Codex Router CLI (`bin/model-router codex ...`) to register
the loopback generic Responses provider, starts this launchd service, discovers
the virtual model from `/v1/models`, curates `jev/auto` with the five supported
effort levels, restarts the router, and runs the full five-part readiness check.

## Lifecycle

| Action | Command |
|---|---|
| Decision log | `tail -f ~/.codex/codex-router/jev-router-live.jsonl` |
| Kill switch (no Jev → frontier) | `touch ~/.codex/codex-router/jev-router.off` / `rm` to re-enable |
| Readiness check | `python3 server/jev_server.py --check` |
| Install the launchd service | `bash server/install-service.sh` (in your own Terminal) |
| Service status | `launchctl print gui/$(id -u)/com.thibaultsaintjean.jev-router` |
| Service restart | `launchctl kickstart -k gui/$(id -u)/com.thibaultsaintjean.jev-router` |
| Watchdog (no launchd) | `server/watchdog.sh`, e.g. cron every 5 min |
| Hide the model | `./bin/control picker set jev/auto hide` (router checkout) |
| Disable the provider | `./bin/model-router codex providers generic disable jev` |
| Revoke native sharing | `./bin/model-router codex chatgpt-session disable` |

## Key setup

For a persistent macOS service, store the TypeSafe key in a file because launchd
does not inherit an interactive shell's `TYPESAFE_API_KEY`:

```bash
mkdir -p ~/.hermes
printf '%s\n' 'TYPESAFE_API_KEY=YOUR_KEY' > ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

To use another file, set `JEV_ENV_FILE=/path/to/env` when running
`server/install-service.sh`; the installer persists only that path in the
launchd plist, never the key itself.

Run `python3 server/jev_server.py --check` at any time. It checks the key,
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
5. Quit and reopen Codex on the host Mac to reload the configuration before
   retrying the existing task from desktop or mobile.

The built-in OpenAI transport override was verified with Codex
`0.155.0-alpha.9.2`; no switch to a different provider or catalog was needed.
See the [official configuration documentation](https://learn.chatgpt.com/docs/config-file/config-advanced)
for the distinction between the built-in endpoint override and custom providers.

## Design notes

- The edge emits SSE with no Content-Type; we always re-emit
  `text/event-stream; charset=utf-8` on stream relays.
- `stream: true` is forced upstream (the edge requires it); non-stream callers
  get the final response object assembled from the SSE stream.
- One Jev decision per request (≈0.6 s, included in total latency). Tool-loop
  continuations are re-classified on the same last-user text; they land on the
  same tier in practice, and everything is logged for tuning.
