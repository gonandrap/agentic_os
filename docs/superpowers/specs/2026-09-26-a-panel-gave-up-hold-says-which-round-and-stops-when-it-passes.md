# A `panel_gave_up` hold says which round, and stops when a later one passes

Work order wo-933db3f6. GitHub issue #786. Design settled by Neo question 731 — this spec
implements that decision and does not reopen it.

Sits on top of #783 (wo-d0b97703, kn-21a400bb), already on `main`: `_hold_is_news`
(`src/jarvis/daemon.py:398`) compares against the NEWEST hold of a kind rather than all
history. That fixed A→B→A re-recording. It did not fix either half below.

## The problem

### Half 1 — the hold names no round and quotes no reason

`autoreview.HELD_PANEL_GAVE_UP` is built from a static string in three places, and each of
them knows only `round_outcome: str`:

* `decide`, `src/jarvis/autoreview.py:795-799` — "the validation panel gave up and put this
  work order in front of you — settling its assumptions would answer that for you too"
* `decide_early`, `src/jarvis/autoreview.py:942-946` — same sentence, different tail
* `decide_confirm`, `src/jarvis/autoreview.py:876-881` — forwards `round_outcome` into
  `decide`, so it inherits the sentence

Both daemon call sites read the whole round row and throw all of it away except the
outcome:

* `Daemon._judge_assumptions`, `src/jarvis/daemon.py:5119-5120` and `5132`/`5159` —
  `latest = store.latest_validation_round(wo_id=wo["id"])`, then
  `outcome = str((latest or {}).get("outcome") or "")`
* `Daemon._deliver_assumption_verdict`, `src/jarvis/daemon.py:5500-5503` — the same read,
  the same single field

`validation_rounds` carries the two facts that are dropped:
`round` (`src/jarvis/project_store.py:777`, 1-based per subject) and `reason`
(`src/jarvis/project_store.py:785`, "what was sent back" — the text the panel wrote). So
`ops.assumption_ruling_line` (`src/jarvis/ops.py:2962-2963`) renders
`Held by the OS — the validation panel gave up …` and a reader cannot tell a real
disagreement from #778's transient OAuth failure, whose round reason reads "nobody could
be reached to review this submission, so the work has not been judged". There is also no
pointer: nothing on either surface says where to read the round.

Evidence that the round reason exists and is decisive at exactly this moment:
`tests/test_autoreview.py:821-823` closes the round with `"the seats could not agree"` and
the hold written one line later (asserted at `:835`) contains none of it.

### Half 2 — the hold outlives the give-up

The hold has no storage but an `autoreview_held` timeline event
(`Daemon._note_autoreview_held`, `src/jarvis/daemon.py:5334-5390`, payload
`{code, reason, assumption_id, n}`), and both renderers show the newest event for the
subject: `ops.assumptions_with_rulings` (`src/jarvis/ops.py:2785`, newest per assumption,
ranked by `_RULING_RANK` at `src/jarvis/ops.py:2778`) and `ops.autoreview_state`
(`src/jarvis/ops.py:2693`, newest per work order). An immutable event is therefore making a
present-tense claim — kn-a2ebbbdb.

#783's fix depends on a LATER hold, or a later event of any kind, being written. Two paths
leave none:

1. **`HELD_STATUS` is suppressed at the ask site.** `_holds_not_recorded(early=False)`
   returns `HELD_STATUS` among others (`src/jarvis/daemon.py:392-395`), on the still-correct
   ground that the parked pass lists `needs_review` only. So the moment a work order leaves
   `needs_review` — which is what a forced round does: `validating`, then
   `waiting_pr_merge` — every subsequent tick holds on `status`, writes nothing, and the
   `panel_gave_up` event stays newest for ever.
