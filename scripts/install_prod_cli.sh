#!/usr/bin/env bash
# install_prod_cli — render and install the production `jarvis` wrapper on PATH.
#
# One-time (idempotent) setup for production, and re-run by scripts/shipit.sh on every
# release. Writes ONE file, $BIN_DIR/jarvis, which exports the three variables the
# systemd units carry and execs the deployed venv's console script. Starts nothing,
# restarts nothing, touches no systemd state.
#
# Issue 757: without it `jarvis` is command-not-found, and the obvious fallback — the
# venv binary by full path — runs production code against ~/.jarvis, the DEV instance's
# state, silently.
#
# Env:
#   PRODUCTION_CODE     production root (default: ~/workspace/production)
#   JARVIS_CLI_BIN_DIR  default target directory (default: ~/.local/bin)
#
# Flags:
#   --dry-run         print the plan and the rendered body; write nothing
#   --bin-dir <dir>   where the wrapper goes
set -euo pipefail

DRY_RUN=0
BIN_DIR="${JARVIS_CLI_BIN_DIR:-$HOME/.local/bin}"
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)  DRY_RUN=1; shift ;;
    --bin-dir)  BIN_DIR="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PRODUCTION_CODE="${PRODUCTION_CODE:-$HOME/workspace/production}"
PROD_ROOT="$PRODUCTION_CODE"
PROD_DIR="$PROD_ROOT/jarvis_os"

# A wrapper that execs a missing file is a `jarvis` reporting `No such file or
# directory`, which is worse than `command not found`.
[ -x "$PROD_DIR/.venv/bin/jarvis" ] || {
  echo "production not deployed yet ($PROD_DIR/.venv/bin/jarvis missing) — run scripts/shipit.sh first" >&2
  exit 1; }

TEMPLATE="$REPO/deploy/jarvis.cli.template"
[ -f "$TEMPLATE" ] || { echo "template not found: $TEMPLATE" >&2; exit 1; }

# Idempotent by construction: the output is a pure function of PROD_ROOT.
BODY="$(sed -e "s#@PROD_DIR@#$PROD_DIR#g" -e "s#@PROD_ROOT@#$PROD_ROOT#g" "$TEMPLATE")"

if [ "$DRY_RUN" = 1 ]; then
  echo "[dry-run] mkdir -p $BIN_DIR"
  echo "[dry-run] write $BIN_DIR/jarvis (mode 755):"
  printf '%s\n' "$BODY"
else
  mkdir -p "$BIN_DIR"
  # Temp-then-mv, not `>`: a redirect writes THROUGH a pre-existing symlink into the
  # venv's own console script — docs/superpowers/specs/2026-10-07-a-production-jarvis-on-path.md
  TMP="$(mktemp "$BIN_DIR/.jarvis.XXXXXX")"
  cleanup() { [ -n "${TMP:-}" ] && rm -f "$TMP" 2>/dev/null || true; }
  trap cleanup EXIT
  printf '%s\n' "$BODY" > "$TMP"
  chmod 755 "$TMP"
  mv -f "$TMP" "$BIN_DIR/jarvis"
  echo "installed $BIN_DIR/jarvis"
fi

# Say it out loud when the directory is not on the caller's PATH — same reasoning as the
# `gh`-reachability note in install_prod_service.sh: the install succeeds either way and
# the only symptom is a feature that silently never happens.
case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "NOTE: $BIN_DIR is NOT on your PATH, so typing \`jarvis\` will still not find" >&2
    echo "      it. Add it in your shell rc file: export PATH=\"$BIN_DIR:\$PATH\"" >&2 ;;
esac
