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
4. **The terminal action**: `jarvis inv verdict <inv-id> --from-file verdict.json`, which
   IS its `jarvis wo finish` (it must not call `wo finish`). `--from-file` for
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
one revision fixes all of them), `parse_verdict`, `render_verdict`, `settle_headline`,
`knowledge_text`.

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
  (`status`, `wo show|list`, `fo show|list`, `io show|list`, `inv show|list`, `validation
  show`, `inspect`, `cost`, `alarms`, `doctor`, `search`, `issues`, `gate list|show|
  explain|rules`, `neo list|show|learnings`, `learn show|list|search|topics|stats`,
  `config wiring`, `brief`, `inbox`), plus exactly four mutations: `jarvis wo ask`,
  `jarvis wo assume`, `jarvis learn add`, and `jarvis inv verdict`. Anything else is
  denied.

Both denials name the alternative in their reason string, as every refusal in this module
does: the write denial says "put your verdict in `verdict.json` and submit it with
`jarvis inv verdict <inv-id> --from-file verdict.json`; you change no other file", the
Bash denial says which command it refused and that an investigation reads.

MCP tools are not `Bash` and are unaffected: Serena reaches the investigator through
`dispatch.serena_allow_rules()` (src/jarvis/dispatch.py:51), which grants **read-only**
Serena tools under both prefixes. Never grant Serena wholesale — it ships
`execute_shell_command`, `create_text_file` and `replace_symbol_body`, which would hand
back everything the two hooks just took away.

### 2.7 CLI and the daemon seam (decided — Neo question 3)

`jarvis investigate <subject> --why '...'` creates; `jarvis inv list|show|cancel` and
`jarvis inv verdict` are the sub-verbs. One word, `inv`, with `investigate` as the
creating alias so the sentence reads like the ask.

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
the route would make the page and `jarvis inv show` two answers to one question
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
   `jarvis inv verdict`. Plus a **negative control**: the same payloads with
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

## 7. Open questions for the lead

1. `investigation_budget_usd`'s shipped default number. §2.8 argues it must be non-None
   and I have no measurement to pick the figure from — the nearest datum is `jarvis cost`
   on the improvement orders already run in this checkout.
2. Whether `jarvis inv verdict` should be spelled `jarvis inv report` for symmetry with
   `jarvis io report`. I chose `verdict` because the document is a verdict and not a
   report, and the two commands must not be confusable at 2am; symmetry is the argument
   the other way.
3. Whether `jarvis learn add` is worth keeping in the allowlist at all. The investigator's
   durable output is the verdict, which `ops` already turns into knowledge on the
   improvement order's path (`findings.knowledge_text` -> `learn_add`); a second write
   channel is a second place the same lesson lands.