2. **An armed row writes `autoreview_asked` only if the ask happens.** The row has to reach
   `autoreview.propose` (`src/jarvis/daemon.py:5167`) for an event to exist; a row that arms
   and is not asked about again (one question per assumption, `decide` condition 6,
   `src/jarvis/autoreview.py:806-809`) contributes nothing newer.

Measured on wo-15f5d969: round 2 escalated on an auth failure, round 3 was forced and
PASSED at the same commit, and both pending assumptions and the `auto_review:` line still
said the panel gave up.

Why the existing #782 regression test does not catch this:
`tests/test_autoreview.py:638-658` has a high-stakes assumption, so a THIRD hold
(`HELD_HIGH_STAKES`) is written after the passing round and becomes newest. Remove that
second reason and nothing overwrites the stale line.

### Root cause, named

The root cause is that a hold is stored as an append-only event and read as a statement
about now. The general fix is a `holds` table re-derived per tick. **That is out of scope
here** and this spec deliberately fixes one hold code at the read side instead: it is the
one hold whose subject (a validation round) is already a queryable row, so its freshness is
derivable for free, and Neo 731 chose that shape. The other hold codes keep the latent
defect.

## The fix

Three changes, in the order they depend on each other.

### (a) Thread the escalated round into the decision

`Decision` (`src/jarvis/autoreview.py:617-634`) gains one field:

```python
    #: The escalated round this hold is about, 0 when no round was named.
    round: int = 0
```

`decide`, `decide_early` and `decide_confirm` each gain two keyword-only arguments beside
`round_outcome`: `round_n: int = 0` and `round_reason: str = ""`. Defaults keep every
existing caller and every unit test calling them positionally/partially valid.
`decide_confirm` forwards both into its tail call to `decide`
(`src/jarvis/autoreview.py:876-881`), exactly as it already forwards `round_outcome` and
`stakes`.

One shared private builder in `autoreview.py`, next to `_held`
(`src/jarvis/autoreview.py:651`), so the two wordings cannot drift:

```python
def _panel_gave_up(n: int, round_n: int, round_reason: str, tail: str, fields) -> Decision
```

It returns `_held(HELD_PANEL_GAVE_UP, reason, round=round_n, **fields)` where reason is

> `the validation panel gave up on round {round_n} and put this work order in front of you — {hint} — {tail}`

`tail` is each function's existing clause, unchanged: "settling its assumptions would
answer that for you too" for `decide`, "the OS does not rule on its assumptions while it
waits for you" for `decide_early`. With `round_n == 0` the words "on round {n}" are omitted
and with an empty hint the `— {hint}` segment is omitted: `decide(round_outcome=
"escalated")` called with nothing else — which is how the table tests call it — must still
produce a sentence rather than "round 0".

WHY THE EXISTING SENTENCE SURVIVES rather than being replaced by "round N: hint": the
prefix `assumption_ruling_line` adds is the generic `Held by the OS — `
(`src/jarvis/ops.py:2963`), so a reason that opens with "round 3:" would render a hold that
never says WHO held it or why. The round number and the hint are additions to the sentence,
not a replacement for it.

The daemon records the round on the payload. `_note_autoreview_held`
(`src/jarvis/daemon.py:5388-5390`) adds `"round": decision.round` to the dict it writes;
every hold but this one carries `0`. This is the comparison key (c) needs.

**The dedupe key must grow with it**, or (c) introduces a new bug. `_note_autoreview_held`
builds `key = (assumption_id, code)` (`src/jarvis/daemon.py:5383-5387`); two consecutive
escalated rounds share that key, so round 3's give-up writes nothing and the payload still
says round 2 — which (c) would then read as stale and drop, hiding a hold that is true. The
key and its `key_of` lambda both gain the round: `(assumption_id, code, round)` and
`int(p.get("round") or 0)`. For every other code both sides are `0`, so the dedupe is
byte-identically what it was.

### (b) The hold reason stays surface-neutral; each surface adds its own pointer

