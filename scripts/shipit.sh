#!/usr/bin/env bash
# shipit — cut a Jarvis OS release from main and deploy it to production.
#
# Flow:
#   1. Verify the working tree is clean and resolve the target version X.Y.Z.
#   2. Bump pyproject.toml (only if needed) and commit the release on the trunk.
#   3. Cut branch  release/jarvis-X.Y.Z  and annotated tag  jarvis-X.Y.Z.
#   4. Deploy the tag to  $PRODUCTION_CODE/jarvis_os  (clone on first run, then
#      fetch + checkout the tag + `uv sync`), and restart the systemd service.
#
# Nothing is pushed to the GitHub remote: production tracks the LOCAL dev repo,
# so releases are offline, deterministic, and trivially reversible.
#
# Usage:
#   scripts/shipit.sh                 # ship the pyproject version if untagged, else patch-bump
#   scripts/shipit.sh 1.2.0           # ship an explicit version
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

say()  { printf '\033[1;36m▶ %s\033[0m\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else eval "$*"; fi; }
die()  { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# --- 1. preconditions -----------------------------------------------------------
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "main" ] || say "warning: shipping from '$BRANCH', not 'main'"
if ! git diff --quiet || ! git diff --cached --quiet; then
  die "tracked changes present — commit or stash before shipping"
fi

cur_version() { grep -m1 -E '^version *= *"' pyproject.toml | sed -E 's/.*"([^"]+)".*/\1/'; }
CUR="$(cur_version)"

bump() {  # bump <X.Y.Z> <major|minor|patch>
  local IFS=.; read -r ma mi pa <<<"$1"
  case "$2" in
    major) echo "$((ma+1)).0.0" ;;
    minor) echo "$ma.$((mi+1)).0" ;;
    patch) echo "$ma.$mi.$((pa+1))" ;;
  esac
}

case "$BUMP_OR_VERSION" in
  "")                 # no arg: ship current if untagged, else patch-bump
    if git rev-parse -q --verify "refs/tags/jarvis-$CUR" >/dev/null; then
      VERSION="$(bump "$CUR" patch)"
    else
      VERSION="$CUR"
    fi ;;
  major|minor|patch)  VERSION="$(bump "$CUR" "$BUMP_OR_VERSION")" ;;
  [0-9]*.[0-9]*.[0-9]*) VERSION="$BUMP_OR_VERSION" ;;
  *) die "invalid version/bump: '$BUMP_OR_VERSION' (want X.Y.Z or major|minor|patch)" ;;
esac

TAG="jarvis-$VERSION"
REL_BRANCH="release/jarvis-$VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "tag $TAG already exists"

say "Releasing Jarvis OS $VERSION  (current pyproject: $CUR)"
say "  branch: $REL_BRANCH   tag: $TAG"
say "  deploy: $PROD_DIR"

# --- 2. bump pyproject + commit on trunk (only if version changes) --------------
if [ "$CUR" != "$VERSION" ]; then
  say "bumping pyproject.toml $CUR → $VERSION and committing on $BRANCH"
  run "sed -i -E '0,/^version *= *\"[^\"]+\"/s//version = \"$VERSION\"/' pyproject.toml"
  run "git add pyproject.toml"
  run "git commit -m 'Release jarvis-$VERSION'"
else
  say "pyproject already at $VERSION — no bump commit"
fi

RELEASE_SHA="$(git rev-parse HEAD)"

# --- 3. cut release branch + tag ------------------------------------------------
say "cutting branch $REL_BRANCH and tag $TAG at ${RELEASE_SHA:0:9}"
run "git branch '$REL_BRANCH' '$RELEASE_SHA'"
run "git tag -a '$TAG' -m 'Jarvis OS $VERSION' '$RELEASE_SHA'"

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
    "defaults": { "model": "sonnet", "permission_mode": "acceptEdits" },
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

say "shipped jarvis-$VERSION → $PROD_DIR"
[ "$DRY_RUN" = 1 ] && say "(dry-run: no changes were made)"
