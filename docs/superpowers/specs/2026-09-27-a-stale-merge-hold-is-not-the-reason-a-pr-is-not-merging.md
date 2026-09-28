# A stale merge hold is not the reason a pull request is not merging

Issue #813. Neo question 836 settled the design; this spec argues *from* that decision and
does not re-open it.

## The problem

`ops.automerge_state` (src/jarvis/ops.py:2651-2716) returns the newest `automerge_held`
event verbatim, and `_automerge_line` (src/jarvis/ops.py:3322-3337) renders it as a
present-tense claim: `f"held — {state.get('reason')}"`. Both surfaces print it —
`jarvis wo show` at src/jarvis/cli.py:2646-2647, the dashboard work-order page at
src/jarvis/ui/app.py:1107.

Measured on wo-659be188: the line read `held — round 3 is waiting for CI` while the order
was at round 5 on 64b8aa2579, CI green (run 36360753216), status `needs_review`. Both
halves of that sentence were false, and the user was sent to look at a CI run that had
already passed for a round two numbers back.

It freezes for ever, because nothing can write a newer hold. `Daemon.auto_merge` returns
at src/jarvis/daemon.py:4941 (`if wo["status"] != "waiting_pr_merge": return`), so the
poll never reaches `automerge.decide` for an order that left `waiting_pr_merge`, and
`Daemon._note_automerge_held` drops `HELD_STATUS` anyway (src/jarvis/daemon.py:5947-5949)
— correctly: the reason is documented at src/jarvis/daemon.py:4903-4911. So the last hold
ever written stays the rendered one.

Root cause, and it is a class not an instance: a **present-tense claim derived from an
append-only timeline event, never re-derived against the current row** — #786,
kn-a2ebbbdb/kn-96f47efb, and the same shape `_panel_hold_is_stale`
(src/jarvis/ops.py:2848-2875) already fixed one authority along for
`autoreview.HELD_PANEL_GAVE_UP`. `automerge_held` was left out of that fix. Fixing the
class wholesale (a re-derived-holds table over every code) is out of scope below; this
spec closes the automerge hold and one sibling.

Second, load-bearing defect, without which the fix inverts itself: the dedupe key in
`_note_automerge_held` is `(head_sha, code, reason)` (src/jarvis/daemon.py:5950-5955) with
no round. Six poll-reachable codes carry reason text with **no round number in it** —
`HELD_PR_CLOSED` (src/jarvis/automerge.py:289-291), `HELD_NOT_MERGEABLE` (292-294),
`HELD_CHECKS_NOT_GREEN` (295-297), `HELD_MERGE_STATE_UNCLEAN` (298-302),
`HELD_ASSUMPTIONS` (237-240), `HELD_PLAN_ASSUMPTIONS` (244-248). At an unmoved head those
sentences are byte-identical across rounds, so round N+1's hold is deduped away, the
stored payload still says round N, and a round-based freshness check would declare a
**true, current** hold stale. This is kn-96f47efb's trap verbatim; `_note_autoreview_held`
already took the same medicine (src/jarvis/daemon.py:5581-5593).

## The fix

Read-side freshness, judged against **local current rows only** — no `gh` call from a
render path, which is `automerge_state`'s own standing rule (docstring,
src/jarvis/ops.py:2660-2665). Write-side gains exactly one thing: the round in the dedupe
key.

### 1. `_automerge_hold_is_stale(wo, latest_round, payload) -> str`

New pure helper in src/jarvis/ops.py, directly beside `_panel_hold_is_stale`
(src/jarvis/ops.py:2848) so the two freshness rules sit in one place and one reader can
see they agree. Returns the reason it is stale (`"status"` / `"round"`) or `""`. Two
checks, in this order:

**BOTH CHECKS ARE GATED ON THE STATUSES A FRESH HOLD CAN STILL ARRIVE IN**, and an order in
one of them is never judged because the next tick judges it instead:

```python
_HOLD_REFRESHABLE_STATUSES = ("waiting_pr_merge", "validating")
```

`Daemon.auto_merge` returns at src/jarvis/daemon.py:4941 on any other status whatever
`record_only` says, so `waiting_pr_merge` is the only status a hold is ever WRITTEN in — and
one tick writes a `HELD_SHA_MOVED` hold and then `_rejudge_moved_head`
(src/jarvis/daemon.py:4963-4968) moves the order to `validating`, where without the gate
every such hold is stale at birth and §3 blanks the re-judge diagnosis for the one
population it was written for. In `validating` the round machine owns the row and the poll
rewrites the hold within a tick of it settling, so the round check is skipped there too: the
running round IS the answer to "why has this not merged".

(a) `wo["status"]` is outside `_HOLD_REFRESHABLE_STATUSES` — where `Daemon.auto_merge`
    returns before `decide`, so the check is the same condition that stopped a newer hold
    from ever being written, not a second opinion about it.

