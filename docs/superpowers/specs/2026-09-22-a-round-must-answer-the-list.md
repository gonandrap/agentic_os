# A round that changed nothing the last one asked about must not reach the panel

wo-22545f7f. Two measured failures, one predicate.

## 1. The failures

**A round that changed no code passed.** wo-2005a89b round 3 changed only a test, a spec
section and the PR body; round 2 had rejected the behaviour underneath, unchanged. The
panel re-judged what it had already rejected and flipped — on model variance, because
nothing else had moved.

**A round that answered none of the feedback cost a full round.** wo-0a9ba9b3 round 2
added two things, neither on the list of five round 1 gave it. It spent a worker turn,
five panel seats, five more follow-up issues and one of `max_rounds = 3`.

## 2. What they have in common

Both are "did the submitter touch anything the previous round asked about?". Neither needs
a reviewer to answer, and neither may have one: a second judge of the work is the
rejection treadmill kn-a1d55bb8 is about, one layer down.

## 3. The primitive: what moved since the last round

No column could say. `packet.files`, `packet.diff` and `packet.stat` are all cumulative
against the base, so a round that edits a file it already touched has the identical file
list, and `diff_sha` only answers "identical or not" over the whole submission.

`evidence.file_digests` takes the UNTRUNCATED diff and returns one sha per changed path,
over that path's own section. Each round persists it in `validation_rounds.file_shas`
beside `head_sha`, written in `Daemon._validate_work_order` at the same point and for the
same reason — it is a fact about the packet, whatever the verdict turns out to be.
`evidence.changed_since` diffs two of those maps; a path missing from the newer one counts
as changed, because reverting a file is answering with it.

`''` is "not recorded" and FAILS OPEN — every round written before the column existed, and
any round whose packet had no diff.

## 4. The classifier is the previous round's own citations

`validation.cited_paths` reads the previous round's BLOCKERS — through
`validation.blockers(validation.findings(...))`, the same pair `ops.prior_round_history`
reads — **anchor first, per blocker**: a finding whose `file` key is non-empty cites that
path and its prose is NOT regexed. Only a finding with no anchor falls back to extracting
repo-relative paths from its title and detail. Mixed sets work finding by finding.

The anchor wins because prose is an argument and may quote any path at all. wo-5ef5f42c
round 2: both blockers carried `file:
docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md` and illustrated it with `sed -n
'40,80p' docs/superpowers/specs/x.md`, so the regex cited a path that exists nowhere and bounced a
commit that did change the spec — twice, to the ceiling.

This is what keeps the rule out of judgement. The work order asked for "production code"
to be distinguished from tests, and for a rejection that was ABOUT the tests to be
distinguished from a behaviour rejection. Neither distinction has to be made: a round that
rejected for missing coverage cites test files, and a round that rejected a behaviour
cites the file the behaviour is in. "Did the submitter touch anything this round cited"
answers both — the legitimate test-only round touches the test file the round named, and
wo-2005a89b's round 3 touches none of the production files round 2 named.

## 5. The rule, and what it will not do

`validation.unanswered_submission(previous, blockers, before, now, *, exists=None)`
returns the cited paths when NONE of them moved, else None.

- **A CITATION THAT NAMES NO REAL FILE IS DROPPED FIRST**, before the `changed_since`
  comparison. A path is kept if `exists(path)`, or if it matches a key of `before`, or of
  `now`. A list that empties returns None, and the tuple returned — the paths the bounce
  message shows the submitter — is always the FILTERED one.
- **`exists` is injected, never looked up here.** `before`/`now` are
  `evidence.file_digests` maps of CHANGED paths, not a repo tree, so this module cannot
  answer "is that a file" and stays pure: no `Path`, no `os`, no stat. `ops.unanswered_paths`
  supplies the work order's worktree (`exists=lambda p: (worktree / p).is_file()`, None
  when no worktree resolves, and a path escaping the tree is never kept). Neo question 1382.
- **SOME of the list is enough to reach the panel.** Partial progress is the panel's to
  judge. Only a round that addressed none short-circuits.
- **Every uncertainty returns None.** No previous round, a previous round that was not a
  rejection, either file map empty, no path cited at all, no citation that survives the
  filter. Failing open costs one panel round, which is what happens today; failing closed
  bounces work that was really done.
- It reads no code and calls no model.

`ops.submit_for_validation` applies it BEFORE opening a round — not in the daemon, where
the round has already been numbered, and the number and the budget are one function
(kn-a5e91633). A bounced submission therefore spends no round at all.

A forced round is never bounced, for `Daemon._repeat_submission`'s reason.

**A bounce TELLS the join where the work order sits** — `land_when_cleared(panel_open=True)`
— and never lets it re-derive that from the latest round. The rule reads
`last_judged_round`, which walks back past `failed` and `void` rows, so the latest row can
be one the bounce never looked at: a `void` written after the rejection is settled, is not
an open outcome, and would land unreviewed work in the merge queue. The assumption half of
the join still runs; a bounce says nothing about assumptions.

## 6. The ceiling

`ops.BOUNCE_LIMIT = 2` consecutive bounces off the same round. The third opens a round and
immediately escalates it through `ops.escalate_validation_round`.

The round is opened rather than the give-up written bare because
`invariants.true_blockers` re-derives `VALIDATION_STUCK_BLOCKER` from a round whose outcome
is `escalated` — an escalation with no round behind it has its attention flag rewritten on
the next reconcile tick, and the user is never asked.

`consecutive_bounces` keys on the round bounced AFTER, so a panel round in between changes
the key and the count restarts without anything having to reset it.

## 7. The record

Every bounce writes `validation_bounced` on the work order's timeline, naming the round it
checked against and the paths it checked (the user's ruling, Neo question 518). A bounce
that left no trace would be the OS discarding a delivery in silence.

The timeline line says NO ROUND WAS SPENT in as many words, or a reader counts it against
`max_rounds`.

The worker hears `ops.BOUNCE_FEEDBACK` over the bus, to the role `implementor`, exactly as
a rejection does — so `Daemon._deliver` flips the work order back to `running` and the
previous round, still `rejected`, keeps it parked in `validating` until then. No new
status, no new outcome, no branch in any caller.

## 8. What is NOT in scope

The feature-order loop. A feature's submitter is a manager that files work orders rather
than a worker that edits files, so "which paths moved" is not the question its rounds
turn on. `Daemon.feature_validation_tick` is unchanged.
