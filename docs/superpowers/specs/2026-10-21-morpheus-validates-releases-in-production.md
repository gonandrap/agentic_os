# Morpheus: every shipped PR validated in production

## Problem

The OS ships many PRs, in every project it runs. Nothing checks that each one works in
production, so "merged and deployed" is the last thing the OS knows about a change.
Production validation is an OS function: every PR in every onboarded project is watched
in production until a check says it works, or the check says it does not, or time runs
out. The last two hand the PR to someone who can find out why.

## The entity

**Morpheus** is deliberately dumb. It runs a check and reads the exit code. It never
diagnoses why a check failed; diagnosing is the job of whoever it hands the PR to. Its
queue is `morpheus_items` in the central `os.db`, with one row per PR, keyed by project.
Each item has a state:

| state | meaning | next |
|---|---|---|
| `queued` | reached production; waiting for its check to be written | `monitoring` |
| `monitoring` | its check runs on a cadence | `validated` / `failed` / `timedout` |
| `validated` | the check exited 0 (or the PR waived validation, or the user did) | terminal |
| `failed` | the check exited 1 | terminal + hand-off |
| `timedout` | `timeout_days` (default 3) passed without exit 0 or 1 | terminal + hand-off |

`jarvis morpheus requeue <id>` moves a terminal item back to `monitoring` with a fresh deadline.

A wrong check is not a wrong PR (issue #1358). `jarvis morpheus rewrite <id> --why "…"`, or Neo's
validation verdict with `"action": "rewrite"` (once per item), moves a failed or timed-out item back
to `queued` with its script, last exit and offers cleared and a fresh deadline. `why` and the rejected
script become the item's `rewrite_brief`, which Morpheus's next turn gets with the hints.

## Three halves, and only one of them spends tokens

1. **Reaching production fills the queue (mechanical).** Each queueing is idempotent per PR.
   * **The OS itself ships by release.** `shipit` calls `jarvis morpheus enqueue-release
     <tag> --from <prev-tag> --to <sha>` after deploying. That reads the first-parent
     log of the range and queues each PR it finds. The daemon runs the same call at boot
     for the tag production is running, which covers `--stage` releases and a `shipit`
     that died halfway.
   * **Every other project ships on merge.** The daemon queues each PR the OS saw merge
     (`pr_merged` events), behind a per-project cursor. A project's first sight starts
     the cursor at now; history is never backfilled.
   * **Projects with their own deploy pipeline** can call
     `jarvis morpheus enqueue-release <tag> --project <p> --repo <checkout>` themselves.
2. **The daemon runs the checks (mechanical, no model).**
   * Each `monitoring` item's check runs every `check_every_minutes` (default 15), with
     a timeout. It runs from its project's production directory: the deployed checkout
     for the OS, and the catalog path for every other project.
   * **Exit 0 = validated, two exit 1 in a row = failed, anything else = not yet.**
     "Not yet" is the normal answer for a change whose code path production has not
     exercised; the deadline is what turns it into `timedout`. A script exits 1 only on
     the PR's own failure signature and 75 when the environment is what it cannot read,
     and one exit 1 that the next check does not repeat is a blip, not a verdict (#1255).
3. **Morpheus's session writes the checks (the only LLM).**
   * PRs give hints, never scripts. One long-lived Claude session, resumed turn after
     turn, is woken by the daemon only when items are `queued`.
   * Each turn, Morpheus spawns one subagent per item (Task tool). The subagent turns
     the hints into a read-only check and registers it with
     `jarvis morpheus script <id> --file <path>`. The project directories of the batch
     are passed as `--add-dir`.
   * Each item is offered at most 3 times, at least 6 h apart, so an unregistered check
     cannot loop. A turn the model never answered — killed by a signal, wrote nothing,
     which is what a release restart does to the turn it just enqueued work for — costs
     no offer: the next pass re-offers the batch at once (#1161).
   * After a turn, if the session's context exceeds `compact_at` (default 30%) of its
     window, the next turn is `/compact`.
   * Its turns respect `jarvis pause`; the mechanical halves spend nothing and do not.
     Morpheus has its own pin over that (`os_state['morpheus.pause']`): `jarvis morpheus
     pause` holds it while the fleet runs, `jarvis morpheus unpause` runs it through a
     fleet pause, and `jarvis morpheus follow-fleet` drops the pin.

The daemon has no conversation context, so monitoring does not grow anything. It is
Python running the checks and reading rows. The session only ever sees "write checks for
these PRs", and each PR's work happens inside its own subagent, whose context is
discarded when it returns.

## The PR section

Every PR in every onboarded project must carry `## How to validate in production`
(`hooks.PR_BODY_SECTIONS`). The section holds hints: what production activity shows the
change working, and where to look. A command in it is a hint and is never run as
written. `None — <reason>` marks the item `validated` immediately, with the reason
recorded.

Enforced by:

* `hooks.pr_body_decision`, which denies a worker's `gh pr create` that lacks the section
  or leaves it as the template;
* the `open-a-pull-request` skill and its bundled template, which every project gets;
* `.github/workflows/pr-body.yml`, which runs `scripts/check_pr_validation.py` on
  `opened/edited/synchronize/reopened` in this repo.

## Failed and timed out hand off

Each terminal item hands off exactly once (the `action` column). The routing is
deterministic and configurable. What is handed over is facts only: the exit code, the
output tail, the hints and the check. It says explicitly that Morpheus has diagnosed
nothing.

* `on_failed` (default `investigate`): an investigation order on the PR's work order,
  parsed from the `[wo-xxxxxxxx]` title prefix. A PR opened by hand names no work order,
  and an investigation needs an order to investigate, so that one gets an improvement
  order instead, with the PR URL as its ref (#1253); the item records
  `improvement:<io-id>` rather than `investigation:<id>`. Either goes through the steward
  as origin `morpheus`, which needs `os.autonomy.self_dispatch`. If the steward declines
  or the order cannot be filed, the item goes to Neo instead.
* `on_timeout` (default `neo`): a Neo question of the new kind `validation`, which has
  no work order behind it, like `triage`. Neo either escalates (a warning in the inbox
  for the user) or answers (recorded on the item, plus an info row in the inbox).

An answer may carry `"action": "requeue"`, and the OS applies it: the item goes back on
the watch list with a fresh deadline instead of waiting for the user to read an inbox
row (issue #1254). The cap is one automatic requeue per item, counted in `auto_requeues`
and claimed atomically, so a verdict can never loop an item through validation. `requeue`
is the only action a verdict may carry: a waive ends the OS's only production check on a
change nobody has seen work, so it stays a command the user runs.

## Surfaces

* `jarvis morpheus status|show|add|enqueue-release|script|check|waive|requeue|pause|unpause|follow-fleet`
* `/morpheus`: the session (id, context %, turns, compactions, paused) and every item.
  Each item shows its id (one click selects it for the CLI), its project, the PR as a link, its state, the check, the exit code,
  the output tail and who it was handed to.
* `INV-MORPHEUS-OVERDUE` (OS invariant). It fires when an item is open past its
  deadline, or when a failed/timed-out item has no hand-off.

## Config — `os.morpheus`

`enabled` (true), `model` (`sonnet`), `timeout_days` (3), `check_every_minutes` (15),
`script_timeout_seconds` (60), `compact_at` (0.30), `on_failed` (`investigate`),
`on_timeout` (`neo`), `turn_timeout_seconds` (900).
