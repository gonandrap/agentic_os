#!/usr/bin/env bash
# shipit — cut a Jarvis OS release from ALREADY-merged main and deploy it to production.
#
# Releases never bypass code review: the code changes must already be on main via a
# reviewed PR before you run this. shipit only does the release-cut + deploy:
#
#   1. Verify the tree is clean and resolve the target version X.Y.Z (from the latest
#      jarvis-* tag — main's pyproject is NOT bumped by shipit).
#   2. Cut branch  release/jarvis-X.Y.Z  from main.
#   3. Bump pyproject.toml + commit + annotated tag  jarvis-X.Y.Z  *on the release
#      branch* — main is never modified (done in a throwaway git worktree so the
#      shared main checkout's HEAD never moves).
#   4. Deploy the tag to  $PRODUCTION_CODE/jarvis_os  (clone on first run, then
#      fetch + checkout the tag + `uv sync`), and restart the systemd services.
#   5. Notify Telegram (best-effort).
#
# Nothing is pushed to the GitHub remote: production tracks the LOCAL dev repo, so
# releases are offline, deterministic, and trivially reversible.
#
# Usage:
#   scripts/shipit.sh                 # patch-bump from the latest jarvis-* tag
#   scripts/shipit.sh 1.2.0           # release an explicit version
#   scripts/shipit.sh patch|minor|major
#   scripts/shipit.sh --dry-run [ver] # print what would happen, change nothing
#
# Env:
#   PRODUCTION_CODE   production root (default: ~/workspace/production)
set -euo pipefail

DRY_RUN=0
BUMP_OR_VERSION=""
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    *) BUMP_OR_VERSION="$arg" ;;
  esac
done

REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$REPO"

PRODUCTION_CODE="${PRODUCTION_CODE:-$HOME/workspace/production}"
PROD_ROOT="$PRODUCTION_CODE"
PROD_DIR="$PROD_ROOT/jarvis_os"
PROD_CONFIG="$PROD_ROOT/config/catalog.json"
PROD_SECRETS="$PROD_ROOT/secrets/jarvis.env"

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else eval "$*"; fi; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. preconditions -----------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || say "warning: shipping from '$BRANCH', not 'main' (releases cut from merged main)"
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "tracked changes present — releases are cut from a clean, already-merged main"
fi

cur_version()   { grep -m1 -E '^version *= *"' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/'; }
latest_tagged() {  # highest X.Y.Z among jarvis-* tags, empty if none
  git tag -l 'jarvis-*' | sed -E 's/^jarvis-//' \
    | { grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' || true; } | sort -V | tail -1
}

bump() {  # bump <X.Y.Z> <major|minor|patch>
  local IFS=.; read -r ma mi pa <<<"$1"
  case "$2" in
    major) echo "$((ma+1)).0.0" ;;
    minor) echo "$ma.$((mi+1)).0" ;;
    patch) echo "$ma.$mi.$((pa+1))" ;;
  esac
}

# Version numbering derives from the latest release TAG, not pyproject.toml — main's
# pyproject is never bumped by shipit (the bump lives only on release branches).
BASE="$(latest_tagged)"; BASE="${BASE:-$(cur_version)}"
case "$BUMP_OR_VERSION" in
  "")                   VERSION="$(bump "$BASE" patch)" ;;
  major|minor|patch)    VERSION="$(bump "$BASE" "$BUMP_OR_VERSION")" ;;
  [0-9]*.[0-9]*.[0-9]*) VERSION="$BUMP_OR_VERSION" ;;
  *) die "invalid version/bump: '$BUMP_OR_VERSION' (want X.Y.Z or major|minor|patch)" ;;
esac

TAG="jarvis-$VERSION"
REL_BRANCH="release/jarvis-$VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"
git rev-parse -q --verify "refs/heads/$REL_BRANCH" >/dev/null && die "branch $REL_BRANCH already exists"

MAIN_SHA="$(git rev-parse HEAD)"
say "Releasing Jarvis OS $VERSION  (base tag: ${BASE}, from $BRANCH ${MAIN_SHA:0:9})"
say "  branch: $REL_BRANCH   tag: $TAG"
say "  deploy: $PROD_DIR"

# --- 2. cut the release branch from main ----------------------------------------
say "cutting release branch $REL_BRANCH from $BRANCH"
run "git branch '$REL_BRANCH' '$MAIN_SHA'"

