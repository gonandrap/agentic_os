# A submitted verdict must settle its investigator

Work order: wo-1453a031. GitHub issue 906, filed by the user on 2026-10-01 after all four
investigations of that day left something on their plate.

Subject of the fix: `ops.submit_verdict` (src/jarvis/ops.py:8695) and the five mechanisms it
hands the investigator work order to afterwards.

Expected behaviour, in one line: **when the verdict is stored, the investigation AND its
investigator both close, with nothing left for the user.**

## The problem

`jarvis investigate verdict` IS the investigator's `jarvis wo finish`
(src/jarvis/ops.py:8700-8703, spec 2026-09-27-investigation-orders.md §2.5 step 5). The
investigation does settle. The INVESTIGATOR does not: it is handed to `ops.finish`
(src/jarvis/ops.py:8818) — the ordinary worker settlement path, built for an order that
authored code and may be judged, bounced, re-prompted and re-funded. Five separate
mechanisms then act on an order whose work is already over and whose output is already
stored.

### 1.1 The evidence (four investigations, 2026-10-01)

| order | what happened after `verdict_submitted` |
|---|---|
| wo-6be2ab21 | turn kept running ~40s, hit the $2 cap, became `budget_exhausted` with an attention item asking the user to raise the budget for work already delivered |
| wo-3d213e90 | same |
| wo-259f7b52 | same |
| wo-1aa88b2f | asked a question and stopped without finishing; carried a pending assumption (held high-stakes by the substring net because its subject's text contains `truncat`), so it landed in `needs_review`; accepting that assumption sent it to a VALIDATION PANEL ROUND, over an order that authors no code |

Plus, on every one of the four: a `heredoc_write_refused` event — turns spent being denied
while trying to create `verdict.json`.

And on the investigation row: a `WAITING_ON_USER` attention flag that is raised once, at the
transition (src/jarvis/ops.py:8798-8800), and never cleared — so the warning sign is still up
after the user acted on the subject and the subject merged.

### 1.2 Root cause

**`submit_verdict` states the OUTCOME of the investigator (`finish(...)` with a summary) and
leaves the SETTLEMENT to a machine that re-decides it.** `ops.finish` does not settle
anything by itself; it records and then asks three questions that are all wrong for this
kind:

1. `validation_applies(cfg, fresh)` (src/jarvis/ops.py:7632-7644) — true for an
   investigator: the only exclusion is `release.is_release_order`. So a panel round opens
   over a submission containing no diff. That is the wo-1aa88b2f path, and the ONE thing
   about it that is not a bug is the shape of the exclusion that already exists for release
   orders.
2. `land_when_cleared` (src/jarvis/ops.py:5134) — `needs_review` while an assumption is
   pending, `validating` while a round is open. Both are OPEN statuses. Nothing here can
   produce `completed` for an investigator carrying either.
3. Nothing stops the worker's session. `finish` has no `stop_worker_session` call; the
   process keeps typing. The ~40s of post-verdict turn on the three budget cases is that
   session, and its spend is charged to the order's $2 default cap
   (spec 2026-09-27-investigation-orders.md §2.8).

The four downstream defects are then reachable BECAUSE the order is still open and still
spending:

* **`budget.escalate` (src/jarvis/budget.py:527-557) has no terminal guard and no
  verdict guard.** Its only guard is `if wo["status"] == EXHAUSTED: return False`
  (budget.py:538-539). Five callers reach it — `Daemon.settle_work_order` (daemon.py:4772),
  `Daemon._deliver` (daemon.py:3240), `Daemon.retry_paused_turns` (daemon.py:1711),
  `dispatch.dispatch_work_order` (dispatch.py:1335), `ops._resume_after_budget`
  (ops.py:13777) — and `_deliver` deliberately relaunches a SETTLED order
  (daemon.py:3270-3272: "A user who sends a message to a finished work order means it to
  continue"), so a queued Neo answer arriving after the verdict is enough on its own.
  Settling inside `submit_verdict` is therefore necessary and NOT sufficient: the status is
  written while the turn is still running, and the cap can be crossed after the write.
* **Autoreview takes investigator assumptions.** `Daemon.auto_review`'s candidate loop
  (daemon.py:5945-5952) keys on status only — `needs_review` and `running` — with no kind
  test anywhere in the pass. So an investigator's `jarvis wo assume` row (one of the four
  writes its prompt explicitly grants, dispatch.py:1030) is put to Neo as if a code
  submission depended on it, and `autoreview.HIGH_STAKES`' substring net
  (`_HS_*`/`_HIGH_STAKES_RE`) holds it for the user on a word quoted out of the subject.
* **`validation_applies` is kind-blind** (ops.py:7644), as above.
* **The investigation's attention flag is write-once by design and clear-never by
  omission.** §2.5 step 6 of the investigation spec, and kn-089de524, correctly forbid
  re-deriving it on a tick; nothing was then written to take it DOWN when the thing it names
  goes away.
* **`verdict.json` has no sanctioned way to be created from the shell.** The one write the
  investigator may make is `Write` on the worktree's `verdict.json`
  (`hooks.investigator_write_decision`, hooks.py:1214-1258, exempt path
  `hooks.VERDICT_FILE`, hooks.py:804). A shell heredoc to the same path is refused twice
  over — `hooks.heredoc_write_decision` (hooks.py:1159-1211) denies it first in
  `preflight_decision`'s Bash branch (hooks.py:1934-1937) and records
  `heredoc_write_refused`, and `investigator_bash_decision` would refuse it too. The
  investigation spec anticipated the exact command ("without it, `cat > verdict.json <<EOF`
  is the least of it", §2.6) but nothing TELLS the session: `_HEREDOC_DENY` (hooks.py:1054)
  says "Use `Edit` or `Write`, which are auto-allowed inside your worktree", which for this
  kind is false for every path but one, and the prompt's verdict section (dispatch.py:974)
  says "Write it to `verdict.json`" without naming the TOOL.

## The fix

Six changes. Each is small; what matters is that each lands in exactly one place, because
every one of these defects is a second place that re-decided something already decided.

### 2.1 `submit_verdict` settles the investigator itself, unconditionally

In `ops.submit_verdict` (src/jarvis/ops.py:8815-8819), replace the `finish(...)` call with
`ops.close_out` (src/jarvis/ops.py:6387), which is the existing "this is over and it went
fine" mechanism shared by `jarvis wo done` and a merged pull request: it calls
`stop_worker_session` (ops.py:7369), writes `completed`, clears attention and records the
event plus a `session_stopped` row.

```
out["investigator"] = close_out(
    store, investigator_row, "verdict_submitted_settled",
    why=f"submitted a {classification} verdict for {inv_id}",
    payload={"investigation": inv_id, "classification": classification})
```

Requirements on the change:

* **Unconditional on status, not on `OPEN_STATUSES`.** The existing `investigator_open`
  read (ops.py:8810-8816) exists because re-settling is not a no-op; keep the read, but the
  only case it should skip is a DELETED row (`KeyError`). `close_out` on an order already
  `completed` writes one event and stops a session that is already gone — harmless, and
  cheaper than a status race.
* **It must share the store connection** the function already holds (ops.py:8787-8808) —
  the `finally: store.close()` block closes it before the current call site, so the
  settlement moves INSIDE the `try`, after the `verdict_submitted` event and before the
  close. The `result_summary` the old path wrote is still wanted, so write it with
  `store.update_work_order(wo_id, result_summary=...)` immediately before `close_out`: the
  record must still say what the order delivered.
* **It must not call `finish`, `land_when_cleared`, `land_finished`, `submit_for_validation`
  or `defer_red_release`.** That is the whole fix: for this kind there is nothing left to
  judge, nothing to land and nothing to defer.
* `stop_worker_session` is best-effort by design (ops.py:7379-7381) and returns
  `stopped: False` with a reason rather than raising. A session that could not be killed
  must NOT change the status: the order is `completed` either way, and the reason is on the
  event.

**Invariants that must still hold for a `completed` investigator** — all four already do,
and none needs an exemption:

* `INV-PR-RECORDED` (invariants.py:3473) — population is `landing.authored` off the
  timeline; an investigator writes no file, so `work.produced` is false and it is silent.
* `INV-WORK-LANDED` (invariants.py:3333) — `if not wo["pr_url"]: continue`; an investigator
  never has one.
* `INV-GATE-ORPHAN` (invariants.py:2201) — supersedes any open gate request on a terminal
  order, which is the right outcome: there is no worker left to run the command.
* `INV-ATTENTION-PHANTOM` (invariants.py:2220) — clears any flag on a `completed` order.
  This is also the reason the status must be `completed` and not a new one: the sweep is
  keyed on `TERMINAL_STATUSES` (invariants.py:106).

**A PENDING ASSUMPTION on a settling investigator: the row stays `pending`, and it holds
nothing.** Spelled out, because this is the case wo-1aa88b2f was:

* It is NOT accepted, not rejected and not rewritten. Only the user or Neo decides an
  assumption; a settlement that decided one would be the silent acceptance `mark_done`
  (ops.py:6340) and `ack_attention` both refuse.
* It does NOT hold the order: `close_out` does not consult `pending_assumptions`
  (ops.py:6396-6399 states that deliberately), and `land_when_cleared`'s `needs_review`
  branch is no longer on the path.
* Where it goes: it stays a row on the investigator, visible on `jarvis wo show
  <investigator>`, decidable with `jarvis wo review <investigator>`, and counted by the
  fleet-wide `ProjectStore.pending_assumptions` (project_store.py:4465-4474, which has no
  status filter). It raises no attention flag, because `INV-ATTENTION-MISSING` only walks
  `BLOCKED_STATUSES` and `INV-ATTENTION-PHANTOM` clears terminal rows. So it is in the
  queue and not in the user's face — which is the "not buried, does not hold the order"
  the issue asks for.
* Consequence worth stating: `jarvis status`' pending-assumption COUNT still includes it.
  See §5 open question 2 — do not change that silently.

### 2.2 The budget settler never parks an order whose work is over

`submit_verdict` writing `completed` is necessary and not sufficient: the turn is still
running when the verdict is stored, so the cap can be crossed AFTER the status write, and
`Daemon._deliver` escalates regardless of status. A terminal/verdict guard does NOT already
exist — the only guard in `budget.escalate` is the already-exhausted one (budget.py:538).

Add it in `budget.escalate` (src/jarvis/budget.py:527), the single funnel all five callers
pass through:

```
if wo["status"] in project_store.TERMINAL_STATUSES:
    return False          # nothing is owed on work that is over
if investigations.verdict_stored(store, wo):
    return False          # an investigator whose verdict is filed is done, whatever it spent
```

* Returning `False` is the established "nothing changed" contract (budget.py:530-531), so a
  reconcile tick that meets the same order again writes no event and sends no notification.
* The second clause is the one that survives a race: it is true the instant
  `update_feature_order(plan=…)` commits, which is BEFORE the status write and before the
  turn exits. `verdict_stored(store, wo)` is `wo["kind"] == "investigator"` and
  `wo["parent_id"]` naming a feature order whose `plan` parses to a document with a
  `classification`. One helper, used by §2.2 and §2.4.
* It must stay narrow. A live investigator with no verdict yet CAN legitimately exhaust and
  must still park: that is the §2.8 cap doing its job.
* Do not add the guard in `exhaustion` (budget.py:468): that function answers "is this order
  over its cap", which stays true and is read by `invariants.budget_blocker` and by
  `worker_session`'s launch-time last line of defence (`BudgetExhausted`, budget.py:511).
  Only the PARKING is wrong.

### 2.3 Autoreview never sees an investigator assumption — one place

The exclusion belongs in `Daemon.auto_review`'s candidate loop
(src/jarvis/daemon.py:5946-5952), as a kind test beside the status test:

```
for wo in store.list_work_orders(statuses=(status,)):
    if wo.get("kind") == "investigator":
        continue        # a diagnosis is not a submission; nothing is gated on it
```

Why there and nowhere else:

* It covers BOTH passes (`needs_review` and `running`) in one line, which the two decision
  functions cannot: a hold code in `autoreview.decide` would have to be repeated in
  `decide_early`, and `_holds_not_recorded` (daemon.py:434-441) would need a third entry.
* `_file_pending_objections` (daemon.py:5969-6002) needs no change and that is a property of
  this placement, not luck: it only acts on rows carrying `provisional_verdict='object'`,
  which only the early pass writes — excluded above, so the path is dry for this kind.
* `Daemon._deliver_assumption_verdict` needs no change either: with nothing asked, no ruling
  can arrive. A ruling filed before this ships still lands through the existing path, which
  is correct — the question was asked and paid for.
* Precedent for kind-keyed branching in a loop like this: `dispatch.feature_context`
  (dispatch.py:322-333) and `dispatch.build_worker_prompt` (dispatch.py:360-365).

### 2.4 `validation_applies` does not open a round over an investigator

**Read `jarvis learn show kn-9256fcb9` before touching this function.** It is one day old,
is about `validation_applies` specifically, ratifies "test the premise, not the kind", and
names three traps a later worker must honour — including that the two `panel_cleared` sites
move in LOCKSTEP. This spec was written without shell access and could not read that entry;
if it contradicts anything below, kn-9256fcb9 wins and the disagreement is a finding.

The two lockstep sites, both currently `release.is_release_order(fresh)`:

* `ops.finish` -> `land_when_cleared(..., panel_cleared=release.is_release_order(fresh))`
  (src/jarvis/ops.py:6321)
* `ops.review_work_order`'s landing -> `cleared = release.is_release_order(fresh)`
  (src/jarvis/ops.py:7672), used at ops.py:7677

**The mechanism: one named predicate, three lines changed, so the open question is a
one-line decision.** Add a single function — `exempt_from_validation(wo) -> bool` — and
call it in exactly three places:

```
# ops.validation_applies (ops.py:7644)
return (cfg is not None and cfg.enabled
        and not release.is_release_order(wo)
        and not exempt_from_validation(wo))

# ops.finish (ops.py:6321)
panel_cleared=release.is_release_order(fresh) or exempt_from_validation(fresh)

# ops.review_work_order (ops.py:7672)
cleared = release.is_release_order(fresh) or exempt_from_validation(fresh)
```

Why all three and not just the first: `land_when_cleared` re-READS the latest round when
`panel_cleared` is false (ops.py:5179-5183). An order for which no round was ever opened has
no row, or has an older one, and the join would then either park it in `validating` for ever
or land it on a stale verdict. That is the lockstep trap, and it is why the predicate is
named once and spent three times.

**Neo question 1195 is open on what that predicate's BODY should be, and this spec does not
decide it.** Under each answer exactly one function body changes and no call site moves:

* **kind-keyed** -> `return wo.get("kind") == "investigator"`. Cheapest; says what the
  reader means; drifts the day a second read-only kind appears.
* **premise-keyed** -> `return nothing_was_authored(wo)`, i.e. the submission changes no
  file, which is the premise `evidence.nothing_to_judge` already asserts after the fact and
  the premise kn-9256fcb9 prefers. Catches every future kind for free; costs a read, and is
  true of an ordinary worker that delivered nothing — which today is a round that correctly
  escalates.
* **kind-narrowed plus premise** -> `return wo.get("kind") == "investigator" and
  investigations.verdict_stored(store, wo)`, the shape `release.is_release_order` already
  has (release.py:88: a kind test implemented as a premise about `metadata`). Needs a
  `store` argument at all three call sites, which all three already hold.

Whichever is chosen, the predicate is ONE function; a reviewer reading the diff must see one
body and three call sites.

### 2.5 A `WAITING_ON_USER` flag clears when the thing it names is gone

**Read `jarvis learn show kn-089de524`.** A flag written on a path that re-derives every tick
re-raises itself and overwrites the user's `jarvis wo ack`. §2.5 step 6 of the investigation
spec names it as the reason the flag is raised at the transition only.

So the sweep is **CLEAR-ONLY. A tick may lower this flag; a tick may never raise it.** That
asymmetry is the whole design, and it is what keeps kn-089de524 satisfied: the raise stays in
`submit_verdict` (ops.py:8798-8800), and nothing anywhere re-derives it.

* **Host**: a new `Daemon` method, called from the reconcile band of `Daemon.tick`
  (src/jarvis/daemon.py:906-945), immediately after `self.settle_features(project, store)`
  (daemon.py:905) — the merge that clears the subject's last blocker is settled on that
  same tick, so the flag goes down on the tick the cause goes away rather than one interval
  later. Reconcile cadence, not every tick: it is a read per flagged investigation, and
  nothing waits on it.
* **Population**: investigations (feature orders of kind `investigation`) that are
  `completed`, `needs_attention`, and whose stored `plan` has
  `classification == "WAITING_ON_USER"`. Everything else is untouched — a filing failure's
  flag (§2.5's "one attention case that is NOT a classification") keeps the order in
  `planning` and is NOT in this population.
* **Predicate**: the subject named in `metadata[ops.SUBJECT_KEY]` owes the user nothing any
  more. For a work-order subject that is `not invariants.true_blockers(store, subject_row)`
  — the single source of truth for "what does this order want from me"
  (invariants.py:856-864), which already subtracts what the user acknowledged
  (`acknowledged(wo)`, invariants.py:1101). For a feature/improvement subject, the feature
  row's `needs_attention` is the equivalent read. A subject that no longer exists counts as
  cleared.
* **Action**: `store.clear_feature_attention(inv_id)` plus one `ops.feature_event` saying
  why and naming the subject. Once: the flag is down afterwards, so the population no longer
  contains it — the same "flag once by construction" argument `Daemon.settle_features`
  makes (daemon.py:1248-1252), run in reverse.
* **Honouring an ack**: nothing special is needed, and that is the point of clear-only. A
  user who acks the investigation lowers the flag; this sweep never puts it back; the raise
  site runs once per verdict and the order can never re-enter `planning`.
* Rejected host: an invariant in `check_invariants`. `clear_feature_attention` is in
  `invariants._BLOCKED` (invariants.py:4559-4570), so the repair cannot run under
  `jarvis doctor`'s read-only proxy, and "the user already dealt with it" is not a
  post-condition violation worth a `jarvis doctor` line.

### 2.6 A sanctioned way to submit the verdict document

**The code already favours ALLOWING THE WRITE, not removing the file.** The evidence is
explicit on both halves:

* `jarvis investigate verdict --from-file` is `required=True` (cli.py:1089-1093) and its own
  help says why: "a verdict is full of repo paths and quoted log lines, which is exactly what
  trips the privileged-action classifier". `jarvis fo plan` (cli.py:915) and `jarvis io
  report` (cli.py:1007) are the same decision twice over.
* An inline/stdin document would be worse, not merely different:
  `hooks.payload_reference_decision` (hooks.py:380-434) refuses a `jarvis` argument carrying
  an oversized payload or any command substitution of an unbounded producer, and a stdin form
  needs a heredoc or a pipe — which is what is being refused in the first place.
* The permitted route already exists and costs nothing: `Write` on the worktree's
  `verdict.json` is exempted by `hooks.investigator_write_decision` (hooks.py:1245-1250).

So the defect is not the permission model; it is that **nothing routes the session to the
permitted tool, and the one message it does get points elsewhere.** Two edits, both text:

1. `hooks._HEREDOC_DENY` (src/jarvis/hooks.py:1054-1061) is generic and, for this kind,
   misleading: `Edit` is refused on every path and `Write` on every path but one. Make the
   refusal kind-aware — when `env.get(WO_KIND_ENV) == "investigator"`, the denial names the
   exact remedy: `Write` the file `hooks.VERDICT_FILE`, whole, then
   `jarvis investigate verdict <inv-id> --from-file verdict.json`. One string chosen inside
   `heredoc_write_decision` where the deny is built (hooks.py:1211); the classifier, the
   parse and the recorded event are untouched. This mirrors kn-832967f3's sibling fix on
   `hooks.jarvis_verbs`: the enforcement was right and the message was what cost the turns.
2. `dispatch._investigator_prompt`'s verdict section (src/jarvis/dispatch.py:974-977) says
   "Write it to `verdict.json`", which reads as prose. Name the TOOL and forbid the shell
   route in the same sentence: "Create it with the **`Write` tool** — not `cat >`, not a
   heredoc, not `python3 -`: a shell heredoc that writes a file is refused for every worker
   in the fleet (`hooks.heredoc_write_decision`), and `Write` on this one path is the single
   write you are permitted."

Explicitly NOT done, and why: no allow-branch for a heredoc that happens to target
`verdict.json`. `heredoc_write_decision` has no `_allow` branch at all, and that absence is
what licenses it to run BEFORE `gate_decision` (hooks.py:1176-1177, hooks.py:1920-1930).
Adding one would move a security-ordering argument, to save a session one tool call.

### 3 Tests

All in `tests/test_investigation_orders.py` unless named otherwise; it already has the
fixtures (`started`, `store`, `_investigating`, `_submitted` at tests/test_investigation_orders.py:451)
and the `# -- §2.5` section to extend. Each test must FAIL on today's code for the stated
reason.

1. `test_a_verdict_settles_the_investigator_completed_and_stops_it` — submit any
   classification; assert the investigator row is `completed`, `needs_attention` false, a
   `session_stopped` event present, and `result_summary` non-empty. Fails today: `finish`
   opens a validation round and the order is `validating`.
2. `test_a_pending_assumption_does_not_hold_a_settled_investigator` — record an assumption
   on the investigator (`store.add_assumption`) with text containing `truncat`, then submit.
   Assert status `completed`, no attention, AND the assumption still `pending` and returned
   by `store.pending_assumptions(investigator)`. Fails today: `land_when_cleared` returns
   `needs_review` and flags "assumptions pending review" (wo-1aa88b2f exactly).
3. `test_a_settled_investigator_is_never_parked_on_its_budget` (new, or
   `tests/test_budget.py`) — give the investigation a cap, submit the verdict, then drive
   spend past the cap and call `budget.escalate` through `Daemon._deliver`'s path with a
   queued message. Assert the status stays `completed`, no `budget_exhausted` event, no
   notification. Fails today: `escalate` has no terminal guard (budget.py:538).
4. `test_a_live_investigator_with_no_verdict_still_parks` — the control for 3, so the guard
   cannot be widened into "investigators never run out of money". Must PASS before and after.
5. `test_an_investigator_assumption_is_never_put_to_neo` — pending assumption on an
   investigator in `needs_review` AND one in `running`, `validation.auto_review` on, run
   `Daemon.auto_review`; assert zero Neo questions filed and no `autoreview_asked` event.
   Fails today: the candidate loop keys on status only (daemon.py:5946).
6. `test_no_validation_round_opens_over_an_investigator` — `ops.validation_applies` false
   for an investigator row with validation enabled, and the settled order has no
   `validation_rounds` row. Fails today: `validation_applies` returns True (ops.py:7644).
7. `test_an_investigator_is_not_parked_in_validating_by_the_join` — the lockstep half: call
   the landing path with the exemption and assert the status is `completed`, not
   `validating`. Fails on a fix that changes `validation_applies` alone.
8. `test_a_release_order_still_skips_the_panel` — the regression guard on §2.4's shared
   predicate. Must PASS before and after.
9. `test_a_waiting_on_user_flag_clears_once_the_subject_is_clear` — submit
   `WAITING_ON_USER`; assert the investigation is flagged; clear the subject's blockers
   (settle it / merge it); run the reconcile tick; assert `needs_attention` false and one
   event saying why. Fails today: nothing clears it.
10. `test_a_tick_never_re-raises_a_cleared_investigation_flag` — kn-089de524's shape, in
    `tests/test_io_attention.py`'s idiom: raise, `jarvis wo ack`-equivalent
    (`clear_feature_attention`), tick repeatedly, assert the flag is still DOWN and no event
    was re-written; then with the subject still blocked, tick repeatedly and assert the flag
    is unchanged — never re-raised and never re-stated.
11. `test_the_heredoc_refusal_tells_an_investigator_how_to_write_its_verdict` (extend the
    §2.6 block at tests/test_investigation_orders.py:30) — `heredoc_write_decision` on
    `cat > verdict.json <<EOF` with `JARVIS_WO_KIND=investigator` still DENIES, and the
    message names `Write` and `--from-file verdict.json`. Plus the control: the same command
    from an ordinary worker keeps the existing text.
12. `test_the_prompt_names_the_write_tool_for_the_verdict` — the prompt string contains
    `Write` tool wording and the `--from-file verdict.json` command (the
    `tests/test_investigation_orders.py:818` idiom).

Full suite before the PR: `uv run pytest tests/ evals/`.

### 4 Rejected alternatives

* **Make `ops.finish` special-case the investigator kind.** The obvious fix, and it loses
  twice: `finish` is the worker contract (gate check, unlanded-work refusal, authorship
  record, round submission, landing join) and every clause of it is about code that must
  land; a kind branch inside it means the next reader has to hold two contracts in one
  function. `close_out` already exists and already does exactly the three things wanted.
* **A new terminal status for "settled by verdict".** Would need adding to
  `WO_STATUSES`/`TERMINAL_STATUSES`/`OPEN_STATUSES` and to every reader keyed on them —
  `INV-ATTENTION-PHANTOM`, `INV-GATE-ORPHAN`, bill sealing, the dashboard. `completed` is
  true and already carries every one of those behaviours; the EVENT (`verdict_submitted`)
  is what says who decided, which is `close_out`'s stated design (ops.py:6391-6393).
* **Guard the budget in `Daemon.settle_work_order` instead of in `budget.escalate`.** Misses
  four of the five callers, and misses the one that actually reaches a settled order
  (`_deliver`).
* **Exclude investigator assumptions inside `autoreview.decide` as a new hold code.** Two
  functions (`decide` and `decide_early`), two hold constants, and an entry in
  `_holds_not_recorded` — three places where the issue asks for one, and the pass would
  still spend a listing and a read per investigator per tick.
* **Drop `--from-file` and take the verdict on stdin or argv.** Rejected on the code's own
  stated reasons: the flag is `required=True` with the classifier argument written into its
  help (cli.py:1089-1093), the same decision is already made twice for plans and findings,
  and `payload_reference_decision` refuses the argv form anyway.
* **Allow a heredoc that targets `verdict.json`.** Puts an `_allow` branch into the one
  check that is permitted to run before the gate precisely because it has none.
* **Re-derive the `WAITING_ON_USER` flag every tick (symmetry with `true_blockers`).**
  kn-089de524 forbids it: it overwrites the user's ack and re-raises for ever.

### 5 Out of scope, and what is knowingly left

1. **The high-stakes substring net is not touched.** `truncat` matching a word quoted out of
   a subject id is a false positive in `autoreview.HIGH_STAKES`, and it is the reason
   wo-1aa88b2f's assumption was held for the user rather than decided. Excluding
   investigators (§2.3) means no investigator pays for it again, but the net is unchanged for
   every other kind. That is a symptom left in place deliberately: it is `stakes_classifier`'s
   territory (`regex` / `regex-tightened` / `shadow` / `classifier`, daemon.py:6282-6292),
   it has its own A/B, and widening a carve-out from one bad match is how that net rots.
2. **Why the three budget cases spent $2 in 40 seconds is not diagnosed here.** The park is
   fixed at the funnel; which of the five callers fired on each of the three orders is a
   record read this spec could not make (no shell). If the implementer can read
   `jarvis wo show wo-6be2ab21`, the `budget_exhausted` event's `doing` field names the
   prompt that was in flight and is worth one line in the PR body.
3. No change to `verdicts.py`, to the duplicate check, to the `--expedite` cascade, or to
   `jarvis investigate`'s CLI surface beyond nothing at all.

### 6 Open questions for the lead

1. **Neo question 1195** decides §2.4's predicate body. Not decided here, by instruction.
   The spec is written so the answer changes one function body and no call site.
2. **Does a pending assumption on a settled investigator still belong in the fleet-wide
   count?** §2.1 keeps it there — visible, decidable, holding nothing. If "nothing left for
   the user" is meant to include that count, the alternative is a settlement that marks such
   rows with a new non-pending status meaning "recorded, nobody is gated on it", which is a
   schema change and a Neo question of its own. Flagging rather than choosing.
3. **kn-9256fcb9 and kn-832967f3 were not read.** This seat has no shell. Both are named at
   the points they govern (§2.4, §2.6) and the implementer must read them first; if either
   contradicts this spec, it wins.