The hint is derived in `autoreview.py` — whitespace-collapsed, truncated at 120 characters
with `…`, the rule `ops.objection_response_line` uses at `src/jarvis/ops.py:2934-2937`:
`" ".join(text.split())`, then `text[:117] + "…"` past 120. Those two lines are duplicated
rather than shared: `autoreview` is a pure module and importing `ops` into it to reuse a
three-line clamp would invert the layering for nothing.

No command string and no URL goes in the reason. `assumption_ruling_line`
(`src/jarvis/ops.py:2941`) is pure and renders on both surfaces; a `jarvis …` command in it
would be printed inside an HTML page and a URL would be printed in a terminal.

* **CLI.** `cli._readable_autoreview` (`src/jarvis/cli.py:179-197`) is the HUMAN-output
  collapse and already owns the one-line rendering of both the summary
  (`row["auto_review"] = state["line"]`, `:192-193`) and each assumption
  (`ops.assumption_line`, `:196`). It appends ` · jarvis validation show <wo-id>` — the
  wo-id is `row["id"]` on the same dict — to each rendered string whose subject's hold is
  this one: for the summary when `state["kind"] == "autoreview_held"` and
  `state.get("code") == autoreview.HELD_PANEL_GAVE_UP`; for an assumption when
  `(a.get("os_ruling") or {})` says the same. `--json` keeps the rows untouched, which is
  what that function's docstring already promises.
* **Dashboard.** `work_order.html:246-255` already renders `assumption_ruling_line(a)` and
  already appends one conditional link (`question →`, `:252-254`) from `a.os_ruling`. A
  second link goes beside it, on the same conditions, pointing at the round:
  `· <a href="#round-{{ a.os_ruling.round }}">round {{ a.os_ruling.round }} →</a>`, shown
  only when `a.os_ruling.code` is `panel_gave_up` and `a.os_ruling.round` is truthy (a
  legacy payload carries no round and gets no link). The anchor does not exist yet:
  `_validation.html:22` gets `id="round-{{ r.round }}"` on the per-round `<div class="row">`
  so the link lands on the round rather than on the section heading `#validation`
  (`_validation.html:19`). That is the whole template change — the round's own reason and
  outcome are already rendered there (`_validation.html:37-45`).

`ops.assumption_ruling_line` and `ops.assumption_line` are NOT given a `wo_id` argument.
They are pure one-line renderers shared by both surfaces; the pointer is per-surface by
construction, which is the same split `assumption_decider` lives under
(`src/jarvis/ops.py:2742-2746`).

### (c) Freshness derived at the enrichment point; `HELD_STATUS` stays suppressed

`_holds_not_recorded` is not touched. Its argument for suppressing `HELD_STATUS` on the
parked pass is still word-for-word true, and recording a `status` hold to refresh the screen
would be the defect `tests/test_automerge.py:608-616` names: telling the user a mechanism
declined an order it was never a candidate for.

Instead both readers drop a stale hold. One predicate, private to `ops.py`, placed beside
`_RULING_RANK`:

```python
def _panel_hold_is_stale(latest_round: dict | None, payload: dict) -> bool
```

True when the payload is an `autoreview_held` with `code == "panel_gave_up"` AND either
(i) `latest_round` is None or `str(latest_round["outcome"]).lower() != "escalated"`, or
(ii) `int(payload.get("round") or 0)` is non-zero and differs from
`int(latest_round["round"])`.

The round is read ONCE per function call via `store.latest_validation_round(wo_id=…)`, and
lazily — only when a `panel_gave_up` payload is actually seen, so a work order with no such
hold pays nothing, the discipline `assumptions_with_rulings` already applies to
`_overtaken` (`src/jarvis/ops.py:2836-2838`) and `objection_response`
(`src/jarvis/ops.py:2827-2833`).

