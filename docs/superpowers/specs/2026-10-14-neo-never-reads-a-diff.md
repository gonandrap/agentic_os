# Neo never reads a diff

Work order wo-3f01dcc8. Live case: wo-09c60d79 assumption #1.

**USER DECISION 2026-10-14: NEO NEVER RECEIVES A DIFF.** Checking delivered code against
the worker's claims is the VALIDATION PANEL's job — the panel already reads the full diff
at `validation.diff_chars` (150,000, `catalog.ValidationConfig.diff_chars`). Neo's own
answer to question 549 (option a, "the delivery-time confirmation must read the diff") is
REVERSED by the user. The reversal is recorded as Neo learning #102.

**SUPERSEDES** `docs/superpowers/specs/2026-10-13-a-confirmation-question-with-a-zero-character-diff.md`
parts 1, 2 and 3 in full: the three-tier ordering, the hunk-level trim and the
`HELD_EVIDENCE_UNREADABLE` backstop all exist to deliver a diff that is no longer sent.

**WITHDRAWS**, for the confirmation path only,
`docs/superpowers/specs/2026-09-26-bounded-model-inputs.md` § 2 ("The confirmation question
gets its own budget, sized for what it is"). That § still governs the PANEL's budget
(`validation.diff_chars`) and every other bound it set; only `confirm_diff_chars` goes.

## The problem

wo-09c60d79, assumption #1 — a routine assumption — was left to the user, and no Neo
question was ever filed. The chain, in order:

1. `Daemon._ask_assumptions` collects one packet per work order
   (`daemon.py:8056-8058` → `Daemon._confirmation_evidence`, `daemon.py:8247`, bounded by
   `daemon.CONFIRM_COLLECT_CHARS = 1_000_000` at `daemon.py:478`).
2. Per assumption it trims to `cfg.confirm_diff_chars` (default 12,000,
   `catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS` at `catalog.py:579`) via
   `autoreview.confirm_evidence` (`autoreview.py:1770`, call site `daemon.py:8062-8067`).
3. Assumption #1's text NAMED the pull request's spec FILE. So `_names_path`
   (`autoreview.py:1817`) matched, and the name-first tier put that file's section FIRST
   in the string handed to `evidence._truncate_hunks` (`evidence.py:928`). **Measured off
   commit 89c7a2c**, the pull request's only documentation file is
   `docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md`: a NEW
   file of 294 lines and 17,929 characters, which as one added hunk — a `+` on every line
   plus the `diff --git` and `@@` headers — is a section of ~18.3k characters.
4. A NEW FILE IS ONE HUNK. `evidence._partial` (`evidence.py:972`) keeps only WHOLE hunks
   — `if len(head) + … + len(hunk) + len(marker) > room: break` — so with one ~18.3k hunk
   and 12,000 of room it took nothing and returned `""`. `_truncate_hunks` then set
   `done = True` and dropped every later section whole (`evidence.py:960-968`), which is
   `_truncate`'s stated rule and its reason.
5. `kept == ""`, so `ConfirmEvidence.unreadable` (`autoreview.py:1748`,
   `full_chars > 0 and self.diff == ""`) was true, and `autoreview.decide_evidence`
   (`autoreview.py:778`) returned `HELD_EVIDENCE_UNREADABLE` (`autoreview.py:204`, branch
   at `autoreview.py:829-835`). No question filed. No model call. The assumption sat with
   the user.

### Root cause

**Not the trim.** The trim is doing what it was told. The root cause is that the
delivery-time confirmation was given a job the OS already pays another reviewer to do:
read the delivered diff and judge the change. That job needs an input whose size is set by
the pull request, and the confirmation question has a 12,000-char slot for it. The
2026-10-13 spec improved the ordering and the granularity inside that slot; the live case
shows the residual class the slot can never hold — ONE file bigger than the whole budget.
Every repair inside the slot moves the threshold and buys the same bug later at a
different file size. The 2026-10-13 spec said so itself in its rejected list (its
"Raise `confirm_diff_chars`": "a bigger budget moves the threshold").

The panel does not have this problem. It reads the whole diff at 150,000 chars, its prompt
already carries the assumption sentences (`validation.build_packet_prompt`,
`validation.py:357-369`), and it is already licensed to reject over a bad call.

## The fix

Take the diff out of the confirmation question, and ask the panel for the one judgement
the confirmation was carrying it for.

### Part 1 — `_confirm_question` loses the diff and the `ev` parameter

`autoreview._confirm_question` (`autoreview.py:1907`): drop the `_what_changed(ev)` block
(`autoreview.py:1955`) and the `ev: ConfirmEvidence` parameter entirely.

KEEPS, in this order, unchanged: the assumption; the mid-turn reading (provisional
verdict, model, reason); the work order's title and description at
`CONFIRM_DESCRIPTION_CHARS` (`autoreview.py:1707`); `_spec_block` (GitHub issue #1035);
the result summary at `CONFIRM_SUMMARY_CHARS` (`autoreview.py:1708`); the siblings via
`sibling_line`; `_record_block`; the answer-shape instruction.

ONE line is re-worded. Today (`autoreview.py:1950`):

```
That reading had NO diff and NO result summary in front of it. You do.
```

It must no longer claim the reader has a diff, because it does not. Replacement must
assert three things: the reading being confirmed was formed with no result summary; the
reader HAS the result summary; and nobody in this question sees the code. Wording to ship:

```
That reading had no result summary in front of it. You do. NEITHER reading sees the
diff: you are confirming a reading against what the worker says it DELIVERED, and
whether the code matches this sentence is the validation panel's judgement, not yours.
```

`_confirm_question`'s docstring (`autoreview.py:1911-1937`) states the diff as one of "the
two new ones [that] are the whole point of the second call (Neo, question 549)". Rewrite
that bullet: what the second call still has that the mid-turn reading did not is the
RESULT SUMMARY and the DECISION RECORD. Cite question 549 and learning #102 there, as the
reversal.

`autoreview.propose_confirmation` (`autoreview.py:1984`) loses its `evidence=` keyword and
the `evidence or ConfirmEvidence()` argument (`autoreview.py:2005`).

### Part 2 — the panel prompt asks for the contradiction

`validation.build_packet_prompt`'s assumptions block (`validation.py:357-369`) already
tells seats a bad call is a defect they may reject over, and already warns that
`accepted` means the user agreed with the SENTENCE, "not that anyone checked the code
against it". It does NOT ask a seat to check the diff against the sentence.

Add ONE clause to that block: ask seats to flag where the diff CONTRADICTS, EXCEEDS or
DOES NOT IMPLEMENT an assumption's sentence, and to say which assumption number and which
part of the diff. Shape it like the spec-section clause two blocks up
(`validation.py:345-351`), which already names "unimplemented, contradicted or exceeded"
over the spec — the same three verbs, now over the assumption sentences.

**That clause is the ONLY place in the system that does this job once this ships.** Neo
cannot: it has no diff. Say so in a comment on the clause, naming this spec, so nobody
later trims it as redundant with the sentence above it.

### Part 3 — `decide_evidence` stays a second gate, narrowed to the texts that travel

`autoreview.decide_evidence` (`autoreview.py:778`) is NOT folded into `decide_confirm`.
Two reasons, both current: the hold ORDERING and `HELD_EVIDENCE_SECRET`'s per-text wording
are tested behaviour, and `decide_confirm` is called on BOTH passes — the ask pass has no
result summary to scan, so folding would hand it a text it has no use for on half its
call sites (its own docstring, `autoreview.py:782-787`).

What it scans changes to exactly the two texts the question still carries:

1. the result summary (`wo["result_summary"]`), via `secret_marker_text` — unchanged,
   including the hold wording at `autoreview.py:824-827`;
2. the ASSUMPTION's own content, via `secret_marker_text` — NEW. The question interpolates
   `assumption["content"]` (`autoreview.py:1945`); a worker that wrote the credential into
   the assumption sentence puts it in the question store by the same route the diff was
   gated on. The hold must NAME which text it was, as the summary branch does.

Order: summary, then assumption content. Keeps the existing branch first so its tested
wording and precedence are untouched.

REMOVED from it: the `stat`/`diff` parameters, the `autoreview.secret_marker(stat, diff)`
call (`autoreview.py:814`), the `unreadable` parameter, and the
`HELD_EVIDENCE_UNREADABLE` branch (`autoreview.py:829-835`).

`autoreview.secret_marker` (`autoreview.py:641`) then has NO production caller —
`decide_evidence` was the only one (`find_referencing_symbols`: that one call site, plus
seven tests at `tests/test_autoreview_confirm.py:884-977`). **KEEP it.** It is pure, it
has no runtime cost while uncalled, and its two nets — the secret-shaped PATH net and the
added-lines net — are the only thing in the codebase that knows the secret shapes of a
DIFF; the panel path has no redaction gate and is the obvious next caller. Add a docstring
line saying it has no caller today and why it was kept, so it does not read as live
machinery. If a reviewer refuses dead code on principle, deleting it is a safe follow-up
that costs only the seven tests — do not bundle it here.

### Part 4 — the catalog field goes, and old catalogs still load

Remove `confirm_diff_chars` from `ValidationConfig` (`catalog.py:659`), from
`_parse_validation` (`catalog.py:2215-2217` and the constructor keyword at
`catalog.py:2248`), and `DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS` (`catalog.py:579`). Fix
the cross-reference in the `assumption_spec_chars` comment (`catalog.py:567`), which sizes
itself against `confirm_diff_chars`.

**Compatibility rule, explicitly: NO MIGRATION AND NO ERROR.** `_parse_validation` reads
every field with `raw.get(<name>, base.<name>)` and never enumerates the keys it was
given, so an unknown key is IGNORED. A catalog that still names
`validation.confirm_diff_chars` — including a per-project override — loads unchanged and
the key is silently dropped. A test pins this (test (c) below); without one, a later
strict-key parser would break every user catalog written before today.

### Part 5 — the machinery that is now dead

Each is dead because the confirmation question was its only consumer. Delete:

| Symbol | Why dead |
|---|---|
| `autoreview.ConfirmEvidence` (`autoreview.py:1718`) | the question's diff carrier; no field of it is read anywhere else |
| `autoreview.confirm_evidence` (`autoreview.py:1770`) | its only caller is `daemon.py:8062` |
| `_names_path` (`:1817`), `_is_documentation` (`:1823`) | the tier classifiers; only `confirm_evidence` calls them |
| `_some_of` (`:1841`), `_truncation_marker` (`:1848`), `_reference` (`:1865`), `_what_changed` (`:1880`) | the `# What changed` renderer and its parts; only `_confirm_question` and `ConfirmEvidence.what_changed` call them |
| `CONFIRM_DROPPED_FILES_SHOWN` (`:1703`), `CANNOT_SEE` (`:1711`), `SOURCE_WORKTREE`/`SOURCE_PULL_REQUEST` (`:1714-1715`) | read only by `_truncation_marker` and `_reference` |
| `HELD_EVIDENCE_UNREADABLE` (`:204`) and its branch (`:829-835`) | the condition cannot arise: no trim, no diff |
| `evidence._truncate_hunks` (`evidence.py:928`), `evidence._partial` (`:972`), `evidence._hunk_marker` (`:985`) | confirm-path only. VERIFIED by `search_for_pattern`: the only non-test reference to `_truncate_hunks` is `autoreview.py:1803`; `_partial` and `_hunk_marker` are called only from `_truncate_hunks`. Their tests at `tests/test_evidence.py:330-396` go with them |
| `Daemon._confirmation_evidence` (`daemon.py:8247`) and `daemon.CONFIRM_COLLECT_CHARS` (`:478`) | the collection existed to feed the trim |
| the per-assumption collect/trim/gate block in `Daemon._ask_assumptions` (`daemon.py:8055-8081`) | replaced by a `decide_evidence(a, summary=…)` call; the `packet = None` local (`daemon.py:8027`) and the lazy-collection comment go with it |

**`evidence._truncate` (`evidence.py:886`) is the PANEL's and must stay byte-identical.**
Its docstring references `_truncate_hunks` as the confirm-path sibling; that sentence is a
comment edit, not a behaviour change. `_truncate_hunks`'s own docstring is the place the
"two functions, two budgets" rule lived — the rule is gone, not relocated.

Two more comment-only edits, so nothing points at a deleted symbol:
`daemon.py:472-478` (the `CONFIRM_COLLECT_CHARS` block comment) and `daemon.py:8128`
(`_stakes_reviewed`'s docstring cites `_confirmation_evidence` as a precedent for cheap
conditions first — re-point it at the `decide_confirm`-before-`decide_evidence` ordering,
which still holds). `autoreview.py:80` becomes `from . import specs`: `navigation` was
imported only for `DOC_SUFFIXES`/`DOC_DIRS` at `autoreview.py:1771-1772`.

### Part 6 — the trim A/B eval is DELETED, not skipped

These measure a trim that no longer exists. A skipped eval is a claim that the
measurement still means something:

- `evals/llm/test_confirm_evidence_ab.py`
- `tests/test_confirm_evidence_ab_harness.py`
- `evals/tools/build_confirm_corpus.py`
- `evals/data/confirm_corpus_synthetic.json`

Nothing replaces them. The question they answered — "does a trimmed packet judge the
same?" — has no subject once no packet is sent. The panel's own diff budget was never
measured by this eval.

**Retract exactly these four**, with `jarvis learn retract <id> --reason "…"` naming this
spec. Each one's central claim is about the confirm-path diff budget or its measured cost,
so each is false or actively misleading once no diff is sent:

- **kn-51a43dcf**
- **kn-e06ff4e6** (successor of the already-retired kn-77e8b67a)
- **kn-ffaffd82**
- **kn-1be10a00** — the per-purpose-budget entry that introduced `CONFIRM_COLLECT_CHARS`
  and the branch-vs-sha reference rule. **Its "every byte persisted or sent has been
  through the secret net" invariant SURVIVES and still governs** (Part 3 is that
  invariant, over the two texts that still travel); it is the budget reasoning that goes.

**KEEP kn-4fbba7ec and kn-7100d002.** Both are eval-METHODOLOGY lessons — enforce arm
discipline in code, and exclude the three non-results because an "agreement with recorded
history" row is confounded when that history was judged by another model — which apply to
any future A/B and are not claims about the deleted trim.

## Invariants

1. A confirmation question contains NO diff text, no diff stat, no changed-file list and
   no truncation marker, at any pull-request size.
2. An assumption is never held for a reason about the SIZE of the delivered change.
3. The result summary and the decision record remain the confirmation's reason to exist:
   both are facts the mid-turn reading did not have.
4. `HELD_EVIDENCE_SECRET` keeps its precedence over everything after it, and keeps naming
   WHICH text carried the secret.
5. The panel's inputs are unchanged in size and content except for one added clause in the
   assumptions block.
6. A catalog naming `confirm_diff_chars` loads, with the key ignored.

## Tests to write first

These are the work order's acceptance criteria.

1. **No diff, whatever the size.** Render a confirmation question for a work order with a
   large delivered change and assert on the RENDERED TEXT — no `# What changed`, no
   `diff --git`, no `[diff truncated`. Assert on the question, never on an internal flag:
   a flag is the thing this change deletes.
2. **The live case, end to end.** A work order whose only changed file is a 20,000-char
   spec document, through the daemon's ask pass, gets a Neo confirmation question FILED —
   not `HELD_EVIDENCE_UNREADABLE`, which no longer exists. This is wo-09c60d79's shape.
   The 20,000 is SYNTHETIC and deliberately above the old 12,000 budget: the fixture
   reproduces the class of failure, not the measured 17,929-char file, so it keeps failing
   the old code without depending on one commit's byte count.
3. **Catalog compatibility.** A catalog whose `os.validation` (and a project's
   `validation`) names `confirm_diff_chars` parses, and the resulting `ValidationConfig`
   has no such attribute.
4. **The panel asks.** `validation.build_packet_prompt` with assumptions present contains
   the contradiction clause; with none, the block is absent as today.
5. **The secret scan still bites, on both texts.** A result summary carrying a credential
   shape holds with `HELD_EVIDENCE_SECRET` naming the summary (existing behaviour,
   existing wording); an ASSUMPTION whose content carries one holds naming the assumption
   text. Neither hold quotes the secret.

Delete, do not adapt, the tests in `tests/test_autoreview_confirm.py` that assert trim
behaviour, truncation-marker wording, `partial_files`, `collect_limit` rendering or
`HELD_EVIDENCE_UNREADABLE` (the `confirm_diff_chars` and `CONFIRM_COLLECT_CHARS`
references at `tests/test_autoreview_confirm.py:1058`, `:1166-1174`, `:1282`, `:1372`,
`:1554` locate them). A rewritten assertion over a deleted mechanism is noise.

## What this loses, and why that is acceptable

Neo can no longer catch "the worker's assumption and the worker's code disagree". That was
a real capability and it is being given up deliberately.

The replacement is Part 2's panel clause, and it is a better seat for the job: the panel
reads 150,000 chars rather than 12,000, reads the whole diff rather than a trim whose
contents depend on which words the assumption happened to use, and already has the
licence to reject over a bad call.

**The residual gap, stated honestly:** the panel judges the CHANGE AS A WHOLE and its
verdict is pass or reject. It can pass a change whose code is fine while a user-facing
assumption SENTENCE is stale — the sentence the user will read and accept. The clause asks
seats to flag that, but a flag inside a passing verdict is a follow-up finding, not a
block. Nothing in this spec closes that gap. It is the price of the user's decision, and
the cheap partial cover it replaces was a reviewer that escalated whenever the pull request
was large.

## Not in scope

- The panel's own budget (`validation.diff_chars = 150,000`). Unchanged, and
  2026-09-26 § 2 still governs it.
- The EARLY (mid-turn) pass. It never had a diff; nothing about it changes.
- Teaching the panel to raise a stale assumption as a BLOCKING defect rather than a
  finding. That is the residual gap above and wants its own decision.
- Any change to `decide_confirm`'s condition table.

## Rejected alternatives

1. **A finer trim granularity — split inside a hunk, or by line.** Tried one level up: the
   2026-10-13 spec moved from file granularity to hunk granularity and this work order is
   the next failure. Line granularity delivers a diff cut mid-hunk, which reads as
   complete and is not — the exact failure `_truncate`'s docstring (`evidence.py:888-893`)
   refuses. It also keeps every line of machinery Part 5 deletes.
2. **A bigger `confirm_diff_chars`.** Rejected in the 2026-10-13 spec and rejected again:
   a budget moves the threshold. 24,000 passes wo-09c60d79's ~18.3k section (a 17,929-char
   new file, measured off commit 89c7a2c) and fails the next 30k one, and the cost of
   every confirmation question rises on every work order to buy it.
