# An assumption-review payload carries its spec section

Work order wo-308c0303, GitHub issue 1034, project `jarvis_os`.

## 1. The problem

### 1.1 The reviewer is asked to rule on a brief it is not shown

`autoreview._ruling_question` (`src/jarvis/autoreview.py:1603`, built by `autoreview.propose`,
`autoreview.py:1965`) assembles six blocks: the assumption, the work order's title plus
`description[:2000]`, what the worker says it delivered, the sibling assumptions, the decision
record, and the answer instruction. **No spec text, and no spec pointer.**

A child work order under a feature order is briefed by one section of the feature's spec. Its
`description` is deliberately only the margin around that section — `src/jarvis/ui/app.py:1615-1619`
states the rule for the dashboard ("a page that showed the brief alone would be a page missing most
of the work"). The worker gets the section (materialised by `dispatch.materialize_design_doc`,
`dispatch.py:387`). The validation panel gets the section (`evidence.collect_work_order`'s
`spec_ref` / `spec_section`, `evidence.py:587-588`). The assumption reviewer — the one seat whose
whole job is "did this decision need the user?" — gets neither.

Evidence: assumption #1 of `wo-ab6c309d` escalated with `escalation_cause`
`evidence-insufficient` (`neo_store.ESCALATION_CAUSES_CHOSEN`, `neo_store.py:105-112`), the reviewer
saying it could not see section 7.2 of `docs/superpowers/specs/2026-09-27-self-evolution.md`. That
section exists, is 667-692 of that file (~1,900 characters), and answers the question in four
numbered lines (674-679: the four conditions `rules.precheck` must satisfy). The user paid a review
action for a fact the OS already owns and did not put in the prompt.

This is the shape of GitHub issue #832, one table along. #832 was escalations caused by the packet
carrying no record of what had already been decided; the fix was `autoreview.decision_record`
(`autoreview.py:1368`) plus the persona paragraph at `autoreview.py:1313-1319`. The same omission
was left in place for the spec.

### 1.2 Two independent resolution defects under it

Adding a block is not enough, because the lookup behind it already fails on the very order that
escalated. `specs.spec_of` (`src/jarvis/specs.py:159`) resolves a child's section as:

```python
which = str(wo.get("spec_section") or "").strip()
text = sections.extract_section(content, which) if which else None
```

**(a) The column can be NULL while the plan names the section.** `work_orders.spec_section` is
written once, at plan release, by `project_store.create_plan_children`
(`project_store.py:3286-3293`, `spec_section=child.get("spec_section")`). A plan whose child item
names a section against a release path that did not pass it, or a row written before the column
existed, leaves `NULL`. `wo-ab6c309d` is such a row: its parent `fo-69ba1cc4`'s plan child names
7.2 and the work order's column is empty, so `spec_of` returns `section_text: ""` and every reader
— worker prompt, materialised section file, panel packet — is equally blind. The escalation is a
symptom of this, not of the missing block alone.

**(b) The stored spec POINTER can name a file that exists nowhere on disk.** `plan["design_doc"]`
is what `specs.spec_link` (`specs.py:183`) renders on `jarvis wo show` and the work order's page,
and is what `jarvis spec section <path> <n>` (`ops.spec_section`, `ops.py:14329`) would be given.
`fo-69ba1cc4` stores a path under the legacy two-segment prefix — `docs/` followed directly by
`specs/`, with no `superpowers/` segment — while the real file is under
`docs/superpowers/specs/`. The pointer resolves to nothing. `spec_of` still works, because the
plan carries `design_doc_content` as a snapshot, so the breakage is invisible until somebody tries
to OPEN the document.

Nothing reports (b). `tests/test_spec_location.py:44` scans TRACKED FILES for the legacy literal
and is the only guard; a dead pointer lives in an `os.db`-adjacent project database row, which no
tracked-file scan can see (`kn-ba3945bf`, `kn-818facbf`).

### 1.3 Root cause, named

The root cause is not "the payload is missing a block". It is that **the spec is treated as a
dispatch-time artifact rather than as resolvable state**: the section is resolved once, from one
column written once by one path, and the pointer to the document is stored and never checked
against the repository again. Every downstream reader inherits whatever that one write happened to
contain, and nothing re-derives or validates it. The three parts of §2 repair that directly — a
fallback plus a heal so the column is derivable rather than write-once, and an invariant so the
pointer is checked rather than assumed.

