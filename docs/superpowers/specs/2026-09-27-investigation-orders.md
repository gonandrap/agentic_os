# Investigation orders — a read-only order that diagnoses a stuck subject and classifies it

Work order: wo-4beada49. Companion (not specified here): fleet health, which auto-opens
investigations for non-progressing orders and will be this feature's main caller.

Siblings this leans on, read them before implementing:
`docs/superpowers/specs/2026-09-23-improvement-orders.md` (the record shape being reused),
`docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md` §4/§7/§8 (the PreToolUse
refusal pattern).

---

## 1. The problem: nothing in the OS can diagnose a stuck order, and the one agent shaped like a diagnostician is unenforced

### 1.1 A human ran the loop by hand for two days

Across 2026-09-26/27 the operator repeatedly did the same manual loop — look at an order
that is not progressing, read its timeline / validation rounds / PR, find the mechanism,
then decide whether to file a fix. Six root causes found that way, each already on the
tracker:

| Issue | Root cause found by hand |
|---|---|
| #786 | a stale "the panel gave up" hold kept an order parked after the cause had cleared |
| #788 | a Neo question that could never be delivered was rendered as in flight |
| #790, #795 | stale-base auto-merges turned `main` red |
| #793 | nothing in the OS noticed that `main` was red |
| #797 | a 152K-char diff that could never fit in a question to Neo |
| #806 | catch-up merges burned validation rounds |

Two facts about that list are the actual problem statement. First: every one of those
diagnoses came from records the OS already exposes through its own CLI — `jarvis wo show`,
`jarvis validation show`, `jarvis inspect`, `gh pr view` — so the work was not privileged,
it was just unautomated and paid for in the user's attention. Second, and worse, one
filing was a DUPLICATE: #792 duplicated #790. The manual loop has no dedupe step, and the
operator is the dedupe step.

### 1.2 The existing order kinds cannot do this job

* A **work order** is defined by shipping: `dispatch.build_worker_prompt`
  (src/jarvis/dispatch.py:301) composes the worker contract, whose core is a worktree, a
  branch and a pull request. Dispatching a diagnosis as a work order tells the session to
  open a PR, which is the one thing a diagnosis must not do.
* A **feature order** decomposes an ask into children. There is nothing to decompose.
* An **improvement order** is the closest and is still the wrong tool, by its own design:
  it takes the user's `--ref` evidence, produces up to `findings.MAX_FINDINGS` findings,
  and **files nothing until the user decides each one**
  (`ops.review_findings`, src/jarvis/ops.py:6316). That is correct for "the OS misbehaves
  and I want the cause argued", and wrong for "wo-X has not moved in six hours": it spends
  user attention on exactly the case whose whole value is that it does not.

### 1.3 The analyst's no-write rule is prose, and the codebase says so in writing

`dispatch._analyst_prompt` (src/jarvis/dispatch.py:676) is the nearest prompt precedent,
and its docstring at src/jarvis/dispatch.py:687-691 is the evidence that this WO exists:

> "You analyse, you do not build" is PROSE, not enforcement, and that is worth stating
> plainly rather than leaving for someone to discover. Same reason as the planner's: an
> analyst is a work order, not a subagent, so its only lever is the `permissions.deny`
> path `_write_worker_settings` writes — and a deny broad enough to stop product code
> also stops it writing the `report.json` it is REQUIRED to submit.

