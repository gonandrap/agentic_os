#!/usr/bin/env bash
# install_prod_service — render and install the Jarvis OS systemd --user service.
#
# One-time (idempotent) setup for the production daemon. Reads the template at
# deploy/jarvis.service.template, resolves absolute paths + a runtime PATH (so the
# service can find `uv`, `claude`, and `node`), installs the unit under
# ~/.config/systemd/user/, then enables + starts it with auto-restart recovery.
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
UNIT="$UNIT_DIR/jarvis.service"
TEMPLATE="$REPO/deploy/jarvis.service.template"

[ -f "$TEMPLATE" ] || { echo "template not found: $TEMPLATE" >&2; exit 1; }
[ -x "$PROD_DIR/.venv/bin/jarvis" ] || {
  echo "production not deployed yet ($PROD_DIR/.venv/bin/jarvis missing) — run scripts/shipit.sh first" >&2
  exit 1; }

# Build a PATH that includes uv, claude, and node (claude is a node CLI).
add() { case ":$RUNTIME_PATH:" in *":$1:"*) ;; *) RUNTIME_PATH="${RUNTIME_PATH:+$RUNTIME_PATH:}$1" ;; esac; }
RUNTIME_PATH=""
for bin in uv claude node; do
  p="$(command -v "$bin" 2>/dev/null || true)"; [ -n "$p" ] && add "$(dirname "$p")"
done
for d in "$HOME/.local/bin" /usr/local/bin /usr/bin /bin; do add "$d"; done

mkdir -p "$UNIT_DIR"
sed -e "s#@PROD_DIR@#$PROD_DIR#g" \
    -e "s#@PROD_ROOT@#$PROD_ROOT#g" \
    -e "s#@PROD_CONFIG@#$PROD_CONFIG#g" \
    -e "s#@PATH@#$RUNTIME_PATH#g" \
    "$TEMPLATE" > "$UNIT"
echo "installed $UNIT (PATH=$RUNTIME_PATH)"

systemctl --user daemon-reload
systemctl --user enable jarvis.service
systemctl --user restart jarvis.service
sleep 2
systemctl --user --no-pager --lines=10 status jarvis.service || true

# Warn if the service won't survive logout/reboot.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — service stops at logout. Enable with: sudo loginctl enable-linger $USER"
fi
