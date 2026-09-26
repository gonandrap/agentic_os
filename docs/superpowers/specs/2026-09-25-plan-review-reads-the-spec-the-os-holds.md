# Plan review reads the spec the OS holds

**Work order:** wo-457d6f88. **Issue:** https://github.com/gonandrap/agentic_os/issues/746.
**Ruled by Neo, question 667** — do both halves (A) and (B) below, and read the COMMITTED
doc, never the dirty worktree. That ruling is decided; this spec implements it and does not
re-open it.

## The problem

### 1. The plan reviewer is handed the spec's FILENAME and no spec

`ops.submit_plan` asks Neo with no context at all:

```python
# src/jarvis/ops.py:6010
q = neo.ask(name, planner_id, question, kind="plan")
```

`NeoStore.ask` (`src/jarvis/neo_store.py:239`) declares `context: str = ""`, and
`neo.build_question_prompt` (`src/jarvis/neo.py:186-194`) emits a context block ONLY when
`context` is non-empty. So the `plan` question's prompt is the skeleton and nothing else.

The skeleton names the document and never carries it —
`plans.render_plan_skeleton`, `src/jarvis/plans.py:503`:

```python
lines.append(f"Design document: {plan['design_doc']}")
```

Meanwhile `PLAN_REVIEWER_PERSONA` (`src/jarvis/plans.py:399`) demands a judgement that
input cannot support: *"The split follows the spec's boundaries … Judge whether those
sections are the feature's real functional seams or a decomposition the planner reached
for first and then carved the spec to fit."* The reviewer is told to judge against a
document it was not given, and told the path to it.

### 2. So it went to disk, and disk is not the OS's copy

Neo runs `claude -p` from a neutral cwd (`src/jarvis/neo.py:351`, `cwd=ensure_home()`).
Given a path and no text, the reviewer opened the path in whatever checkout it could
reach — one the OS neither controls, refreshes nor records.

### 3. Evidence: production question 658

Verbatim from `jarvis neo show 658`: the `context:` field is EMPTY. The answer denied the
plan for scope drift, arguing that
`docs/specs/2026-09-24-order-observability.md` *"stops at §9"* and quoting §8: *"Changing
the bill … This feature links to it and does not touch it"*.

Both statements were true of spec revision `2ed7523` (PR 735) and FALSE of `7cd0dfb`,
which the planner committed 13:34:38 UTC and which merged as PR 745 (`fb7739d`, 14:14
UTC). Neo answered at 14:14:22 — 22 seconds after the merge — off a checkout nobody had
pulled. The plan was wrongly rejected and the planner spent another revision cycle.

### 4. The OS held the right text the whole time

`ops.py:5994` snapshots `plan["design_doc_content"]` at submit. Two defects in one line:

* It is never passed to the reviewer (§1).
* It is read from `.claude/worktrees/<plan_wo_id>/<design_doc>`, falling back to the
  project root (`ops.py:5982-5994`) — i.e. from the **dirty worktree**. The snapshot the
  children are built from can therefore be text that exists in no commit.

**Root cause, named:** a plan question references its one load-bearing artifact by name
instead of carrying it, and the OS's own copy of that artifact is taken from a mutable
working tree. Everything else here is downstream.

## The fix

Two halves, both required. (A) removes the reason to read disk. (B) keeps the carried copy
true while the review is open. (A) alone still reviews a spec the planner has since
revised; (B) alone still leaves the reviewer reading disk.

### 5. (A) The spec travels in the question

New in `plans.py`, beside `build_plan_question` (`plans.py:463`) because that function
already owns everything the reviewer reads:

```
plans.build_plan_context(plan, source: str) -> str
```

Returns the note plus the (possibly clipped) spec text. Wording is load-bearing and is
asserted by test, not paraphrased:

```
SPEC UNDER REVIEW — <design_doc>, as committed on <source>.
This text IS the spec this plan decomposes. Judge against THIS text and nothing else.
Do NOT open this path on disk: the checkout you can reach is not the one this plan was
written against, and reading it is how question 658 was answered wrongly.
```

`source` is the human-readable provenance from §6 (`branch feat/x @ 7cd0dfb` or
`main @ fb7739d`), so the record says which commit was judged.

Three more edits:

1. `ops.submit_plan` passes it: `neo.ask(name, planner_id, question,
   context=plans.build_plan_context(plan, source), kind="plan")`.
2. `neo.build_question_prompt` labels by kind: `plan` renders `Spec under review:`; every
   other kind keeps `Work order context:` byte-identical. A spec under a label that says
   "work order context" invites the reviewer to treat it as background rather than as the
   subject.
3. `PLAN_REVIEWER_PERSONA` gains one sentence under the spec-boundaries bullet: the spec
   is supplied with the question; do not read the repository for it; if it is marked
   TRUNCATED, do not conclude the spec ends where the text ends.

### 6. Source of truth: the COMMITTED doc, one ladder, one function

New public function in `landing.py` — it already owns `worktree_of` (`landing.py:278`),
`authored` (`landing.py:290`) and `_git` (`landing.py:426`), and a copy elsewhere would be
a fourth answer to "which branch is the default", which `evidence`'s module docstring
forbids:

```
landing.committed_text(project_path, planner_wo, repo_path) -> tuple[str, str] | None
```

Returns `(text, source)` or None. The ladder, in order:

1. **The planner's branch.** `worktree_of(project_path, planner_wo)`; if it is on disk,
   `branch = authored(worktree).branch`, then `git show <branch>:<repo_path>` run in that
   worktree. Also read `git rev-parse --short <branch>` for `source`.
2. **The default branch**, in the project root: `ref = evidence.base_ref(project_path)`
   (the pinned ladder — origin/HEAD, origin/main, main, ""); `""` means give up. Then
   `git show <ref>:<repo_path>`, with `evidence.default_branch_head(project_path)` for
   `source`.
3. **Nothing readable** → None. Callers must never write `""` over a good snapshot: an
   empty `design_doc_content` breaks `specs.spec_of`, the materialised section and the
   feature agent (§11).

Every read is `git show`. The working tree is never read, per the ruling. Failures are
`landing._git`'s existing None-not-empty-string contract.

**Branch before main, and that order is the fix, not a detail.** In question 658 the older
revision was already on main while the newer one sat on the branch; main-first would have
served exactly the stale text that caused the bug. The branch is a superset in time: it is
either ahead of main (unmerged) or content-identical to it (merged).

**Accepted residual:** someone edits the spec on main after the planner's branch merged
while the feature is still in `plan_review`. Window is minutes; the branch copy is still
what the planner submitted. Not handled.

### 7. Merged vs unmerged — the check never asks

kn-ae871d91 and `landing.py:23-37`: after a squash merge the branch's own sha is not on
main, and commit reachability, ahead-count and patch-id all report merged branches as
unmerged. Any check that needs a merged/unmerged boolean is built on a predicate known to
be wrong.

So this check has no such predicate. **Which rung is READABLE decides**, and after a squash
merge both rungs carry the same bytes, so the content hash cannot tell them apart and does
not need to. The two cases that matter:

* **Planner worktree present** (unmerged, or merged and not yet reclaimed): rung 1. Same
  text either way once merged.
* **Planner worktree gone** — the planner settled and its worktree was reclaimed, so
  `worktree_of` returns None: rung 2, the default branch. This is the "read main once the
  branch has merged" half of the ruling, reached by the worktree being gone rather than by
  guessing at merge state.
* **Worktree gone and the path is not on the default branch** (never merged, branch
  deleted, or a renamed spec): rung 3 — leave the snapshot and the question alone, log
  once per feature per tick at WARNING. A stale spec the reviewer was told is the spec is
  strictly better than no spec.

### 8. The check DOES fetch, narrowly