The prompt repeats it at src/jarvis/dispatch.py:799-802 ("This is stated as prose and
nothing enforces it"). So today the OS has an agent kind whose defining constraint is
un-enforced, and the settings-level `permissions.deny` was ruled out for a real reason: it
cannot express "no writes except one file".

`hooks.crew_edit_decision` (src/jarvis/hooks.py:435) already proves the shape that CAN
express it — a kind-scoped, path-scoped refusal keyed on `hooks.WO_KIND_ENV`
(src/jarvis/hooks.py:348), exempting `.jarvis` paths at src/jarvis/hooks.py:463-464 — and
its docstring at src/jarvis/hooks.py:443-446 names the hole this spec must close rather
than inherit:

> The hole, stated in the spec and worth restating here: Bash is not denied, so a heredoc
> or `sed -i` writes a file anyway. A wall for the tool surface, a speed bump for the
> shell.

A speed bump is acceptable for a lead that is supposed to ship code through its crew. It
is not acceptable for a kind whose entire contract is "never changes code", because for
this kind the shell is not an escape hatch, it is the primary tool: the investigator lives
in `git log`, `gh pr view` and `jarvis … show`.

### 1.4 Adding a work-order kind is a three-edit change that looks like a two-edit change

`WO_KINDS = ("worker", "planner", "manager", "analyst")` at src/jarvis/project_store.py:282.
Per kn-31f0f450, adding a member costs more than the tuple:

1. the tuple itself;
2. `dispatch.build_worker_prompt` branches `planner / manager / analyst / else`
   (src/jarvis/dispatch.py:320-325) — a fifth kind falls to `else` and **silently receives
   the worker contract**, i.e. is told to open a pull request;
3. `supervisor._what_it_is` (src/jarvis/supervisor.py:299-329) branches the same way and
   would describe the new kind to the burning-turn judge as "an ordinary work order",
   which inverts what normal looks like for a read-only session;
4. `tests/test_stores.py:332` asserts the tuple **exactly**
   (`test_the_manager_and_the_analyst_are_the_only_other_work_order_kinds`), so a fifth
   kind is a red unit job while every file the author touched is green.

`ops._require_kind` (src/jarvis/ops.py:6085) and `project_store.is_feature_order_id`
(src/jarvis/project_store.py:459, `startswith(("fo-", "io-"))`) have the same property one
level up for `FO_KINDS` (src/jarvis/project_store.py:443).

---

## 2. The fix: a fifth work-order kind whose no-write rule is a hook, and whose verdict is settled by `ops`, not by the model's diligence

Three moving parts, in the order of how much they matter:

1. **Enforcement in `hooks.py`.** The `investigator` kind cannot write (one exempt path)
   and cannot run a mutating command (one narrowed allowlist). Prose is a backstop, not
   the mechanism.
2. **A structured verdict `ops` acts on.** The investigator submits a verdict naming what
   it wants; `ops` performs the duplicate check and the filing itself, and settles the
   order. Four classifications, one of which raises attention.
3. **The record shape is the improvement order's, re-used verbatim** — a `feature_orders`
   row with `kind='investigation'` plus one child work order of `kind='investigator'`.

### 2.1 Record shape (decided — Neo question 1)

A `feature_orders` row, `kind='investigation'`, id prefix `inv-`, plus exactly one child
work order `kind='investigator'` linked through `feature_orders.plan_wo_id`. Everything the
improvement order already does is done by the same function with the other kind asked for.

Edits, each beside its `improvement` sibling:

* `FO_KINDS` (src/jarvis/project_store.py:443) gains `"investigation"`. The `kind` column
  already exists with `DEFAULT 'feature'` (src/jarvis/project_store.py:1170), so the
  migration is free — same argument as §2.1 of the improvement-orders spec.
* `create_feature_order` (src/jarvis/project_store.py:2449) already special-cases the
  prefix at line 2461: `db.new_id("io" if kind == "improvement" else "fo")`. Replace that
  conditional with a small `FO_ID_PREFIXES = {"improvement": "io", "investigation": "inv"}`
  mapping, defaulting to `fo`. A third arm of a two-arm ternary is where a fourth kind
  goes wrong.
* `is_feature_order_id` (src/jarvis/project_store.py:459) **must learn `inv-`**. It exists
  because three sites had grown their own `fo-` literal; missing it here is a bug that
  surfaces as `jarvis search`, `jarvis cost` and `jarvis inspect` not recognising a valid
  id. `inv-` does not collide with `io-` under `startswith` — that is why the prefix is
  `inv` and not `in`.
* `FO_STATUS_LABELS` (src/jarvis/project_store.py:449) gains an `investigation` entry:
  `planning` -> `investigating`, `plan_review` is **not** used by this kind (see §2.4).
* `ops._require_kind` (src/jarvis/ops.py:6085) currently picks its message from a two-kind
  world ("an improvement order" / "a feature order"). Replace the `other` derivation with
  a `{kind: phrase}` table so a third kind reads correctly.

Statuses are the existing `FO_STATUSES` (src/jarvis/project_store.py:416), unchanged and
with no new member: `pending` -> `planning` (investigator running) -> `completed`.
`plan_review` is skipped on purpose — **submitting the verdict settles the order
directly**, there is no `needs_review`. `failed` and `cancelled` behave as they do for an
improvement order.

### 2.2 The `investigator` work-order kind

`WO_KINDS` (src/jarvis/project_store.py:282) gains `"investigator"`. Then, and in the same
commit or CI is red for the wrong reason:

* `dispatch.build_worker_prompt` (src/jarvis/dispatch.py:320-325): a fourth branch
  `if wo.get("kind") == "investigator": return _investigator_prompt(...)`, placed with the
  other three so nothing about the worker contract is reachable. Update the docstring's
  "Four kinds of work order get four shapes" to five.
* `supervisor._what_it_is` (src/jarvis/supervisor.py:299-329): a branch beside the
  `analyst` one, and it says the sharper thing — an investigator is expected to be
  read-heavy AND its writes are refused by a hook, so a long stretch of retried denied
  tool calls is the abnormality worth naming.
* `tests/test_stores.py:330-340`: extend the exact-tuple assertion and add
  `create_work_order(..., kind="investigator")` beside the manager and analyst rows.
  Renaming that test is part of the change.
* `dispatch._write_worker_settings` (src/jarvis/dispatch.py:177) already exports
  `JARVIS_WO_KIND` for every kind, so **no dispatch change is needed for the hook to see
  the kind**. This is the single fact that makes the hook approach cheap.

### 2.3 `dispatch._investigator_prompt` — beside `_analyst_prompt`

New function in src/jarvis/dispatch.py immediately after `_analyst_prompt`
(src/jarvis/dispatch.py:676-829), static like it (no spec, so `specs.install_agent` is
never called), ending with `_common_briefing` so it inherits the knowledge index and the
Serena-before-grep navigation ranking. No planning seats, for `_analyst_prompt`'s reason
(`bootstrap.install_agent_assets` gives those to `kind == "planner"` only).

What it must say, and what is different from the analyst's:

1. **The subject and the symptom, verbatim**, plus the exact commands that read the
   subject: `jarvis wo show <subject>` / `jarvis fo show <subject>`,
   `jarvis validation show`, `jarvis inspect`, `jarvis cost`, `jarvis alarms`,
   `jarvis gate list --pending` / `gate show`, `jarvis neo list` / `neo show`,
   `jarvis doctor`, `jarvis search`, `jarvis issues`, `gh pr view|diff`,
   `git log|show|diff`. Reuse `_analyst_prompt`'s "Read the evidence through the CLI,
   never the databases" block (src/jarvis/dispatch.py:724-736) — same words, extended.
2. **The four classifications, with the discriminator for each** (§2.4). A classification
   is a decision about the SUBJECT, not about the codebase in general.
3. **Evidence is quoted verbatim or the verdict is refused by the validator.** Reuse
   `_analyst_prompt`'s wording at src/jarvis/dispatch.py:743-747, and say that the
   validator enforces `findings.MIN_QUOTE_CHARS`.
4. **The terminal action**: `jarvis investigate verdict <inv-id> --from-file verdict.json`,
   which IS its `jarvis wo finish` (it must not call `wo finish`). `--from-file` for
   `_planner_prompt`'s documented reason (src/jarvis/dispatch.py:410-415): the gate
   classifier's quote-blanking fails on nested and mixed quoting, and a verdict is full of
   repo paths and quoted log lines. `verdict.json` in the worktree root, and the prompt
   says **that filename exactly** — it is the one path the write hook exempts (§2.6).
5. **It does not file anything, and it says why.** A GAP verdict NAMES the bug to file;
   `ops` files it after a duplicate search. The prompt must not tell the investigator to
   run `jarvis bug report` (see §2.5, and §3 correction 1).
6. **Bounded inputs.** Never paste a diff, a transcript or a log into the verdict: cite
   the command and quote the decisive line. A verdict over
   `verdicts.MAX_VERDICT_CHARS` is refused with the fix named, on
   `sections.QUESTION_MAX_CHARS`'s pattern.
7. The operating-contract tail is `_analyst_prompt`'s: Neo as first responder
   (`jarvis wo ask`), `jarvis wo assume` for a call made with no doubt, "the work order
   record IS this conversation".

### 2.4 The verdict, and where the classification is decided

Submitted document (the schema the prompt prints and `verdicts.parse_verdict` enforces):

```json
{
  "subject": "wo-4beada49",
  "classification": "GAP",
  "root_cause": "one paragraph, in mechanism terms",
  "evidence": [
    {"source": "jarvis wo show wo-4beada49", "quote": "the verbatim line"}
  ],
  "proposed_fix": {
    "title": "what the bug report is titled",
    "description": "the brief, standing alone",
    "expected": "what should happen",
    "actual": "what happens",
    "priority": "high"
  },
  "user_owes": "which assumption, gate or decision, by id",
  "unsticks": {"what": "the mechanism that clears it", "when": "next reconcile tick"},
  "duplicate_of": "#790"
}
```

Field requirements by classification — the one structural rule of the validator, because
a verdict whose payload does not match its classification is the failure that most looks
like success:

| classification | required beyond the common four | forbidden |
|---|---|---|
| `GAP` | `proposed_fix` (all five fields) | `user_owes`, `unsticks`, `duplicate_of` |
| `WAITING_ON_USER` | `user_owes`, naming an id | `proposed_fix`, `unsticks` |
| `TRANSIENT` | `unsticks.what` + `unsticks.when` | `proposed_fix`, `user_owes` |
| `ALREADY_TRACKED` | `duplicate_of` (issue `#n`/URL, or a wo-/fo-/io-/inv- id) | `proposed_fix` |

`subject`, `classification`, `root_cause` and at least one `evidence` entry are required
for all four. `subject` must equal the order's recorded subject — a verdict about
something else is not a verdict about this order.

What `ops` then writes onto the stored document, and never the investigator:
`filed` (`{"issue_url": …, "wo_id": …}` or `null`), `classified_by` (`"investigator"` or
`"ops"`), `submitted_classification` (kept when ops downgrades, §2.5), `filing_error`.

**Where the validator lives: a new leaf module `src/jarvis/verdicts.py`, beside
`findings.py`, not inside it.** `findings.parse_report` is a multi-finding document with
`MAX_FINDINGS`, per-finding `key` slugs and `proposed_orders`; a verdict is exactly one
diagnosis with a classification-dependent shape. One parser for both would branch on
document shape in every function — `_field_problems`, `_parse_orders`, `render_*` — which
is how a validator stops being more trustworthy than the thing it checks (the property
`plans.py` is held to). What IS shared is the evidence rule: promote
`findings._parse_evidence` to a public `findings.parse_evidence` and have `verdicts` import
it plus `findings.MIN_QUOTE_CHARS`. Two leaves, one dependency edge, one definition of what
a quote is. `verdicts.py` mirrors findings' surface: `CLASSIFICATIONS`,
`MAX_VERDICT_CHARS`, `VerdictError` with a `problems` list (every problem named at once, so
one revision fixes all of them), `parse_verdict`, `render_verdict`, `settle_headline`.

### 2.5 `ops` does the duplicate check and the filing (decided — my ruling, recorded as an assumption on wo-4beada49)

The mandatory duplicate check happens in `ops` AT FILING TIME. It is not left to the
investigator prompt's diligence, because **a prompt instruction cannot be verified and a
search in `ops` can be tested** — and #792/#790 is the standing proof that the diligent
version fails.

`ops.submit_verdict(inv_id, doc, project_name=None)`, modelled step for step on
`ops.submit_findings` (src/jarvis/ops.py:6237) — and the order of the steps is the design,
for that function's stated reason:

1. `find_feature_order` + `_require_kind(fo, "investigation", "jarvis io report")`.
2. Refuse unless the order is `planning`. There is no `plan_review` for this kind, so
   there is no resubmission window: the verdict settles the order, and a second verdict is
   refused by the status check rather than by a special case.
3. `verdicts.parse_verdict(doc)` FIRST. A bad verdict costs the investigator one revision
   and nothing else — nothing stored, no attention spent, no state to unwind.
4. **If `classification == "GAP"`: the duplicate check, then the filing.**
   * Tracker: `issues.follow_ups_filed`'s pattern (src/jarvis/issues.py:535-561) — a
     `gh issue list --state all --search "<terms> in:body"` read. `--state all` is
     load-bearing here for the same reason it is there: a closed duplicate that drops out
     of the answer is re-filed for ever. Search terms come from the subject id and the
     `proposed_fix.title`.
   * Live orders: `search.search(query, kinds=…)` (src/jarvis/search.py:120) for work and
     feature orders, plus `issues.live_work_order` (src/jarvis/issues.py:1260) for "an
     order is already on this issue".
   * A hit -> **the verdict is recorded as `ALREADY_TRACKED`** with `duplicate_of` set to
     what was found, `classified_by: "ops"` and `submitted_classification: "GAP"`. The
     record must never read as though the investigator classified it that way; and it must
     never read as though the investigator was wrong, because finding the duplicate was
     never its job.
   * No hit -> `bugreport.report_bug(..., expedite=True)` (src/jarvis/bugreport.py:394),
     called **as a Python function from `ops`**, not by an agent shelling out. `filed` is
     set from its return value (`url`, and `pickup["wo_id"]`).
5. Store the document (`update_feature_order(plan=…)`), `set_feature_status(inv_id,
   "completed")`, add a `verdict_submitted` event on the investigator's work order, and
   finish the investigator with `finish(...)` — conditionally, exactly as
   `submit_findings` does at src/jarvis/ops.py:6306-6312, because settling an
   already-settled work order is not an idempotent no-op in this codebase.
6. **Attention: raised for `WAITING_ON_USER` only** (decided — Neo question 1), via
   `store.flag_feature_attention(inv_id, verdicts.settle_headline(...))` naming what the
   user owes. Raised HERE, at the transition, and nothing re-derives it on a tick —
   kn-089de524: a flag written on a re-deriving path re-raises itself and overwrites the
   user's ack. The other three classifications settle silently.

**THE `--expedite` CASCADE IS INTENDED, AND IS THE HIGHEST-CONSEQUENCE THING IN THIS
SPEC.** Per kn-efaad866 and kn-ffb94e3a, `jarvis bug report --expedite` files a GitHub
issue AND dispatches a work order on it immediately AND ships a release when that fix
lands — at any priority, whatever Neo later says about the rating. A GAP verdict therefore
commits the fleet to a release. That is the point: the six issues in §1.1 were all
"something is broken and nobody noticed", and a fix that lands but does not ship leaves
production broken. Three things bound it: the duplicate check in step 4 (so the cascade
cannot fire twice for one cause), the requirement that `proposed_fix` carry a real
`expected`/`actual` (`report_bug` refuses without them), and the per-order budget in §2.8.
A red `main` already holds the release, and Neo still re-assesses the rating
independently.

**The one attention case that is NOT a classification: a filing failure.** `gh`
unreachable in step 4 must not settle the order silently. Follow `review_findings`'s
`stuck` path (src/jarvis/ops.py:6484-6513): record `filing_error` on the document, keep the
order in `planning`, `flag_feature_attention` naming the error and the retry command, and
raise a `CentralStore.add_inbox` warning row. An error is not a verdict, so this does not
touch the "attention only for WAITING_ON_USER" ruling.

### 2.6 No-write enforcement: two new hooks (decided — Neo question 2)

Both key on `env.get(WO_KIND_ENV) == "investigator"` (src/jarvis/hooks.py:348), which
dispatch already exports (src/jarvis/dispatch.py:177), and both are pure functions of
`(payload, env)` like every other decision in that module.

**`hooks.investigator_write_decision`**, placed immediately after `crew_edit_decision`
(src/jarvis/hooks.py:435) so the two kind-scoped write refusals are read together. Denies
`Write`, `Edit` and `NotebookEdit` for this kind, exempting exactly one path: the
worktree's `verdict.json`, and **only for the `Write` tool**. `Edit` on `verdict.json` is
refused too — a verdict is written whole, and allowing `Edit` would mean a hook that must
reason about a fragment, which is the argument `spec_shape_decision`
(src/jarvis/hooks.py:488-489) already makes for being `Write`-only. Path resolution is
`crew_edit_decision`'s: `Path(file_path).resolve().relative_to(Path(cwd).resolve())`, and
`rel != Path("verdict.json")` is the refusal. Unlike `crew_edit_decision` this does NOT
exempt `.jarvis` — an investigator has no generated state to own.

**`hooks.investigator_bash_decision`**, in the `Bash` branch, **before the
`is_jarvis_command_chain` auto-allow at src/jarvis/hooks.py:871** and after
`background_task_decision`. The ordering argument is the one `preflight_decision`'s
docstring (src/jarvis/hooks.py:835-842) already makes for the two PR checks and the one
the `finish_summary_decision` call site makes at src/jarvis/hooks.py:860-862: a denial
placed after the auto-allow is unreachable, because that auto-allow waves through every
`jarvis` verb and, with it, every mutating one.

This is the half `crew_edit_decision` deliberately does not have, and it is why a hook and
not a `permissions.deny`: without it, `cat > verdict.json <<EOF` is the least of it —
`sed -i` on product code and `git commit` both go straight through.

It denies unless the command satisfies one of:

* `gate_rules.reads_only(command)` (src/jarvis/gate_rules.py:597) — the existing
  structural primitive, all-or-nothing across the pipeline, which already refuses command
  substitution, shell invokers and unterminated heredocs. It covers `cat`/`grep`/`jq`/
  `sed -n` and correctly refuses `sed -i`.
* a `(program, subcommand)` pair in a new `hooks.INVESTIGATOR_READS` table. **`reads_only`
  does not cover `git` or `gh` and must not** — `gate_rules._READERS`
  (src/jarvis/gate_rules.py:300-306) excludes them by the rule "membership is decided by
  what a tool CAN do": `git` pushes. So the investigator's table is keyed on the pair, the
  concept `gate_rules._SUBCOMMAND_TOOLS` (src/jarvis/gate_rules.py:330) already names —
  "`git` is not a thing you do, `git push` is". Contents: `git log|show|diff|status|blame|
  rev-parse|rev-list`, `gh pr view|diff|checks`, `gh issue view|list`, `gh run view|list`.
* a `jarvis` chain whose every segment's verb is permitted. `is_jarvis_command_chain`
  (src/jarvis/hooks.py:29) **must be narrowed for this kind and only for this kind**: it
  answers "is this a chain of `cd`/`jarvis` segments", which for every other kind is the
  right question. Add a sibling primitive `hooks.jarvis_verbs(command) ->
  tuple[tuple[str, str], ...]` returning each segment's `(verb, subverb)`, reusing the
  same `_SHELL_DANGEROUS` / `shlex` parse, and keep `is_jarvis_command_chain` untouched so
  no other kind's behaviour changes. Permitted for an investigator: every read verb
  (`status`, `wo show|list`, `fo show|list`, `io show|list`, `investigate show|list`,
  `validation show`, `inspect`, `cost`, `alarms`, `doctor`, `search`, `issues`, `gate
  list|show|explain|rules`, `neo list|show|learnings`, `learn show|list|search|topics|
  stats`, `config wiring`, `brief`, `inbox`), plus exactly four mutations: `jarvis wo ask`,
  `jarvis wo assume`, `jarvis learn add`, and `jarvis investigate verdict`. Anything else
  is denied.

Both denials name the alternative in their reason string, as every refusal in this module
does: the write denial says "put your verdict in `verdict.json` and submit it with
`jarvis investigate verdict <inv-id> --from-file verdict.json`; you change no other file",
the Bash denial says which command it refused and that an investigation reads.

MCP tools are not `Bash` and are unaffected: Serena reaches the investigator through
`dispatch.serena_allow_rules()` (src/jarvis/dispatch.py:51), which grants **read-only**
Serena tools under both prefixes. Never grant Serena wholesale — it ships
`execute_shell_command`, `create_text_file` and `replace_symbol_body`, which would hand
back everything the two hooks just took away.

### 2.7 CLI and the daemon seam (decided — Neo question 3)

`jarvis investigate <subject> --why '...'` creates; `jarvis investigate list|show|cancel`
and `jarvis investigate verdict` are the sub-verbs. The shipped surface is one word,
`investigate`, with a closed set of sub-verbs (create/list/show/cancel/verdict), so the
crib sheet has a single entry.

**The CLI is a thin wrapper over `ops` functions and holds no logic**, because the
companion fleet-health order is the main caller and calls them directly from the daemon —
never by shelling out to `jarvis`. Each function beside its improvement-order twin in
src/jarvis/ops.py:6099-6516:

```python
def create_investigation_order(project_name: str, subject: str, why: str,
                               budget_usd: float | None = None,
                               origin: str = "jarvis") -> dict[str, Any]: ...   # beside create_improvement_order:6099
def list_investigation_orders(project_name=None, include_settled=False): ...    # beside list_improvement_orders:6141
def show_investigation_order(inv_id, project_name=None): ...                    # beside show_improvement_order:6149
def cancel_investigation_order(inv_id, project_name=None): ...                  # beside cancel_improvement_order:6227
def submit_verdict(inv_id, doc, project_name=None): ...                          # beside submit_findings:6237
```

`create_investigation_order` resolves the subject, stores it in
`feature_orders.metadata` under a new `SUBJECT_KEY` (the `EVIDENCE_REFS_KEY` pattern,
src/jarvis/ops.py:6075), and applies two refusals of its own:

* **At most one live investigation per subject.** An `ops`-level refusal, not a prompt
  rule: scan `store.list_feature_orders(kind="investigation")` for a non-terminal row
  whose `SUBJECT_KEY` matches and raise `OpsError` naming that `inv-` id. Without it the
  companion order opens one per tick.
* **An investigation never investigates an investigation.** Refuse a subject whose kind is
  `investigation` (an `inv-` id) or whose work-order kind is `investigator`. Diagnosing the
  diagnostician is a loop with a budget attached.
* `why` is required, for `create_improvement_order`'s stated reason
  (src/jarvis/ops.py:6114-6119): the investigator's first reader is a fresh session with
  no memory of the conversation that produced it.

The daemon opens the investigator exactly as it opens an analyst — a third sibling loop in
`Daemon.plan_features` beside src/jarvis/daemon.py:1044 and src/jarvis/daemon.py:1063,
`list_feature_orders(statuses=("pending",), kind="investigation")`, creating one
`kind="investigator"` child with `parent_id=fo["id"]`, then `update_feature_order(plan_wo_id=…)`
and `set_feature_status(…, "planning")` **last**, which is what makes a crashed tick
re-file rather than strand the order with no investigator. A sibling loop rather than one
loop over three kinds, for the reason that docstring already gives at
src/jarvis/daemon.py:1039-1042: `list_feature_orders` filters kind POSITIVELY.

### 2.8 Cheap by default

An investigation reads records; it should cost cents, and an investigation that costs more
than the order it is diagnosing is not worth having.

* Budget: `budget.feature_default_for` (src/jarvis/budget.py:639) as
  `create_improvement_order` does (src/jarvis/ops.py:6135-6136) — the family here is the
  order plus its one investigator, so the family arithmetic is already correct — but
  falling back to a NEW catalog field `investigation_budget_usd` on
  `catalog.WorkerDefaults` (src/jarvis/catalog.py:356-369) with a **non-None shipped
  default**. This is the one place this spec departs from "no ceiling is the default":
  the caller is a daemon loop, not a human typing, so an uncapped default is an uncapped
  loop. Resolution order: explicit `--budget`, then `investigation_budget_usd`, then
  `feature_budget_usd`.
* Model and effort: a new `investigation_model` / `investigation_effort` on the same
  dataclass, defaulting to a cheap model at low effort, resolved where
  `dispatch` already resolves per-kind model choices. A diagnosis is reading and
  quoting, not design.
* Bounded inputs: `verdicts.MAX_VERDICT_CHARS`, and the prompt rule in §2.3 item 6.

### 2.9 Dashboard

`/inv/{name}/{inv_id}` in src/jarvis/ui/app.py, beside the improvement-order route at
src/jarvis/ui/app.py:1010, rendering a new
`src/jarvis/ui/templates/investigation_order.html` mirrored on
`src/jarvis/ui/templates/improvement_order.html`. The page leads with the classification
and the subject, then the root cause, then the evidence table, then what was filed (issue
URL + work order id) or why nothing was.

Everything it renders comes off `ops.show_investigation_order` — a second resolution in
the route would make the page and `jarvis investigate show` two answers to one question
(src/jarvis/ui/app.py:1016-1018). **No POST action**: there is nothing to decide. That
absence is the visible difference from the improvement-order page and is what "the order
settles itself" means on a screen. The project page lists open investigations beside
feature and improvement orders (src/jarvis/ui/app.py:926-928).

Cost rollup needs nothing new once `is_feature_order_id` knows `inv-`:
`bill.for_feature_order` already rolls up a row and its children.

### 2.10 The CLAUDE.md crib entry

One entry in the `## Command crib sheet` fenced block of
/home/gonzalo/workspace/agentic_os/.claude/worktrees/wo-4beada49/CLAUDE.md, positioned
**immediately after the `jarvis io …` group and before `jarvis backlog promote`** — the
improvement order is its nearest neighbour and the two are the pair a reader must tell
apart ("io wants the cause argued and files nothing until you decide; inv settles itself").

**Length cap: 12 lines, comment column included** — comparable to the `jarvis issues
start` entry and shorter than the `io create`/`io review` pair. Constraint, from this
repo's own CLAUDE.md: `evals/llm/test_jarvis_judgment.py:24` loads CLAUDE.md as a bare
system prompt with no repo context and LLM-grades the operator persona across 14
scenarios, so the operator content must stay FIRST and DOMINANT. The crib sheet is already
the longest section; adding a fifteenth order kind at the bottom of it is where that
balance tips. Two rules for the author: the entry goes in the existing block (no new
heading, nothing above the prime directives), and it must say what the four
classifications are and that GAP expedites — an operator who does not know that does not
know what the command costs. Do not draft it in this spec; drafting it here would put two
versions of the operator text in the repository.

---

## 3. Where this spec corrects the brief it was given

1. **`jarvis bug report` must NOT be in the investigator's allowlist.** The brief lists it
   (with `jarvis issues start`) among the permitted mutations. It cannot be, given the
   ruling in §2.5: `ops` files the expedited bug after its own duplicate search, so an
   investigator holding `jarvis bug report` has a route that bypasses the duplicate check
   entirely — which is the #792 defect, re-introduced through the allowlist. Same for
   `jarvis issues start`, which dispatches a work order. Both are denied; `ops` calls
   `bugreport.report_bug` in-process instead. Consequence to state in the prompt: an
   investigator that hits a bug in Jarvis OS **while investigating** cannot use its
   `report-jarvis-bug` skill and must put that in the verdict instead.
2. **`dispatch.build_worker_prompt` branches on four kinds, not three.** The brief said
   "branch planner / manager / else"; src/jarvis/dispatch.py:320-325 already has an
   `analyst` arm. The failure mode is unchanged (`else` gives the worker contract) but the
   edit is a fourth arm, not a third.
3. **`is_feature_order_id` (src/jarvis/project_store.py:459) is a fourth edit site the
   brief did not name**, alongside the `WO_KINDS`/`FO_KINDS` work. It is a one-line change
   whose omission breaks `search`, `cost` and `inspect` for every `inv-` id.
4. **One more attention case than the ruling covers**: a GAP whose filing fails (§2.5).
   Not a re-litigation of "attention only for WAITING_ON_USER" — a `gh` outage is not a
   classification.

## 4. Rejected alternatives

* **Extend the improvement order with a "fast path" flag instead of a new kind.** Loses
  on the enforcement that is the point: `kind='improvement'` is the analyst's, whose
  no-write rule is prose (src/jarvis/dispatch.py:687-691), and a flag that changes what a
  PreToolUse hook enforces would mean the hook reading the database on every Bash call to
  learn whether this session may write. `JARVIS_WO_KIND` is already in the environment.
* **`permissions.deny` in the worker settings instead of a hook.** Ruled out by the code,
  not by preference: src/jarvis/dispatch.py:687-691 records that a deny broad enough to
  stop product code also stops the one file the session must write, and deny rules cannot
  express "no writes except `verdict.json`". They also say nothing about Bash.
* **Declare the investigator a subagent with `tools: Read, Grep, Glob`** — the enforced
  posture the feature-order seats use. Loses because an investigation is a work order: it
  needs its own record, its own budget, its own timeline and a session that can be
  messaged, and a seat has no `Bash`, so it could not read `git log`, `gh pr view` or any
  `jarvis` verb — i.e. could not read the evidence at all.
* **Inline verdict flags (`--classification … --root-cause …`) or stdin.** Both rejected
  for the improvement order's documented reason (src/jarvis/dispatch.py:410-415,
  src/jarvis/issues.py:503-505): `gates.scannable()`'s quote-blanking fails on nested and
  mixed quoting, which a verdict full of quoted log lines guarantees; and a PreToolUse
  hook cannot see stdin, so the write hook could not tell a verdict submission from any
  other write. `--from-file`.
