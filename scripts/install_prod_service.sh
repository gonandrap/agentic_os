#!/usr/bin/env bash
# install_prod_service — render and install the Jarvis OS systemd --user services.
#
# One-time (idempotent) setup for production. Installs two units:
#   jarvis.service      — the orchestrator daemon (jarvis start --foreground)
#   jarvis-ui.service   — the web dashboard (jarvis ui), always-on
# Both get a runtime PATH that finds uv, claude, node, and the prod venv, then are
# enabled + started with auto-restart recovery.
#
# Env:
#   PRODUCTION_CODE   production root (default: ~/workspace/production)
set -euo pipefail

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PRODUCTION_CODE="${PRODUCTION_CODE:-$HOME/workspace/production}"
PROD_ROOT="$PRODUCTION_CODE"
PROD_DIR="$PROD_ROOT/jarvis_os"
PROD_CONFIG="$PROD_ROOT/config/catalog.json"
UNIT_DIR="$HOME/.config/systemd/user"

[ -x "$PROD_DIR/.venv/bin/jarvis" ] || {
  echo "production not deployed yet ($PROD_DIR/.venv/bin/jarvis missing) — run scripts/shipit.sh first" >&2
  exit 1; }

# Runtime PATH: prod venv first (so `jarvis` resolves to prod), then uv/claude/node, then base.
add() { case ":$RUNTIME_PATH:" in *":$1:"*) ;; *) RUNTIME_PATH="${RUNTIME_PATH:+$RUNTIME_PATH:}$1" ;; esac; }
RUNTIME_PATH=""
add "$PROD_DIR/.venv/bin"
for bin in uv claude node; do
  p="$(command -v "$bin" 2>/dev/null || true)"; [ -n "$p" ] && add "$(dirname "$p")"
done
for d in "$HOME/.local/bin" /usr/local/bin /usr/bin /bin; do add "$d"; done

# UI port from the prod catalog (default 8787).
UI_PORT=8787
if [ -f "$PROD_CONFIG" ]; then
  UI_PORT="$(python3 -c "import json;print(json.load(open('$PROD_CONFIG')).get('os',{}).get('ui',{}).get('port',8787))" 2>/dev/null || echo 8787)"
fi

render() {  # render <template> <unit-name>
  local tmpl="$REPO/deploy/$1" unit="$UNIT_DIR/$2"
  [ -f "$tmpl" ] || { echo "template not found: $tmpl" >&2; exit 1; }
  sed -e "s#@PROD_DIR@#$PROD_DIR#g" \
      -e "s#@PROD_ROOT@#$PROD_ROOT#g" \
      -e "s#@PROD_CONFIG@#$PROD_CONFIG#g" \
      -e "s#@PATH@#$RUNTIME_PATH#g" \
      -e "s#@UI_PORT@#$UI_PORT#g" \
      "$tmpl" > "$unit"
  echo "installed $unit"
}

mkdir -p "$UNIT_DIR"
render jarvis.service.template    jarvis.service
render jarvis-ui.service.template jarvis-ui.service
echo "PATH=$RUNTIME_PATH  UI_PORT=$UI_PORT"

systemctl --user daemon-reload
systemctl --user enable  jarvis.service jarvis-ui.service
systemctl --user restart jarvis.service jarvis-ui.service
sleep 2
systemctl --user --no-pager --lines=4 status jarvis.service jarvis-ui.service || true

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — services stop at logout. Enable with: sudo loginctl enable-linger $USER"
fi
