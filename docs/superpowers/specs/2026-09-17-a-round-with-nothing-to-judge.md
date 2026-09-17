# A round with nothing to judge

Work order wo-47242e78. Neo, question 378. Triggering case: wo-ec96a1e9, whose whole job
was to stage a release and ship 0.10.5. It did, perfectly, and the panel escalated it to
the user with "this submission changes no files and records no other durable effect, so
there is nothing to review." The user: *"that will make jarvis not able to auto ship a
new version. The validation panel should be able to void a validation, not escalate and
let it go through."*

## §1 The same lesson, a third time

`Daemon._validate_work_order` escalated on `not packet.files`. Issue #200 widened that to
`not packet.files and not packet.side_effects` (spec
2026-09-12-the-pull-request-is-the-artifact.md §5) after a work order whose deliverable
was a knowledge-base retraction was escalated unjudged. This is the same sentence one
step further out: a release authors nothing in its worktree and writes no knowledge
entry, so both halves of the premise are empty and the guard fires on work that succeeded
completely.

The lesson each time is that **"no files changed" is not the same statement as "nothing
was delivered"**, and each time it was paid for with a hard-coded widening. So the shape
here is chosen to make the NEXT one cheap: a durable effect the diff cannot show is
collected by a *registered collector*, and adding a kind is a registration rather than an
edit to a guard.

## §2 Two answers, and they are not exclusive

A release's effects are real and verifiable — they are simply not in the worktree.
wo-ec96a1e9 pushed `release/jarvis-0.10.5` and the annotated tag `jarvis-0.10.5` to
origin, deployed that tag to the production checkout, rebuilt its venv, re-rendered the
systemd units and wrote `$JARVIS_HOME/run/pending_release.json`.

* **Collection** (`ops.side_effects_of`) is the right answer wherever the effect exists
  and nobody was looking for it. It is what stops the packet being empty.
* **Void** is the right answer where the effect exists, is collected, and there is still
  nothing for a *reviewer* to add.

Both land here. The release effect is collected — so it is on the record, in the packet
and in the fingerprint — and because every effect in that packet is one the OS verifies
ITSELF, the round is voided rather than handed to four seats that can only rubber-stamp
it.

## §3 The guard is not a bypass

`_escalate`'s fear is stated in its own docstring: *"A reviewer handed nothing to review
will approve it, and that single silent pass would make the whole feature theatre."*
Void must therefore be:

* **Derived, never chosen.** No seat may return it and no validator verdict produces it.
  It is decided from the packet before any seat is called.
* **Unreachable by delivering nothing.** No files and NO effects still escalates,
  verbatim as today. A work order that was supposed to author code and authored none
  reaches the user exactly as it does now.
* **Opt-in per collector.** `attested` defaults to False. A future collector that forgets
  to think about it gets its effects JUDGED (or, if it is the only one, escalated) — never
  silently voided. Today the release collector is the only member.

The decision table, and it is the whole rule (`evidence.nothing_to_judge`):

| files | side effects                  | outcome                                     |
|-------|-------------------------------|---------------------------------------------|
| any   | any                           | judged — the diff is the review             |
| none  | none                          | **escalate** — unchanged, this is the guard |
| none  | at least one NOT attested     | judged — issue #200's case, unchanged       |
| none  | all attested                  | **void**                                    |

## §4 What makes the release attested

Two independent machine checks, neither of which is a model reading prose:

1. The marker is written by the deploy script's `--stage` step 5b, which is reached only
   after the branch push, the tag push and the production deploy have all succeeded. Its
   existence is a claim no submitter can make by delivering nothing — writing it requires
   running the release command, which is a GATED privileged action that Neo has already
   reviewed.
2. `release.verify_on_boot` proves the release landed after the restart — production's
   `pyproject.toml` version equals the marker's, and `ExecMainStartTimestamp` on BOTH
   units is newer than the restart — and settles the work order itself. Never
   `is-active`, never `git describe` (kn-58429229).

A panel asked to re-judge that adds nothing it can check.

## §5 The collector reads two sources, and that is the race

`ops._release_effects` reads the marker when it names this work order, and otherwise the
`release_verified` / `release_restart` events on the order's own timeline. Neither alone
covers the window: before the restart only the marker exists; after `verify_on_boot`
succeeds the marker is DELETED, and a round still pending across that daemon restart
would collect an empty packet and escalate a release that had already shipped. The union
has no gap — the marker exists from staging until the restart events are on the timeline.

A collector MAY NOT RAISE on absence. Nothing swallows it: a collector that throws leaves
the round unjudged and retried, which is correct, whereas swallowing it could drop a
judgeable effect from a packet that then reads as all-attested and voids.

## §6 `attested` is stamped by the registry and hashed by nothing

The registry stamps `attested` onto every record its collector returns, so a collector
cannot mark its own effects. That makes it a field the OS writes and the submitter cannot
move — `history`'s rule exactly (spec 2026-09-15-the-panel-blocks-on-blockers.md §5.5) —
so `evidence.side_effects_digest` excludes it. Including it would change the digest of
every knowledge effect already collected and make the next round of every open work order
read as new evidence, silently disabling `Daemon._repeat_submission`.

## §7 Where a voided unit lands

* **Work order** — `ops.land_when_cleared(..., panel_cleared=True)`, the no-validator
  path's own settlement: the round was closed by this caller and must not be re-read. A
  release order with no pull request therefore lands `completed`. NO attention flag and
  NO notification, and that is checkable rather than hopeful:
  `invariants._validation_escalated` keys on `outcome == "escalated"`, so `true_blockers`
  cannot re-derive `VALIDATION_STUCK_BLOCKER` for a voided round on the next reconcile
  tick.
* **Feature order** — `_complete_feature`, the same place a pass lands it.
* `void` joins `VALIDATION_OUTCOMES` and NOTHING else. Not `COUNTED` — nobody judged, so
  no round was spent. Not `RUNNABLE` and not `OPEN` — it is terminal, and the panel has
  finished with the unit.

The staged-release handshake still runs end to end on top of that: the void settles the
order `completed`, the reconcile hook restarts the units because the shipping worker's
turn has ended, and `verify_on_boot` verifies the version on disk and leaves the order
exactly where the void put it (`_settle`'s `completed` branch).

## §8 The feature-order guard gets the same treatment

`daemon.py`'s two empty-packet guards call ONE helper (`evidence.nothing_to_judge`)
rather than carrying two copies of the rule — otherwise a feature order whose children
were releases escalates for the identical reason and the two guards drift.

The feature's OTHER guard, `not packet.base`, still does not yield and is still checked
FIRST. §5 of the 2026-09-12 spec is unchanged: it asks *can we honestly diff what was
delivered*, which an attested effect does not answer, so a baseless feature escalates
before the void rule is ever consulted.