* **Leave the duplicate check to the prompt** (the obvious fix, and what a reviewer will
  propose). It is what the human loop did and #792 is the result. A prompt instruction has
  no failing test; `issues.follow_ups_filed`'s read has one already.
* **Let the investigator write the fix when it is small.** This is the improvement
  order's founding argument (src/jarvis/dispatch.py:714-717): the cheapest fix that turns
  a symptom green keeps winning. A kind that may sometimes ship is a kind whose no-write
  hook has to be conditional, and a conditional wall is a speed bump.
* **A `needs_review` stop before filing.** Rejected by Neo question 1. It would reproduce
  the improvement order, which already exists for the case where the user wants to decide.

## 5. Tests (named by the work order; each maps to a mechanism above)

New `tests/test_investigation_orders.py`, plus additions to existing files:

1. **The no-write guarantee is enforced.** `hooks.investigator_write_decision` with
   `JARVIS_WO_KIND=investigator` denies a `Write` to a source file and allows
   `verdict.json`; `investigator_bash_decision` denies `sed -i`, a `cat > file <<EOF`
   heredoc, `git commit`, `jarvis bug report …` and `jarvis wo finish …`, and allows
   `git log -5`, `gh pr view <url>`, `jarvis wo show wo-x`, `jarvis wo ask`,
   `jarvis investigate verdict`. Plus a **negative control**: the same payloads with
   `JARVIS_WO_KIND=worker` are untouched, so nothing here narrows an ordinary worker —
   the shape `tests/test_feature_order_team.py` uses for the seats. And a
   reachability test that goes through `preflight_decision`, not the decision function
   directly: a denial placed after the auto-allow at src/jarvis/hooks.py:871 passes a
   direct unit test and does nothing in production.
