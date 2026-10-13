# A confirmation question with a zero-character diff

Work order wo-c5081102. GitHub issue #1029. Live case: wo-b4ffc787 assumption #3,
Neo question 1665.

## The problem

A confirmation question went to Neo carrying `(no diff)` and the marker
`[diff truncated — 0 of 68,675 chars; 9 file(s) not shown: …]`. Neo escalated
evidence-insufficient; wo-b4ffc787 sat in `needs_review` over an hour on a routine
spec-wording confirmation the user had to answer by hand.

Zero of 68,675 characters. The first dropped file was a 435-line spec document.

### Root cause: an all-or-nothing file rule applied to a budget of 12,000 chars

`autoreview.confirm_evidence` (src/jarvis/autoreview.py:1687) trims the collected packet
per assumption by calling `evidence._truncate` (src/jarvis/evidence.py:854) with
`cfg.confirm_diff_chars` (default 12000, `catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS`
at src/jarvis/catalog.py:559). `_truncate` keeps WHOLE file sections in order and, once
one section does not fit, drops that section and every section after it:

```
        if not dropped and used + len(text) <= limit:
```

So when the FIRST section alone exceeds the limit, `used + len(text) > limit` on the
first iteration, nothing is ever appended, and `kept` is `""`. The question is rendered
by `_what_changed` (autoreview.py:1756), whose diff branch is
`… if ev.diff else "(no diff)"`. 68,675 chars collected, 0 delivered.

The live case hit the worst ordering: the 435-line spec document sorted first in `git
diff` order and was ~5x the budget on its own.

### Why the two existing safeguards did not catch it

1. **Name-first ordering.** `confirm_evidence` partitions sections into `named` and
   `rest` using `_names_path` (autoreview.py:1722), which matches only the full path or
   the basename. Assumption #3 named SYMBOLS, not paths — the usual case for a wording
   or behaviour assumption — so `named` was empty, the `if named else full` branch kept
   the raw collection order, and the doc stayed first.
2. **The evidence gate.** `decide_evidence` (autoreview.py:766) is the gate every byte
   passes before `neo.ask` persists it (daemon.py:7524, reading `ce.what_changed()`). It
   checks two things, both secret-shaped: `secret_marker(stat, diff)` and
   `secret_marker_text(summary)`. Nothing in it looks at whether a diff survived, so a
   zero-character packet is armed, filed, sent, and paid for.

Net effect: the OS spends a Neo call that can only escalate, then parks the order for a
person. It is strictly worse than not switching the feature on.

## The fix

Three mechanical parts. 1 and 2 make zero characters nearly unreachable; 3 makes the
remaining case stop instead of asking blind.

### Part 1 — three tiers in `confirm_evidence` (autoreview.py:1687)

Replace the two-way `named` / `rest` partition with three lists, joined in this order:

| Tier | Contents |
|---|---|
| (a) | sections whose path the assumption NAMES (`_names_path`, unchanged) |
| (b) | source sections |
| (c) | documentation sections |

Documentation is a mechanical path test on the section's path (`new or old`), no content
sniffing:

- suffix in `.md`, `.rst`, `.txt`, `.adoc`, case-insensitive; OR
- any path component equal to `docs` or `specs`.

A doc the assumption names stays in tier (a) — tier (a) is tested first and wins. A
source file the assumption names also stays in (a); the tiering only reorders what was
previously the undifferentiated `rest`.

Drop the `if named else full` special case: the joined three tiers are the diff handed
to the trim in every case, which also removes a branch where the ordering silently
reverted.

**Why here.** This is the only place that knows the assumption. `evidence._sections`
already gives the per-file split, and the ordering decision is already this function's
job per its own docstring ("THE HUNKS THE ASSUMPTION NAMES GO FIRST"). Part 1 alone
fixes the live case: the 435-line spec goes last and can no longer eat the budget ahead
of the source the assumption is about.

### Part 2 — hunk-level trimming, a NEW function in `evidence.py` beside `_truncate`

`evidence._truncate` is UNCHANGED. It is also the validation panel's trim, via
`collect_work_order` (evidence.py:534) and `collect_feature` (evidence.py:675), at
`validation.diff_chars` = 150000 (catalog.py:542). At that budget the all-or-nothing
file rule costs nothing, and its stated reason — a section order that no longer matches
the repository "reads as complete and is not" — is deliberate. Changing it would change
what every panel seat sees.

The new function (confirm path only, called by `confirm_evidence`) returns
`(diff, cut?, dropped, partial)`:

1. Walk sections in the order given. While a section fits WHOLE in the remaining
   budget, keep it whole.
