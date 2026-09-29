# A review control for an escalated round

Work order wo-564ebedb, GitHub issue #816. Neo question 838 ruled the design; §3 and §4
restate that ruling and are not open.

## The problem

**A work order the validation panel gave up on has no review control on its page when no
assumption is pending.** The page names the ask and offers no way to answer it.

Evidence, live case wo-659be188: status `needs_review`, latest validation round
`escalated`, **zero assumption rows ever recorded**.

1. The escalation is the page's headline. `ops.escalate_validation_round`
   (`src/jarvis/ops.py:4019`) closes the round `escalated`, sets `needs_review` and calls
   `store.flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)` — "the review could not be
   satisfied — the work needs your judgement" (`src/jarvis/invariants.py:337`).
   `invariants.true_blockers` re-derives the same line every tick, case 2,
   `src/jarvis/invariants.py:835`.
2. The only review form on the page is inside `{% if unreviewed %}`,
   `src/jarvis/ui/templates/work_order.html:311-323` — text input `feedback`, buttons
   `decision=accept` / `decision=reject`, POST `/wo/{name}/{wo_id}/review`. That block is
   nested inside `{% if assumptions %}` (line 256). With no assumptions at all **both**
   gates are false, so nothing renders. `unreviewed` is
   `store.pending_assumptions(wo_id)`, `src/jarvis/ui/app.py:1085`.
3. What the page does offer instead is Conversation -> "Send to worker"
   (`ops.send_message`, `src/jarvis/ui/app.py:1528-1530`). It delivers the text and that
   is all: no `reviewed` event, no Neo learning, attention flag stays up.
4. The CLI in the same state works. `ops.review_work_order`
   (`src/jarvis/ops.py:6087-6161`) handles zero pending assumptions — the `for a in
   pending` loop is a no-op, it still records the `reviewed` event, still adds the Neo
   learning via `neo.learning_from_assumption_review`, and on a rejection with feedback
   still calls `send_message`, which resumes the open worker. An acceptance goes through
   `_land_after_acceptance`.

**Root cause: the template gate, and nothing else.** The route
(`ui/app.py:1532-1536`) and the operation are already correct for this state; the form's
visibility was tied to a pending assumption when the assumption review was the only thing
that ever put a work order in `needs_review`. A second route into `needs_review` — the
panel giving up (`Daemon._escalate`) — arrived later and inherited no control. Fixing the
gate is the whole fix, not a symptom patch.

The rejected-at-cap case issue #816 lists separately is the same state, not a second one:
`Daemon._run_validation_round`'s `elif outcome == "rejected"` branch with no rounds left
calls `Daemon._escalate` -> `ops.escalate_validation_round`, which closes the round
`escalated`. There is no distinct round outcome to test for.

## The fix

One projection in `ops`, one Jinja macro, two placements chosen by state. No new route, no
change to `ops.review_work_order`.

### 1. The predicate already exists — do not add a second

`invariants.validation_escalated(store, wo)` (`src/jarvis/invariants.py:1011-1019`) reads
`store.latest_validation_round` and is true when its outcome is `escalated`. That is
verbatim the predicate `true_blockers` case 2 uses to raise `VALIDATION_STUCK_BLOCKER`
(line 835), so the control appears exactly when the attention line asks for the judgement
and disappears when it stops. Any fresh derivation here is a second definition of "the
panel gave up" and the two would drift.

### 2. The projection: `ops.review_state`

Lives in `src/jarvis/ops.py` beside `automerge_state` (line 2896) and `autoreview_state`
(line 2985), whose docstrings state the rules it inherits. The **projection**, not the
template, owns whether a decision is owed, which round escalated, how many assumptions are
pending, and the sentence each button's effect is described by.

```python
def review_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
```

Returns `None` when no decision is owed: neither a pending assumption nor an escalated
latest round.

**The `needs_review` status check gates the ESCALATED half only.** A pending assumption is
owed whatever the status — the early pass records assumptions while the worker still runs,
and the form has rendered on a `pending` order since before this projection existed
(`tests/test_ui.py::test_mark_done_is_not_offered_while_assumptions_are_pending`). So
`escalated = wo["status"] == "needs_review" and validation_escalated(...)`, and `pending`
stands on its own. Otherwise:

```python
{
    "pending": int,            # len(store.pending_assumptions(wo["id"]))
    "escalated": bool,         # invariants.validation_escalated(store, wo)
    "round": int | None,       # the escalated round's number; None when not escalated
    "placement": str,          # "assumptions" when pending else "validation"
    "scope": str,              # what this ONE decision settles, in words
    "accept": str,             # what `decision=accept` does
    "reject": str,             # what `decision=reject` does
    "strands": str,            # warning: a bare rejection re-flags attention
}
```

`placement` is the mutual exclusion, computed **once**, in one place:
`"assumptions" if pending else "validation"`. Two independent template conditions could
both be true on a work order that is both escalated and holding an assumption — issue 212
made that co-occurrence reachable and `invariants.py:828-836` documents it — and that is
the two-forms bug. The template branches on this string and never on `pending` and
`escalated` separately.

The sentences are `ops`' because both surfaces print them (`_readable_autoreview`'s rule,
`src/jarvis/cli.py:179-215`): a phrase written in the template could not be printed in a
terminal, and one written in `cli.py` could not reach the page.

Facts each sentence must carry, per issue #816 — exact copy is the implementer's:

- `accept`: lands the work order **over the panel's objection**; and when `pending`, that
  it accepts every pending assumption too.
- `reject`: **resumes the worker** with the reason as guidance; and when `pending`, that it
  rejects every pending assumption too.
- `scope`: this one decision settles the whole order — `review_work_order` settles every
  pending assumption AND lands or sends back the order in a single call.
- `strands`: a rejection with no reason leaves the order flagged and the worker unguided.
  That is `review_work_order`'s `elif not feedback: store.flag_attention(...)` branch
  (`ops.py` ~6130), the same fact `work_order.html:321-322` already warns about.

When `placement == "assumptions"`, `round` is non-None only if a round also escalated; the
template renders the extra line naming that round and what the decision now **also**
settles, and keeps the existing assumptions-block wording otherwise.

### 3. One control, one renderer

Per Neo 838: **exactly one review form on the page, never two.** POST
`/wo/{name}/{wo_id}/review` decides the whole order, so two forms with different labels
whose buttons post identically would misrepresent scope, and a validation-section form on
an order with a pending assumption would silently accept that assumption.

A single macro — `review_form(project, wo, review)` in a new
`src/jarvis/ui/templates/_review.html` — emits the form and its warning line. It is called
from `work_order.html` in the assumptions block at line 311 (replacing the inline form) and
again beside the escalated round; `review.placement` decides which call renders. Both call
sites are in `work_order.html`, guarded by the one string, so "never two" is structural.

### 4. `jarvis wo show` prints the same projection

The point of the projection: the CLI and the page cannot word the same decision
differently. In `cli.py`'s wo-show `detail` dict (`src/jarvis/cli.py:2686-2699`), on the
**never-always rule** its neighbours state in their comments — `None` means the key is
ABSENT, like `auto_merge`, `merge_state` and `auto_review`, not present-and-empty:

```python
**({"review": owed} if (owed := ops.review_state(store, wo)) else {}),
```

Human output gets a `_readable_review(detail)` helper following the convention of
`_readable_automerge` (`cli.py:163-176`) and `_readable_autoreview` (line 179): pop the
key, replace it with the collapsed human lines, and let `--json` keep the dict because
other tooling reads the counts. Threaded into the existing composition at
`cli.py:2718-2720`.

### 5. The feature-order page must not grow a work-order form

`_validation.html`'s `section()` macro (line 17) is shared: `work_order.html:407` calls
`section(validation)` and `feature_order.html:136` calls `section(validation, 'h3')`. The
file's docstring says so outright — "One implementation for both, because a round is the
same fact either way".

**Recommendation: the form is not inside `section()` at all.** `work_order.html` renders
the macro call itself, immediately after `{{ validation_ui.section(validation) }}` at line
407, under `{% if review and review.placement == 'validation' %}`. An optional macro
parameter defaulting to nothing would work, but it puts a work-order-only concept in the
shared file and makes "does the feature-order page have this form?" a question about a
default argument rather than about which template contains the call. Keeping it out means
the feature-order page cannot render it however `section()` is later changed.