2. **A verdict is recorded for each of the four classifications.** Four cases through
   `ops.submit_verdict`: `GAP` (fake `gh` finds nothing -> issue filed, work order
   dispatched, `filed` populated, `expedited` true, order `completed`, **no attention**);
   `WAITING_ON_USER` (nothing filed, attention raised, reason names what the user owes);
   `TRANSIENT` (nothing filed, no attention); `ALREADY_TRACKED` (nothing filed,
   `duplicate_of` preserved). Plus the ops-side downgrade: a `GAP` whose duplicate the
   fake tracker DOES hold is stored as `ALREADY_TRACKED` with `classified_by="ops"` and
   `submitted_classification="GAP"`, and **no issue is created**. The fake `gh` in
   `testing.py` already honours `issue list --state all --search "… in:body"`
   (src/jarvis/testing.py:1137-1150) and `add_issue` seeds titles and bodies
   (src/jarvis/testing.py:1665), so the whole duplicate check runs offline — which is the
   testability the §2.5 ruling was made for.
3. **Duplicate subjects are refused.** Two `create_investigation_order` calls for one
   subject: the second raises `OpsError` naming the live `inv-` id. Plus the same refusal
   for an `inv-` subject and for a `kind='investigator'` subject, and a control that a
   settled investigation does NOT block a new one.