2. The FIRST section that does not fit is kept PARTIALLY: its pre-hunk header lines
   (everything from `diff --git` up to the first `@@`) plus as many WHOLE `@@` hunks as
   the remaining budget allows, then an explicit in-diff marker line naming how many
   hunks were omitted from that file. Never a mid-hunk cut.
3. Every section after the partial one is dropped WHOLE — preserving `_truncate`'s
   reason for that rule.
4. A section of which at least one hunk is kept is NOT in `dropped_files`. It goes in a
   new `partial_files: tuple[str, ...]` field on `ConfirmEvidence` (autoreview.py:1650),
   and `_truncation_marker` (autoreview.py:1728) names it beside the dropped ones —
   "partially shown" is a third state and must read as neither complete nor absent.
5. A BINARY section carries no `@@` hunk at all (`evidence._sections` docstring,
   evidence.py:816: `Binary files a/x and b/x differ`). It therefore has no hunk to keep
   and cannot be kept partially — if it does not fit whole, it drops, and it goes in
   `dropped_files`. Do not implement step 2 by splitting on `@@` without this case:
   a corpus of text diffs will never show it.
6. If even the partial section's header-plus-first-hunk exceeds the remaining budget,
   nothing of it is kept and it drops. When that happens on the FIRST section, the
   result is `diff == ""` — part 3's case.

Path-name selection in `dropped_files` / `partial_files` follows `_truncate`'s existing
rule verbatim: prefer the name `files` knows (`old if new not in files and old in files
else new`).

**Why a new function and not a flag on `_truncate`.** A `partial=True` keyword would put
the panel's trim and the confirmation's trim behind one body with two contracts and a
fourth return value the panel discards; the next person tuning one would be reading the
other's docstring. Two functions, two budgets, two stated rules.

### Part 3 — the backstop: `HELD_EVIDENCE_UNREADABLE`

New hold code in autoreview beside `HELD_EVIDENCE_SECRET` (autoreview.py:192), raised by
`decide_evidence` (autoreview.py:766) AFTER both secret checks, so secret precedence is
unchanged: a packet that is both secret-shaped and untrimmable still reports the secret.

The fact it fires on: **the collection handed over a diff and the trim kept none of it.**
`ConfirmEvidence` carries it — `full_chars > 0 and diff == ""` — exposed as a predicate
on the dataclass so the gate reads a boolean and does not re-derive the condition.
`decide_evidence` gains one keyword-only argument for it; the daemon call site
(daemon.py:7524) passes it from the same `ce` it already renders.

HELD means no question is filed, per `decide_evidence`'s existing contract: the
assumption stays pending and is the user's, and nothing is sent. The record says the
diff could not be trimmed to the budget — instead of paying a Neo call that can only
escalate.

The hold is RECORDED. It is NOT added to `daemon._holds_not_recorded` (daemon.py:564).
Its argument is `HELD_EVIDENCE_SECRET`'s word for word: the hold is the only thing on the
record saying why a work order the feature was switched on for is still waiting for a
person. `_note_autoreview_held` dedupes per assumption on `assumption_id`/`n`, which this
hold carries like the others.

#### The distinction the implementer must not collapse

**An EMPTY COLLECTION still asks.** `Daemon._confirmation_evidence`'s "AN EMPTY DIFF
STILL ASKS" (daemon.py:7705) and `confirm_evidence`'s `packet is None` branch are
UNCHANGED. `full_chars == 0` means git reported nothing — or the collector failed — and
the result summary alone is still worth confirming. Only a NON-EMPTY collection trimmed
to nothing holds. The guard is `full_chars > 0`, never `not diff` alone.

## Invariants

1. `evidence._truncate` is byte-identical and its behaviour for `collect_work_order` /
   `collect_feature` at 150000 chars is unchanged.
2. Every byte sent still goes through `decide_evidence`'s net: the gate reads the WHOLE
   rendered block from `ConfirmEvidence.what_changed()` (daemon.py:7525), including the
   partial section, its in-diff omitted-hunks marker, and the truncation marker. The new
   hold adds a check; it never bypasses the secret scan or reads a different text than
   the one persisted.