(b) in `waiting_pr_merge` ONLY, `int(payload.get("round") or 0)` is non-zero and less than
    `int(store.latest_validation_round(wo_id=...)["round"] or 0)`. Strictly `<`, not `!=`:
    a payload round *above* the latest row is not a thing the store can produce, and
    treating it as stale would hide a hold on an arithmetic surprise.

**A payload with `round` 0 or absent is skipped by (b) entirely** — only (a) can reach it.
That is `HELD_ASSUMPTIONS` and `HELD_PLAN_ASSUMPTIONS`, which pass no `round_n`
(src/jarvis/automerge.py:237-248; `Decision.round_n` defaults to 0 at
src/jarvis/automerge.py:171), plus every hold written before this ships. No freshness key,
no round verdict — silence rather than a guess.

The round row is read **lazily and once**, `_stale_panel_hold`'s discipline
(src/jarvis/ops.py:2878-2896): an order whose newest event is not a hold pays nothing.

### 2. `automerge_state` MARKS, never drops

At the end of `automerge_state` (src/jarvis/ops.py:2714-2716), after the terminal and
sticky-denial resolutions have chosen `newest` — so neither of those rules changes — and
only when `newest["kind"] == "automerge_held"`:

```python
if (why := _automerge_hold_is_stale(wo, ..., newest)):
    newest = {**newest, "stale": True, "stale_because": why}
```

`kind`, `code`, `judged_sha`, `head_sha` and `round` all stay in the dict; only `line`
changes (via `_automerge_line`, which reads the marker). Dropping the event or changing
its `kind` would silently break `ops.force_validation_state`
(src/jarvis/ops.py:4184-4228), which branches on `state.get("kind") != "automerge_held"`
and then on `code` to build the re-judge control's diagnosis: a `None` state or a mutated
kind would erase the `HELD_SHA_MOVED` / `HELD_SHA_UNRECORDED` sentences that are the whole
value of that control.

This is why the contract is mark-not-drop, and it differs from `autoreview_state`, which
filters stale rows out (src/jarvis/ops.py:2769-2771): nothing downstream of
`autoreview_state` reads the payload's fields.

### 3. `force_validation_state` IGNORES a stale hold

src/jarvis/ops.py:4212 becomes the same guard with one more clause:

```python
if state.get("kind") != "automerge_held" or state.get("stale"):
    state = {}
```

Same treatment as a non-hold kind: no diagnosis, control otherwise unchanged (`refusal`,
`can_force` untouched). Diagnosing from a stale hold would tell the user the head moved
away from a commit that a later round has since judged — the exact class of claim this
spec removes, restated inside a button.

### 4. The round joins the dedupe key (write side)

In `_note_automerge_held` (src/jarvis/daemon.py:5950-5955), `round` goes into **both** the
key tuple and `key_of`:

```python
key = (str(decision.head_sha or ""), str(decision.code or ""),
       str(decision.reason or ""), int(decision.round_n or 0))
```
and the lambda gains `int(p.get("round") or 0)`. The payload already carries `round`
(src/jarvis/daemon.py:5956-5960), so nothing new is stored.

Without this, check (b) is a bug generator rather than a fix: see the six codes listed
above. `_hold_is_news` compares against the newest hold **for the same `key[0]`** only
(src/jarvis/daemon.py:405-434), so the bound this dedupe exists for — one event per
changed state, not one per two-minute tick — is preserved: `decide` is deterministic given
state, and the round only changes when a round opens.

### 5. Wording of the two replacement lines

In `_automerge_line` (src/jarvis/ops.py:3322-3337), before the final `held — …` return.
House voice there: lowercase fragment, no full stop, em dash separating the fact from its
detail, and the verb says who acted.

* `stale_because == "status"`:
  `f"not parked for merge: {wo status}"`
  — issue #813's own suggestion. It makes **no** claim about the merge mechanism at all,
  which is correct, because for this order the mechanism is not running; and it names the
  row the reader can act on. Needs the status, so the marker carries it:
  `"stale_status": str(wo.get("status") or "")`.
* `stale_because == "round"`:
  `f"round {payload round}'s hold is out of date — round {current} is the current round"`
  — names both numbers, as required, and the only verb is `is out of date`, which is a
  claim about the *hold*, never about the merge. Nothing in it can be read as "held".
  Marker carries `"stale_round": <current>` so the line needs no store.

Neither sentence reuses the stored `reason`. A stale reason quoted even in the past tense
is what sent wo-659be188's reader to a passed CI run.

### 6. Sibling in scope: autoreview's `HELD_STATUS`

One sibling only. `autoreview.decide` holds on `HELD_STATUS` at src/jarvis/autoreview.py:
834-838 (`the work order is {status}, not waiting on a review`) and `decide_early` on the
same code at src/jarvis/autoreview.py:993-997 (`…, not running — there is no worker to
tell`). Recorded holds of this code come from the early pass and from the settle site,
which suspends the suppression list (`settling=True`, src/jarvis/daemon.py:5736; the
parked pass suppresses the code at src/jarvis/daemon.py:403).

