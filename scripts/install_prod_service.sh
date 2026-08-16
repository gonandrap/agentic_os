#!/usr/bin/env bash
# install_prod_service — render and install the Jarvis OS systemd --user services.
#
# One-time (idempotent) setup for production. Installs two units:
#   jarvis.service      — the orchestrator daemon (jarvis start --foreground)
#   jarvis-ui.service   — the web dashboard (jarvis ui), always-on
# Both get a runtime PATH that finds uv, claude, node, gh, and the prod venv, then are
# enabled + started with auto-restart recovery.
#
# Env:
#   PRODUCTION_CODE   production root (default: ~/workspace/production)
#
# Flags:
#   --dry-run           render the units and print the plan; touch no systemd state
#   --unit-dir <dir>    where the units go (default: ~/.config/systemd/user)
set -euo pipefail

DRY_RUN=0
UNIT_DIR="$HOME/.config/systemd/user"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --unit-dir) UNIT_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PRODUCTION_CODE="${PRODUCTION_CODE:-$HOME/workspace/production}"
PROD_ROOT="$PRODUCTION_CODE"
PROD_DIR="$PROD_ROOT/jarvis_os"
PROD_CONFIG="$PROD_ROOT/config/catalog.json"

[ -x "$PROD_DIR/.venv/bin/jarvis" ] || {
  echo "production not deployed yet ($PROD_DIR/.venv/bin/jarvis missing) — run scripts/shipit.sh first" >&2
  exit 1; }

# Runtime PATH: prod venv first (so `jarvis` resolves to prod), then uv/claude/node/gh,
# then base. This PATH is the daemon's AND every worker it spawns; a binary missing from
# it is a feature silently switched off, not an error anyone sees. `gh` is the one that
# bit us (#41, #90): PR-merge polling and `jarvis bug report` both need it.
add() { case ":$RUNTIME_PATH:" in *":$1:"*) ;; *) RUNTIME_PATH="${RUNTIME_PATH:+$RUNTIME_PATH:}$1" ;; esac; }
RUNTIME_PATH=""
add "$PROD_DIR/.venv/bin"
for bin in uv claude node gh; do
  p="$(command -v "$bin" 2>/dev/null || true)"; [ -n "$p" ] && add "$(dirname "$p")"
done
# MIRRORS bugreport.GH_SEARCH_DIRS — keep the two in step. /snap/bin matters because the
# probe above runs under whatever PATH invoked this script, and when that is a Jarvis
# worker (which this often is) it has the same hole we are patching.
for d in /snap/bin "$HOME/.local/bin" /usr/local/bin /usr/bin /bin; do add "$d"; done

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

# Say it out loud when the rendered PATH cannot reach `gh`. Silence here is what made
# issue #90 take a release to notice: the units install fine, and the only symptom is a
# feature that quietly never happens.
if ( PATH="$RUNTIME_PATH"; hash -r 2>/dev/null; command -v gh >/dev/null 2>&1 ); then
  echo "gh on the service PATH: $( PATH="$RUNTIME_PATH"; hash -r 2>/dev/null; command -v gh )"
else
  echo "NOTE: gh is NOT on the service PATH — auto-complete on merge and \`jarvis bug" >&2
  echo "      report\` will be off. Install the GitHub CLI, or add its directory to the" >&2
  echo "      fallback list in this script and re-run it." >&2
fi

if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] systemctl --user daemon-reload"
  echo "[dry-run] systemctl --user enable jarvis.service jarvis-ui.service"
  echo "[dry-run] systemctl --user restart jarvis-ui.service"
  echo "[dry-run] systemctl --user restart jarvis.service"
  exit 0
fi

systemctl --user daemon-reload
systemctl --user enable  jarvis.service jarvis-ui.service

# Restart order matters, for the same reason it does in scripts/shipit.sh: run from a
# Claude session that Jarvis spawned, this script lives inside jarvis.service's cgroup,
# and restarting that unit SIGTERMs the script mid-way. So restart the UI (which never
# hosts us) first, then hand the daemon's restart to a transient unit outside our
# cgroup and do nothing afterwards that we would mind losing.
systemctl --user restart jarvis-ui.service
sleep 2
systemctl --user --no-pager --lines=4 status jarvis-ui.service || true

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — services stop at logout. Enable with: sudo loginctl enable-linger $USER"
fi

if command -v systemd-run >/dev/null 2>&1; then
  systemd-run --user --collect --unit=jarvis-install-restart \
    --description='install_prod_service: restart jarvis.service' \
    /bin/sh -c 'sleep 3; systemctl --user restart jarvis.service'
  echo "queued: jarvis.service restarts in ~3s — check with: systemctl --user status jarvis"
else
  echo "systemd-run unavailable — restarting jarvis.service inline (this script may be killed)"
  systemctl --user restart jarvis.service
  sleep 2
  systemctl --user --no-pager --lines=4 status jarvis.service || true
fi

