# A decided assumption is not "left with you"

Work order wo-40e98fce, GitHub issue #759. Decision taken by Neo for the user, question 698:
RE-RENDER, do not suppress.

## The problem

The work-order page banner claims the user still owes a decision on an assumption they have
already decided:

```
⚙ auto-review: assumption #6 left with you — <reason>
```

It stays on the page after `jarvis wo review` accepted assumption #6, and it stays there for
ever: nothing in the path that builds it ever reads the assumption again.

Where the line comes from, end to end:

- `src/jarvis/ui/templates/work_order.html:78-79` renders `auto_review.line`.
- `src/jarvis/ui/app.py:1015` fills it: `auto_review = ops.autoreview_state(store, wo)`.
- `src/jarvis/cli.py:2369-2370` puts the SAME dict on `jarvis wo show --json` under
  `auto_review`, so the stale sentence is on both surfaces, not just the page.
- `ops.autoreview_state` (`src/jarvis/ops.py:2327-2360`) walks `AUTOREVIEW_EVENTS`
  (`ops.py:2321`), keeps the newest event by `ts` of ANY kind, and returns
  `{**newest, "line": _autoreview_line(newest)}`.
- `ops._autoreview_line` (`src/jarvis/ops.py:2699-2731`) is pure over that dict. Two of its
  branches assert the user owes a decision, and both render unconditionally:

```python
    if kind == "autoreview_escalated":
        return (f"assumption #{n} left with you — "
                f"{state.get('reason') or 'no reason recorded'}")
    ...
    if kind == "autoreview_unconfirmed":
        return (f"assumption #{n} left with you — the OS did not confirm its early "
                f"reading: {state.get('reason') or 'no reason recorded'}")
```

**Root cause: `autoreview_state` derives a claim about who owes a decision purely from an
immutable timeline event, and never from the assumption's current row.** An event is a fact
about the past and cannot go stale; "left with you" is a fact about the PRESENT and does.
`ops.assumption_ruling_line` (`src/jarvis/ops.py:2566`) already has the exact guard this is
missing — its first two lines are:

```python
    if str(a.get("status") or "") != "pending":
        return str(a.get("decided_reason") or "").strip()
```

so the per-assumption rows on the same page are correct while the summary banner above them
contradicts them. Same guard, missing in one place.