## 2. The fix

Three independent parts. (A) and (B) together close the escalation; (C) closes the pointer defect,
which is what makes the cite line in (B) openable.

### 2.1 (A) `specs.spec_of` gains a fallback and a heal

In `specs.spec_of` (`specs.py:159`), when `work_orders.spec_section` is empty, resolve the section
from the parent feature's plan item, then write it back to the row.

**The join is the TITLE, because it is the only one that exists.** `create_plan_children` passes
`title=child["title"]` and `spec_section=child.get("spec_section")` and keeps no plan key on the
row (`project_store.py:3286-3293`); `work_orders` has no `plan_key` column. So the derivable join
is the child's `title` against `plan["children"][i]["title"]`.

Neo ruled for the user on question 1690 and the rule is the mechanism, not a guideline:

* the match must be **exact** — no normalisation, no prefix, no fuzzy comparison;
* it must be **unique** — two plan children with the same title resolve to nothing;
* absent or ambiguous resolves to **nothing**, never to a guess. `section` stays `""` and
  `section_text` stays `""`, exactly as today.

**The heal.** When the fallback produced a section and the column is empty, write it:
a new narrow store method `ProjectStore.adopt_spec_section(wo_id, section) -> bool` running

```sql
UPDATE work_orders SET spec_section=?, updated_at=? WHERE id=? AND (spec_section IS NULL OR spec_section='')
```

Guarded in SQL rather than by a read-then-write in Python, so two readers racing on the same tick
cannot both decide the column is empty and one overwrite the other. It **never** overwrites a
recorded value: a user or a manager that corrected a section by hand owns it.

**Why heal rather than resolve on every read.** `spec_of` has four readers after (B) — the worker
prompt and the materialised section file (`dispatch.py:387`), the panel packet
(`evidence.py:588`, through `daemon.py:2842` and `ops.py:5539`), the dashboard
(`ui/app.py:1620`), and the assumption-review payload. Resolving per read leaves the ROW saying
one thing and four prompts saying another, so `jarvis wo show`, a `--json` consumer and a future
reader that does not go through `spec_of` keep seeing the defect. Healing makes the row true once
and makes the fallback dead code on every subsequent tick, which is also what keeps a title-based
join cheap: it runs at most once per work order, not once per prompt.

Consequence, accepted deliberately: the dashboard's work-order page can perform this write on a
GET. It is write-once, idempotent, derives the same value any other reader would, and touches no
status, no flag and no timeline event — so it is not a state transition rendered behind a page
load, it is the page agreeing with every other reader. The alternative (a `heal=True` parameter
only the daemon passes) re-creates the disagreement the single lookup exists to prevent.

`spec_of` keeps returning `None` for a planner or a manager, unchanged: three readers depend on
"a planner has no section of its own" (`specs.py:170`, `specs.py:186-189`).

### 2.2 (B) The payload carries the section, bounded

`_ruling_question` gains one block, built from the `spec_of` dict `propose` already has a `store`
to fetch:

```
# The spec section this work order was told to implement — the brief, and authority you may CITE
<repo_path> § <section>

<section text>
```

The cite line is `evidence._spec_ref`'s format (`evidence.py:649-653`) verbatim, so the reviewer's
`reason` cites a spec the same way the panel does, and the two seats cannot describe the same
section differently.

**Position: immediately after the work order's title/description block, before the delivered
block.** The description is the margin around the section; the two are one brief and read as one.
The decision record stays last, for the reason its own comment gives (`autoreview.py:1593-1595`):
it is what the reviewer reads just before it answers.

**Absent, not empty-headed, when there is no section.** A standalone work order, a planner, a
manager, a feature planned before specs existed, and an ambiguous title under (A) all produce no
block at all. A heading over `(none)` would tell the reviewer a spec exists and was withheld, which
is the opposite of true and — per the persona at `autoreview.py:1318` — a case to escalate.
Condition is `spec and spec.get("section_text")`, the same test `_spec_ref` applies.

**The budget.** New config key `validation.assumption_spec_chars`, default constant
`catalog.DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS = 6000`, parsed beside
`decision_record_chars` in `catalog._parse_validation` (`catalog.py:2183-2186`) with the same
`>= 1` refusal, held on `catalog.ValidationConfig` (`catalog.py:639`), and read through
`autoreview._spec_chars(cfg)` placed beside `autoreview._record_chars` (`autoreview.py:1921`) with
the same `getattr` fallback so pure tests with no catalog in hand get the fleet answer.

