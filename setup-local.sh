#!/bin/zsh
# One-command local setup for Jev Codex Router + Codex Desktop on macOS.
#
# Usage:
#   bash setup-local.sh /path/to/codex-router
# or:
#   CODEX_ROUTER_DIR=/path/to/codex-router bash setup-local.sh
#
# Prerequisite:
#   ~/.hermes/.env contains TYPESAFE_API_KEY=...
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(command -v python3 || true)"

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This setup script currently targets macOS/Codex Desktop."
  exit 2
fi
[ -n "$PYTHON" ] || { echo "python3 is required."; exit 2; }

resolve_router_dir() {
  if [ -n "${1:-}" ]; then
    printf '%s\n' "$1"
    return
  fi
  if [ -n "${CODEX_ROUTER_DIR:-}" ]; then
    printf '%s\n' "$CODEX_ROUTER_DIR"
    return
  fi
  for candidate in \
    "$HERE/../codex-router" \
    "$HOME/Documents/Github/codex-router" \
    "$HOME/Github/codex-router" \
    "$HOME/Developer/codex-router"
  do
    if [ -x "$candidate/bin/model-router" ]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  return 1
}

ROUTER_DIR="$(resolve_router_dir "${1:-}")" || {
  echo "Codex Router checkout not found."
  echo "Pass it explicitly:"
  echo "  bash setup-local.sh /path/to/codex-router"
  exit 2
}
ROUTER_DIR="$(cd "$ROUTER_DIR" && pwd)"
MODEL_ROUTER="$ROUTER_DIR/bin/model-router"
CURATE="$ROUTER_DIR/bin/curate-models"
CONTROL="$ROUTER_DIR/bin/control"

[ -x "$MODEL_ROUTER" ] || { echo "Missing $MODEL_ROUTER"; exit 2; }
[ -x "$CURATE" ] || { echo "Missing $CURATE"; exit 2; }
[ -x "$CONTROL" ] || { echo "Missing $CONTROL"; exit 2; }

DEFAULT_KEY_FILE="$HOME/.hermes/.env"
KEY_FILE="${JEV_ENV_FILE:-$DEFAULT_KEY_FILE}"
if [ ! -r "$KEY_FILE" ]; then
  echo "TypeSafe/Jev key file is missing: $KEY_FILE"
  echo ""
  echo "Create it first:"
  echo "  mkdir -p ~/.hermes"
  echo "  printf '%s\\n' 'TYPESAFE_API_KEY=YOUR_KEY' > ~/.hermes/.env"
  echo "  chmod 600 ~/.hermes/.env"
  exit 2
fi
chmod 600 "$KEY_FILE" 2>/dev/null || true

echo "== 1/8  Codex Router installation =="
if ! "$MODEL_ROUTER" codex status >/dev/null 2>&1; then
  echo "Codex Router is not installed/running for Codex."
  echo "Install its local plane first from:"
  echo "  $ROUTER_DIR"
  echo "For a minimal install:"
  echo "  ./install.sh --target codex --no-provider --no-discovery --no-tray"
  exit 2
fi

echo "== 2/8  ChatGPT/Codex session =="
if command -v codex >/dev/null 2>&1; then
  if ! codex login status >/dev/null 2>&1; then
    echo "Codex is not logged in. Run 'codex login' once, then rerun this script."
    exit 2
  fi
fi
"$MODEL_ROUTER" codex chatgpt-session enable

echo "== 3/8  Jev generic provider =="
if "$MODEL_ROUTER" codex providers generic show jev --json >/dev/null 2>&1; then
  "$MODEL_ROUTER" codex providers generic edit jev \
    --name "Jev Router" \
    --base-url http://127.0.0.1:4319/v1 \
    --adapter openai-responses \
    --allow-private
else
  "$MODEL_ROUTER" codex providers generic add jev \
    --name "Jev Router" \
    --base-url http://127.0.0.1:4319/v1 \
    --adapter openai-responses \
    --allow-private
fi

echo "== 4/8  Jev local service =="
SERVICE_STATE_DIR="${CODEX_ROUTER_STATE_DIR:-$HOME/.codex/codex-router}"
if [ -n "${JEV_ENV_FILE:-}" ]; then
  CODEX_ROUTER_DIR="$ROUTER_DIR" \
  CODEX_ROUTER_STATE_DIR="$SERVICE_STATE_DIR" \
  JEV_ENV_FILE="$JEV_ENV_FILE" \
    bash "$HERE/server/install-service.sh"
else
  CODEX_ROUTER_DIR="$ROUTER_DIR" \
  CODEX_ROUTER_STATE_DIR="$SERVICE_STATE_DIR" \
    bash "$HERE/server/install-service.sh"
fi

echo "== 5/8  Provider discovery =="
"$MODEL_ROUTER" codex providers generic test jev

echo "== 6/8  Curate jev/auto =="
MODEL_ROUTER_TARGET=codex "$CURATE" jev \
  --models auto \
  --efforts low,medium,high,xhigh,max \
  --apply

# A newly curated slug is present in the rebuilt catalog immediately, but the
# running router loads route tables at process start. Restart explicitly so the
# picker and the serving process cannot disagree.
echo "== 7/8  Reload route table =="
"$CONTROL" service restart

echo "== 8/8  Full readiness =="
"$PYTHON" "$HERE/server/jev_server.py" --check

echo ""
echo "Jev Codex Router is ready."
echo "Fully quit and reopen Codex Desktop, then select: Jev Codex Router (jev/auto)"
echo ""
echo "Useful commands:"
echo "  python3 $HERE/server/jev_server.py --check"
echo "  tail -f ~/.codex/codex-router/jev-router-live.jsonl"
echo "  cd $ROUTER_DIR && ./bin/model-router codex doctor"