The heading that hosts it gets an anchor consistent with the round anchors
(`_validation.html:25`, `id="round-{{ r.round }}"`), so the projection's `round` can be
linked the way the assumptions block already links (`work_order.html:277`).

### 6. No JavaScript

`_validation.html:10-11` states the dashboard has none and that a validation section must
not be what introduces it. The form is a plain POST; the warning about a bare rejection is
rendered text, not a validation handler and not a `confirm()`.

### 7. Also update

`work_order.html:12` — `{% set pending = 'assumptions' if unreviewed else (...) %}`. The
`#pending` deep-link target used by notifications must land on the form when it is in the
validation section, so this reads `review.placement` rather than `unreviewed`. A
notification about an escalation that deep-links to the reply box sends the user to the
control this spec exists to stop them using.

## Tests

`tests/test_ui.py` for the page, `tests/test_validation_surfaces.py` for the projection and
the CLI (its neighbours `test_forced_validation_ui.py` are the model for a
validation-driven control).

1. The live shape: `needs_review`, latest round `escalated`, **no assumption rows at all** —
   the page contains the form (action `/wo/{project}/{wo_id}/review`, both `decision`
   button values).
2. It renders **once**, not twice, when an assumption is also pending: count occurrences
   of the action URL in the HTML; assert exactly 1, and that it sits in the assumptions
   block with the line naming the escalated round.
3. POST `decision=reject` with `feedback` on the no-assumptions order: a `reviewed` event
   is on the record and the feedback was delivered to the worker.
4. The form is **absent** from the feature-order page, for a feature order with an
   escalated round (§5).
5. `ops.review_state` returns `None` — hence key absent from `jarvis wo show --json` — for
   an order with no escalated round and no pending assumption (§4).

**Build the escalated fixture through the real path** — `ops.escalate_validation_round` —
not `store.set_status("needs_review")` plus a hand-written round row. Ruling kn-303839ee:
`set_status` alone leaves no attention flag and no closed round, so the test cannot
distinguish a flagged order from an unflagged one and passes against a page that renders
the form unconditionally.

## Not covered

- `ops.review_work_order` itself. Its zero-pending behaviour is already correct (§the
  problem, 4) and is what this control relies on.
- The wording of `VALIDATION_STUCK_BLOCKER`.
- Any control for the feature-order page's own escalations. A feature order has no
  `review_work_order` equivalent and §5 deliberately keeps this off that page.
- Final button copy. §2 fixes the facts each label must carry; the words are the
  implementer's.

## Rejected alternatives

**Drop `{% if unreviewed %}` and render the existing form whenever
`wo.status == 'needs_review'`.** One-line change, and wrong: it renders on the two other
routes into `needs_review` that `invariants.py:837-850` enumerates — an unlanded branch
and an idle worker — where "accept as is over the panel's objection" describes nothing
that happened, and it still renders nothing on an order with no assumption rows, because
the outer `{% if assumptions %}` at line 256 is the gate that actually failed in
wo-659be188.

**A second form in the validation section, conditioned on `validation_escalated`.**
Refused by Neo 838. Both forms post to one route that decides the whole order; on an order
that is escalated AND holds a pending assumption both conditions are true (reachable since
issue 212, `invariants.py:828-836`), so the user sees two differently-labelled controls
doing the same thing, and pressing the validation one silently accepts the assumption.

**Let "Send to worker" carry it — record a `reviewed` event when the order is escalated.**
Overloads a delivery primitive with a settlement decision, gives the user no accept path at
all, and hides the choice inside a tab; `work_order.html:414-416` states an ask behind a
tab is an ask that does not happen.

**Page-side derivation instead of an `ops` projection.** Jinja can call
`invariants.validation_escalated` if it is exported. Then the sentences describing what
each button does exist only in HTML, `jarvis wo show` stays silent about an owed decision,
and the two surfaces are free to disagree — the duplication `_readable_autoreview`'s
docstring (`cli.py:188-191`) was written to prevent.