6,000 against its neighbours:

* **not 150,000** (`DEFAULT_VALIDATION_DIFF_CHARS`): that number buys a five-seat SHARED cached
  prefix. This call is uncached and per assumption.
* **not 20,000** (the worker prompt's spec_section, measured at `claude_cli.py:85-87`): that rides
  in a system prompt built once per turn and cached.
* **12,000** (`DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS`, `catalog.py:558-566`) is the right
  ECONOMICS — uncached, per assumption — and its reasoning transfers: that cap puts a whole
  rendered question at ~17-18K chars. This packet is ~10-12K today (2,000 description + 1,500
  summary + 6,000 record + the assumption and siblings), so 6,000 is the number that keeps one
  section from more than doubling the packet, and it equals the decision record's cap — the other
  evidence block in the same prompt, bounded for the same reason.
* §7.2 of the self-evolution spec, the section whose absence caused the escalation, is ~1,900
  characters: 6,000 carries it whole with 3x headroom.

Honest limit: the fleet-wide distribution of spec-section lengths was not measured for this spec.
6,000 is chosen against the neighbouring caps and one measured case, which is why it is a config
key and not a literal.

**The trim.** Keep the head, cut at a **line boundary**, append a marker line last. Never a
whole-unit drop: `kn-e06ff4e6` and
`docs/superpowers/specs/2026-10-13-a-confirmation-question-with-a-zero-character-diff.md` are the
known defect — an all-or-nothing unit over a small budget delivers zero of N characters, so the
reviewer is shown nothing and escalates for exactly the reason this change exists. A section whose
first line alone exceeds the budget is cut mid-line rather than dropped; delivering zero characters
is never an outcome.

The marker says what was dropped, by count, and where the rest is:

```
  (… 2,431 of 8,431 characters of this section omitted — capped at 6000 characters; read the rest with `jarvis spec section <repo_path> <section>`)
```

Modelled on `decision_record`'s omission line (`autoreview.py:1434-1444`): stated, last, exact
count, naming the cap as the cause. It is a `(not shown)`-class line the persona already refuses to
treat as a blank. The command it names reads from DISK, which is precisely why (C) is part of this
change: on a feature whose pointer is dead the marker would name an uncallable command.

**The reviewer instruction DOES change.** One paragraph in `ASSUMPTION_REVIEWER_PERSONA`
(`autoreview.py:1267`), beside the decision-record paragraph at `autoreview.py:1313-1319` and in
its shape:

* the spec section is the brief the user approved when they approved the plan, and the reviewer may
  cite it;
* when the section settles the assumption, ACCEPT and name `<path> § <section>` in `reason`
  instead of escalating `evidence-insufficient`;
* the omission marker is not a blank to fill in;
* a change the user would recognise still escalates whatever the spec says — the spec is authority
  about what was ASKED, never about a preference nobody recorded.

Without that paragraph the payload grows by up to 6,000 characters and the reviewer keeps
escalating for evidence quoted in its own prompt, which is the failure the decision-record
paragraph had to be written to stop. The persona is per question KIND (`neo.py:180`) and
`QUESTION_KIND` is shared with the confirmation pass, so the paragraph must be true of both — it
is: it tells the reviewer what to do with a block, and says nothing about which pass is asking.

### 2.3 (C) A stored spec pointer that resolves to no file is a reported, self-healing defect

New invariant `INV-SPEC-POINTER-REAL`, function `check_spec_pointer_resolves(store)` in
`src/jarvis/invariants.py`, registered in **`SLOW_INVARIANTS`** (`invariants.py:5229`).

Predicate, over every row of `store.list_feature_orders(statuses=None, kind=None)` whose plan has a
non-empty `design_doc`: `store.project_path / design_doc` must exist. Settled features included,
on `check_work_lands`' reasoning (`invariants.py:3791`, "hiding drops a record from listings, it
does not mean the record may go on saying something untrue") — a settled feature's spec pointer is
still rendered by `spec_link` and still has to open.

**Why `SLOW_INVARIANTS` and not `INVARIANTS`.** The tuple's docstring
(`invariants.py:5216-5228`) defines it as the checks whose cost scales with the SETTLED BACKLOG
rather than with live work, run on their own cadence and always by `jarvis doctor`. This check has
both properties of that class and neither property of the fast one: its population is every feature
order a project has ever had, and its repair shells out to `git ls-files` over the whole
repository. `INVARIANTS` runs every reconcile tick, roughly twice a minute per project; paying a
repository-wide file listing there to answer a question about a document that changes when somebody
MOVES A FILE — not when work moves — is the exact trade the slow tuple exists to refuse. Note the
counter-examples in that tuple's neighbourhood (`invariants.py:5173-5175`, `5180-5182`): a check
belongs in the fast tuple when it is a pure record read or when an hour of blind spot would matter.
Neither applies.

**Self-healing, per the pinned fleet rule that a one-off manual fix is not a fix.** When the stored
path does not exist:

* collect tracked files whose BASENAME equals `Path(design_doc).name` (`git ls-files -z` run in
  `store.project_path`);
* **exactly one** match: rewrite that plan's `design_doc` to the matched repo-relative path via
  `store.update_feature_order(fo_id, plan=db.to_json(plan))`, and yield
  `Violation(..., repaired=True, repair="repointed <fo-id>'s design_doc to <new path>")`;
* **zero or more than one** match: yield it unrepaired with an `owed` sentence naming the decision
  and the command that takes it, in `check_work_lands`' style (`invariants.py:3911-3925`) and under
  `Violation.owed`'s rule (`invariants.py:564-569`): no elapsed time and nothing that changes tick
  to tick, or the item can never be acked down.

**No path literal is hardcoded, and a hardcoded migration would be the wrong shape.** A literal
rewrite from the legacy prefix to the canonical one would fix exactly the rows that exist today and
nothing else: it would not survive a spec being renamed, or moved WITHIN the canonical directory,
or a project whose specs live somewhere else entirely — and it would put the forbidden literal into
`src/`, which `tests/test_spec_location.py:44` refuses in any tracked file. The basename repair is
generic, and migrating the recorded legacy-prefix pointers to `docs/superpowers/specs/` is a
consequence of it rather than its purpose.

**`_ReadOnly` must learn one more mutator.** `invariants._ReadOnly._BLOCKED`
(`invariants.py:5266-5277`) intercepts `set_feature_status` and `clear_feature_attention` but
**not** `update_feature_order`. Without adding it, `check_project(repair=False)` — what `jarvis
doctor` runs before the user has agreed to let it touch anything — would rewrite plans. Add
`update_feature_order` to `_BLOCKED`; the `Violation` still claims `repaired=True`, which is that
proxy's documented contract (`invariants.py:5260-5263`).

**What (C) does NOT fix.** It repairs the citable POINTER only. `spec_of` resolves text from
`plan["design_doc_content"]`, a snapshot, and keeps working whether the pointer resolves or not.
(A) and (B) fix the evidence; (C) fixes the citation.

### 2.4 Rejected alternatives

1. **Put the whole spec in the payload instead of the section.** `spec_of` already returns
   `content`. Rejected: the full self-evolution spec is ~200KB, this call is uncached and per
   assumption, and the panel's own measurement (`catalog.py:540-548`) refuses 300,000 chars on
   CONTEXT grounds for a seat with a much larger budget. The section is what the child was briefed
   with; anything more is the reviewer re-reading the feature.
2. **Give the reviewer a tool or a path instead of the text.** Rejected: the reviewer is a headless
   single-call `structured.request` with no tool loop, and the materialised section file lives
   under the project's gitignored `.jarvis/` tree, which is not a path a Neo call can read.
3. **Backfill `work_orders.spec_section` with a one-off migration and skip the fallback.**
   Rejected: a migration fixes the rows that exist at release and the next release path that forgets
   to pass `spec_section` re-creates the gap silently. The fallback plus heal is the migration AND
   the guard, and it needs no release-ordering.
4. **Normalise or fuzzy-match the title join so more rows resolve.** Rejected by the user's ruling
   on question 1690: a near-match writes the WRONG section into a column four readers trust and the
   reviewer then cites it as authority. Resolving to nothing reproduces today's behaviour, which is
   merely unhelpful.
5. **Add a `plan_key` column to `work_orders` and join on it.** The correct long-term join, and
   rejected HERE: it is a schema migration plus a change to the one write path, and it does nothing
   for the rows that already exist — which still need the title fallback. Worth filing separately;
   it would let the fallback be deleted later.
6. **Make the dead pointer an `OS_INVARIANTS` check or a `jarvis doctor`-only report.**
   Rejected: `OS_INVARIANTS` take no store (`invariants.check_os`) and cannot reach a project's
   feature orders, and a doctor-only report heals nothing until a human types the command — the
   fleet rule this change is written under.
7. **Refuse to dispatch a child whose spec section does not resolve.** Rejected: `specs.py:13-15`
   is explicit that everything in that module degrades to `None` or a no-op and that a missing spec
   may never stop a work order dispatching. A reviewer ruling blind is a worse outcome than a
   worker briefed by its description alone, but blocking the work is worse than both.

### 2.5 Tests owed

Unit level and pure wherever the function allows.

**(A) — `tests/test_feature_orders.py`**, beside the existing `spec_of` coverage
(`test_a_refreshed_spec_reaches_the_children_and_the_feature_agent`, line 1100):

* a child with `spec_section` NULL whose plan item names a section resolves `section_text`, AND the
  column is written;
* a second call changes nothing (idempotent, and the fallback is not re-entered);
* a recorded `spec_section` that disagrees with the plan is NOT overwritten and wins;
* two plan children sharing one title resolve to `""` and write nothing;
* a title in no plan item resolves to `""` and writes nothing;
* a planner and a manager still return `None`.

**(B) — `tests/test_autoreview.py`** (`_ruling_question` takes plain dicts, so all pure):

* the block is present, carries the `<path> § <section>` cite line and the section text, and sits
  between the description block and the delivered block;
* no block at all — heading included — for `spec=None` and for `section_text=""`;
* a section over the budget delivers a non-zero head, cuts on a line boundary, and ends with the
  marker carrying the exact dropped count and the cap;
* a section whose first line exceeds the budget still delivers characters;
* no marker when the section fits;
* `_spec_chars` returns the default with `cfg=None` and the project value with a `cfg`;
* `propose` passes the resolved spec through (one store-backed test, not pure);
* the persona names the spec block and the cite-instead-of-escalate instruction.

**(B) — `tests/test_catalog.py`**: `validation.assumption_spec_chars` parses, inherits the fleet
default, and a value under 1 is refused at boot with the key named.

**(C) — `tests/test_invariants.py`**:

* a plan pointing at a path that does not exist, with exactly one tracked file of that basename →
  one `Violation`, `repaired=True`, and the stored plan now holds the new path;
* two tracked files of that basename → unrepaired, non-empty `owed`, plan untouched;
* no tracked file of that basename → unrepaired, non-empty `owed`;
* a pointer that resolves → no violation;
* a feature with no plan and a plan with no `design_doc` → silent;
* `check_project(repair=False)` reports it and leaves the plan byte-identical (the `_ReadOnly`
  addition);
* membership: the check is in `SLOW_INVARIANTS` and absent from `INVARIANTS`.

### 2.6 Out of scope

* **`_confirm_question` (`autoreview.py:1890`) does not gain the block in this change.** Both
  passes share `QUESTION_KIND` and the persona, so the confirmation reviewer can escalate
  `evidence-insufficient` for the same reason, and the same block under the same key would be a
  few lines. It is excluded because that packet already runs ~17-18K chars with a 12,000-char diff
  and a second 6,000-char cap on it is a budget question that wants its own measurement. Named here
  as the follow-up rather than left implicit.
* A `plan_key` column on `work_orders` (alternative 5).
* The dashboard rendering the section on a work order's page: `ui/app.py:1615-1619` decided
  deliberately not to, and nothing here reopens it.
* Anything about how a planner CHOOSES the `design_doc` path it stores. (C) repairs the recorded
  value; the write path is another order's question.

## Appendix: notes for the implementer

* `autoreview` importing `specs` is a downward edge: `specs` imports only `db` and `sections`, with
  `ProjectStore` under `TYPE_CHECKING`.
* `sections.extract_section` (`sections.py:73`) matches through `find_heading`; "7.2" resolving is
  that function's behaviour and this change adds no matching rule of its own.
* The heal and the invariant repair are the only writes in this change. Neither sets a status,
  raises a flag, or appends a `wo_events` row.