`origin/main` going stale in the local checkout is the same class of bug being fixed here,
so rung 2 is not trusted blind. Before rung 2 only — never before rung 1, which is local by
construction — run `git fetch --quiet origin <default branch>` with a 20s timeout,
best-effort: a failure logs and falls through to the local ref.

Cost bound: rung 2 is reached only when the planner worktree is gone, only while a feature
order is in `plan_review` (rare, minutes), and at most one fetch per such feature per tick.
No fetch anywhere else in this feature.

### 9. (B) The divergence check

**Where.** Logic in `ops.py` as `ops.refresh_plan_spec(fo_id, project_name=None)`, beside
`submit_plan`, because it performs the same three writes and must not perform them
differently. The daemon hook is thin, matching `plan_features`/`settle_features`:
`Daemon.refresh_plan_specs(project, store)`.

**The seam:** `daemon.py`, in the per-project tick, immediately AFTER
`self.plan_features(project, store)` (`daemon.py:728`) and before `self.dispatch_pending`.
Why there: that is the tick's feature-order planning band — `plan_features` opens planners,
this reconciles the features those planners have already handed back — and a question asked
here is picked up by the same Neo FIFO drain on the same tick rather than one interval
late. Not on the `reconcile` or `poll_prs` cadence: `plan_review` is short-lived and the
question is often answered within a tick or two, so a 6-tick check would frequently fire
after the verdict it exists to pre-empt. Cost when nothing is in `plan_review` — the normal
case — is one indexed `list_feature_orders(statuses=("plan_review",))` returning zero rows,
and no git subprocess at all.

**Per feature in `plan_review`:**

1. Load `plan`; require `design_doc`, `design_doc_content` and `plan_question_id`. Read the
   question (`NeoStore.get`). Skip on `answered` — the decision is taken.
2. `landing.committed_text(...)`. None → §7 rung 3, stop.
3. Compare `hashlib.sha256(text.encode()).hexdigest()` against the same hash of the stored
   snapshot. Content hash, not mtime and not length: a same-length edit is exactly the
   §8-shaped revision this must catch. **No new column** — both sides are hashed per tick;
   the stored plan JSON is unchanged in shape.
4. Equal → done. This is the overwhelmingly common path and it costs one or two `git show`
   calls.
5. Differ → run `plans.spec_problems(plan, new_text)` FIRST. If the committed revision
   broke the plan (a child's `spec_section` no longer resolves, the `Agent profile`
   appendix is gone), do NOT refresh: call
   `ops.review_plan(fo_id, accept=False, feedback=…, decided_by="os")` with the problems
   listed. That returns the feature to `planning`, delivers the reason to the planner's
   existing session, and closes the question through the one path that already exists.
6. Otherwise, in this order: `neo.supersede(old_qid, …)` → store the refreshed
   `design_doc_content` → `neo.ask(..., context=build_plan_context(plan, source),
   kind="plan")` → `update_feature_order(plan=…, plan_question_id=new_qid)` →
   `store.add_event(plan_wo_id, "plan_spec_refreshed", {...})`.

**Reuse, not a second path.** Steps 6's supersede-then-ask-then-repoint is exactly what
`submit_plan` already does for a resubmission (`ops.py:6011-6023`). Extract it as
`ops._ask_plan_review(name, path, fo, plan, planner_id, source)` and have both callers use
it; the supersede reason keeps `submit_plan`'s wording shape, naming the new question id.

**Question status decides whether it may be superseded** (`neo_store.py:35-37`):

