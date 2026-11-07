# Neo owns assumption review on OS-origin orders; the user is told and can overrule

Feature order `fo-eaf1a59a`, filed from improvement order `io-c223fe10` (finding
`no-decider-for-os-orders`, knowledge entry `kn-a7fc12b0`). Live case: `wo-f93928c2`
(origin `os`, steward decision #916) sat 2.0 days in `needs_review` on one assumption
the user could not judge, because they never asked for the order.

## 1. Why, what is fixed, and the shared interfaces

### 1.1 The defect

The requestor of an OS-origin order is a rule (the steward, a detector, Morpheus, the
panel), not a judge. So the only one who can answer for it is Neo. Today autoreview treats
every order alike: on a FINISHED order Neo may only accept or escalate
(`ASSUMPTION_REVIEWER_PERSONA`, `src/jarvis/autoreview.py`), and `_ruling_question` never
tells Neo who asked for the order. Anything "the user would recognise" goes to a user who
asked for nothing.

### 1.2 Fixed by the user (do not re-litigate)

- On an OS-origin order Neo is the OWNER of assumption review. On a finished order its
  answers are ACCEPT or REWORK (back to the worker with a reason). Never escalate, except:
  the high-stakes net, a conflict with a user-answered question or a pinned user ruling,
  and model failure.
- Model failure keeps today's rule (kn-40db1828): the question stays pending and retryable,
  no verdict is ever synthesised.
- At most 2 reworks per assumption; the next non-accept goes to the user.
- Every owner ruling tells the user: one `info` inbox item plus one timeline event
  (kn-53a20d3a: silence is worse than a bad answer).
- `jarvis wo overrule <wo> <n> --feedback "…"` lets the user reverse an owner ruling.
- `jarvis neo stats` splits by requestor.
- User-origin orders: behaviour unchanged, byte for byte in the prompt Neo reads.

### 1.3 Shared interfaces (every section relies on these; none owns them alone)

| Name | Shape | Owner section |
|---|---|---|
| `ops.requestor(store, wo) -> Requestor` | dataclass `Requestor(kind: "user"\|"os", label: str)` | 2 |
| `assumptions.requestor` | `TEXT`, `'user'`\|`'os'`, NULL on rows asked before §2 shipped | 2 |
| `autoreview.OWNER_REQUESTOR = "os"` | the stamp value that turns owner mode on | 2 |
| `_ruling_question(..., requestor_label="")`, same on `_confirm_question` | "who asked" block only when non-empty | 2 |
| `_ruling_question(..., owner=False)`, same on `_confirm_question` | owner text only when `owner=True` | 3 |
| `Ruling.rework: bool = False`; `read_ruling(verdict, default_model="", *, owner=False)` | | 3 |
| `autoreview.MAX_REWORKS = 2` | | 3 |
| `autoreview.OWNER_ESCALATION_CAUSES = ("conflicting-authority",)` | the only cause Neo may CHOOSE on owner mode | 3 |
| cause `rework-cap` | appended to `neo_store.ESCALATION_CAUSES_OVERRIDDEN` | 4 |
| event `autoreview_reworked` | `{assumption_id, n, reason, neo_question_id, confirming, attempt}` | 4 |
| event `autoreview_owner_ruled` | `{assumption_id, n, outcome: accepted\|reworked\|escalated, reason, neo_question_id, cause}` | 6 |
| event `assumption_overruled` | `{assumption_id, n, prior_decided_by, neo_question_id, feedback, learning_id, fix_wo_id}` | 5 |
| `ops.overrule_assumption(wo_id, n, feedback) -> dict` | `{question_id, learning_id, delivered \| fix_wo_id}` | 5 |

Owner mode is a property of the ASK, never re-derived later: the stamp written at ask time
(`assumptions.requestor`) is what the verdict site, the notice and the stats read. The
same principle as `confirm_question_id`: which pass a verdict answers is recorded when it
is asked.

### 1.4 Running siblings this feature builds on

Two orders from the same improvement order touch the same code and are NOT children of
this plan:

- `wo-ec17bf74` adds `jarvis wo review --delegate`: the user hands an escalated assumption
  back to Neo, which must accept or reject-with-reason-to-the-worker. That is the same
  "Neo returns it to the worker" path §4 needs. §4 REUSES its code path; it never adds a
  second one.
- `wo-179ffdf5` relabels Neo-answered record rows in `_record_block` and the persona so Neo
  never calls its own ruling the user's. §3 edits the same persona text.

A child whose section names one of these first runs `jarvis wo show <id>`. If it has not
merged, the child asks Neo (`jarvis wo ask`) before writing code that overlaps it.

## 2. Requestor: who asked for an order, stamped at ask time

### 2.1 The predicate

`ops.requestor(store, wo) -> Requestor` answers "did a person ask for this work?".

1. Walk `parent_id` up to the root. Feature, improvement, investigation and decision
   orders all live in `feature_orders`; a work-order parent lives in `work_orders`.
   Stop at a row with no parent. Guard the walk with a visited set (a cycle is `os`,
   logged).
2. Judge the ROOT by provenance only: `kind="os"` when its `origin` is in
   `ops._SELF_ORIGINS` or it carries a steward stamp (`steward.stamp_of`), else
   `kind="user"`. Do NOT call `launched_by_os` on the root: its legacy clause reads any
   `jarvis`-origin row with a non-`worker` kind as OS, and a user-filed feature order is
   origin `jarvis`, kind `feature`. A root that is itself a work order with no parent is
   judged by `launched_by_os` (its legacy clause is right for work orders). So a user's
   `jarvis fo create` / `io create` / `do create` and everything under it is `user`.
3. `label` names the source in words the user reads, from the root: the steward stamp's
   `origin_kind` (e.g. `steward: bug_agent, decision #916`), else the origin
   (`morpheus`, `fleet health`, `neo rider`, `os`), else `you`.

So a child of a feature order the user filed is `user`, even though `launched_by_os(child)`
is true because it has a `parent_id`. `launched_by_os` itself is NOT changed:
`notify_completed`, `park_user_action` and `wo_tags` depend on its current meaning.

A fix order filed by `jarvis wo overrule` (§5) must read as `user`. §5 creates it with
origin `jarvis`, no `parent_id`, no `trigger_wo_id` and no steward stamp, and records the
link in `metadata.overrules`. This section adds a test that pins it.

### 2.2 The column and the stamp

- `assumptions.requestor TEXT` via `ProjectStore.ADDED_COLUMNS`. NULL means "asked before
  this shipped" and reads as `user` everywhere (today's behaviour).
- Written when the question is linked: in `autoreview.propose` (next to
  `store.link_assumption_question`) and in `autoreview.propose_confirmation`. A
  confirmation re-stamps with the same predicate; it never flips an `os` row to `user`
  mid-review unless the predicate itself says so.
- Advisory questions (`propose(..., advisory=True)`) are stamped too, but §4 never runs
  owner mode on them.
- A `ProjectStore` method `set_assumption_requestor(assumption_id, kind)`; no raw SQL
  outside the store.

### 2.3 What Neo reads

`_ruling_question` and `_confirm_question` gain the keyword `requestor_label: str = ""`
(this section owns it) and one information-only block, after the work order block:
`# Who asked for this work order\n<label>`, rendered only when the label is non-empty.
`propose` / `propose_confirmation` pass the label only when the stamp is `os`; a
user-origin question is byte-identical to today. Prove it against a GOLDEN constant: before
editing, capture today's output of both builders for one fixture into a test constant, then
assert the default call still equals it. This block states a fact; it does not change the
answers open to Neo — that is §3.

### 2.4 Done means

The predicate is covered for: user root, steward-stamped root, `os` root, a child of a
user-filed FO, a child of an OS-filed FO, a legacy `jarvis` row with a steward stamp, the
§5 fix order shape. The stamp lands on the row for a ruling and a confirmation question.
`tests/test_autoreview.py` and the store tests pass.

## 3. The owner prompt and how its answer is read

Pure `autoreview.py`. Ships INERT: nothing calls it with `owner=True` until §4.

### 3.1 The question text

`_ruling_question(..., owner=False)` and the same keyword on `_confirm_question`
(`requestor_label` is §2's; the owner block refers to "who asked" above it and never
repeats the label). With `owner=False` the output is byte-identical to today, proven
against a golden constant captured before editing. With `owner=True` and the delivered
pass (`early=False`):

- The answers line becomes: accept (`verdict: approve`), or REWORK (`verdict: deny`, whose
  `reason` is delivered to the worker as the change to make). `escalate: true` is open
  ONLY with cause `conflicting-authority`, and only when the decision record holds a
  question the USER answered, a message the user sent, or a pinned user ruling that the
  assumption contradicts; the `reason` names that id.
- An owner block, short, states: the user did not ask for this order (`requestor_label`
  says who did); you own this decision; a disagreement is a rework, not an escalation;
  missing data is a rework asking the worker to restate the assumption self-contained;
  `stakes` is still judged honestly and `high` still goes to the user whatever you rule.
- The early pass (`early=True`) on an owner order keeps today's answers (accept, object);
  it only adds the owner block, so an objection is understood as the owner's.

The persona (`ASSUMPTION_REVIEWER_PERSONA`) gets ONE paragraph saying the question may
grant owner mode and, when it does, the question's answers line wins over the persona's
"on a finished order your answers are accept and escalate". The persona is shared by every
assumption question (`neo.build_system_prompt` picks it by kind), so owner instructions
ride in the question, never in a second persona: that keeps the cached prefix stable.

### 3.2 Reading the answer

`read_ruling(verdict, default_model="", *, owner=False)`:

- `owner=False`: unchanged.
- `owner=True`:
  - explicit routine approval: accept, as today;
  - approval with non-routine stakes: the existing override (`overridden=True`,
    `stakes-high` etc.) — the high-stakes net is not owner-overridable;
  - `escalate: true` with cause in `OWNER_ESCALATION_CAUSES`: escalation, as today;
  - every other non-accept (`deny`, `escalate` with any other cause or none, a `refine`):
    `Ruling(accept=False, rework=True, reason=<Neo's reason>)`. A disagreement on an
    order Neo owns is a rework.
- The cause is the verdict's own `cause` key: `neo.parse_verdict` always carries one,
  filtered to the chosen causes. No new reply key.
- `escalation_cause(ruling)` returns `neo-denied` for any non-accept; a rework is not an
  escalation and §4 must never record it with that cause.

No LLM eval: the code turns every non-`conflicting-authority` escalation into a rework, so
"Neo must not escalate" is enforced by code, not by the model.

### 3.3 Done means

Golden-constant tests for `owner=False` on both question builders; owner text present with
`owner=True` and containing no `rework-cap`; a parametrized `read_ruling` table test
covering every row of §3.2. `tests/test_neo.py::test_a_model_may_never_name_an_overridden_cause`
still passes.

## 4. Owner delivery: rework, the cap, and the holds

The daemon half. Builds on §2 (the stamp) and §3 (the reader), and on the delegated
"return to worker" path from `wo-ec17bf74` (§1.4).

### 4.1 Turning it on

`autoreview.propose` / `propose_confirmation` pass `owner=True` and the requestor label to
the question builders when the stamp they just wrote is `os`. `_deliver_assumption_verdict`
reads the stamp from the assumption row and calls `read_ruling(..., owner=True)`. Advisory
questions (`is_advisory`) never run in owner mode.

### 4.2 A rework

Applied BEFORE the delivered branch re-runs `decide` (the transient-drop re-check would
otherwise see `needs_review` and a pending row and drop it):

1. The assumption stays `pending`. Its question link is KEPT (`neo_question_id`, or
   `confirm_question_id` when the rework answers a confirmation). Clearing it would arm
   the early pass while the worker reworks, and an early objection is `HELD_OBJECTED` for
   the user.
2. Deliver a PLAIN `store.queue_message(wo_id, …, source="neo")`, never an
   objection envelope (`deliver_messages` withdraws those on `needs_review`, so it would
   never arrive). The text: `[Neo, owner of this OS-origin order] rework assumption #n:
   <reason>. Change the work, then run jarvis wo finish again with a fresh summary; if the
   decision itself changed, record it with jarvis wo assume citing assumption #n.` The
   fresh-summary line matters: `settle_work_order` returns the order to `needs_review` on
   the OLD `result_summary` otherwise, and Neo would re-judge stale text.
3. Event `autoreview_reworked` with `attempt` = prior `autoreview_reworked` events for this
   assumption + 1. The cap is DERIVED from these events, no counter column.
4. Mark the Neo question `answered` if the drain already marked it `escalated` (a
   disallowed escalate read as rework is an answer, not an escalation).

On re-delivery the review pass must re-ask Neo on the same row. `decide` (HELD_ASKED) and
`decide_confirm` (HELD_CONFIRMING / HELD_CONFIRM_SPENT) take a new
`reworked_question_ids: Collection[int] = ()`, built by the daemon from
`autoreview_reworked` events the way `unreachable_question_ids` is, and treat a linked
question in that set as re-askable. The re-ask writes a fresh link.

### 4.3 The cap

When a non-accept arrives and `attempt` would exceed `MAX_REWORKS`, the assumption goes to
the user: mark the question `escalated` with cause `rework-cap` (added to
`neo_store.ESCALATION_CAUSES_OVERRIDDEN`), write `autoreview_escalated` with `cause` in the
payload, `ops.flag_assumptions_pending`. The reason says Neo asked for rework twice and the
worker's third delivery still did not satisfy it, and quotes Neo's last reason.

### 4.4 What still goes to the user on an owner order

- the high-stakes net and the secret-evidence net (code, unchanged);
- `conflicting-authority` escalations (§3.2);
- `rework-cap` (§4.3);
- OS-side holds that never reach Neo: auto-review disabled, the panel gave up, a refusal
  unanswered. Out of scope; they keep today's wording.

`HELD_OBJECTED` (an early objection holding the delivered row for the user) does NOT apply
on an owner order: the delivered pass re-asks Neo, which owns it. A delivered-pass `refine`
(today escalated with `scope-too-large`) is a rework on an owner order.

### 4.5 Done means

Daemon tests with the fake `claude` covering: owner accept settles; owner deny reworks
(message delivered, row pending, link kept, event written, order back to `running`); the
re-delivery re-asks Neo on the same row; third non-accept escalates with `rework-cap`;
a disallowed escalate becomes a rework; `conflicting-authority` escalates; a model failure
leaves the question pending with no event; an early objection on an owner row is not
`HELD_OBJECTED`; a user-origin order's delivery path is unchanged (existing tests green).

Test plumbing this section owns: the fake `claude` (`src/jarvis/testing.py`, the
`FORCE_*` switches) has no escalation-with-cause knob; add one (e.g. `FORCE_CONFLICT`
replying `escalate` with cause `conflicting-authority`). The fake's default reply
(escalate, no cause) is now a REWORK on an `os`-stamped row; that is expected. "Back to
running" needs a `daemon.tick()` after the drain. Adding `rework-cap` breaks the exact
five-member pin of the overridden causes in `tests/test_neo.py` (around line 1007):
update it here.

## 5. `jarvis wo overrule`: the user reverses an owner ruling

### 5.1 The verb

`jarvis wo overrule <wo-id> <n> --feedback "…"` (also `--feedback-file PATH|-`, like
`wo review`), implemented by `ops.overrule_assumption(wo_id, n, feedback)`. The CLI handler
holds no logic. Refused from a worker session (same guard as `ops.neo_review`).

Refusals, each naming the fix:
- no assumption `#n` on that order;
- the assumption was not decided by Neo (`decided_by != autoreview.DECIDER`), including a
  still-pending row Neo sent back for rework: "nothing of Neo's to overrule — use
  `jarvis wo review`";
- empty feedback.

It applies to any Neo-decided assumption, owner mode or not: an overrule adds a verb and
changes no existing behaviour.

### 5.2 What it does, in order

1. Find the question Neo answered: the `neo_question_id` of the latest
   `autoreview_accepted` event for that assumption (on a
   confirmation that is the confirm question, not `neo_question_id`).
2. Record the correction: the learning write factored out of `ops.neo_review` (mark the
   question reviewed-corrected, `neo.add_learning(..., origin_kind="assumption")`).
   `neo_review` calls the factored helper; its behaviour is unchanged.
3. Flip the assumption: `store.review_assumption(id, "rejected", decided_by="user",
   reason=feedback)`. This overwrites `decided_by`, so step 4 keeps the prior decider.
4. Event `assumption_overruled` (payload §1.3).
5. Where the work is:
   - order in `OPEN_STATUSES` (including `needs_review` and `waiting_pr_merge`): deliver
     the feedback to its worker with `send_message(..., source="jarvis")`, prefixed
     `The user overruled Neo on assumption #n:`;
   - order `completed` (merged): create a fix order with `ops.create_work_order`, origin
     `jarvis`, NO `parent_id`, NO `trigger_wo_id`, `metadata.overrules = {wo_id, n,
     question_id}`, title `Overrule: <assumption first line>`, description carrying the
     assumption text, Neo's reason, the user's feedback and the original PR url. §2
     pins that this shape reads as `user`;
   - `failed` / `cancelled`: record steps 2-4 only and say so in the output.

### 5.3 Surfaces

`cli.py` subcommand + help, the crib-sheet line in `CLAUDE.md` (keep operator content
first; `evals/llm/test_jarvis_judgment.py` loads it), `docs/os/operator/` entry for
`wo review`'s family, a timeline label for `assumption_overruled` in `timeline._describe`.
No dashboard button (out of scope, §8).

### 5.4 Done means

`ops.overrule_assumption` tests for: open order (message delivered, learning recorded,
row rejected, event), completed order (fix order created and `ops.requestor` reads it
`user`), failed/cancelled order (steps 2-4 only, output says so), refusals on a
user-decided row, on empty feedback and from a worker session (`JARVIS_WO_ID` set).
`neo_review`'s existing tests unchanged and green.

## 6. The owner-ruling notice

Every owner ruling the user did not make tells them, without asking for attention.

- On each owner outcome written by §4 (accepted, reworked, escalated with
  `conflicting-authority` or `rework-cap`), write event `autoreview_owner_ruled` and one
  `central.add_inbox(level="info", push=False, wo_id=…)`.
- Title: `Neo <accepted|sent back for rework|handed you> assumption #n on <wo-id>`. Body:
  the order title, who asked (§2 label), Neo's one-line reason, and the two commands:
  `jarvis wo show <wo-id>` and `jarvis wo overrule <wo-id> <n> --feedback "…"`. The
  escalated variant says why it reached the user instead of the overrule line.
- For owner orders this REPLACES the batched `The OS decided N assumption(s) for you`
  notice at the end of `_deliver_assumption_verdict`. User-origin orders keep the batched
  notice exactly.
- Timeline label for `autoreview_owner_ruled` and `autoreview_reworked` in
  `timeline._describe`.
- Notification failure must not undo the ruling: the ruling is written first, the notice
  after, a failed notice is logged.

Done means: tests that each outcome writes exactly one inbox row of level `info` whose title
starts `Neo ` (scope the count that way: `ops.flag_assumptions_pending` may add its own row
on the escalated outcome) and one event, the
batched notice is absent on an owner order and present on a user order, and the inbox
text names the overrule command with the right `<n>`.

## 7. `neo stats` split by requestor

`ops.neo_stats_report` gains `by_requestor: {"user": {...}, "os": {...}}` built from
`ProjectStore.assumption_review_facts` / `ops.assumption_row_outcomes` grouped by
`assumptions.requestor` (NULL counts as `user`). The `os` bucket adds `reworked`
(count of `autoreview_reworked` events), `rework_capped` (count of `autoreview_escalated`
events whose payload `cause == "rework-cap"`) and `overruled` (count of
`assumption_overruled` events). `assumption_review_facts` today selects no `requestor`
and filters out these event kinds: extend both. The CLI text prints one two-line block after the
existing headline; `--json` carries the dict. Existing keys and their values unchanged.

Done means: tests that write rows and events directly (no daemon) and assert both buckets;
existing `neo stats` tests unchanged.

## 8. Out of scope, and rejected alternatives

- **Auto-accept after N hours** (the cheap fix): synthesises a verdict from silence
  (kn-40db1828) and hides the decision (kn-53a20d3a). Rejected.
- **A per-requestor persona**: breaks the cached prefix for every assumption question.
  Owner text rides in the question.
- **A `rework_count` column**: derived from events instead; one fact, one place.
- **A `neo_store` requestor column**: stats are per assumption row in the project store;
  Neo's DB does not need it.
- **Dashboard overrule button**: not asked; the inbox names the CLI command.
- **Changing `launched_by_os`**: three readers depend on it; `requestor` is a new
  predicate.
- **User-origin delegation** is `wo-ec17bf74`, not this feature.

## Agent profile

You are the AUTOREVIEW OWNER-MODE ENGINEER, building one section of a Jarvis OS feature in
the dev checkout: on work orders the OS opened by itself, Neo owns assumption review and
the user is told and can overrule. You write Python in `src/jarvis/` and its tests.

The rule above every other: **a user-origin order must behave exactly as before.** Every
owner-mode behaviour is keyed off the `assumptions.requestor` stamp written at ask time
(§1.3, §2) and defaults off. If your change alters what Neo reads, or what happens to a
verdict, on an order a person asked for, you have broken the feature. Pin that with a
byte-identity or unchanged-path test in your section.

The second rule: **never fabricate a verdict from a failure** (kn-40db1828, pinned). A Neo
call that failed or returned garbage leaves the question pending and retryable; it is not
a rework, not an acceptance, not an escalation. And **never be silent** (kn-53a20d3a,
pinned): an owner ruling the user did not make is always surfaced.

What you must know about this codebase:

- **The CLI is the OS.** Never touch SQLite directly. Reads and writes go through
  `ProjectStore` / `CentralStore` / `NeoStore` methods; a user action is an `ops.py`
  function that `cli.py` calls. A CLI handler holding logic is a defect. New columns go in
  `ProjectStore.ADDED_COLUMNS`.
- **Navigate with Serena** (`find_symbol`, `find_referencing_symbols`,
  `get_symbols_overview`) after reading the memories `codebase-map`,
  `work-order-lifecycle` and `testing`. Grep only for text. Read specs with
  `jarvis spec toc` / `jarvis spec section`; your section cites §1.3 for every shared
  name — use those names exactly, a sibling relies on them.
- **The assumption review pipeline**: `Daemon._review_assumptions_of` (daemon.py) decides
  and asks (`autoreview.decide` / `decide_early` / `decide_confirm`, then `propose` /
  `propose_confirmation`, which call `neo.ask` and link the question); Neo's drain in
  `neo.py` answers; `Daemon._deliver_assumption_verdict` applies it via
  `autoreview.read_ruling` and `ops.accept_assumption`. Early pass (running order),
  delivered pass (`needs_review`) and confirmation pass (an early verdict re-checked) each
  have their own link column; advisory questions (`is_advisory`) are user-owned.
- **Escalation causes are a closed vocabulary** in `neo_store.py`. An OVERRIDDEN cause
  (`rework-cap`) is the OS's label and must never appear in any persona or question text;
  `tests/test_neo.py::test_a_model_may_never_name_an_overridden_cause` enforces it.
- **Messages to a `needs_review` worker**: a plain `queue_message` is delivered and puts
  the order back to `running`; an `assumption_objection` envelope is withdrawn. Use plain.
- **Two running siblings** (`wo-ec17bf74` delegate, `wo-179ffdf5` record labels) edit
  autoreview and the verdict site. Rebase on main first; if your section names one and it
  has not merged, ask Neo before overlapping it. Reuse, never duplicate, its path.

Conventions:

- House style (caveman full) for every word a person reads, including inbox text, commit
  messages and PR bodies. Exact strings, numbers and not/never/only are never compressed.
- Comments carry the one-line WHY, especially where the obvious alternative was rejected.
- A user-facing refusal names the command to run instead.
- Every new timeline event gets a label in `timeline._describe`.
- The PR body carries `## How to validate in production` (CI checks it).
- Tests in `tests/test_*.py` use the fixtures in `src/jarvis/testing.py` (`jarvis_home`,
  `fake_claude`, `project`); none call the real `claude` or the network. Run ONLY the test
  files covering what you changed (`uv sync --extra dev`, then
  `uv run pytest tests/<file> -q`); the full suite is CI's job (kn-356c724b). Grep
  `tests/` for tuples you extend: exact pins elsewhere go red.

Traps:

- Do not change `ops.launched_by_os`; three readers rely on it. Use `ops.requestor`.
- Do not clear a question link on rework; it arms the early pass (§4.2).
- Apply a rework before the delivered branch's `decide` re-check, which would drop it.
- Do not change `ops.neo_review`'s behaviour when factoring its learning write.
- Do not add a dashboard button, a config knob or a per-requestor persona; none was asked.
- Do not build another section's piece because it looks small; your description names who
  owns it.