The link back to the row exists: every escalation payload carries `assumption_id` —
`src/jarvis/daemon.py:5148` (the `payload` dict shared by the `autoreview_unconfirmed` /
`autoreview_escalated` write at `daemon.py:5155-5158`) and `daemon.py:5291-5295` (the early
pass's escalation). `ops.assumptions_with_rulings` (`ops.py:2435`) already indexes events by
that key, which is the proof the key is reliably present on current events.

## The fix

Resolve the assumption's CURRENT status inside `ops.autoreview_state` and have
`_autoreview_line` append what it says. Keep the escalation sentence verbatim; add a clause.

```
assumption #6 left with you — <reason>; since accepted by you
assumption #6 left with you — <reason>; since rejected by you
assumption #6 left with you — <reason>; since accepted by the OS (neo, claude-opus-5)
```

Suppression was rejected (see §Rejected alternatives): the banner is the record that the OS
escalated, and deleting it erases that.

### 1. `ops.autoreview_state` (`src/jarvis/ops.py:2327`) — one extra read, conditional

After `newest` is chosen and before `_autoreview_line` is called:

1. Do nothing unless `newest["kind"]` is in a module-level pair constant
   — name it beside `AUTOREVIEW_EVENTS`, e.g.
   `_AUTOREVIEW_OWED_BY_USER = ("autoreview_escalated", "autoreview_unconfirmed")` — with a
   comment saying it is the kinds whose prose asserts the user owes a decision. Every other
   kind keeps today's behaviour with zero extra work.
2. Read `aid = int(newest.get("assumption_id") or 0)`. If falsy, stop: **the line must stay
   exactly as today.** Older events, written before the payload carried the key, are
   unresolvable and a banner is not the place to guess.
3. `row = store.get_assumption(aid)` — `src/jarvis/project_store.py:3868`, the single-row
   read, taken with the ProjectStore `autoreview_state` already holds. **ONE read, and only
   on this branch.** Not `all_assumptions`: this needs one row, and the surrounding function
   is called on every page render and every `jarvis wo show`.
4. `None` row (deleted assumption) — stop, line unchanged, same rule as step 2.
5. `status = str(row.get("status") or "")`. If `status == "pending"`, stop: line unchanged.
   That covers every genuinely-still-owed escalation, which is the common case.
6. Otherwise put two NEW keys on the state dict before rendering:
   `"resolved_status": status` and `"resolved_decider": assumption_decider(row)`.
   Those names do not collide with any payload key currently written
   (`n`, `reason`, `stakes`, `model`, `neo_question_id`, `overridden`, `early`, `dropped`).

**Attribution comes from `ops.assumption_decider` (`src/jarvis/ops.py:2363`) and nowhere
else.** Do not spell `"you"` or `"the OS (…)"` a second time in `_autoreview_line`; that
function's docstring-level invariant is that one renderer owns the phrase, and
`tests/test_ui.py:594` exists to pin exactly that. `assumption_decider` handles the empty
`decided_by` (= the user) case already.

### 2. `ops._autoreview_line` (`src/jarvis/ops.py:2699`) — stays pure, signature unchanged

It stays a pure function of the state dict — that is what makes it testable without a store,
and what `test_autoreview.py` and the UI tests both lean on. It gains no parameter. In both
"left with you" branches, build the base sentence as today and append:

```python
    if state.get("resolved_status"):
        base += (f"; since {state['resolved_status']} by "
                 f"{state['resolved_decider']}")
```

Applying it in BOTH branches, from one shared suffix expression, is required: the
`autoreview_unconfirmed` sentence makes the same claim in different words and a fix to one
branch only leaves half the bug.

`rejected` must read correctly, not only `accepted`. Interpolating the status column rather
than hard-coding "accepted" is what buys that, and the unit test below pins it.

### 3. Scope: `jarvis wo show` needs no change

`cli.py:2370` serialises the dict this function returns, so the CLI is fixed by the same
edit. `ui/app.py:1015` likewise. No template change.

## Rejected alternatives

- **Suppress the line when the assumption is settled.** Loses the record that the OS
  escalated at all. Rejected by Neo for the user, question 698 — do not re-open.
- **Pick the newest event whose assumption is still pending.** Changes which event the
  banner is about, silently: a work order whose only escalation was then decided would fall
  back to an older, unrelated hold and read as if that were the last thing that happened.
  `autoreview_state`'s stated contract is newest-event-wins and this would break it.
- **Guard in `_autoreview_line` by passing it the store.** Makes the one pure renderer
  impure and store-bound. Every existing test that builds a state dict by hand breaks, and
  the DB read moves into a function called from template-render paths.
- **Write a `autoreview_resolved` event when the user reviews an assumption.** More
  machinery, a new kind in `AUTOREVIEW_EVENTS` and `_RULING_RANK`, and it would still be
  derived from the same row — with a new way to be missed (a review path that forgets to
  emit). The row IS the authority; read it.
- **Fix it on the page only.** `jarvis wo show` carries the same stale string (`cli.py:2370`)
  and would then disagree with the page.

## Tests required

Unit, in `tests/test_autoreview.py` beside the existing escalation assertion at line 696
(`assert "left with you" in ops.autoreview_state(...)["line"]`), reusing that file's
`started` / `park` / `drain` fixtures:

1. **Accepted.** Escalate an assumption through auto-review, then
   `store.review_assumption(aid, "accepted", ...)` as the user. Assert the line still
   contains `"left with you"` AND contains `"since accepted by you"`.
2. **Rejected.** Same, `"rejected"`. Assert `"since rejected by you"` — the discriminating
   case against a hard-coded "accepted".
3. **Still pending — unchanged.** The existing line-696 assertion, strengthened:
   `"since "` not in the line.
4. **Decided by the OS.** `review_assumption(..., decided_by=ASSUMPTION_DECIDER_OS,
   model="claude-opus-5")`; assert the suffix is `"since accepted by the OS (neo,
   claude-opus-5)"`, i.e. exactly `ops.assumption_decider(row)`, asserted against that call
   rather than against a literal — that is what stops a second spelling appearing.
5. **No `assumption_id` in the payload** (an older event): `store.add_event(wo_id,
   "autoreview_escalated", {"n": 1, "reason": "..."})` with no `assumption_id`; assert the
   line is byte-identical to today's and no exception is raised.

UI, in `tests/test_ui.py`, next to
`test_the_auto_review_line_shows_before_there_is_a_pull_request` (line 637) and following
`test_a_pending_assumption_says_what_the_os_did_with_it` (line 562) in style — hand-written
events, `client` and `project` fixtures, `ProjectStore(project)`, page fetched via
`client.get(f"/wo/proj_a/{wo['id']}")`:

6. **The banner stops claiming a decision is pending.** Add an assumption, write an
   `autoreview_escalated` event carrying its `assumption_id`, `store.set_status(wo_id,
   "needs_review")`, assert the page says `"left with you"` and NOT `"since accepted"`; then
   `store.review_assumption(aid, "accepted", reason="fine")`, re-fetch, and assert the page
   still says `"left with you"` (the escalation is still on the record) and now also says
   `"since accepted by you"`.

## Not covered

- The per-assumption rows below the banner. `assumption_ruling_line` is already correct.
- `automerge_state` / `_automerge_line` (`ops.py:2300`). Same newest-event-wins shape, and a
  head SHA can go stale the same way, but that is a different claim about a different record
  and is not in this work order.
- The other seven `AUTOREVIEW_EVENTS` kinds. Only the two that say "left with you" assert
  something the user can falsify by acting; `autoreview_accepted` and friends are statements
  about what the OS did, which stay true.