| status | action |
|---|---|
| `queued` (Neo-held) | refresh and re-ask. The case this exists for. |
| `answering` (Neo-held) | refresh and re-ask. The in-flight call is wasted; its verdict lands on a question `feature_order_for_question` (`daemon.py:3023`) no longer matches, so `_deliver_plan_verdict` drops it with a log and it cannot reach `ops.review_plan`. A verdict judged against superseded text MUST not apply. Cosmetic consequence: the drain's `record_answer` may overwrite the SUPERSEDED marker on that row; the pointer, not the row, is what protects the decision. |
| `escalated` / `failed` (user-held) | do NOT supersede and do NOT refresh. The user owns the decision, and silently pulling a question out of their queue would leave `flag_feature_attention` pointing at a closed row. Instead re-flag attention once with `the spec has been revised since this plan was reviewed — reject it and let the planner resubmit`. |
| `answered` | nothing. |

**Ordering invariant, stated so it is not traded away later:** the text the reviewer judges
and the text the children are built from are the same text. That is why step 6 never
refreshes the snapshot without also re-asking, and why step 5 rejects rather than
refreshing under a plan the new text no longer satisfies.

One Neo call per committed revision while in `plan_review` is the accepted cost (ruling
667).

### 10. Size bound, and what a clipped spec must say

New in `plans.py`: `SPEC_CONTEXT_MAX_CHARS = 40_000` (~10k input tokens).

Calibration. The 84KB questions #65–#67 were a defect because they DUPLICATED briefs the
plan already stored and the user already read; this is the one artifact under judgement and
it cannot be referenced by name — that is the bug. Measured in this repo:
`2026-08-29-spec-driven-feature-orders.md` is 153 lines (~8.6KB);
`2026-09-24-an-auto-merge-request-that-proves-itself.md` is 383+ lines (~26KB). 40,000
chars clips nothing currently in `docs/superpowers/specs/` and stays under half of #67's
pre-diet 21,250 input tokens.

**Clipping cuts at a heading boundary, never mid-sentence.** New pure function in
`sections.py` (it owns `HEADING_RE` and `extract_section`, `sections.py:48`, and has no
IO): `clip_at_heading(markdown, max_chars) -> tuple[str, list[str]]` — the kept prefix
ending at the last heading that fits, and the heading LINES omitted, verbatim.

The note then carries, above the text:

```
TRUNCATED at <n> of <m> characters. The sections below were NOT included and you have not
seen them — do not conclude the spec stops where this text stops:
  ## 10. …
  ## 11. …
```

This clause is the direct counter to the observed failure: question 658's reviewer
concluded the spec "stops at §9". A silently clipped spec the reviewer believes is complete
would recreate this bug in a new costume.

The snapshot stored in the plan is NEVER clipped — clipping is a rendering of the question
only. Children and the agent profile keep the whole text.

### 11. What else this corrects, and what must not break

`design_doc_content` has four other readers. All of them improve, none change shape:

* `specs.spec_of` (`specs.py:173`) — the worker prompt, the materialised section, the
  panel packet. They stop being built from possibly-uncommitted text.
* `worker_session.feature_agent` (`worker_session.py:141`) → `specs.install_agent`, and
  `ops.rebuild_feature_agent` (`ops.py:6170`): both rebuild from the stored plan on every
  dispatch, so a refreshed snapshot reaches the feature's agent type for free.
* `plans.spec_problems` at submit — now validated against the committed text, which is what
  the children will actually get.

**One behaviour change to call out:** `submit_plan` reading `git show` instead of the file
means a plan whose spec is written but NOT COMMITTED is refused. That is the ruling applied
at its source. The refusal must name the fix:

```
the plan names design_doc 'docs/…md' but no COMMITTED copy exists on <branch> — the
reviewer is sent the committed text, never your working tree. Commit it and resubmit:
  git add docs/…md && git commit -m "spec: …"
```

