# Morpheus: every shipped PR is validated in production

## Problem

Releases ship many PRs. Nothing checks that each one actually works in production, so
"merged and deployed" is the last thing the OS knows about a change. The user wants the
loop closed: every PR in a release is watched in production until it is shown to work,
shown to be broken, or runs out of time — and the last two must cause something.

## The entity

**Morpheus** is the OS's production validator. It owns a durable queue (`morpheus_items`
in the central `os.db`) of PRs to validate. Each item has a state:

| state | meaning | next |
|---|---|---|
| `queued` | waiting for a validation script (only hints were given) | `monitoring` |
| `monitoring` | its script runs on a cadence | `validated` / `failed` / `timedout` |
| `validated` | the script exited 0 (or the PR waived validation, or the user did) | terminal |
| `failed` | the script exited 1 | terminal + action |
| `timedout` | `timeout_days` (default 3) passed with no verdict | terminal + action |

`jarvis morpheus requeue <id>` moves a terminal item back to `monitoring` with a fresh deadline.

## Three halves, and only one of them spends tokens

1. **The release fills the queue (mechanical).** `shipit` calls
   `jarvis morpheus enqueue-release <tag> --from <prev-tag> --to <sha>` after deploying.
   It reads the first-parent log of that range and finds each PR number. It reads each
   PR's `## How to validate in production` section and queues one item per PR. The
   daemon runs the same call at boot for the version production is running, which covers
   `--stage` releases and a `shipit` that died halfway. It is idempotent per PR.
2. **The daemon runs the scripts (mechanical, no model).** Each `monitoring` item's
   script runs every `check_every_minutes` (default 15) with a timeout, from the
   production checkout. **Exit 0 = validated, exit 1 = failed, anything else = not yet
   observed.** "Not yet" is the normal answer for a change whose code path production
   has not exercised; the deadline is what turns that into `timedout`.
3. **Morpheus's session writes scripts (the only LLM).** One long-lived Claude session,
   resumed turn after turn. The daemon wakes it ONLY when an item is `queued`, meaning
   the PR gave hints without a script. Each turn, Morpheus spawns one subagent per item
   (Task tool). Each subagent turns that PR's hints into a read-only script and
   registers it with `jarvis morpheus script <id> --file <path>`. After a turn, if the
   session's context exceeds `compact_at` (default 30%) of its window, the next turn is
   `/compact`. Its turns respect `jarvis pause`; the mechanical halves do not, because
   they spend nothing.

## The PR section

```markdown
## How to validate in production

Hints in prose: what production activity proves this works.

```bash
# exit 0 = validated, 1 = failed, anything else = not yet observed
jarvis status --json | python3 -c '...'
```
```

* A fenced `bash`/`sh` block is the script → the item starts in `monitoring`, no LLM.
* Prose only → `queued`; Morpheus writes the script.
* `None — <reason>` → `validated` immediately, the reason recorded (docs-only, tests-only…).

Enforced three ways:

* A repo template heading. `.github/pull_request_template.md` carries the section.
* The PreToolUse hook. It denies a worker's `gh pr create` whose body lacks the section
  or leaves it empty, but only in repositories whose template carries the heading, so
  other projects are not affected.
* A CI check. `.github/workflows/pr-body.yml` runs `scripts/check_pr_validation.py` on
  `opened/edited/synchronize/reopened`.

## Failed and timed out must act

Exactly once per item (`action` column), deterministic, configurable:

* `on_failed` (default `investigate`): an investigation order on the PR's work order,
  parsed from the `wo-xxxxxxxx:` title prefix. It goes through the steward as origin
  `morpheus`. If the PR has no work order, or the steward declines, Neo is asked instead.
* `on_timeout` (default `neo`): a Neo question of the new kind `validation`, with no work
  order behind it, like `triage`. Neo either escalates, which puts a warning in the inbox
  for the user, or answers, which records its reading on the item as the resolution and
  puts an info row in the inbox.

## Surfaces

* `jarvis morpheus status|show|add|enqueue-release|script|check|waive|requeue`
* `/morpheus` dashboard page. It shows the session (id, context %, turns, compactions,
  paused), counts by state, and every item with its script, last output and action.
* `INV-MORPHEUS-OVERDUE` (OS invariant). It fires when an item is past its deadline but
  still open, or when a failed/timed-out item has no action recorded. Either means the
  daemon loop stopped doing its job.

## Config — `os.morpheus`

`enabled` (true), `model` (`claude-sonnet-5-5`), `timeout_days` (3),
`check_every_minutes` (15), `script_timeout_seconds` (60), `compact_at` (0.30),
`on_failed` (`investigate`), `on_timeout` (`neo`), `turn_timeout_seconds` (900).