3. The changed-file list is never truncated (`_what_changed`, autoreview.py:1764: "this
   list is never truncated"; evidence.py rule 3). Tiering reorders DIFF SECTIONS only —
   `ConfirmEvidence.files` keeps the collection's full list and order.
4. The marker never states a total it cannot know: when `collect_truncated` is true it
   says `more than {collect_limit:,}` (autoreview.py:1730, Neo question 945). Adding
   `partial_files` must not turn that into a computed total.
5. No mid-hunk cut, ever. A partial section ends on a hunk boundary.
6. A binary section is never reported as partial.
7. `(no diff)` with `full_chars > 0` is now unreachable in a FILED question.
8. Secret precedence: `HELD_EVIDENCE_SECRET` before `HELD_EVIDENCE_UNREADABLE`.

## Tests to write first

Order matters — 1 is the live case.

1. **The live-case shape.** Collection of nine sections, first a 60,000-char `.md` under
   `docs/`, assumption content naming a SYMBOL and no path, budget 12000. Assert: the
   source sections are present, the doc is last and absent or partial, `diff != ""`, and
   `decide_evidence` arms.
2. **Tiering.** (a) A doc the assumption names by path stays first, ahead of source.
   (b) Source before doc when the assumption names nothing. (c) Path test cases:
   `x.md`, `x.rst`, `x.txt`, `x.adoc`, `docs/a/b.py`, `specs/a.py` are docs;
   `src/markdown.py`, `src/docsite.py`, `a.mdx` are not.
3. **Partial section.** First section 2x budget: header lines kept, a whole number of
   `@@` hunks kept, output ends on a hunk boundary, in-diff marker names the omitted
   hunk count, the file is in `partial_files` and NOT in `dropped_files`, later sections
   all dropped whole.
4. **Binary.** A binary section too big to fit is dropped, never partial, and appears in
   `dropped_files`.
5. **The hold.** `full_chars > 0` and a first hunk alone over budget: `decide_evidence`
   returns `HELD_EVIDENCE_UNREADABLE`, carries `assumption_id` and `n`, and no question
   is filed by the daemon pass.
6. **Empty collection still asks.** `packet is None`, and a packet with
   `diff == ""`/`full_chars == 0`: `decide_evidence` ARMS and the question goes out
   (regression guard for the distinction above).
7. **Secret precedence.** Evidence both secret-shaped and trimmed to nothing reports
   `HELD_EVIDENCE_SECRET`.
8. **Panel untouched.** `_truncate` keeps its existing tests unmodified; add one
   asserting `collect_work_order` still drops whole files at the panel budget.
9. **Marker.** With `collect_truncated=True` the marker still says `more than 1,000,000`
   and names partial and dropped files distinguishably.
10. **Gate completeness.** The existing
    `test_every_byte_of_the_evidence_block_went_through_the_net`
    (tests/test_autoreview_confirm.py:1342) still passes with a partial section in the
    block.

Existing suites to extend: tests/test_autoreview_confirm.py, the `_truncate` tests in
tests/test_evidence*.py.

## Not in scope

- `_names_path`'s weakness itself: it still matches paths and basenames only, not
  symbols. The tiering routes around it rather than teaching the OS which file a symbol
  lives in — that needs the repository, and `confirm_evidence` is pure by contract.
  **This is a deliberate symptom-level choice**; the root cause of the LIVE case is the
  all-or-nothing trim at a 12,000-char budget (parts 2 and 3 fix it outright), and the
  name match is the secondary ordering heuristic that failed to compensate.
- Raising `validation.confirm_diff_chars`. A bigger budget moves the threshold and leaves
  the cliff; see rejected alternatives.
- Hunk RANKING inside a section (most-relevant-first). Order within a file stays the
  diff's.
- Collection-level change. `CONFIRM_COLLECT_CHARS = 1_000_000` (daemon.py:361) stands.

## Rejected alternatives

1. **Raise `confirm_diff_chars`.** The obvious fix and the one a reviewer proposes. It
   does not remove the cliff — any single file over the new number reproduces 0 of N
   exactly — and it raises the per-assumption token cost of every confirmation on the
   fleet to insure against a tail case.
2. **Fall back to "keep the first `limit` characters" when nothing fits.** Cuts
   mid-hunk; hands the reviewer a truncated hunk header that reads as a real change.
   Rejected for invariant 5.
3. **Change `_truncate` to keep later sections that fit.** Breaks the panel's stated
   rule: a diff whose section order no longer matches the repository reads as complete.
4. **File the question with `(no diff)` and let Neo escalate.** Status quo. Costs a Neo
   call per tick-eligible assumption to produce an outcome known in advance, and
   `decide_evidence`'s docstring already rejects "filed with the diff withheld" (Neo
   question 549, kn-deef42ea).
5. **Suppress the new hold from the timeline.** It would leave the user with a pending
   assumption and no recorded reason — the exact failure `HELD_EVIDENCE_SECRET` is
   recorded to avoid (kn-22ba6087).
6. **Content-sniff documentation instead of a path test.** Non-deterministic over a
   diff fragment and untestable. The path test is mechanical and wrong only in cases
   tier (a) already covers.