Cost to the planner: one commit it was going to make anyway (its spec is the base of the
children's stack).

### 12. Rejected alternatives

* **Tell Neo the absolute path to the planner's worktree instead of sending text.** The
  obvious cheap fix. Rejected: it keeps the reviewer's answer dependent on a filesystem the
  question does not record, it reads the DIRTY tree (against ruling 667), and it fails
  outright for a reclaimed worktree. It also cannot be replayed — `jarvis neo show` would
  still show an empty `context`.
* **Send only the sections the children name.** Cheaper, and wrong for this reviewer: the
  judgement asked for is whether the split follows the spec's seams, which requires the
  parts the plan did NOT claim. §9's absence is precisely what 658 got wrong.
* **Refresh the snapshot without re-asking.** Half the cost, and it breaks §9's ordering
  invariant: the plan would be released on a verdict about text nobody built from.
* **Re-ask on every tick while in `plan_review`.** Rejected by the hash: a re-ask per tick
  is a Neo call per 5 seconds for a document nobody touched.
* **A `design_doc_sha` column.** Rejected: a schema migration to cache a sha256 of a string
  already in the row, recomputed in microseconds.
* **Make the divergence check a work order / a new feature-order status.** Rejected: it is
  a reconciler, and `plan_review` already means exactly "a submitted plan is awaiting a
  verdict".

### 13. Tests

`tests/test_feature_orders.py` (plus `tests/test_plan_validator.py` for the pure helpers).
Now:

1. **Context carries the spec.** After `submit_plan`, the question row's `context` is
   non-empty, contains the spec's body text and the `Do NOT open this path on disk` note,
   and `neo.build_question_prompt` renders it under `Spec under review:`.
2. **The named case from issue #746** — submit, then the spec is revised and merged, then
   the review sees the merged text: submit; commit a revision; simulate the squash merge by
   putting the new text on the default branch as a fresh commit AND removing the planner's
   worktree; run one tick. Assert the old question is superseded, `plan_question_id` moved,
   and the NEW question's context contains the merged text and `main @ <sha>`.
3. **Unmerged revision, worktree present:** the refresh reads the branch copy, not main's
   older copy. This is 658's exact shape and it must not resolve to main.
4. **The dirty worktree is invisible:** an uncommitted edit to the spec produces NO
   divergence, NO supersede and NO Neo call.
5. **Uncommitted spec at submit is refused**, and the message names `git commit`.
6. **No divergence costs nothing:** across several ticks with the spec untouched, the Neo
   ask count stays at 1 (assert CALL COUNTS and question rows, per the panel testing rule).
7. **Escalated question is untouched:** divergence while `escalated` leaves
   `plan_question_id` and the snapshot alone and updates the attention line.
8. **An in-flight verdict cannot land after a refresh:** question `answering`, refresh,
   then deliver the old verdict — `ops.review_plan` is never called.
9. **Nothing readable:** worktree gone and path absent from the default branch — snapshot,
   question and status all unchanged, one WARNING.
10. **A revision that breaks the plan** (child's section deleted) rejects to the planner:
    feature back to `planning`, feedback names the section, snapshot unchanged.
11. **Clipping:** a spec over `SPEC_CONTEXT_MAX_CHARS` yields a context ending at a heading
    boundary, containing `TRUNCATED` and the omitted heading lines verbatim; the STORED
    snapshot is unclipped.
12. **Downstream intact:** after a refresh, `specs.spec_of` returns the new text and the
    feature agent rebuilds from it.

**Test trap, inherited:** the fake Neo keys `FORCE_*` markers on the QUESTION text, so test
plans must keep their markers in the plan SUMMARY (`a_plan` in
`tests/test_feature_orders.py`). The spec now also rides in `context`; a marker moved into
the spec body would not route the fake.

### 14. Not in scope

* Worker questions (`kind="question"`) — `ops.ask_question` already resolves in-text
  section references (`sections.QUESTION_MAX_CHARS`, 4000) and is a different contract.
* The child-dispatch snapshot pathway (`dispatch.materialize_design_doc`) — it consumes
  `design_doc_content` and inherits the fix unchanged.
* Feature orders already `executing`: their plan was judged, and re-judging released
  children is `jarvis fo resume`'s territory.
* Anything about WHERE the planner commits its spec, or branch stacking (backlog
  `bl-e3a88979`).