Resolved with **no payload field and no write-side change**: the claim is that the order
is not in a state this pass acts in, so it is stale exactly when the order now **is** in
one. The authority is the two `decide` conditions themselves —
`status != "needs_review"` (autoreview.py:835) and `status != "running"`
(autoreview.py:994). Lift those two literals into module constants in
`autoreview` that the conditions *themselves* read, and export the pair:

```python
REVIEW_PASS_STATUS = "needs_review"   # autoreview.decide's condition
EARLY_PASS_STATUS = "running"         # decide_early's condition
REVIEW_PASS_STATUSES = (REVIEW_PASS_STATUS, EARLY_PASS_STATUS)
```

`ops` reads `REVIEW_PASS_STATUSES`. No second list of statuses exists to drift.

**The union, deliberately, and it is slightly imprecise.** The payload does not say which
pass wrote the hold, so a hold from the early pass is also dropped once the order reaches
`needs_review` and vice versa. Both mis-drops suppress a sentence about a pass that no
longer owns the row, while the pass that does own it writes its own events on the next
tick — a lost stale sentence, never a lost live one. Adding a `pass` field to the payload
would be exact, and is rejected: it is a write-side change that answers nothing for the
holds already stored.

Mechanically it joins `_stale_panel_hold` (src/jarvis/ops.py:2878-2896) rather than adding
a parallel mechanism, so both its callers — `autoreview_state`
(src/jarvis/ops.py:2761,2769-2771) and `assumptions_with_rulings`
(src/jarvis/ops.py:2985) — get it for free and the drop-not-mark behaviour stays
consistent for that kind. `_stale_panel_hold` needs the status, so its signature takes the
`wo` row (or a `status=` keyword) instead of `wo_id`; both callers already hold the row.

### Out of scope

The other seven autoreview hold codes (`HELD_ASKED`, `HELD_CONFIRMING`, `HELD_SETTLED`,
`HELD_JUDGED`, `HELD_REFUSAL_UNANSWERED`, `HELD_OBJECTION_IN_FLIGHT`, `HELD_UNJUDGED`),
the `autoreview_provisional` and `autoreview_objected` sentences, and the general
re-derived-holds table — the lead files those as one tracker issue.

### Not changed

* `invariants.rejudge_exhausted` (src/jarvis/invariants.py:513-545) — it already does its
  own freshness check (`validated_head(latest_validation_round(...)) != head`) and reads
  the raw event, not `automerge_state`. Untouched.
* `ops.merge_state`, `ops.round_line`, `automerge.validation_standing` — none of them
  render a hold.
* `automerge.decide`'s condition table, the terminal-kind rule and the sticky-denial rule
  in `automerge_state`.

## Tests

TDD order; all in tests/test_automerge.py unless noted. `uv run pytest tests/ evals/`.

1. `test_a_hold_from_a_round_a_later_one_overtook_is_not_read_as_live` — record a hold at
   round N on a parked order, open round N+1, assert `automerge_state(...)["stale"]` is
   True, `"held"` not in `line`, and both `N` and `N+1` appear in `line`.
2. `test_an_order_awaiting_a_person_shows_no_live_merge_hold` — wo-659be188's shape: hold
   stored, status `needs_review`; assert `line == "not parked for merge: needs_review"` and
   `state["code"]` is still the stored code (the payload survived the marking).
3. `test_a_hold_at_the_current_round_on_a_parked_order_is_unchanged` — regression the
   dedupe fix protects: `HELD_CHECKS_NOT_GREEN` at the latest round, status
   `waiting_pr_merge`; assert `"stale" not in state` and `line.startswith("held — ")`.
4. `test_two_rounds_at_one_head_with_the_same_reason_both_record_a_hold` — poll twice with
   identical `HELD_CHECKS_NOT_GREEN` reason and head, a new round between; assert
   `len(events_of_kind(wo_id, "automerge_held")) == 2` and the newest payload's `round` is
   the current one.
5. `test_force_validation_state_keeps_a_fresh_diagnosis_and_offers_none_when_stale` — a
   `HELD_SHA_MOVED` hold at the current round on a parked order yields a non-empty
   `diagnosis` naming both shas; the same hold once a later round exists yields
   `diagnosis == ""` with `can_force`/`refusal` unchanged.
6. `test_a_status_hold_stops_being_shown_once_a_review_is_owed_again` (tests/
   test_autoreview.py) — record an `autoreview_held` with `HELD_STATUS`, move the order to
   `needs_review`, assert `autoreview_state(...)` carries no `HELD_STATUS` line (and that
   `assumptions_with_rulings` shows no OS ruling from it).