# --- 3. bump + commit + tag ON the release branch (main is never modified) -------
# Use a throwaway worktree so the shared main checkout's HEAD/tree never moves.
say "bumping pyproject.toml → $VERSION and committing on $REL_BRANCH (main untouched)"
if [ "$DRY_RUN" = 1 ]; then WT="<tmp-worktree>"; else WT="$(mktemp -d)"; fi
cleanup() { [ "$DRY_RUN" != 1 ] && [ -n "${WT:-}" ] && [ -d "$WT" ] \
              && git worktree remove --force "$WT" 2>/dev/null || true; }
trap cleanup EXIT
run "git worktree add --quiet '$WT' '$REL_BRANCH'"
run "sed -i -E '0,/^version *= *\"[^\"]+\"/s//version = \"$VERSION\"/' '$WT/pyproject.toml'"
run "git -C '$WT' add pyproject.toml"
run "git -C '$WT' commit -m 'Release jarvis-$VERSION'"
say "tagging $TAG on $REL_BRANCH"
run "git -C '$WT' tag -a '$TAG' -m 'Jarvis OS $VERSION'"
run "git worktree remove --force '$WT'"

# --- 4. deploy to production ----------------------------------------------------
say "deploying to $PROD_DIR"
run "mkdir -p '$PROD_ROOT'"
if [ ! -d "$PROD_DIR/.git" ]; then
  say "first deploy — cloning local repo into $PROD_DIR"
  run "git clone '$REPO' '$PROD_DIR'"
fi
run "git -C '$PROD_DIR' fetch origin --tags --prune --force"
run "git -C '$PROD_DIR' checkout -f '$TAG'"
say "building production venv (uv sync --frozen --extra ui)"
run "(cd '$PROD_DIR' && uv sync --frozen --extra ui)"

# default production catalog (empty fleet until prod onboarding lands)
if [ ! -f "$PROD_CONFIG" ]; then
  say "creating default production catalog at $PROD_CONFIG"
  run "mkdir -p '$PROD_ROOT/config'"
  if [ "$DRY_RUN" != 1 ]; then
    cat > "$PROD_CONFIG" <<'JSON'
{
  "$comment": "Production fleet catalog. Projects under $PRODUCTION_CODE get added here (prod onboarding — future). Empty is valid.",
  "os": {
    "defaults": { "model": "sonnet", "permission_mode": "auto" },
    "notifications": {
      "sinks": ["log", "telegram"],
      "telegram": { "token_env": "JARVIS_TELEGRAM_TOKEN", "chat_id_env": "JARVIS_TELEGRAM_CHAT_ID" }
    },
    "ui": { "port": 8787 }
  },
  "projects": []
}
JSON
  fi
fi

# --- 5. restart services if installed -------------------------------------------
restarted=0
for svc in jarvis.service jarvis-ui.service; do
  if systemctl --user list-unit-files "$svc" 2>/dev/null | grep -q "$svc"; then
    say "restarting $svc"
    run "systemctl --user restart '$svc'"
    restarted=1
  fi
done
if [ "$restarted" = 1 ]; then
  [ "$DRY_RUN" = 1 ] || sleep 2
  run "systemctl --user --no-pager --lines=0 status jarvis.service jarvis-ui.service || true"
else
  say "services not installed — run scripts/install_prod_service.sh once to enable them"
fi

# --- 6. notify Telegram (best-effort) -------------------------------------------
notify_telegram() {
  local text="🚀 Shipped $TAG to production ($PROD_DIR)"
  if [ "$DRY_RUN" = 1 ]; then
    run "curl telegram sendMessage: $text"
    return 0
  fi
  [ -f "$PROD_SECRETS" ] || { say "telegram: no secrets at $PROD_SECRETS — skipped"; return 0; }
  # shellcheck disable=SC1090
  set -a; . "$PROD_SECRETS"; set +a
  local tok="${JARVIS_TELEGRAM_TOKEN:-}" chat="${JARVIS_TELEGRAM_CHAT_ID:-}"
  if [ -z "$tok" ] || [ -z "$chat" ]; then
    say "telegram: token/chat_id not in $PROD_SECRETS — skipped"; return 0
  fi
  if curl -sS --max-time 15 "https://api.telegram.org/bot${tok}/sendMessage" \
       -d "chat_id=${chat}" -d "text=${text}" >/dev/null 2>&1; then
    say "telegram: notified"
  else
    say "telegram: send failed (non-fatal)"
  fi
}
say "notifying telegram"
notify_telegram

say "shipped $TAG → $PROD_DIR"
[ "$DRY_RUN" = 1 ] && say "(dry-run: no changes were made)"
exit 0
