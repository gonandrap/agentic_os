#!/usr/bin/env bash
# install.sh — one-command installer for Jarvis OS (https://github.com/gonandrap/agentic_os).
#
#   curl -fsSL https://raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh | bash
#
# What it does:
#   1. Checks prerequisites (git, python3.11+ or uv, and the Claude Code CLI).
#   2. Resolves the newest release tag (jarvis-X.Y.Z) on the remote — this script is
#      fetched from main, but what it installs is always a RELEASE, never main.
#   3. Clones that tag (shallow) into a temp dir and installs it into an isolated
#      environment, putting the `jarvis` executable on your PATH.
#   4. Scaffolds a starter catalog and prints the onboarding steps.
#
# Re-running it upgrades an existing install to the newest release (nothing else is
# touched: your catalog, $JARVIS_HOME state and per-project .jarvis/ dirs all survive).
#
# Usage (when piped through bash, pass flags after `-s --`):
#   curl -fsSL <url> | bash -s -- --tag jarvis-0.1.8
#
#   --tag <jarvis-X.Y.Z>  install this exact release instead of the newest
#   --repo <url>          install from another remote (fork)
#   --bin-dir <dir>       where the `jarvis` executable goes  (default ~/.local/bin)
#   --prefix <dir>        venv location for the pip fallback  (default ~/.local/share/jarvis-os)
#   --catalog <path>      starter catalog path (default $JARVIS_HOME/catalog.json)
#   --no-catalog          do not create a starter catalog
#   --no-ui               skip the [ui] extra (no web dashboard)
#   --dry-run             print the plan, change nothing
#   -h, --help            this help
#
# Every flag has an env var equivalent: JARVIS_TAG, JARVIS_REPO, JARVIS_BIN_DIR,
# JARVIS_PREFIX, JARVIS_CATALOG, JARVIS_HOME.
set -euo pipefail

REPO="${JARVIS_REPO:-https://github.com/gonandrap/agentic_os.git}"
TAG="${JARVIS_TAG:-}"
BIN_DIR="${JARVIS_BIN_DIR:-$HOME/.local/bin}"
PREFIX="${JARVIS_PREFIX:-$HOME/.local/share/jarvis-os}"
JARVIS_HOME_DIR="${JARVIS_HOME:-$HOME/.jarvis}"
CATALOG="${JARVIS_CATALOG:-}"
WANT_CATALOG=1
WANT_UI=1
DRY_RUN=0

# Self-contained so `curl … | bash -s -- --help` works (there is no $0 file to read).
usage() {
  cat <<'USAGE'
install.sh — one-command installer for Jarvis OS.

  curl -fsSL https://raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh | bash

Installs the newest release tag (jarvis-X.Y.Z) into an isolated environment and puts
the `jarvis` executable on your PATH. Re-run it to upgrade; your catalog, $JARVIS_HOME
state and per-project .jarvis/ dirs are never touched.

When piped through bash, pass flags after `-s --`:
  curl -fsSL <url> | bash -s -- --tag jarvis-0.1.8

  --tag <jarvis-X.Y.Z>  install this exact release instead of the newest
  --repo <url>          install from another remote (fork)
  --bin-dir <dir>       where the `jarvis` executable goes  (default ~/.local/bin)
  --prefix <dir>        venv location for the pip fallback  (default ~/.local/share/jarvis-os)
  --catalog <path>      starter catalog path (default $JARVIS_HOME/catalog.json)
  --no-catalog          do not create a starter catalog
  --no-ui               skip the [ui] extra (no web dashboard)
  --dry-run             print the plan, change nothing
  -h, --help            this help

Env var equivalents: JARVIS_TAG, JARVIS_REPO, JARVIS_BIN_DIR, JARVIS_PREFIX,
JARVIS_CATALOG, JARVIS_HOME.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --tag)        TAG="${2:?--tag needs a value}"; shift 2 ;;
    --repo)       REPO="${2:?--repo needs a value}"; shift 2 ;;
    --bin-dir)    BIN_DIR="${2:?--bin-dir needs a value}"; shift 2 ;;
    --prefix)     PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
    --catalog)    CATALOG="${2:?--catalog needs a value}"; shift 2 ;;
    --no-catalog) WANT_CATALOG=0; shift ;;
    --no-ui)      WANT_UI=0; shift ;;
    --dry-run)    DRY_RUN=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) printf 'unknown option: %s (try --help)\n' "$1" >&2; exit 2 ;;
  esac
done
CATALOG="${CATALOG:-$JARVIS_HOME_DIR/catalog.json}"

if [ -t 1 ]; then C_HL=$'\033[1;36m'; C_OK=$'\033[1;32m'; C_WARN=$'\033[1;33m'
                 C_ERR=$'\033[1;31m'; C_DIM=$'\033[2m';   C_OFF=$'\033[0m'