This follows `autoreview_state`'s own precedent exactly: at `src/jarvis/ops.py:2728-2734`
it already resolves a present-tense claim (`_AUTOREVIEW_OWED_BY_USER`, "left with you")
against the current row, "once", citing
`docs/superpowers/specs/2026-09-25-a-decided-assumption-is-not-left-with-you.md` §1. A
panel hold is the same shape of claim with a different subject — the round instead of the
assumption — and is resolved the same way and in the same place.

WHAT "DROPPED" MEANS, concretely, in each function:

* `ops.assumptions_with_rulings` (`src/jarvis/ops.py:2805-2819`): inside the event loop, a
  stale payload is `continue`d and never becomes `newest[aid]`. The row therefore falls back
  to the next-best event by `(ts, _RULING_RANK)`, and to `os_ruling = None` when the stale
  hold was the only event about that assumption. `assumption_ruling_line` returns `''` for
  `os_ruling = None` (`src/jarvis/ops.py:2952` — "`''` means nothing has looked at this
  assumption"), so the pending row renders as nothing-has-looked-at-it. That is the
  designed outcome: not-looked-at is an understatement, the stale sentence is a lie, and the
  history is still on the timeline and in `jarvis validation show`.
* `ops.autoreview_state` (`src/jarvis/ops.py:2713-2721`): the `autoreview_held` branch
  cannot simply take `rows[-1]`. It walks that kind's rows from newest backwards to the
  first that is not a stale panel hold; if every one of them is, the kind contributes no
  candidate at all. The function then proceeds unchanged, so the returned dict is the newest
  surviving event of any kind — and `None` (no `auto_review:` line, the never-touched case)
  only when the whole work order has nothing else. `cli._readable_autoreview:191-193` and
  the dashboard already treat a falsy state as "no line", so neither surface needs a change
  for this.

Both functions already take `store`, so no signature changes.

**A payload written before this ships carries no `round`.** It is dropped when the current
newest round is not escalated (clause i) and KEPT when it is (clause ii cannot fire on
`0`). That is the honest reading: the failure being fixed is "a later round passed", which
clause i covers, and an order whose newest round is still escalated has in fact had its
panel give up, so suppressing the sentence there would replace a stale truth with a fresh
silence. Such a payload also gets no dashboard round link and no `jarvis validation show`
pointer only if (b)'s conditions are read off `code` alone — they are read off `code`, so
the CLI pointer DOES appear (it needs no round number) and only the anchored link is
withheld.

## Rejected alternatives

* **Re-record a hold every tick, or drop `HELD_STATUS` from `_holds_not_recorded`.** The
  cheapest patch, and it puts "auto-review: held — the work order is validating" on a screen
  for a mechanism that was never a candidate — the exact defect
  `tests/test_automerge.py:608-616` pins for auto-merge. It also reintroduces the
  event-per-tick flood `_note_autoreview_held` exists to prevent.
* **A `holds` table re-derived per reconcile tick.** The real root cause and the right
  eventual answer for all fourteen hold codes. Rejected as scope: it is a schema change, a
  new reconciler post-condition and a migration, for a bug whose subject is already a
  queryable row.
* **Put the round's freshness into `autoreview.decide`.** It would make the decision
  functions read the store, and `decide`'s docstring makes purity a stated property
  ("PURE — no store, no clock, no model", `src/jarvis/autoreview.py:726`). The decision is
  about whether to ACT; the staleness is about whether to SHOW.
* **Let each surface decide freshness.** Two readers deriving it apart is GitHub issue
  #712's defect verbatim, which is why `assumptions_with_rulings` exists at all.
* **Write the full round reason into the hold instead of a hint.** A panel reason is
  paragraphs; `assumption_ruling_line` is one line inside a list of assumptions. The 120-char
  clamp plus a per-surface pointer to the full text is the split `objection_response_line`
  already uses.
* **Delete or rewrite the stale event.** The timeline is append-only, and the hold did
  happen.

## Tests

Existing tests that encode the old behaviour — found, and what happens to each:

1. `tests/test_autoreview.py:806` `test_a_panel_that_gives_up_while_neo_is_thinking_stops_
   the_settle`, assertion at `:835` `assert "gave up" in held["reason"]`. Still passes —
   (b) keeps that phrase — but it is the one test whose round already closes with a real
   reason (`"the seats could not agree"`, `:823`), so TIGHTEN it in place: the reason must
   also carry the round number and that phrase, and the payload must carry `round`.
2. `tests/test_autoreview.py:638` `test_a_hold_returning_to_an_earlier_reason_is_recorded_
   again` (#782). Stays green and is now WEAK: its high-stakes second reason writes a third
   hold that masks the render, so it would pass with (c) absent. Leave it, and add the
   sibling below that only (c) can satisfy.
3. `tests/test_autoreview.py:938-942` asserts the `status` hold IS recorded at the SETTLE
   site. Unaffected: `_holds_not_recorded` is unchanged and `settling=True` still suspends
   the list.
4. `tests/test_autoreview.py:1266`, `tests/test_autoreview_confirm.py:105-113`,
   `tests/test_early_review.py:88` and `:92` all assert on `HELD_*` CODES, not on the
   reason text, so the new wording does not touch them.

New tests:

1. `tests/test_autoreview.py`, beside `settled_round` (`:631`) — **the hold text carries the
   round number and the truncated hint.** Park on one routine assumption, close round 1
   `escalated` with a 300-character reason, run `ask`: the `autoreview_held` payload has
   `round == 1`, its `reason` contains "round 1", contains the first words of the round
   reason, is whitespace-collapsed, and the hint segment is 120 characters ending `…`.
2. Same file — **a `panel_gave_up` hold is not rendered once a later round passed.** ONE
   assumption, routine (no second hold reason, which is what makes this the test #782's
   cannot be): ask under an `escalated` round, then `settled_round(..., "passed")`, then
   read `rulings(store, wo["id"])` (`:1260`) and
   `ops.autoreview_state(...)["line"]` via `_line` (`:862`). The ruling line is `''` and the
   summary line does not say "gave up". Assert the `autoreview_held` event is still on the
   timeline, so the test says the history is kept and only the claim withdrawn.
3. Same file — **a second escalated round refreshes the hold rather than voiding it.** Two
   consecutive `escalated` rounds and two `ask` passes: two `autoreview_held` events, the
   newer carrying `round == 2`, and the ruling line still says the panel gave up. This is the
   test that fails if (a)'s dedupe key does not grow.
4. Same file — **a legacy payload with no round number.** Write an `autoreview_held` payload
   by hand with `code=panel_gave_up` and no `round`: rendered while the newest round is
   `escalated`, dropped once it is `passed`.
5. `tests/test_autoreview.py`, in the surfaces section (`:1257` onward) — **the CLI line
   carries the pointer.** Through `cli._readable_autoreview`, the assumption line and the
   `auto_review` line both end in `jarvis validation show <wo-id>`; and a HELD_HIGH_STAKES
   hold on the same order does not get the pointer, so the test says where it does NOT go.
6. `tests/test_assumption_early_state.py` (which already guards
   `assumptions_with_rulings`' `_RULING_RANK` contract at `:240`) — **`decide_confirm`
   forwards the round.** A provisional row confirmed under an escalated round holds with a
   reason naming the round, which is the path `decide_confirm`'s forwarding covers.
7. `tests/test_early_review.py`, beside `:88` — `decide_early(round_outcome="escalated",
   round_n=4, round_reason="…")` names round 4, and the bare
   `decide_early(round_outcome="escalated")` call still produces a sentence with no "round
   0" in it.

Template rendering of the `#round-N` link is covered by whichever dashboard test already
renders `work_order.html` for an order with assumptions; the assertion to add is that the
`href` matches the `id` emitted by `_validation.html`, because an anchor that does not
resolve is the one failure a link test exists to catch.
