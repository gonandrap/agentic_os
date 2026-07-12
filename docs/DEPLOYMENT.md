# Jarvis OS — deployment & the dev/prod split

Jarvis OS runs in two isolated instances so that developing the OS never disturbs the
running fleet.

| | **Development** | **Production** |
|---|---|---|
| Location | `~/workspace/agentic_os` | `$PRODUCTION_CODE/jarvis_os` (default `~/workspace/production/jarvis_os`) |
| Git | branch `main` (trunk) | detached at tag `jarvis-X.Y.Z` |
| Run | `uv run jarvis …` (manual) | `systemctl --user … jarvis` (service) |
| `JARVIS_HOME` | `~/.jarvis` (default) | `$PRODUCTION_CODE/state` |
| Catalog | `catalogs/gonzalo.json` (gitignored, dev fleet) | `$PRODUCTION_CODE/config/catalog.json` |
| Secrets | none | `$PRODUCTION_CODE/secrets/jarvis.env` |
| UI port | 8788 | 8787 |

`JARVIS_HOME` is what keeps them apart: separate pidfiles, databases, and logs, so both
daemons can run at once without clashing. The dev catalog and `MIGRATION.md` are
gitignored, so a production clone is automatically free of dev-only config, and no
secrets ever live in git.

## Releasing (dev → prod)

Use the **shipit** skill, or run the script directly from the dev checkout on `main`:

```bash
scripts/shipit.sh --dry-run        # preview
scripts/shipit.sh                  # ship pyproject version if untagged, else patch bump
scripts/shipit.sh minor            # or patch | major
scripts/shipit.sh 1.4.0            # explicit version
```

This bumps `pyproject.toml` (if the version changes), commits on `main`, cuts
`release/jarvis-X.Y.Z` + tag `jarvis-X.Y.Z`, deploys the tag to
`$PRODUCTION_CODE/jarvis_os` (`git fetch` + `checkout` + `uv sync --frozen`), and
restarts the service. Production's git `origin` is the **local** dev repo, so releases
are offline and deterministic — nothing is pushed to GitHub.

> Git note: there is no bare `release` branch. Git cannot hold both a ref named
> `release` and refs named `release/…` (a file/directory conflict), so the release line
> is the versioned `release/jarvis-X.Y.Z` branches plus `jarvis-X.Y.Z` tags.

## First-time production setup

```bash
scripts/shipit.sh                                   # 1. create + populate the prod checkout
mkdir -p "$PRODUCTION_CODE/secrets"                 # 2. place secrets (KEY=VALUE, no export)
printf 'JARVIS_TELEGRAM_TOKEN=…\nJARVIS_TELEGRAM_CHAT_ID=…\n' > "$PRODUCTION_CODE/secrets/jarvis.env"
chmod 600 "$PRODUCTION_CODE/secrets/jarvis.env"
scripts/install_prod_service.sh                     # 3. install + enable + start the service
```

Start-on-boot needs user lingering (survives logout/reboot):

```bash
sudo loginctl enable-linger "$USER"        # already enabled on this host
```

## Managing the production service

```bash
systemctl --user status  jarvis            # health + recent logs
systemctl --user restart jarvis
systemctl --user stop    jarvis
journalctl --user -u jarvis -f             # follow logs
```

`Restart=always` + `RestartSec=5` + `StartLimitIntervalSec=0` means the daemon is
brought back up whenever it exits, indefinitely (recovery).

## Rollback

Redeploy a previous tag, or point production back and restart:

```bash
git -C "$PRODUCTION_CODE/jarvis_os" checkout -f jarvis-<older>
(cd "$PRODUCTION_CODE/jarvis_os" && uv sync --frozen)
systemctl --user restart jarvis
```