else             C_HL=; C_OK=; C_WARN=; C_ERR=; C_DIM=; C_OFF=; fi

say()  { printf '%s▶ %s%s\n' "$C_HL" "$*" "$C_OFF"; }
ok()   { printf '%s✓ %s%s\n' "$C_OK" "$*" "$C_OFF"; }
warn() { printf '%s⚠ %s%s\n' "$C_WARN" "$*" "$C_OFF" >&2; }
die()  { printf '%s✗ %s%s\n' "$C_ERR" "$*" "$C_OFF" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  %s[dry-run] %s%s\n' "$C_DIM" "$*" "$C_OFF"; else eval "$*"; fi; }
have() { command -v "$1" >/dev/null 2>&1; }

# --- 1. prerequisites -----------------------------------------------------------------
say "checking prerequisites"

have git || die "git is required (Jarvis runs every worker in a git worktree) — install git and re-run"
ok "git $(git --version 2>/dev/null | awk '{print $3}')"

# Any Python >= 3.11 will do. uv can provision one itself, so it only has to be found
# when uv is absent.
PYTHON=""
python_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
for cand in python3.13 python3.12 python3.11 python3 python; do
  if have "$cand" && python_ok "$cand"; then PYTHON="$(command -v "$cand")"; break; fi
done
if have uv; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
elif [ -n "$PYTHON" ]; then
  ok "python $("$PYTHON" -c 'import platform; print(platform.python_version())') ($PYTHON)"
elif have pipx; then
  ok "pipx ($(command -v pipx))"
else
  die "need uv, pipx, or Python 3.11+ — install one of:
     uv       curl -fsSL https://astral.sh/uv/install.sh | sh
     python   your package manager (python3.11 or newer)"
fi

if have claude; then
  ok "claude ($(command -v claude))"
else
  warn "the Claude Code CLI ('claude') is not on your PATH.
    Jarvis spawns every worker through it, so install and authenticate it before
    'jarvis start':  https://code.claude.com"
fi

# --- 2. resolve the release to install ------------------------------------------------
version_gt() {  # version_gt A B -> 0 when A > B, for X.Y.Z
  local IFS=. a b i; read -r -a a <<<"$1"; read -r -a b <<<"$2"
  for i in 0 1 2; do
    [ "${a[i]:-0}" -gt "${b[i]:-0}" ] && return 0
    [ "${a[i]:-0}" -lt "${b[i]:-0}" ] && return 1
  done
  return 1
}

latest_tag() {  # newest jarvis-X.Y.Z on the remote (numeric compare, so 0.1.10 > 0.1.9)
  local ref v best="" bestv=""
  while read -r _sha ref; do
    v="${ref#refs/tags/jarvis-}"
    [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue   # release tags only
    if [ -z "$bestv" ] || version_gt "$v" "$bestv"; then bestv="$v"; best="jarvis-$v"; fi
  done < <(git ls-remote --tags --refs "$1" 'refs/tags/jarvis-*' 2>/dev/null || true)
  printf '%s' "$best"
}

if [ -n "$TAG" ]; then
  say "installing pinned release $TAG"
else
  say "resolving the newest release on $REPO"
  TAG="$(latest_tag "$REPO")"
  [ -n "$TAG" ] || die "no jarvis-X.Y.Z release tags found on $REPO
     (pass --tag <jarvis-X.Y.Z>, or --repo <url> if you meant another remote)"
  ok "newest release: $TAG"
fi
VERSION="${TAG#jarvis-}"

# --- 3. fetch that tag ----------------------------------------------------------------
SRC=""
# Must end 0: an EXIT trap that fails would become the script's exit status.
cleanup() { if [ -n "${SRC:-}" ] && [ -d "$SRC" ]; then rm -rf "$SRC"; fi; return 0; }
trap cleanup EXIT
if [ "$DRY_RUN" = 1 ]; then SRC="<tmp-clone>"; else SRC="$(mktemp -d)"; fi

say "downloading $TAG"
run "git -c advice.detachedHead=false clone --quiet --depth 1 --branch '$TAG' '$REPO' '$SRC/jarvis_os'" \
  || die "could not clone $TAG from $REPO"

# --- 4. install -----------------------------------------------------------------------
SPEC="$SRC/jarvis_os"
[ "$WANT_UI" = 1 ] && SPEC="$SPEC[ui]"

run "mkdir -p '$BIN_DIR'"
UNINSTALL_HINT=""
if have uv; then
  UNINSTALL_HINT="uv tool uninstall jarvis-os"
  say "installing with uv into an isolated environment"
  run "UV_TOOL_BIN_DIR='$BIN_DIR' uv tool install --force --python '>=3.11' '$SPEC'" \
    || die "uv could not install $TAG (try 'uv self update', or re-run with --no-ui)"
elif have pipx; then
  UNINSTALL_HINT="pipx uninstall jarvis-os"
  say "installing with pipx into an isolated environment"
  run "PIPX_BIN_DIR='$BIN_DIR' pipx install --force '$SPEC'" \
    || die "pipx could not install $TAG (try re-running with --no-ui)"
else
  UNINSTALL_HINT="rm -rf '$PREFIX' '$BIN_DIR/jarvis'"
  say "installing into a venv at $PREFIX/venv"
  run "'$PYTHON' -m venv '$PREFIX/venv'" \
    || die "could not create a venv — on Debian/Ubuntu: sudo apt install python3-venv"
  run "'$PREFIX/venv/bin/python' -m pip install --quiet --upgrade pip"
  run "'$PREFIX/venv/bin/python' -m pip install --quiet '$SPEC'"
  run "ln -sf '$PREFIX/venv/bin/jarvis' '$BIN_DIR/jarvis'"
fi
ok "installed jarvis $VERSION → $BIN_DIR/jarvis"

# --- 5. verify the executable actually runs -------------------------------------------
if [ "$DRY_RUN" != 1 ]; then
  say "verifying"
  [ -x "$BIN_DIR/jarvis" ] || die "no executable at $BIN_DIR/jarvis — install failed"
  reported="$("$BIN_DIR/jarvis" --version 2>/dev/null || true)"
  if [ -n "$reported" ]; then
    case "$reported" in
      *"$VERSION"*) ok "jarvis --version → $reported" ;;
      *) warn "jarvis reports '$reported' but $TAG was installed" ;;
    esac
  else
    # Releases before jarvis-0.1.9 have no --version flag; --help still proves it runs.
    "$BIN_DIR/jarvis" --help >/dev/null 2>&1 || die "$BIN_DIR/jarvis does not run"
    ok "jarvis $VERSION runs"
  fi