4. **The daemon can create one.** `ops.create_investigation_order` followed by
   `Daemon.plan_features` opens exactly one `kind="investigator"` child with
   `parent_id` set and leaves the order `planning` — no CLI in the test, which is the
   assertion that the companion fleet-health order has a seam. Plus a validator battery
   in `tests/test_verdict_validator.py` on `plans.py`'s model: every rejection with a
   negative control beside it (a missing `proposed_fix` on a GAP, `user_owes` on a
   TRANSIENT, a paraphrased sub-`MIN_QUOTE_CHARS` quote, a `subject` that is not this
   order's, an over-`MAX_VERDICT_CHARS` document), and every problem reported at once.

Extend, do not copy: `tests/test_stores.py:330` (the exact-tuple assertion),
`tests/test_dispatch*.py` (the investigator prompt never contains the worker contract's
pull-request lines — assert on the absence, that is the regression that matters), and the
UI smoke test that every route renders.

## 6. Out of scope

* **The companion fleet-health order** — what counts as "not progressing", how often the
  daemon looks, and the rate limit on opening investigations. This spec only guarantees
  the seam (§2.7) and the per-subject refusal it will lean on.
* **Investigating anything but a work order, a feature order or an improvement order.** No
  investigation of a project, a release or the fleet as a whole.
* **A verdict that proposes more than one fix.** One subject, one root cause, one
  classification. A symptom with two independent causes is an improvement order.
* **Retrying or re-opening a settled investigation.** The subject is still stuck? Open a
  new one — the per-subject refusal only blocks LIVE ones.
* **`probes.py` integration.** The health probes are the supervisor's vocabulary for a
  burning turn; whether an investigation should be raised from a probe belongs to the
  companion order.

## 7. Questions for the lead — all resolved

1. `investigation_budget_usd`'s shipped default number. §2.8 argues it must be non-None
   and I had no measurement to pick the figure from. **Decided: ships at `2.00`, as
   `catalog.DEFAULT_INVESTIGATION_BUDGET_USD`, recorded as PENDING the user's answer on
   escalated Neo question 826.** It is one constant and one line to change when the
   measurement arrives.
2. Whether `jarvis investigate verdict` should be spelled `jarvis investigate report` for
   symmetry with `jarvis io report`. **Decided in favour of `verdict`**, for the reason
   already given: the document is a verdict and not a report, and the two commands must
   not be confusable at 2am; symmetry is the argument the other way.
3. Whether `jarvis learn add` is worth keeping in the allowlist at all. **Decided: it
   STAYS** (the user confirmed). `ops.submit_verdict` deliberately writes no knowledge —
   there is no knowledge-write path on the verdict — so the investigator's own
   `jarvis learn add` is the single channel, not a second one.

---
---

# Appendix A — the self-evolution loop (the user's design addition, 2026-09-27)

**Added after §1–§7 were written and reviewed. Nothing above is rewritten.** The five
edits this appendix makes to shipped §2 behaviour are listed in A.10 and nowhere else, so
a reader of §2 alone is never silently wrong about more than those five.

The requirement is the user's, in five items: a stable GAP CLASS on the verdict; two
outputs per GAP (unblock now, and a fix order owing three deliverables); enforcement of
those deliverables rather than instruction; a recurrence ledger that treats a repeat as an
INCOMPLETE FIX; and a metric that shows the mechanical share rising.

**Two knowledge entries this appendix was told to honour could not be read from this
seat** — it has `Write` and Serena only, no `Bash`, so `jarvis learn show kn-85265170` and
`kn-a2b5efe0` were unavailable, and neither id appears anywhere in the tree. A.1 and A.9
use the lead's summary of them. **Verify both before implementing**; if kn-85265170 says
more about the remedy registry's extension mechanism than "fix orders add to it", A.1's
seam split is the paragraph to re-read.

## The problem — an investigation's output dies with the investigation, so the same gap class is diagnosed by a model for ever

§2 ships a diagnostician. It does not ship a system that gets stuck less often, and the
gap between those two is measurable in the code as it stands.

**1. A GAP verdict's only durable output is prose a human reads.**
`ops.submit_verdict` (src/jarvis/ops.py:6702-6827) ends in exactly two writes: the verdict
document into `feature_orders.plan`, and an expedited bug through
`bugreport.report_bug(..., expedite=True)`. `verdicts.parse_verdict`
(src/jarvis/verdicts.py:102) requires `root_cause` to be a PARAGRAPH — `MIN_FIELD_CHARS`
of free text. Nothing on the record is comparable between two investigations, so "this is
the fourth one of these this month" is not a query anyone can run; it is an operator
remembering. The `_verdict_duplicate` check (src/jarvis/ops.py:6830) searches on the
SUBJECT ID, deliberately and correctly (`db.score_sql` is word-OR, kn-c6e8fbf0) — which
means two investigations of the same MECHANISM on two different subjects are, by
construction, not duplicates of each other and both file.

**2. The mechanical actuator exists and nothing connects it to a diagnosis.**
`remedies.REMEDIES` (src/jarvis/remedies.py:273) holds three entries — `nudge`, `unblock`,
`file_work_order` — and `remedies.apply` (src/jarvis/remedies.py:552) is the only code in
the OS that acts on an order nobody asked it to touch. Its selector today is a supervisor
model call judging an alarm. There is no mapping from "what is wrong" to "which remedy
clears it", so a diagnosis that KNOWS the answer cannot say it in a form the daemon can
execute.

**3. The mechanical detector surface exists and is hand-written, one incident at a time.**
`invariants.INVARIANTS` (src/jarvis/invariants.py:3783) is ~30 checks, each added by a
human after something broke. #793 in §1.1 — "nothing in the OS noticed that `main` was
red" — is the standing measurement of what that costs: the detector for a class arrives
only if somebody writes it, and until then every occurrence is found by a person or, after
this WO ships, by a model session at `investigation_budget_usd` a go (§2.8, `2.00`).

**4. So the loop as specified converges on the wrong fixed point.** Five of §1.1's six
causes would now be found by an investigator instead of by the operator — an improvement
in attention and a permanent cost in tokens. Occurrence *n* costs the same as occurrence
1, and the record cannot tell you it is occurrence *n*.

**5. And a fix order filed from a GAP is held to nothing.** The brief
`issues.WORK_ORDER_BRIEF` (src/jarvis/issues.py:815) says "Fix {url}" and reproduces the
issue body. The validation panel then judges the PR against `assets/validator-seats/*.md`
mandates, whose vocabulary has no notion of a detector or a remedy. A fix that turns the
symptom green and leaves the OS just as blind passes every check there is — which is
`dispatch._analyst_prompt`'s own founding argument (src/jarvis/dispatch.py:714-717),
arriving through a door that argument never had.

**Root cause, stated once:** the OS's diagnosis vocabulary (prose) and its self-healing
vocabulary (`remedies` ids, `INVARIANTS` callables) are disjoint, and nothing in the path
from verdict to merged fix requires them to meet.

**What this appendix does NOT fix, deliberately.** It builds the loop; it does not write
the detectors and remedies for §1.1's six historical classes. Each of those is its own fix
order — that is the loop working, not a gap in it (A.13).

## The fix — the verdict carries a class slug, and the expedited fix order it files cannot merge without a detector and a remedy registered for that slug

Six moving parts. Read A.1 first: it says which of them this order does not build.

### A.1 The `jarvis wo fix` seam — what wo-dbea82cf owns, what this order owns

`jarvis wo fix` **does not exist in this checkout**: no `wo fix` verb in `cli.py`, and
`remedies.apply` has exactly one caller shape (the supervisor's `remedy_tick`). It is
being built by wo-dbea82cf, under the user ruling kn-85265170 that its remedy table must
be an extensible registry that fix orders add to.

**wo-dbea82cf owns:** the `wo fix` CLI verb and its `ops` entry point; the remedy-selection
table and its extension mechanism; and honouring the four refusals `remedies.py`'s
docstring lists (closed registry, `catalog.RemedyConfig` off by default with an empty
allow-list, an approved-unexpired-unspent `self_heal` grant through `gates.open_gate`, the
AST pin).

**This order owns:** one call site, one indirection, and the ledger row that call writes.
Specifically `ops._try_unblock(project, subject_id, gap_class)`, a private helper beside
`submit_verdict`, which

* imports the `wo fix` entry point LAZILY inside the function body, and treats
  `ImportError` / `AttributeError` as the outcome `"unavailable"` — not as an error. This
  is what makes this order landable and mergeable before wo-dbea82cf, and it is the same
  lazy-import-inside-the-body discipline `cli.py` and `daemon.py:134` already use for a
  different reason;
* passes the remedy id that `gaps.get(gap_class).remedy` names, and never a remedy the
  investigator chose in prose. The investigator's `proposed_fix.remedy` field (A.3) is a
  RECOMMENDATION to the fix order's author; the registry is what a running daemon obeys.
  A model naming a remedy that then executes would put a free-text action into the one
  module that has none (`remedies.py`'s "no free-text action and no 'other'");
* records the outcome on the verdict document under `unblock` and in the ledger row (A.7).

**Load-bearing assumption about the seam, state it and test it:** `wo fix` applies through
`remedies.apply`, so `remedies.apply` stays the single choke point every mechanical action
passes. A.7's mechanical ledger write lives THERE for that reason. If wo-dbea82cf ends up
with a second acting path that bypasses `remedies.apply`, the metric in A.8 silently
under-counts the mechanical share — i.e. it reports the loop failing while it works, which
is the worst direction for a number whose whole job is to be believed.

**What happens when a remedy fits but is not armed.** All four refusals stand; none is
worked around. `_try_unblock` returns one of five outcomes, each stored verbatim on the
verdict under `unblock.outcome` and rendered by `verdicts.render_verdict`:

| outcome | cause | what the record says |
|---|---|---|
| `applied` | the remedy ran | what it did, from `remedies.apply`'s return string |
| `not-armed` | `RemedyConfig.enabled` false, or the id not in `allowed` | names the project's config path and the exact `jarvis wo fix <subject> <remedy>` the user can run |
| `awaiting-grant` | the `self_heal` approval is pending or absent | names the gate request; a grant is a review, not a delay to route around |
| `no-remedy` | the class's registry entry has `remedy=gaps.REMEDY_NONE` | says the class is deliberately not automated, and names what the detector will raise instead |
| `unavailable` | the seam is not merged yet | names wo-dbea82cf |

**None of the five raises attention**, and that is a decision rather than an omission.
§2.5 ruling 6 spends the user's attention on `WAITING_ON_USER` only, and a refused unblock
is not a new thing the user owes: the subject is still stuck, its own
`invariants.true_blockers` line already says so on its own record, and the expedited fix
order is already filed. Raising a second flag here would re-spend exactly the attention an
investigation exists to save. It is visible without a flag — on the investigation page, in
the ledger, and as an `attention`-resolved episode in the A.8 metric, which is the number
the user checks when they want to know whether arming remedies is worth it.

### A.2 `src/jarvis/gaps.py` — the class registry, closed by test and grown by a fix order

New leaf module, stdlib-only at module level, modelled line for line on `remedies.py`:

```python
@dataclass(frozen=True)
class GapClass:
    id: str            # the slug: stale-hold, round-burn, red-main, oversized-input
    headline: str      # the mechanism, in the terms a reviewer needs
    symptom: str       # what a stuck order looks like from outside
    detector: str      # the invariant id or doctor check that recognises it, from state
    remedy: str        # a `remedies.REMEDIES` id, or REMEDY_NONE
    issue_url: str     # the tracker issue whose fix registered this class
    since: str         # the jarvis version the detector shipped in

GAP_CLASSES: dict[str, GapClass] = {...}
SHIPPED_GAP_CLASSES: tuple[str, ...] = (...)   # asserted == tuple(GAP_CLASSES)
REMEDY_NONE = "none"                            # unsafe to automate; detector only
SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+){1,4}$")
```

plus `get`, `registered(slug) -> bool`, `by_invariant() -> dict[str, str]` (the detector id
to class id inverse, for the daemon in A.7), `checked_slug(raw) -> str` (shape only), and
`render_registry()` — one renderer for the investigator prompt and the fix order's brief,
for `remedies.render_catalogue`'s stated reason: a model shown a different list from the
one the code enforces asks for things that are refused.

**Ships with the four slugs the user named**, each pointing at its §1.1 issue and each
with `detector=""` and `remedy=REMEDY_NONE` — i.e. registered as KNOWN and NOT YET
MECHANICAL. A registry entry with an empty `detector` is the honest state of `stale-hold`
on the day this lands, and the metric in A.8 reads it as "0% mechanical", which is the
true number.

**The tension the lead named, resolved: TWO TIERS, and only one of them is closed.**

* **The ledger and the verdict accept any WELL-FORMED SLUG** — `SLUG_RE`, nothing else.
  They must: the first occurrence of a class nobody has seen is exactly the case the
  investigation exists for, and a closed vocabulary at the verdict would either refuse
  that verdict or force the investigator to lie by picking the nearest existing slug. The
  second is worse than the first, and it is what a model does under a closed enum.
* **`GAP_CLASSES` is closed by `SHIPPED_GAP_CLASSES`, asserted by a test**, on
  `remedies.SHIPPED_REMEDIES`' precedent (src/jarvis/remedies.py:311-313). Membership does
  not mean "a name the OS has heard"; it means **"this class has a detector and a remedy,
  and a human reviewed both"**.
* **The fix order grows it, and the PR *is* the reviewed diff.** Registering the class is
  one of the three deliverables A.6 enforces, so the only route into `GAP_CLASSES` is a
  merged pull request that also carries the detector, the remedy and the test. The
  investigation cannot grow it — an investigator's writes are refused by
  `hooks.investigator_write_decision` (§2.6), which is the one property that makes this
  split safe rather than notional.
* **The loop closes with one check**: `invariants.check_gap_classes_are_registered`, in
  `OS_INVARIANTS` (they take nothing, are never repairable — the right list, and it is
  where `INV-UI-HEALTHY` lives). `INV-GAP-REGISTERED` is false when the ledger holds a
  class whose fix work order has LANDED and which is not in `GAP_CLASSES`. That is
  precisely "somebody shipped a symptom fix and called it done", reported by `jarvis
  doctor` every tick, with no model call.

### A.3 The verdict gains a class and two requirement fields

`src/jarvis/verdicts.py`:

* **`gap_class`, required on ALL FOUR classifications**, validated by `gaps.checked_slug`
  shape only (A.2). Required on `WAITING_ON_USER` and `TRANSIENT` too, which is the
  non-obvious half: item 4 asks for the class on EVERY investigation, and "we opened four
  investigations on `awaiting-signin` and every one ended WAITING_ON_USER" is a finding
  about the OS — the user is being made to do something a state check could do. Excluding
  the non-GAP classifications would delete exactly that signal. The ledger stores the
  classification beside the class, so any consumer that wants GAP-only has it.
* **`proposed_fix` gains `detector` and `remedy`**, appended to `PROPOSED_FIX_FIELDS`
  (src/jarvis/verdicts.py:82) and therefore required with no per-field exception, as every
  field there already is. `detector`: what predicate over STATE would have recognised this
  — named in mechanism terms, not "add a check". `remedy`: which existing
  `remedies.REMEDIES` id clears it, or the word `none` with the reason it is unsafe to
  automate. These are the investigator's diagnostic contribution and the fix order's
  acceptance criteria; the panel in A.6 checks the code against them.
* `settle_headline` and `render_verdict` (src/jarvis/verdicts.py:275, 285) render the
  class, the `unblock` outcome and the regression link. `render_verdict` leads with the
  classification today; the class goes on that same first line, because "which kind of
  broken" is the first thing a reader of a settled investigation wants.
* `MAX_VERDICT_CHARS` is unchanged: two short prose fields do not move a 20 000-char cap.

`dispatch._investigator_prompt` (§2.3) gains one section, `gaps.render_registry()`, and one
rule: **reuse an existing slug when the mechanism matches, and coin a new one only when it
does not** — with the consequence stated, that a new slug commits a fix order to
registering it. Item 6 of §2.3 (bounded inputs) is unchanged.

### A.4 Item 2(a) — unblock the subject now

Inside `ops.submit_verdict`, after the filing in step 4 and before the settle in step 5:
`_try_unblock` (A.1), for a `GAP` only, and ONLY when `gaps.registered(gap_class)` and the
entry's `remedy` is not `REMEDY_NONE`. An unregistered class has no vetted remedy by
definition, and guessing one from prose is the free-text action `remedies.py` refuses.

Order matters and is the same argument §2.5 makes for its own step order: the bug is filed
FIRST, so a remedy that unsticks the subject can never cause the cause to go unfiled. The
inverse order would mean a successful unblock followed by a `gh` outage leaves a subject
that looks healthy and a defect nobody recorded — the §1.1 failure mode exactly.

### A.5 Item 3, part one — the structured fields survive verdict -> issue -> work order -> PR

Four hops, and the last one is the one that matters: the panel judges a WORK ORDER, so the
class has to be on the work order's row before the panel ever runs.

1. **verdict -> `report_bug`.** `bugreport.report_bug` (src/jarvis/bugreport.py:394) gains
   keyword-only `gap_class`, `detector`, `remedy`, threaded into `render_body`
   (src/jarvis/bugreport.py:240), which gains one section after Actual:

   ```
   ### Required deliverables (filed by an investigation — jarvis:gap-class=stale-hold)
   ```

   with the three named deliverables, the detector and remedy the verdict proposed, and a
   machine marker `bugreport.GAP_MARKER = "<!-- jarvis:gap-class={slug} -->"` beside the
   existing `<!-- Filed automatically by … -->` comment. The marker is for
   `issues.issues_mentioning`-style reads and for a human pasting the issue into a session;
   **it is not the panel's source** (see 3).
2. **`report_bug` -> `route_filing`.** `issues.route_filing`
   (src/jarvis/issues.py:863) gains `gap_class=""` and puts it in the metadata it already
   writes: `metadata={EXPEDITED_KEY: True, GAP_CLASS_KEY: gap_class}` at
   src/jarvis/issues.py:939, with `GAP_CLASS_KEY = "gap_class"` declared beside
   `EXPEDITED_KEY` (src/jarvis/issues.py:847). Nothing else about that call changes.
3. **`promote_confirmed` -> the work order row.** `promote_confirmed`
   (src/jarvis/issues.py:1319) already passes `metadata` to `ops.create_work_order` **at
   creation** — its docstring says why, and the reason is exactly ours: "the daemon can
   claim and dispatch the row before a second write lands". So the gap class is on
   `work_orders.metadata` from the instant the row exists, and `issues.was_expedited`'s
   pattern is the precedent for reading it back (`issues.gap_class_of(wo)`).
4. **The work order -> the panel.** `ops.submit_for_validation`
   (src/jarvis/ops.py:3820) holds `wo` in hand. No network read, no issue fetch, no PR
   body parse. **This is why the metadata hop is the seam and the issue marker is not:** a
   panel that had to read the tracker to learn whether to enforce would fail open on a `gh`
   outage, i.e. would stop enforcing precisely when the fleet is degraded.

`issues.start_work` — the other route from an issue to a work order — reads the issue body
and so must lift the class off `GAP_MARKER` and write the same metadata key. Otherwise a
class filed by an investigation, left on the tracker and picked up later by hand loses its
deliverables silently, which is the one hole a reader will look for.

### A.6 Item 3, part two — the enforcement, mechanically first and the tester seat second

Two layers, and the split is by what each can actually decide.

**Layer 1: a mechanical bounce, no model call, no round spent.** The precedent is exact
and already shipped: `ops.unanswered_paths` (src/jarvis/ops.py:3783) reads the store,
`validation.unanswered_submission` (src/jarvis/validation.py:847) is the pure rule, and
`ops.submit_for_validation` (src/jarvis/ops.py:3872-3900) bounces without opening a round.
Mirror all three:

* `validation.missing_gap_deliverables(gap_class, packet) -> tuple[str, ...] | None` —
  pure, `(class, EvidencePacket)` in, the names of the missing deliverables out, `None`
  when nothing is missing or when it cannot tell. It asks three path-and-content questions
  over `packet.file_shas` and the diff, which is all a string comparison can honestly
  answer:
  1. `src/jarvis/gaps.py` is touched AND the diff adds the slug to both `GAP_CLASSES` and
     `SHIPPED_GAP_CLASSES` — the registry deliverable, which subsumes "a detector is
     named" and "a remedy is named" because the dataclass has no optional fields;
  2. the file the new entry's `detector` names is touched (`invariants.py`, or wherever a
     doctor check lives) — a registry entry pointing at a detector that was not written is
     the exact lie this layer exists to catch;
  3. a file under `tests/` is touched whose diff mentions the slug — the test deliverable,
     at the only granularity a sha map can see.
* `ops.unmet_gap_deliverables(store, wo, packet)` — the store half: reads the gap class off
  `wo["metadata"]`, returns `None` for every work order that has none, which is the fleet.
* In `submit_for_validation`, beside the `unanswered` branch and **before** it (a
  submission can be both, and the missing-deliverable message is the more specific), with
  its own event kind `gap_bounced`, its own `GAP_BOUNCE_FEEDBACK` naming the three
  deliverables and the registry file, and its own `GAP_BOUNCE_LIMIT = 2` counted off that
  event.
* **On exhaustion, copy the trick at src/jarvis/ops.py:3880-3894 exactly**: open the round
  and immediately `escalate_validation_round` it. The comment there is the reason and it
  is not optional — `invariants.true_blockers` (src/jarvis/invariants.py:657) re-derives
  `VALIDATION_STUCK_BLOCKER` from a round whose outcome is `escalated`, and
  INV-ATTENTION-REASON rewrites any attention reason it cannot re-derive. A give-up written
  bare loses its flag on the next tick and the user is never asked. **Do not invent a new
  blocker constant here**: a new attention reason needs a new derivation inside
  `true_blockers`, and reusing the existing escalation costs nothing and is already tested.
* **A FORCED round is never bounced**, for `submit_for_validation`'s stated reason: there
  is no submitter to send anything back to.
* **Every uncertainty returns `None`** — no gap class on the work order, an empty file map,
  a packet that predates the metadata key. `unanswered_submission`'s docstring makes this
  argument and it holds here: the cost of failing open is one panel round, the cost of
  guessing wrong is bouncing work that was really done.

**Layer 2: the `tester` seat, for the question a string comparison cannot ask.** Layer 1
proves a test file mentions the slug. Whether the test actually makes the detector FIRE and
the remedy CLEAR it is a reading of code, and that is a seat's job. `tester` already holds
a veto (`validation.VETO_SEATS`, src/jarvis/validation.py:129), so the enforcement needs no
new seat and no change to `arbitrate`: one paragraph in
`assets/validator-seats/tester.md` — on a submission that registers a gap class, a test
that does not both fire the detector from constructed state and show the remedy clearing it
is a BLOCKING finding. Note the constraint in `tests/test_validation_seats.py`: it asserts
the mandate prose against the veto table, so the paragraph has to say "blocking" and the
table has to already agree. It does.

**Why not a sixth seat.** A seat is a model call per round, on every work order in the
fleet, to answer a question that is `None` for all but a handful of them — and the veto
table's own docstring names the failure mode of a seat with a narrow mandate and a veto: an
annoying rejection loop that spends the attention this whole lineage exists to save.

### A.7 Item 4 — the recurrence ledger

**Where: a new `gap_events` table in the CENTRAL store** (`$JARVIS_HOME/os.db`,
src/jarvis/central_store.py:89-285), not in the per-project DB. Three reasons, in order:

1. **A gap class is a property of the OS, not of a project.** `stale-hold` recurring in
   `jarvis_os` and in `shared_schedule` is one incomplete fix, and a per-project table
   makes that the one join nobody can do.
2. **The metric is fleet-wide** (A.8). A per-project ledger would make the dashboard page
   open every project DB to draw one number — and a project whose path has moved would
   silently drop out of the denominator.
3. **The regression decision needs the issue and the fix order together**, and those cross
   projects: the issue is on the OS tracker, the fix order is in whichever project filed
   it.

Columns — append-only, `inbox`'s shape (src/jarvis/central_store.py:98):

```
id INTEGER PK, ts REAL, gap_class TEXT, project TEXT, subject_id TEXT,
subject_kind TEXT, episode TEXT,          -- health.fingerprint at the moment it was seen
inv_id TEXT, classification TEXT,          -- '' when no investigation was involved
resolved_by TEXT,                          -- investigation | mechanical | attention
detector TEXT,                             -- the invariant id, when a detector saw it
issue_url TEXT, fix_wo_id TEXT, landed_at REAL,
regression_of INTEGER                      -- the earlier row whose fix was incomplete
UNIQUE(gap_class, subject_id, episode)
```

**`episode` is `health.fingerprint(pstore, subject)`** (src/jarvis/health.py:51) — the
codebase's own "two units with the same fingerprint are the same situation" primitive,
reused rather than reinvented, and the UNIQUE constraint on it is what stops a detector
firing every tick from inflating the denominator. Note the correctness rule in
`health.observer_kinds()`: a fingerprint must not move because it was looked at. A
`gap_events` row is written to the CENTRAL store and writes no `wo_events` row, so the
ledger cannot perturb the value it is keyed on. A remedy application does move it — and
that is right: the episode is over.

Store methods on `CentralStore`, beside the knowledge ones: `record_gap_event(...)`
(idempotent on the unique key, returns the row), `gap_events(gap_class=None, since=None)`,
`mark_gap_fix_landed(fix_wo_id, ts)`, `landed_fix_for(gap_class)`, `gap_rollup(days)`.

**Three writers, one per resolution path:**

| writer | where | `resolved_by` |
|---|---|---|
| `ops.submit_verdict`, at the settle | src/jarvis/ops.py:6702, step 5 | `investigation` |
| `remedies.apply`, after the handler returns | src/jarvis/remedies.py:552 | `mechanical` |
| `Daemon.reconcile_project`, per `Violation` whose invariant is in `gaps.by_invariant()` | daemon | `mechanical` if `v.repaired` else `attention` |

The third is the neatest consequence of the existing design: `invariants.INVARIANTS`
checkers already "repair what is unambiguous", so `Violation.repaired`
(src/jarvis/invariants.py:443) IS the detector-plus-remedy-in-one case, and the flag is
already there to read.

**"The fix already landed", decided from state with no model call.** `landed_fix_for(class)`
returns the earliest row for that class with a non-empty `fix_wo_id` and a non-NULL
`landed_at`. `landed_at` is stamped by the daemon where it already watches that
transition — the issue sync / PR poll path that completes a `waiting_pr_merge` order — so
the question is one indexed read at verdict time and never a crawl. Two facts, both
already in the store: the fix work order reached a terminal landed status, and its issue
closed.

**The regression path, in `ops.submit_verdict` step 4, BEFORE `_verdict_duplicate`:** if
`landed_fix_for(gap_class)` returns a row, this is not a duplicate and not a new bug — the
earlier fix was INCOMPLETE. So:

* `issues.reopen(url)` — **a new function**, beside `issues.close`
  (src/jarvis/issues.py:~445), one `gh issue reopen`. It does not exist today, and this is
  the only new GitHub verb the appendix needs;
* `issues.comment(url, …)` (src/jarvis/issues.py:430) with the new investigation's id, the
  new subject and the root cause — the evidence that the class recurred;
* a `regression` label, on `FOLLOW_UP_LABEL`/`ensure_follow_up_label`'s pattern
  (src/jarvis/issues.py), so the recurrence is visible to somebody scanning the tracker
  and never only inside the OS;
* a new `gap_events` row with `regression_of` pointing at the earlier one, and
  `issue_url` the ORIGINAL issue's;
* the verdict stored with `classification` unchanged at `GAP`, `classified_by: "ops"`, and
  a new `regression_of` field naming the original issue and fix order. **Not
  `ALREADY_TRACKED`**: that classification means "somebody is on this", and the truth here
  is the opposite — somebody was on it, shipped, and it came back.
* **No second expedited bug.** The fix order: hand back `issues.live_work_order(spec, url)`
  when one is live, exactly as `promote_confirmed` already does; otherwise
  `promote_confirmed` files a fresh one against the REOPENED issue, which is what that
  function's docstring already calls the reopened-issue case.

### A.8 Item 5 — the metric

**The rollup: `ops.gap_report(days=90, project=None)`**, computed in `ops` over one
`CentralStore.gap_rollup` read. `ops` and not the route, on
`ops.knowledge_usage_report`'s precedent (rendered at src/jarvis/ui/app.py:1312): the page
and the CLI must not be two answers to one question.

Shape, per gap class, newest activity first:

```
{"gap_class", "occurrences", "weeks": [(iso_week, n), ...],
 "mechanical", "attention", "investigation",
 "mechanical_share", "registered", "detector", "remedy",
 "issue_url", "regressions", "first_seen", "last_seen"}
```

plus a fleet total row with the same keys.

**The denominator, defined precisely enough to implement.** One `gap_events` row is one
EPISODE — one occurrence of one class on one subject in one situation, deduped by
`UNIQUE(gap_class, subject_id, episode)`. For a window W:

* `occurrences = count(rows in W)`
* `investigation = count(resolved_by = 'investigation')` — a model session diagnosed it
* `attention = count(resolved_by = 'attention')` — a detector saw it and a human acted
* `mechanical = count(resolved_by = 'mechanical')` — a registered remedy ran, or a
  repairing invariant fixed it; **no agent turn and no user attention**
* `mechanical_share = mechanical / occurrences`, and `occurrences` is exactly
  `investigation + attention + mechanical` because `resolved_by` is written once per row
  and is never empty.

`attention` counts as NOT mechanical, and it is reported as its own number rather than
folded into either side: it is the state "the detector exists, the remedy is not armed or
is unsafe" — which is A.1's `not-armed` / `no-remedy` outcomes seen from the metric end,
and it is the number that tells the user whether arming a remedy would pay.

**Where it goes: a new `/evolution` page**, `@app.get("/evolution")` in
src/jarvis/ui/app.py beside `/alarms` (src/jarvis/ui/app.py:1455), template
`src/jarvis/ui/templates/evolution.html`, one nav entry. Not a section on `/`: that page is
the pulse and holds what needs the user, and this metric needs nobody — it is read when the
user asks whether the OS is getting better. The per-class rows lead with
`mechanical_share`, then the weekly sparkline counts, then the registry state
(`registered` / `detector` / `remedy`), so an unregistered class with four occurrences and
0% mechanical reads as the backlog item it is. Each project's page gets the same table
scoped to its own rows, on the `jarvis issues` per-project precedent.

**CLI: `jarvis gaps [--days n] [--project p]`**, a thin wrapper on `ops.gap_report`, on
`jarvis learn stats`' precedent (CLI and page, one computation). One crib-sheet entry
under §2.10's rules: it goes in the existing fenced block, and it is capped the same way.

### A.9 The detector in item 2(ii) vs the fleet-health mechanical trigger (kn-a2b5efe0, wo-9f00e3b5)

Related, different, and the difference is worth one paragraph in the code as well as here,
because the names will be confused.

**The fleet-health trigger (wo-9f00e3b5) answers "is this order not progressing?"** — one
symptom-level, kind-agnostic question about any open unit, whose output is a MODEL SESSION:
it opens an investigation. Its nearest shipped relative is `health.due`
(src/jarvis/health.py:89), whose whole job is deciding when a model call is worth its
money, and `health.fingerprint`, which is the cheap deterministic summary that decision
reads. A trigger like that is necessarily vague: it fires on "something is wrong here, and
what is wrong is not known".

**The detector in item 2(ii) answers "is THIS gap class present?"** — one named class, a
predicate over state, whose output is a remedy or a precise attention line naming a
command. Its home is `invariants.INVARIANTS` / the doctor checks, whose contract
(src/jarvis/invariants.py) is "no LLM, ever".

**The relationship: the detector is what the trigger's output should turn into.** Every
class that gains a detector is a class the fleet-health trigger no longer has to spend a
session on — which is exactly the number A.8 reports. The two must not be merged: a
detector that fired an investigation would be a mechanical check paying for a model to
re-derive what it already knew, and a trigger keyed on gap classes would only ever notice
the ones already fixed. **The one edit where they meet:** when the fleet-health trigger
opens an investigation, `Daemon.reconcile_project` should skip the classes whose detector
already fired on that subject this episode — same episode key, so the ledger answers it
with no extra state. That edit belongs to wo-9f00e3b5, not here; this appendix owes it the
ledger read, which A.7 provides.

### A.10 Every file this appendix changes, with its precedent

New:

| file | what | precedent |
|---|---|---|
| `src/jarvis/gaps.py` | `GapClass`, `GAP_CLASSES`, `SHIPPED_GAP_CLASSES`, `SLUG_RE`, `checked_slug`, `get`, `registered`, `by_invariant`, `render_registry`, `REMEDY_NONE` | `remedies.py` in full: closed registry, dataclass carrying the words a reviewer reads, one renderer for every reader |
| `src/jarvis/ui/templates/evolution.html` | the metric page | `alarms.html` |
| `tests/test_gap_ledger.py`, `tests/test_gap_deliverables.py` | A.11 | `tests/test_validation_bounce.py`, `tests/test_remedies.py` |

Changed, each edit sited:

1. `src/jarvis/verdicts.py` — `gap_class` required on all four (`parse_verdict`:102, via a
   new common-field check beside `root_cause`); `PROPOSED_FIX_FIELDS`:82 gains `detector`
   and `remedy`; `render_verdict`:285 and `settle_headline`:275 render class, `unblock` and
   `regression_of`.
2. `src/jarvis/ops.py` — `submit_verdict`:6702 gains the regression branch (before
   `_verdict_duplicate`), the `_try_unblock` call and the ledger write; new
   `_verdict_regression`, `_try_unblock`; `GAP_CLASS_METADATA` read helper;
   `unmet_gap_deliverables` and the `gap_bounced` branch in `submit_for_validation`:3872
   with `GAP_BOUNCE_FEEDBACK` / `GAP_BOUNCE_LIMIT` / `_gap_bounce` beside
   `BOUNCE_FEEDBACK`:3741 / `BOUNCE_LIMIT`:3736 / `_bounce`:3917; new `gap_report`.
3. `src/jarvis/central_store.py` — the `gap_events` DDL plus its index, beside
   `knowledge_reads`:159; the five methods.
4. `src/jarvis/bugreport.py` — `report_bug`:394 and `render_body`:240 gain
   `gap_class`/`detector`/`remedy`; new `GAP_MARKER`, `DELIVERABLES_SECTION`.
5. `src/jarvis/issues.py` — `GAP_CLASS_KEY` beside `EXPEDITED_KEY`:847;
   `route_filing`:863 threads it into the metadata at :939; `start_work` lifts it off
   `GAP_MARKER`; new `reopen`, `gap_class_of`, `REGRESSION_LABEL` +
   `ensure_regression_label` on `FOLLOW_UP_LABEL`'s pattern.
6. `src/jarvis/validation.py` — `missing_gap_deliverables`, pure, beside
   `unanswered_submission`:847.
7. `assets/validator-seats/tester.md` — one blocking paragraph (A.6 layer 2).
8. `src/jarvis/invariants.py` — `check_gap_classes_are_registered` in `OS_INVARIANTS`;
   `INV-GAP-REGISTERED`.
9. `src/jarvis/remedies.py` — one `central.record_gap_event` call at the end of
   `apply`:552, when the alarm or the `wo fix` request carries a class. Not a name the AST
   pin in `tests/test_remedies.py` walks for, and it acts on nothing.
10. `src/jarvis/daemon.py` — the per-`Violation` ledger write in `reconcile_project`; the
    `landed_at` stamp where a fix order's PR merge is already observed.
11. `src/jarvis/dispatch.py` — `_investigator_prompt` (§2.3) gains
    `gaps.render_registry()` and the coin-a-slug rule.
12. `src/jarvis/cli.py` — `jarvis gaps`, one handler, lazy imports as every handler does.
13. `src/jarvis/ui/app.py` — `/evolution` beside `/alarms`:1455; the per-project table.
14. `CLAUDE.md` — one crib entry for `jarvis gaps`, under §2.10's rules.

### A.11 Tests

1. **The registry is closed and the slug is not.** `tuple(gaps.GAP_CLASSES) ==
   gaps.SHIPPED_GAP_CLASSES` (the `tests/test_remedies.py` assertion, copied); `SLUG_RE`
   accepts `stale-hold` / `oversized-input` and refuses `Stale_Hold`, `x`, a sentence and a
   40-char run; `parse_verdict` ACCEPTS an unregistered well-formed slug on all four
   classifications and REFUSES a missing one — the two halves of A.2's two tiers, and the
   negative control is the one that will be deleted by accident.
2. **`INV-GAP-REGISTERED` fires on exactly the incomplete fix.** A ledger row with a
   landed `fix_wo_id` and a class absent from `GAP_CLASSES` yields the violation; the same
   row with the class registered yields nothing; a row with no `landed_at` yields nothing.
3. **The deliverables bounce.** Through `ops.submit_for_validation` and not the pure rule
   alone — a bounce placed after the round is opened passes a direct unit test and spends a
   round in production, which is the §5 item 1 reachability argument applied here. Four
   cases: a packet touching all three deliverables opens a round; one missing the registry
   entry bounces with the three named; the second bounce bounces; the third opens a round
   and escalates it, and `invariants.true_blockers` re-derives
   `VALIDATION_STUCK_BLOCKER` on the next call (the assertion that the give-up survives a
   tick). **Negative control: a work order with no gap class in its metadata is never
   bounced** — that is the fleet, and getting it wrong stops every project.
4. **The class survives all four hops.** `ops.submit_verdict` with a fake `gh` -> the
   issue body contains `GAP_MARKER` -> the dispatched work order's `metadata` carries
   `GAP_CLASS_KEY` -> `ops.unmet_gap_deliverables` reads it back with no network. One test,
   asserted at every hop, because each hop is a different author's edit.
5. **The ledger's episode key deduplicates.** Two `record_gap_event` calls with one
   fingerprint make one row; a moved fingerprint makes two; and a `health.fingerprint`
   computed before and after a `gap_events` write is byte-identical — the
   `observer_kinds()` rule, asserted rather than trusted.
6. **The regression path.** A landed fix for `stale-hold`, then a second investigation on
   a different subject with the same class: no new issue is created, the original is
   reopened and commented, the new row's `regression_of` points at the first, the verdict
   stays `GAP` with `classified_by="ops"`, and a live fix order is handed back rather than
   duplicated.
7. **Item 2(a) against all four refusals.** `_try_unblock` returns `not-armed` with
   `RemedyConfig` off, `awaiting-grant` with a pending approval, `no-remedy` for
   `REMEDY_NONE`, `unavailable` when the seam is absent (monkeypatch the import to raise
   `ImportError`), `applied` on the happy path — and **in every one of the five, no
   attention flag is raised on the investigation** (A.1's ruling), while the outcome is on
   the stored document.
8. **The metric adds up.** `ops.gap_report` over a seeded ledger: `mechanical + attention
   + investigation == occurrences` per class and for the fleet total; `mechanical_share`
   of a class with no mechanical rows is `0.0` and not a `ZeroDivisionError`; the window
   excludes older rows; and the `/evolution` route renders (the existing every-route smoke
   test).

### A.12 Rejected alternatives

* **A closed enum of gap classes at the verdict** (the obvious fix, and what a reviewer
  will propose). Loses on the first occurrence of anything new: the verdict would be
  refused, or — worse and likelier — the investigator picks the nearest existing slug and
  the ledger records a lie. A.2's two tiers get the reviewed diff without that cost.
* **The ledger in the per-project store.** Cheaper (no new central table, no cross-DB
  read) and it destroys the metric: the same class in two projects would never be one
  recurrence, which is the entire signal item 4 asks for.
* **A sixth validation seat that owns the deliverables.** A model call per round on every
  work order in the fleet to answer a question that is `None` for nearly all of them, plus
  the narrow-mandate-with-a-veto failure `validation.VETO_SEATS`' own comment names.
* **Enforce the deliverables in the fix order's BRIEF only** (i.e. instruct, do not
  enforce). This is precisely what the user ruled against, and the codebase agrees twice:
  `dispatch._analyst_prompt`:687-691 ("this is PROSE, not enforcement") and
  `validation.arbitrate`'s "a safety rule that lives in prose is a rule that holds by
  prompt luck". The brief still says it — a worker that learns the rule from a bounce paid
  a round to read it.
* **Let the investigator register the gap class itself.** One `Write` to `gaps.py` and the
  loop closes with no fix order. It would require a second exempt path in
  `hooks.investigator_write_decision`, and §4 has already settled this: a kind that may
  sometimes ship is a kind whose no-write hook is conditional, and a conditional wall is a
  speed bump.
* **Derive the mechanical share from `wo_events` instead of a ledger table.** No new
  table, and it cannot answer the question: an episode nobody investigated leaves no work
  order event, so the denominator would count only the cases the loop is trying to
  eliminate — a metric that improves by definition.
* **Put the metric on `/` (the pulse).** The pulse holds what needs the user. This needs
  nobody, and a number on that page competes with an attention item.

### A.13 Out of scope for this session, and what each would cost

* **Detectors and remedies for §1.1's six classes.** Each is a fix order of its own, and
  filing them is the loop working. Rough cost per class: one invariant plus its test, half
  a session, plus a remedy only where one of the three existing ones fits — and for
  `red-main` and `round-burn` none does, so those want a new `REMEDIES` entry, which is a
  reviewed diff and a gate conversation of its own.
* **Extending `remedies.REMEDIES`.** The closed registry stays closed in this order. The
  extension mechanism is kn-85265170's subject and wo-dbea82cf's work.
* **Backfilling the ledger from the six historical issues.** A one-off script, ~1h,
  and it writes rows nobody measured; the metric's first useful window starts when the
  table does. Say so on the page rather than faking history.
* **A time series longer than weekly counts** — no charting dependency exists and none
  should be added for this.
* **Auto-arming a remedy once its class has recurred N times.** The natural next ask, and
  it is a permission decision: it would let the OS widen `RemedyConfig` on its own, which
  is the one thing `remedies.py`'s four refusals are for. Bring it as a user ruling, not as
  a follow-up commit.
* **Investigations of a class with no subject** (a fleet-wide gap, e.g. `red-main`). §6
  already excludes investigating the fleet as a whole, and the ledger's
  `UNIQUE(gap_class, subject_id, episode)` assumes a subject. A fleet-level gap currently
  arrives attached to whichever order tripped over it, which is good enough and worth
  naming as a known imprecision in the ledger.

### A.14 Questions for the lead

1. **Is `gap_class` really required on `WAITING_ON_USER` and `TRANSIENT`?** A.3 rules yes
   and argues it. It is the one place this appendix makes an investigator do work for a
   metric rather than for its subject. Cheap to reverse: one entry in a required-fields
   table.
2. **Does a regression re-expedite?** A.7 files against the reopened issue and lets
   `promote_confirmed` dispatch, which means a second expedited work order and therefore a
   second release commitment for one class. That follows §2.5's cascade ruling — the
   original was expedited and the bug is still live — but it is the highest-consequence
   line in this appendix and the user may want a regression to be LOUDER and slower
   instead: reopen, label, notify, and let them press go.
3. **The `/evolution` window default.** A.8 says 90 days with no measurement behind it, on
   §7 item 1's precedent (one constant, one line).
4. **Confirm kn-85265170 and kn-a2b5efe0** against A.1 and A.9. This seat could not read
   them.