3. **Send the diff only when it FITS.** Keeps a reviewer whose input depends on pull-request
   size, so the same assumption gets a different quality of review depending on how big
   the PR was — unobservable from the record, and worse than a consistent rule either way.
   Keeps `ConfirmEvidence`, the trim, the marker and the gate. Buys nothing the panel is
   not already doing.
4. **Summarise the diff for Neo with another model call.** A model-written diff summary is
   a second unreviewable authority between the code and the ruling: when it is wrong, the
   record shows a confident confirmation and no trace of why. It is also the panel's
   reading, re-bought at a second price.
5. **Make the mid-turn provisional ruling FINAL at delivery unless the assumption text or
   the result summary changed since it was formed** (the option this work order offered).
   IT COLLAPSES INTO THE SAME CALL. The mid-turn ruling is formed while the worker is
   still typing — `EARLY_PASS_STATUS = "running"` (`autoreview.py:238`) — when
   `wo["result_summary"]` is EMPTY; the summary exists only at `jarvis wo finish`. So "the
   result summary changed" is true on essentially every order, the exemption never fires,
   and the only thing shipped is a rule nobody can observe working. Keeping the call also
   keeps what the confirmation is FOR once the diff is gone: the result summary and the
   decision record are facts the mid-turn reading did not have.
6. **Fold `decide_evidence` into `decide_confirm`** now that it scans only texts a row
   carries. Rejected: see Part 3 — the ask pass has no summary, and the hold ordering and
   per-text wording are tested behaviour that a merged function would re-derive.