fi

# --- 6. starter catalog ---------------------------------------------------------------
# `jarvis start` requires --catalog, so a fresh install has nothing to point it at.
if [ "$WANT_CATALOG" = 1 ]; then
  if [ -e "$CATALOG" ]; then
    ok "keeping your existing catalog at $CATALOG"
  else
    say "writing a starter catalog to $CATALOG"
    run "mkdir -p '$(dirname "$CATALOG")'"
    if [ "$DRY_RUN" != 1 ]; then
      cat > "$CATALOG" <<'JSON'
{
  "$comment": "Your Jarvis fleet. Add one entry per project under \"projects\", then: jarvis start --catalog <this file>",
  "os": {
    "defaults": { "model": "claude-opus-5", "permission_mode": "auto", "max_concurrent": 5 },
    "notifications": {
      "$comment": "add \"telegram\" once JARVIS_TELEGRAM_TOKEN and JARVIS_TELEGRAM_CHAT_ID are exported; \"desktop\" uses notify-send",
      "sinks": ["log"]
    },
    "ui": { "port": 8787, "base_url": "http://127.0.0.1:8787" }
  },
  "projects": []
}
JSON
    fi
    ok "catalog created (empty fleet — add your projects to it)"
  fi
fi

# --- 7. PATH check + next steps -------------------------------------------------------
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) warn "$BIN_DIR is not on your PATH. Workers and hooks call 'jarvis' by name, so add it:
    echo 'export PATH=\"$BIN_DIR:\$PATH\"' >> ~/.bashrc && exec \$SHELL" ;;
esac

cat <<EOF

${C_OK}Jarvis OS $VERSION is installed.${C_OFF}

${C_HL}Onboard your first project${C_OFF}
  1. Add it to your catalog — $CATALOG
       "projects": [ { "name": "my_app", "path": "~/workspace/my_app",
                       "description": "what this project is" } ]
     (the project must be a git repository)

  2. Start the OS — this also adopts every project in the catalog
     (OPERATION.md contract, .jarvis/ state dir, injected .claude/settings.json):

       jarvis start --catalog $CATALOG

  3. Give it work, then step away:

       jarvis wo create my_app "Add dark mode to the settings page"
       jarvis status                # what's running, what needs me?
       jarvis ui                    # dashboard on http://127.0.0.1:8787

To adopt a project without starting the fleet:  jarvis adopt ~/workspace/my_app
Details: https://github.com/gonandrap/agentic_os/blob/main/PROJECT_ONBOARDING.md
Upgrade: re-run this installer.  Uninstall: jarvis stop && $UNINSTALL_HINT
EOF
[ "$DRY_RUN" = 1 ] && say "(dry-run: nothing was installed)"
exit 0
