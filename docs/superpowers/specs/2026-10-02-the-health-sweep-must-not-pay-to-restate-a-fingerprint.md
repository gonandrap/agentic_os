# The health sweep must not pay to restate a fingerprint

Work order wo-deaf566b. Evidence: kn-f1ef52df (io-edacb3ea), alarm al-4bb82f7e, Neo
questions 1040, 1217 and 1241, standing rule kn-1cec46b5.

## The problem

Two defects, one root cause: **the sweep spends a model call to learn something the OS
can already derive, and its packet withholds the derivation.**

### 1. A stale look is paid for even when nothing it could say is new

`health.due` (src/jarvis/health.py:93-125) ends at

```python
    return "stale" if since >= stale else None
```

`src/jarvis/health.py:125` — fingerprint UNCHANGED, so by that module's own contract
("two units with the same fingerprint are the same situation", health.py:62-63) nothing
about the unit has moved. The trigger fires once per `health_stale_minutes` window
(health.py:104-109, deliberately, and that part is correct), and
`Daemon._health_sweep` hands it to `supervisor.review_health`
(src/jarvis/daemon.py:4281-4285), which buys a `structured.request`
(src/jarvis/supervisor.py:916-937) to conclude what the fingerprint already said.

Measured in the io-edacb3ea window: 226 sweep calls, $33.68, 50 alarms, 1 of which
asked for the user. Repeats on unchanged subjects: fo-ac00376e three times ("Nothing is
moving"), wo-9f00e3b5 twice, wo-0cb6dc6b twice, wo-3b93b1ea twice. The money bought a
restatement; the alarm-side dedupe (`probes_reported_at`,
src/jarvis/project_store.py:3657-3675) correctly suppressed the duplicate alarm, which
is precisely why the spend produced nothing.

### 2. The packet never states a blocker the OS has already derived

Alarm al-4bb82f7e on wo-fb7c0fc2 — *"This order is still pending with no session…
nothing has changed since the last check. The packet does not show why."* —
**was true when raised.** Alarm ts 1790713900; that order's first `dispatched` event is
1790751962, 38,000s later; it was `pending` with `depends_on: ["wo-5e0d3ec2"]`. It reads
as a misread only because `jarvis alarms` renders the order's CURRENT status
(`completed`) beside a historical reason.

The defect is the packet. `build_evidence`'s work-order section
(src/jarvis/supervisor.py:631-642) states id, status, model, what kind of session it is,
title and brief, then the session lines and what was last said — and never the order's
unmet dependencies, nor any other re-derivable blocker. So an order correctly waiting on
a dependency looks unexplained, the `waiting-on-nobody`-shaped probe fires, and Neo
escalated it to the user (question 1040: *"no usage-limit hold, no gate, no dependency
and no open question"* — three of those four are columns the OS holds).

### Root cause, named

The sweep's spend decision reads ONE bit of state (did the fingerprint move) and the
judge's packet reads none of the blocker state. Both halves of the OS already know why a
still unit is still: `ops.blocked_by` (src/jarvis/ops.py:916-924),
`invariants.true_blockers` (src/jarvis/invariants.py:856-1101), `holds.held`
(src/jarvis/holds.py:198-209), `ProjectStore.pending_assumptions`
(src/jarvis/project_store.py:4465) and the row's own `status`/`pr_url`/`pr_state`. None
of that reaches either decision. Nothing below is a symptom patch: the fix routes those
existing derivations into both.

## The fix

Four parts. (a) names the reason, (b) gates on it, (c) re-asserts for free, (f) puts it
in the packet. (e) makes the blocker set a catalog setting.

### (a) `health.blocker(pstore, subject, cfg)` — a pure helper, no model, no transcript

New function in `src/jarvis/health.py`, beside `fingerprint`, returning
`str | None`: the id of the first re-derivable reason this unit is standing still, or
None. It may take `pstore` (so does `fingerprint`, health.py:55) and must stay as cheap
and deterministic as `fingerprint` — the module docstring's rule (health.py:8-10): it is
computed per open unit per sweep tick, so nothing here opens a session file or calls a
model.

It lives in `health.py` and not in `supervisor.py` because `due` is the only consumer
that must stay pure, and a helper in the module that owns the spend decision cannot be
bypassed by a caller that forgets it.

Seven blocker ids, evaluated in this order, FIRST MATCH WINS. Each comes from the
existing canonical re-derivation — nothing new is written:

1. `user` — `invariants.true_blockers(pstore, row)` non-empty (invariants.py:856). That
   function *is* "what does this work order want from me" (its own docstring,
   invariants.py:862-864), and every other surface already agrees with it. First because
   it is the one answer that outranks the rest: an order owing the user a decision is
   explained whatever else is also true, and `true_blockers` already ranks within itself.
   It also subsumes the `pr_state == 'CLOSED'` case (invariants.py:1051) and the dead
   dependency case (invariants.py:1116-1117), so neither is duplicated below.
2. `dependency` — `ops.blocked_by(pstore, row)` non-empty (ops.py:916-924). NOT
   `invariants.dead_dependencies` (invariants.py:1416-1423), which answers the narrower
   "can never clear", and NOT `true_blockers`, which is silent on ordinary blocking by
   design (invariants.py:1114-1119: *"ordinary blocking is silent on purpose"*). Ordinary
   blocking is exactly the wo-fb7c0fc2 case, so this is the one reason that needed its
   own read.
3. `pull-request` — `row["status"] == "waiting_pr_merge"` and `row["pr_url"]` set. Pure
   row read, no network: `github.pr_view` is the daemon's poll, never an invariant's or a
   sweep's. `pr_state == 'CLOSED'` cannot reach here — the status gate plus rule 1 above.
4. `usage-limit` — an OPEN `holds.Hold` whose `cause` is `holds.PAUSE_USAGE_LIMIT`
   (`"usage_limit"`).
5. `api-outage` — an OPEN hold whose `cause` is `holds.PAUSE_TRANSIENT` (`"transient"`).
6. `expired-login` — an OPEN hold whose `cause` is `holds.PAUSE_AUTH` (`"auth"`).
7. `assumptions` — `pstore.pending_assumptions(row["id"])` non-empty
   (project_store.py:4465). Last, and reachable only where rule 1 declined it —
   `true_blockers` suppresses the assumptions line while `neo_reviews_later` holds
   (invariants.py:872-884), and an assumption with Neo is still a re-derivable reason the
   unit is still.

4-6 share one read, `holds.held` (holds.py:198-209), documented cheap enough to run per
running work order per reconcile tick (holds.py:202-204) and reading the OPEN/CLOSE event
pairs rather than inferring a gap (holds.py:17-23) — so "held right now" is a fact, not a
guess.

**One id per transport cause, not one for all three** (Neo question 1241, Option B). The
three `holds.TRANSPORT` causes (holds.py:68) have different remedies and different
expected durations: a spent window clears itself on a known deadline, an API outage
clears itself on an unknown one, and an expired sign-in clears only when a person runs
`/login` and may therefore never clear at all (worker_session.py:1096-1108). (e) lets a
catalog narrow the set, and a single id makes the one narrowing a project would actually
want unsayable: *a spent window explains stillness, an expired sign-in does not.* Under
one id those two are indistinguishable, so a project either pays to restate both or goes
free on both.

The ids are hyphenated, like `pull-request`. `usage-limit` keeps that exact spelling —
not `usage_limit` matching the cause — because catalogs already name it and renaming it
would break them.

The link between an id and its cause is ONE module-level map in `health.py`, beside
`blocker`, plus one resolver:

```python
_TRANSPORT_CAUSE_NAMES = {
    "usage-limit": "PAUSE_USAGE_LIMIT",
    "api-outage": "PAUSE_TRANSIENT",
    "expired-login": "PAUSE_AUTH",
}


def transport_blockers() -> dict[str, str]:
    """`_TRANSPORT_CAUSE_NAMES` with each name resolved to the `holds` pause cause."""
    from . import holds

    return {blocker_id: getattr(holds, name)
            for blocker_id, name in _TRANSPORT_CAUSE_NAMES.items()}
```

The values are the `holds` constant NAMES rather than the constants themselves because a
module-level `from .holds import …` in `health.py` CANNOT import: `catalog.py:13` does
`from . import health as health_mod`, `holds:63` does `from .worker_session import
PAUSE_*` and `worker_session:53` does `from .catalog import ProjectSpec`, so the chain
raises `ImportError: cannot import name 'ProjectSpec' from partially initialized module
'jarvis.catalog'`. `transport_blockers()` imports `holds` lazily inside the function,
which is also why no cause literal is ever re-spelled: every consumer calls the resolver.

Canonical, and the reason it is a map and not three `if` branches: its values must cover
`holds.TRANSPORT` exactly, which is one assertion (test 7) rather than a review. The
catalog's known-id list in (e) is derived from it too, so a fourth transport cause added
to `holds` later fails loudly in one place instead of going silently unexplained.

Feature orders. The helper takes a `subject` dict, so it must answer for
`kind == "feature_order"` too — fo-ac00376e's triple repeat is in the evidence. Rule:
derive the children (`pstore.feature_children`, as `fingerprint` already does at
health.py:72) and return `children` when every non-settled child has a blocker by the
rules above and none is running; otherwise None. No new concept, one reuse of the
work-order helper per child, and a feature with no children or one live child stays paid.
**This is the one sub-decision Neo question 1217 did not settle** — see Open questions.

### (b) `due` keeps its purity; the blocker arrives as an argument

`health.due` gains one keyword argument, `blocker: str | None = None`, and
`health.TRIGGERS` (health.py:26) gains `"re-assert"`. `due` never calls `blocker()` — it
reads no state today (health.py:98-100 says why) and must not start.

The gate is Option A, narrow and exact. At the stale clause (health.py:125), return
`"re-assert"` when ALL FOUR hold:

1. the fingerprint is unchanged (already the clause's condition);
2. `since >= stale` (already the clause's condition);
3. `str(review.get("outcome")) == "findings"`;
4. `blocker is not None`.

Anything else that reaches the stale clause returns `"stale"` and stays PAID. In
particular a previous `clear` buys a paid stale re-review: a clear verdict said nothing
is wrong, so there is no prior judgement to copy forward and a free row would be the OS
inventing one. `"first-look"` and `"changed"` are untouched.

`Daemon._health_candidates` (daemon.py:4214-4252) computes the blocker beside the
fingerprint and passes it in. `_health_sweep` (daemon.py:4281-4285) routes on the
trigger: `"re-assert"` to the new function in (c), everything else to `review_health`.

Cap. `health_max_units_per_tick` (daemon.py:4282) bounds SPEND, so it applies to the
paid triggers only; every due `re-assert` is processed. Otherwise a mostly-parked project
would spend its whole cap on free rows and starve the paid looks that the cap's rotation
(daemon.py:4216-4220) exists to guarantee.

### (c) `supervisor.reassert_health` — one row, no call

A sibling of `review_health` in `src/jarvis/supervisor.py`, because the thing it writes is
the thing `review_health` writes and a second writer of `health_reviews` belongs next to
the first. It takes the same arguments minus `neo_store` and `probes`, and:

* computes `health.fingerprint` itself, for `review_health`'s stated reason
  (supervisor.py:884-888): the row must record the state actually judged;
* writes exactly one `pstore.record_health_review(..., trigger="re-assert",
  outcome=prior["outcome"], findings=prior["findings"], detail=prior["detail"])` at
  `ts=now` (`record_health_review` stamps `db.now()` itself, project_store.py:3570);
* raises NO alarm, writes NO `health_finding`, and calls NO `flag_attention`.

Why that is safe rather than a silent drop: the fingerprint is identical, so
`probes_reported_at` (project_store.py:3657-3675) already returns every probe the prior
review named, and `review_health` would have deduped every one of them
(supervisor.py:971-975). The alarm-side dedupe stays exactly where §4 of
docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md puts it. Copying
`detail` forward keeps that memory consistent: the extra `outcome='findings'` row at the
same fingerprint is a set union with itself.

No `health_reviewed` event either. That kind is in
`project_store.ALARM_EVENT_KINDS` (project_store.py:602-604) and therefore excluded from
the fingerprint by `health.observer_kinds` (health.py:34-52), so writing one would be
harmless — and omitting it is still right: the event means "a judge looked", and none did.

No `HEALTH_WHY` entry (supervisor.py:806-811). That table is read only when a packet is
built (supervisor.py:912); an entry for `re-assert` would promise a prompt that never
goes out.

No schema change and no store change. `trigger` is a free TEXT column
(project_store.py:749); `outcome` stays inside `HEALTH_OUTCOMES`
(project_store.py:552), so `record_health_review`'s assertion (project_store.py:3563)
passes. The row IS counted by `last_health_review` (project_store.py:3575-3594) and by
`last_health_attempt_ts` (project_store.py:3596-3616), deliberately in both cases: the
first is what slides the stale window forward so a re-assertion costs at most one row per
window, and the second floors the next look exactly as a paid stale review already does.

### (d) INV-OS-HEALTH-SWEEP-DARK stays green, on purpose

`check_os_health_sweep_alive`'s judgement query is

```sql
SELECT ts FROM health_reviews WHERE outcome IN ('clear','findings')
ORDER BY ts DESC, id DESC LIMIT 1
```

src/jarvis/invariants.py:3302-3304. A `re-assert` row carries a copied `outcome` in that
set, so it counts and the invariant stays green on an all-parked project. **Decided
behaviour, not an oversight** — Neo question 1217: requiring a paid trigger there would
alarm `critical` (invariants.py:3329) on a legitimately all-parked fleet, which is the
OS asking the user to look at something the OS created and understands.

The transport canary is unaffected: `check_health_sweep_produces_judgements`
(INV-HEALTH-SWEEP-MUTE, invariants.py:3130-3186) fires on a run of `failed` rows, and
every `changed` fingerprint and every `first-look` still buys a real call — so a broken
prompt or a broken transport still produces failures to count. Pinned by
`tests/test_health_sweep.py::test_a_sweep_that_never_judges_anything_is_reported`
(test_health_sweep.py:889) and
`::test_the_canary_is_silent_on_a_sweep_that_works_and_on_one_that_never_ran`
(test_health_sweep.py:908), plus the new test 9 below.

### (e) The blocker set is a catalog setting

`supervisor.health_reassert_blockers` on `catalog.SupervisorConfig`
(src/jarvis/catalog.py:1164-1203): a whole immutable `tuple[str, ...]` of blocker ids,
defaulting to all seven, addressed as one list the way `supervisor.probes`
(catalog.py:1187-1191) and `supervisor.remedies.allowed` (catalog.py:1146-1161) are.
Never a module constant — Neo question 1217's one condition, and kn-1cec46b5's standing
rule.

Parsed by a new `_parse_blockers(raw, base, where)` alongside `_parse_remedies`
(catalog.py:1916-1938), whose shape it copies exactly: field-level per-project
inheritance, a non-list refused as `'"{where}" must be a list of blocker ids'`, and an
unknown id a `CatalogError` naming the known ids — `GateConfig.parse`'s rule and
`_parse_remedies`' reason (catalog.py:1919-1922): a permission the user believes they set,
silently unset, is the failure the whole block exists to prevent. The known-id list
includes the three transport ids from `health.transport_blockers()`, read from it rather
than re-spelled. Wired in `_parse_supervisor` (catalog.py:2029-2037) beside `probes` and
`remedies`.

**The trap:** `health_reassert_blockers` MUST be added to `_SUPERVISOR_NON_NUMERIC`
(catalog.py:1913). That set's own comment says what happens otherwise — the reflective
`int()` cast at catalog.py:2036 is a `TypeError` on every catalog load.

`health_stale_minutes` and `health_every_ticks` DO NOT MOVE
(catalog.py:1193-1196). The unit is still looked at exactly as often; the look is just
free when there is nothing to buy.

### (f) The packet states the blocker

In `build_evidence`'s work-order section (supervisor.py:631-639), after the
`this session is …` line, add one line when and only when `health.blocker` returns an id:

```
blocked: <the sentence for that id>
```

For the three transport ids the sentence is READ FROM `holds.HOLD_CAUSES`
(holds.py:78-87) — "a fleet usage limit", "a Claude API outage", "an expired Claude Code
sign-in" — via `transport_blockers()`, never re-spelled here. That table exists precisely
so "the report, the alarm and the dashboard cannot call one hold three different things"
(holds.py:75-77), and the packet is a fourth surface.

ABSENT when there is no blocker — not empty, not `blocker: none`. Two reasons: an
unblocked order's packet keeps its current bytes, and a line asserting the absence of a
blocker is a claim the judge would weigh, where silence is the status quo it already
reads.

**The pin:** `tests/test_supervisor.py:830` `EXPECTED_WORK_ORDER_PACKET` fixes that
packet byte for byte (asserted at test_supervisor.py:877, with the companion at
test_supervisor.py:880-914), because it is the cached prompt prefix of every review the
OS runs — a packet that changes shape reprices them all silently (test_supervisor.py:824-
829). The fixture's order is `running` with no dependency, no pull request and no pending
assumption, so the helper returns None and **the literal must come out of this change
unchanged**. If it does change, that is a reprice and must be a deliberate, reviewed edit
of the literal, not a re-capture.

## Tests

Each names what it would catch. In `tests/test_health_sweep.py` unless stated.

**The spend matrix** (the four cases the fix is):

1. `test_a_stale_unchanged_finding_on_a_blocked_order_is_re_asserted_for_free` — the
   defect itself. One paid sweep raises a finding on a `pending` order with
   `depends_on`; advance the clock past the stale window; the second sweep adds ZERO
   `_health_calls`, writes one more `health_reviews` row with `trigger == "re-assert"`,
   and that row's `outcome`/`findings`/`detail` equal the prior row's. Catches a
   regression that re-pays, and one that re-asserts without recording.
2. `test_a_prior_clear_still_buys_the_stale_call` — same shape with the first sweep
   `clear`. Exactly ONE more call. Catches the over-wide gate that would invent a
   judgement from a verdict that found nothing.
3. `test_an_unexplained_still_order_still_buys_the_stale_call` — same shape, prior
   outcome `findings`, no dependency and no hold. Exactly ONE more call. Catches a gate
   that drops condition 4 and makes every stale look free — the regression that would
   hide a genuinely unexplained stall.
4. `test_a_changed_fingerprint_always_buys_a_call` — blocked order, prior `findings`,
   then `add_event` to move the fingerprint. ONE more call, `trigger == "changed"`.
   Catches a gate accidentally hoisted above the fingerprint comparison.

**The guard rails:**

5. Existing `test_a_still_true_symptom_is_not_re_raised_until_the_unit_moves`
   (test_health_sweep.py:471-512) must pass UNEDITED. Its subject is
   `_wo(store, status="running")` — no dependency, no pull request, no assumption, and
   `parked_reason` returns None with no turn row — so no blocker is re-derivable, the
   trigger stays `"stale"`, and all four of §4's assertions (three calls, one finding,
   status out of the predicate, `changed` on a real move) hold. This is the dedupe test
   the whole §4 design rests on; if it needs an edit, the gate is too wide.
6. `test_a_re_assertion_raises_no_alarm_and_puts_no_flag_back` — after case 1, the
   finding count is unchanged, `needs_attention` is still 0 after a `clear_attention`,
   `probes_reported_at` returns the same set, and no `health_finding` or
   `health_reviewed` event was written. Catches the §6.3 wallpaper failure arriving
   through the free path.
7. `test_each_blocker_id_comes_from_its_canonical_source` — a pure unit test per id
   against `health.blocker`: an unmet dependency, a `waiting_pr_merge` order with a
   `pr_url`, an order with a pending assumption, one `true_blockers` case, and a live
   hold per transport cause asserted SEPARATELY — `usage_limit` yields `usage-limit`,
   `transient` yields `api-outage`, `auth` yields `expired-login`; plus None on a plain
   `running` order. Also asserts `set(health.transport_blockers().values()) ==
   holds.TRANSPORT`, so a fourth transport cause added to `holds` fails here instead of
   becoming an unexplained stall. Catches a reason re-derived locally instead of reused,
   which is how two surfaces come to disagree, and an id wired to the wrong cause.
8. `test_the_blocker_set_is_the_catalogs_to_narrow` — an unknown id raises `CatalogError`
   naming the known ids; a project override replaces the list field-by-field while the
   rest of `os.supervisor` is inherited; a project that disables `dependency` gets a PAID
   stale look on a blocked order. In `tests/test_catalog.py` for the first two. Catches a
   module constant smuggled back in, and the `_SUPERVISOR_NON_NUMERIC` trap (which would
   otherwise fail every catalog test at once — loudly, which is the point).
9. `test_an_all_parked_project_keeps_the_os_sweep_invariant_green` — a project whose only
   open units are blocked, swept only by re-assertions for longer than
   `OS_HEALTH_SWEEP_DARK_MINUTES` (invariants.py:3194), yields no
   INV-OS-HEALTH-SWEEP-DARK violation; and a run of `failed` rows still trips
   INV-HEALTH-SWEEP-MUTE. Catches (d) being "fixed" later by someone who reads the free
   row as a dark sweep.
10. `test_the_work_order_packet_names_a_blocker_and_stays_silent_without_one` — in
    `tests/test_supervisor.py`: the blocked order's packet contains the `blocked:` line,
    the sentence for a held order is the `holds.HOLD_CAUSES` string verbatim, and
    `EXPECTED_WORK_ORDER_PACKET` still matches byte for byte for the unblocked one.
    Catches the `blocker: none` shape, which would reprice every cached review, and a
    hold sentence re-spelled in the packet.
11. `test_a_re_assertion_does_not_spend_a_paid_slot` — a project at
    `health_max_units_per_tick=1` with one blocked re-assert candidate and one changed
    unit: both are handled in one tick, one call. Catches the cap starving paid looks.
12. `test_narrowing_to_the_usage_limit_still_pays_for_an_expired_login` — a project with
    `health_reassert_blockers: ["usage-limit"]`; a stale unchanged `findings` order held
    by an OPEN `auth` hold buys exactly ONE more call, and the same order held by a
    `usage_limit` hold is re-asserted for free. This is the narrowing the split exists
    for; catches a re-collapse of the three causes under one id, which would make both
    halves behave the same and leave the distinction unsayable.

**Note for the test author:** `catalog._parse_supervisor` refuses every supervisor
integer below 1 (catalog.py:2038-2040), so the stale clause is unreachable by
configuration — the only way in is to move the clock. `tests/test_health_sweep.py` has
the idiom: the `_Clock` callable over `jarvis.db.now` (test_health_sweep.py:50-76) with
`_sweep(daemon, clock)` (test_health_sweep.py:110-124), and `_health_calls(fake_claude)`
(test_health_sweep.py:94-96) is how a sweep's calls are counted — it keys on the
CHECKLIST, because the sweep shares `SUPERVISOR_PERSONA` with the cost review.

## Rejected alternatives

* **Make the `stale` trigger fire once for ever.** The obvious fix, and
  `health.due`'s own docstring already refuses it (health.py:104-109): §4's
  four-consecutive-sweep dedupe test cannot be produced by any strictly-once rule, and
  once-for-ever moves the dedupe out of the alarm side where §4 put it. It would also
  silence a unit for good on a fingerprint that stopped moving for an uninteresting
  reason.
* **Raise `health_stale_minutes`.** Buys quiet by watching less. The unit then sits
  unlooked-at for longer whether or not anything is known about it, and the paid
  restatement still arrives — later, at the same price. The defect is not the cadence.
* **One `usage-limit` id covering all three `holds.TRANSPORT` causes.** Shorter list, one
  `in TRANSPORT` test, and it was this spec's first answer — rejected by the user and by
  Neo question 1241. It destroys the only narrowing a project would reach for: a spent
  window explains stillness and an expired sign-in does not, and under one id a catalog
  cannot say so. It also lies by name, calling an outage a usage limit in the packet.
* **Let `due` call `health.blocker` itself.** Shorter call site, and it destroys the one
  property that makes the spend decision testable without a store (health.py:98-100).
  Every `due` test would need a `pstore`.
* **Skip the unit entirely instead of writing a row.** Free and wrong three ways: the
  stale window would never slide (so the skip is re-decided every tick with no record),
  INV-OS-HEALTH-SWEEP-DARK would go critical on a parked project, and `jarvis doctor`
  would have no evidence that the unit was looked at at all.
* **Put the blocker in the packet and stop there** (fix (f) alone). It fixes defect 2 and
  leaves the $33.68: the judge would read the blocker and still be paid to say so.
* **Teach the probes not to fire on a blocked unit.** Moves a cheap, deterministic fact
  into a prompt, where it is re-derived by a model per sweep at full price and can be got
  wrong. `probes.py` describes symptoms; it is not where state is read.

## Out of scope

* `jarvis alarms` rendering an order's CURRENT status beside a HISTORICAL reason — the
  thing that made al-4bb82f7e read as a misread. Real, and a separate defect in a
  separate surface. Worth filing; not fixed here.
* Any change to `health_stale_minutes`, `health_every_ticks`,
  `health_min_interval_minutes` or `health_max_units_per_tick` defaults.
* The feature-order packet (`_feature_lines`, supervisor.py:620) — (f) touches the
  work-order section only.
* Retrofitting existing rows. Nothing re-derives a past trigger, by `TRIGGERS`' own
  design note (health.py:19-21).

## Open questions

1. **The feature-order rule in (a).** Neo question 1217 settled a blocker catalog made
   of work-order concepts; the children rule is this spec's inference from the evidence
   (fo-ac00376e, three paid repeats of "Nothing is moving"). The cheaper alternative is to
   keep feature orders out of the gate entirely — always paid, never re-asserted — which
   is strictly safer and leaves part of the measured waste in place. Recommend the
   children rule; flagging it rather than burying it.
