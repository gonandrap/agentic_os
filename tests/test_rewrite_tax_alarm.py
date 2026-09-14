"""The re-write tax as a STANDING condition, split by the cause that produced it.

Issue 164 item 1; finding 1 of
docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md.

`inspection.WRITE_ALARM` already reports one call re-sending one conversation while the
turn is still running, and already names its cause. What is new here is the AGGREGATE:
`bill.rewrite_tax` over a project's sealed bills, `Daemon.check_rewrite_tax` raising it,
and `remedies.file_work_order` letting the supervisor ask for the fix and its shipping.

THE FIXTURES THAT DISCRIMINATE ARE THE ONES WHOSE TWO CAUSES DISAGREE. A window whose
tax is all one cause passes under either rule — "name the dominant cause" and "split the
tax" look identical there — so every arithmetic test below mixes the two, and one order
in each window is deliberately UNCLASSIFIED, which is the only way to tell a ratio taken
over the classified orders from one taken over all of them.
"""

from __future__ import annotations

import json

import pytest

from jarvis import bill, daemon as daemon_mod, inspection, ops, remedies, timeline
from jarvis.catalog import CatalogError, InspectConfig, load_catalog, parse_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import NO_TURN, ProjectStore

DAY = 86_400


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _seal(store, *, title, bill_usd, tax_usd, tax_tokens, ttl_tokens, age_days=1.0,
          classified=True):
    """One settled order with a frozen bill, as `Daemon.seal_bills` writes it.

    `classified=False` is the sealed bill of an order across whose boundaries nothing
    could be attributed: `ttl_share` is None, which is `usage.Usage.rewrite_ttl_share`'s
    honest "not measured" — never a zero.
    """
    from jarvis import db

    wo = store.create_work_order(title, status="completed")
    payload = {
        "total": {"cost": {"list_usd": bill_usd, "exact_usd": 0.0}},
        "rewrite": {
            "tokens": tax_tokens,
            "list_usd": tax_usd,
            "boundaries": 4,
            # None is `usage.Usage.rewrite_ttl_share`'s own answer whenever nothing was
            # classified — including an order that crossed no boundary at all.
            "ttl_share": (ttl_tokens / tax_tokens) if classified and tax_tokens else None,
            "ttl_tokens": ttl_tokens if classified else 0,
            "ttl_boundaries": 2 if classified else 0,
        },
    }
    store.seal_bill(wo["id"], json.dumps(payload), at=db.now() - age_days * DAY)
    return wo["id"]


def _mixed_window(store, *, prefix_heavy=True):
    """Five sealed orders: four classified, one not, and the two causes disagree.

    $1,000 of bill and $300 of tax, of which $240 is one cause and $60 the other. The
    unclassified order carries $100 of bill and $0 of tax, so it moves the DENOMINATOR
    without moving the split — which is exactly the pairing that grades the population
    rule in `bill.rewrite_tax`.
    """
    ttl = 0.2 if prefix_heavy else 0.8
    return [
        # 1,000,000 tax tokens carrying $300, split 80/20 between the two causes.
        _seal(store, title="the expensive one", bill_usd=600.0, tax_usd=240.0,
              tax_tokens=800_000, ttl_tokens=int(800_000 * ttl)),
        _seal(store, title="a cheaper one", bill_usd=200.0, tax_usd=60.0,
              tax_tokens=200_000, ttl_tokens=int(200_000 * ttl)),
        _seal(store, title="no tax at all", bill_usd=50.0, tax_usd=0.0,
              tax_tokens=0, ttl_tokens=0, classified=False),
        _seal(store, title="also free", bill_usd=50.0, tax_usd=0.0,
              tax_tokens=0, ttl_tokens=0, classified=False),
        _seal(store, title="unclassified but billed", bill_usd=100.0, tax_usd=0.0,
              tax_tokens=0, ttl_tokens=0, classified=False),
    ]


# -- 1. the arithmetic ------------------------------------------------------------------


def test_the_split_is_a_ratio_over_the_classified_orders_applied_to_the_whole(store):
    """THE POPULATION RULE, and it is the one thing here that is easy to get wrong.

    The ratio comes from the orders whose boundaries were classified; the denominator is
    every sealed order in the window, including the ones that paid no tax. Dropping the
    unclassified orders from the denominator instead would inflate every share printed,
    and this fixture is built so the two answers differ: $300 of $1,000 is 30%, $300 of
    the $800 that was classified would be 37.5%.
    """
    _mixed_window(store, prefix_heavy=True)
    tax = bill.rewrite_tax(store, days=7)

    assert tax is not None
    assert tax.orders == 5
    assert tax.bill_usd == pytest.approx(1000.0)
    assert tax.tax_usd == pytest.approx(300.0)
    assert tax.tax_share == pytest.approx(0.30)
    # 200,000 of the 1,000,000 classified tax tokens were the entry expiring.
    assert tax.ttl_share == pytest.approx(0.20)
    assert tax.prefix_share_of_bill == pytest.approx(0.24)
    assert tax.ttl_share_of_bill == pytest.approx(0.06)
    # …and the two halves are a PARTITION of the whole, which is the property
    # kn-7a2180ba asks of every finer accounting in this area.
    assert tax.prefix_share_of_bill + tax.ttl_share_of_bill == pytest.approx(
        tax.tax_share)


def test_the_carrier_is_the_biggest_single_contributor(store):
    """Not the newest order and not the dearest one: the one that paid the most TAX.

    Discriminating fixture: `the expensive one` has the largest tax AND the largest
    bill, so a second order is given a bigger bill and no tax at all — under "dearest
    order" it would win, and it is not the exemplar of anything.
    """
    _mixed_window(store, prefix_heavy=True)
    _seal(store, title="dear but clean", bill_usd=5_000.0, tax_usd=0.0,
          tax_tokens=0, ttl_tokens=0, classified=False)
    tax = bill.rewrite_tax(store, days=7)

    assert tax is not None
    assert tax.worst_title == "the expensive one"
    assert tax.worst_tax_usd == pytest.approx(240.0)


def test_a_bill_sealed_outside_the_cohort_window_is_not_counted(store):
    """THE COHORT WINDOW, kn-1449447a (5): the split is drifting, so averaging over all
    history reports the trend away. A window that ignored `bill_sealed_at` would read
    the two orders below as one population."""
    _seal(store, title="inside", bill_usd=100.0, tax_usd=30.0,
          tax_tokens=100_000, ttl_tokens=100_000, age_days=1.0)
    _seal(store, title="long ago", bill_usd=900.0, tax_usd=0.0,
          tax_tokens=0, ttl_tokens=0, age_days=40.0, classified=False)

    week = bill.rewrite_tax(store, days=7)
    month = bill.rewrite_tax(store, days=60)

    assert week is not None and month is not None
    assert (week.orders, week.bill_usd) == (1, pytest.approx(100.0))
    assert (month.orders, month.bill_usd) == (2, pytest.approx(1000.0))
    # The same tax against two denominators — which is the whole reason the window is a
    # setting rather than "everything we have".
    assert week.tax_share == pytest.approx(0.30)
    assert month.tax_share == pytest.approx(0.03)


def test_an_empty_window_and_a_window_of_unsealed_orders_both_answer_none(store):
    """No sealed bill is not the same claim as a tax of zero, and neither may raise."""
    store.create_work_order("still running", status="running")
    assert bill.rewrite_tax(store, days=7) is None


# -- 2. naming the cause, and refusing to name one it does not have ---------------------


def test_an_unmeasured_split_raises_neither_alarm(store):
    """THE FAIL-SAFE. `ttl_share` is None exactly when no boundary was classified, and
    an alarm that named a cause anyway would be inventing the one fact it carries —
    `usage.Usage.rewrite_ttl_share`'s "unmeasured must not print as 0%" rule, applied at
    the surface that acts on it rather than only at the one that prints it."""
    for n in range(5):
        _seal(store, title=f"order {n}", bill_usd=200.0, tax_usd=190.0,
              tax_tokens=900_000, ttl_tokens=0, classified=False)
    tax = bill.rewrite_tax(store, days=7)

    assert tax is not None
    assert tax.tax_share > 0.9        # an enormous tax…
    assert tax.ttl_share is None      # …with no cause anyone can name
    assert bill.rewrite_alarms(tax, InspectConfig()) == []


def test_each_cause_raises_its_own_kind_and_only_when_it_crosses(store):
    """TWO KINDS, NOT ONE WITH THE CAUSE IN ITS PROSE. The two cures are opposite, so
    the fixture below makes one cause cross and the other not: a single merged kind
    could not express this window at all."""
    _mixed_window(store, prefix_heavy=True)  # prefix 24% of bill, TTL 6%
    tax = bill.rewrite_tax(store, days=7)
    assert tax is not None

    both_low = bill.rewrite_alarms(tax, InspectConfig(alarm_rewrite_prefix_share=0.05,
                                                      alarm_rewrite_ttl_share=0.05))
    only_prefix = bill.rewrite_alarms(tax, InspectConfig(alarm_rewrite_prefix_share=0.20,
                                                         alarm_rewrite_ttl_share=0.20))
    neither = bill.rewrite_alarms(tax, InspectConfig(alarm_rewrite_prefix_share=0.90,
                                                     alarm_rewrite_ttl_share=0.90))

    assert [a.kind for a in both_low] == [inspection.REWRITE_PREFIX_ALARM,
                                          inspection.REWRITE_TTL_ALARM]
    assert [a.kind for a in only_prefix] == [inspection.REWRITE_PREFIX_ALARM]
    assert neither == []


def test_the_ttl_alarm_says_it_is_not_the_ttl_switching_trigger(store):
    """THE ANTI-MISREADING PIN, and the most load-bearing assertion in this module.

    The threshold this alarm crossed is a share of the BILL. The decision to switch the
    write TTL is taken against `rewrite_ttl_write / cache_write` at 39.5% — a different
    ratio over a much larger denominator, running at about half this figure
    (kn-1449447a (4)). Reading one against the other says "switch now" when the answer
    is "keep the five-minute write", and the findings doc's own draft made exactly that
    error. The supervisor judges on this sentence and can look nothing up, so the
    warning and the command that settles it have to be IN it.
    """
    _mixed_window(store, prefix_heavy=False)  # TTL-dominant
    tax = bill.rewrite_tax(store, days=7)
    assert tax is not None
    alarms = bill.rewrite_alarms(tax, InspectConfig(alarm_rewrite_ttl_share=0.05,
                                                    alarm_rewrite_prefix_share=0.90))

    assert [a.kind for a in alarms] == [inspection.REWRITE_TTL_ALARM]
    reason = alarms[0].reason
    assert "39.5%" in reason
    assert bill.COHORT_COMMAND in reason
    # Not a restatement: this is the half that says WHICH denominator this number used,
    # without which "39.5%" is one more number beside another one.
    assert "share of the BILL" in reason

    # …and the prefix alarm does NOT carry it. The TTL decision is irrelevant to a tax
    # no TTL can touch, and repeating it there teaches the judge the two are one topic.
    prefix = bill.rewrite_alarms(tax, InspectConfig(alarm_rewrite_prefix_share=0.01,
                                                    alarm_rewrite_ttl_share=0.90))
    assert [a.kind for a in prefix] == [inspection.REWRITE_PREFIX_ALARM]
    assert "39.5%" not in prefix[0].reason


def test_a_partial_split_says_so_and_a_complete_one_does_not(store):
    """A share drawn from half the evidence and one drawn from all of it must not read
    identically — the pairing kn-6a11a5ea asks for, both halves asserted."""
    _seal(store, title="classified", bill_usd=100.0, tax_usd=30.0,
          tax_tokens=100_000, ttl_tokens=100_000)
    complete = bill.rewrite_tax(store, days=7)
    _seal(store, title="unclassified but taxed", bill_usd=100.0, tax_usd=30.0,
          tax_tokens=100_000, ttl_tokens=0, classified=False)
    partial = bill.rewrite_tax(store, days=7)

    assert complete is not None and partial is not None
    assert complete.coverage == pytest.approx(1.0)
    assert partial.coverage == pytest.approx(0.5)
    low = InspectConfig(alarm_rewrite_ttl_share=0.01, alarm_rewrite_prefix_share=0.99)
    assert "The split is measured over" not in bill.rewrite_alarms(complete, low)[0].reason
    assert "The split is measured over 50%" in bill.rewrite_alarms(partial, low)[0].reason


def test_both_new_kinds_have_a_standing_meaning_a_surface_can_render(store):
    """`ui.app` hands `inspection.ALARM_KINDS` to /alarms as the legend, so a kind
    missing from it renders as a bare id — the same failure kn-376c88eb (4) records for
    a timeline kind with no branch."""
    assert inspection.REWRITE_PREFIX_ALARM in inspection.ALARM_KINDS
    assert inspection.REWRITE_TTL_ALARM in inspection.ALARM_KINDS
    # Each names the cause it is about, which is the whole reason there are two.
    assert "PREFIX" in inspection.ALARM_KINDS[inspection.REWRITE_PREFIX_ALARM]
    assert "EXPIRED" in inspection.ALARM_KINDS[inspection.REWRITE_TTL_ALARM]


# -- 3. the daemon raises it, once, on the right carrier --------------------------------


def _raise(daemon, store, **overrides):
    project = daemon.catalog.projects[0]
    project.inspect = InspectConfig(alarm_rewrite_prefix_share=0.05,
                                    alarm_rewrite_ttl_share=0.05, **overrides)
    daemon.check_rewrite_tax(project, store)
    return store.alarms_across()


def test_the_daemon_raises_one_alarm_per_cause_on_the_exemplar_order(started, store):
    _mixed_window(store, prefix_heavy=True)
    rows = _raise(started, store)

    assert {r["kind"] for r in rows} == {inspection.REWRITE_PREFIX_ALARM,
                                         inspection.REWRITE_TTL_ALARM}
    for row in rows:
        assert row["title"] == "the expensive one"   # the carrier, via the join
        assert row["seq"] == NO_TURN                 # it judged no turn
        assert row["source"] == "cost"
        assert row["alarm_status"] == "raised"       # on the supervisor's queue


def test_the_carrier_is_not_flagged_for_attention(started, store):
    """DELIBERATE, and it would read as an omission. The carrier is a SETTLED order and
    `invariants.check_no_phantom_attention` clears the flag on every terminal order on
    the next tick — so a flag here would be raised and then hidden by the OS itself. The
    inbox row is the durable half."""
    _mixed_window(store, prefix_heavy=True)
    rows = _raise(started, store)
    assert rows

    carrier = store.get_work_order(rows[0]["wo_id"])
    assert not carrier["needs_attention"]
    inbox = [r for r in started.central.unacked_inbox()
             if r["title"] == daemon_mod.REWRITE_INBOX_TITLE.format(project="proj_a")]
    assert len(inbox) == 2
    assert all(r["wo_id"] == carrier["id"] for r in inbox)


def test_it_is_one_alarm_per_kind_per_window_however_often_the_tick_runs(started, store):
    """THE DEDUPE. The condition is still true on the next tick — that is what makes it
    standing rather than burning — so `check_burning_turns`' `(kind, seq)` match would
    re-raise it every reconcile for ever."""
    _mixed_window(store, prefix_heavy=True)
    first = _raise(started, store)
    again = first
    for _ in range(3):
        again = _raise(started, store)

    assert len(first) == 2
    assert len(again) == 2
    # …and an alarm the supervisor has already SETTLED still counts as reported. Keying
    # on the open ones would re-raise the moment it was acked.
    for row in again:
        store.update_alarm(row["id"], status="acked")
    assert len(_raise(started, store)) == 2


def test_an_alarm_older_than_the_window_does_not_suppress_the_next_one(started, store):
    """THE OTHER HALF OF THE DEDUPE, and without it the suppression above is satisfied by
    "has this kind EVER been raised" — which is once per project for ever.

    The mutation this kills: delete `float(last["ts"]) >= tax.since` from
    `Daemon.check_rewrite_tax` and every other test in this module still passes, because
    they all re-raise inside one window. Here the previous alarm is backdated past the
    window's start, which is precisely the state a project is in on the first tick of its
    second cohort window — the tax is still crossing and nobody has been told this week.
    """
    from jarvis import db

    _mixed_window(store, prefix_heavy=True)
    raised = _raise(started, store)
    assert len(raised) == 2

    # Backdate BOTH rows to before the window, exactly as the clock would have.
    old = db.now() - 30 * DAY
    for row in raised:
        store.conn.execute("UPDATE wo_alarms SET ts=? WHERE id=?", (old, row["id"]))

    fresh = [r for r in _raise(started, store) if float(r["ts"]) > old]
    assert {r["kind"] for r in fresh} == {inspection.REWRITE_PREFIX_ALARM,
                                          inspection.REWRITE_TTL_ALARM}
    assert len(store.alarms_across()) == 4


def test_both_floors_hold_and_neither_covers_for_the_other(started, store):
    """Two floors answering two questions: too few orders is one order's shape wearing
    the project's name, too little money is a percentage of nothing. Measured against
    real fleet data — the first excludes a single $28 order sitting at 31.8%, the second
    a project that spent $0.54 across three orders."""
    # Enough money, not enough orders.
    _seal(store, title="one big one", bill_usd=500.0, tax_usd=250.0,
          tax_tokens=500_000, ttl_tokens=250_000)
    assert _raise(started, store, alarm_rewrite_min_orders=5,
                  alarm_rewrite_min_usd=10.0) == []

    # Enough orders, not enough money: five orders whose whole bill is under a dollar.
    for n in range(5):
        _seal(store, title=f"tiny {n}", bill_usd=0.10, tax_usd=0.05,
              tax_tokens=100, ttl_tokens=50)
    assert _raise(started, store, alarm_rewrite_min_orders=5,
                  alarm_rewrite_min_usd=1_000.0) == []

    # Both satisfied — the same fixtures now raise, so the two assertions above are
    # about the floors and not about a window that could never alarm.
    assert _raise(started, store, alarm_rewrite_min_orders=1,
                  alarm_rewrite_min_usd=1.0)


def test_the_off_switch_covers_it(started, store):
    """`InspectConfig.enabled` is one switch for "raise nothing here" — a second way to
    turn one thing off is a second way to be surprised by it."""
    _mixed_window(store, prefix_heavy=True)
    assert _raise(started, store, enabled=False) == []


# -- 3b. the supervisor is actually engaged ---------------------------------------------
#
# THE SECTION REVIEW ROUND 1 ASKED FOR, and its point is that everything above proves a
# `wo_alarms` row exists. That is not the deliverable: the brief asked for an alarm that
# REACHES the supervisor's review loop, and every path between the raise and the judge
# has a filter on it — the drain excludes by age and by a missing work order, the evidence
# builder branches on the subject kind and on the turn, and the remedy checks the subject
# kind before it will propose. An aggregate alarm is unlike every alarm those paths were
# written for: `seq` is NO_TURN, the carrier has COMPLETED, and nothing is flagged. Each
# test below drives the real function rather than asserting around it.


def _raised_alarm(started, store, kind=inspection.REWRITE_PREFIX_ALARM):
    _mixed_window(store, prefix_heavy=True)
    rows = _raise(started, store)
    return next(r for r in rows if r["kind"] == kind)


def test_the_supervisors_own_queue_returns_it_and_the_drain_judges_it(started, store):
    """THE ENGAGEMENT PIN. Two things, because either alone would pass while the
    supervisor was never reached: the alarm is in the read `Daemon.supervisor_tick`
    counts before it kicks a drain, and `_drain_project_alarms` — the real one, with a
    stub judge standing in for the model call — claims it and hands the judge the
    COMPLETED carrier.

    `_drain_project_alarms` moves an alarm it will not judge OUT of the queue with a
    reason, so the `skipped` assertion is what discriminates: if either exclusion fired
    on a settled carrier, the row would be `skipped` and `seen` empty, and a test that
    only checked `alarms_across` would still be green.
    """
    from jarvis.neo_store import NeoStore

    alarm = _raised_alarm(started, store)
    project = started.catalog.projects[0]

    # What `supervisor_tick` counts to decide whether to kick the drain at all.
    waiting = store.alarms_across(statuses=("raised",))
    assert alarm["id"] in {r["id"] for r in waiting}

    seen: list[tuple] = []

    class _Judge:
        @staticmethod
        def review(pstore, neo_store, project_name, wo, row, cfg, **kw):
            seen.append((row["id"], row["kind"], wo["id"], wo["status"]))
            return {"decision": "ack", "reason": "explicable", "note": "ok",
                    "failed": False}

    neo_store, central = NeoStore(), started.central
    try:
        started._drain_project_alarms(project, store, neo_store, central, _Judge)
    finally:
        neo_store.close()

    judged = {row[0]: row for row in seen}
    assert alarm["id"] in judged, "the drain never handed this alarm to the judge"
    _, kind, wo_id, wo_status = judged[alarm["id"]]
    assert kind == inspection.REWRITE_PREFIX_ALARM
    assert (wo_id, wo_status) == (alarm["wo_id"], "completed")
    # Nothing was excluded: `skipped` is how the drain declines one.
    assert not [r for r in store.alarms_across() if r["alarm_status"] == "skipped"]


def test_an_acked_aggregate_alarm_reaches_the_users_review_queue(started, store):
    """The far end of the loop. `ops.alarm_review_queue` is `acked` + `unreviewed` by
    design (kn-a066e10c), so a freshly raised alarm is correctly absent from it — both
    halves asserted, because "absent" and "never arrives" look identical with one."""
    from jarvis import db

    alarm = _raised_alarm(started, store)
    assert alarm["id"] not in {r["id"] for r in ops.alarm_review_queue("proj_a")}

    store.update_alarm(alarm["id"], status="acked", verdict="ack",
                       verdict_reason="the shape of the work accounts for it",
                       note="a big refactor week", decided_at=db.now())
    queued = {r["id"]: r for r in ops.alarm_review_queue("proj_a")}

    assert alarm["id"] in queued
    row = queued[alarm["id"]]
    assert row["kind"] == inspection.REWRITE_PREFIX_ALARM
    assert row["seq"] == NO_TURN
    assert row["subject_kind"] == "work_order"
    assert row["title"] == "the expensive one"   # the exemplar, named on the surface
    # `live` is the CARRIER's attention flag, which this alarm deliberately never set.
    assert not row["live"]


def test_an_evidence_packet_builds_for_a_turnless_alarm_on_a_settled_order(started,
                                                                          store):
    """The judge is given a packet, not the alarm row, and this one is built from an
    alarm with no turn hanging off an order that has stopped.

    THE ASSERTION THAT MATTERS IS `turn -1`. `build_evidence` renders `seq` and the
    sentinel is `-1`; "raised on turn -1" is nonsense the judge would have to interpret,
    which is the failure `project_store.NO_TURN` is named after. The reason has to be in
    there too — it is the only place the number, the cause and the exemplar appear.
    """
    from jarvis import supervisor
    from jarvis.catalog import SupervisorConfig

    alarm = _raised_alarm(started, store)
    carrier = store.get_work_order(alarm["wo_id"])
    assert carrier["status"] == "completed"

    packet = supervisor.build_evidence(
        store, {"kind": "work_order", "row": carrier}, alarm,
        SupervisorConfig(), InspectConfig())

    assert "raised on no particular turn" in packet
    assert "turn -1" not in packet
    assert inspection.REWRITE_PREFIX_ALARM in packet
    assert "PROMPT PREFIX" in packet          # the cause, which is the point of the split
    assert carrier["id"] in packet


def test_the_remedy_goes_through_propose_and_apply_on_a_settled_carrier(started, store):
    """THE REMEDY THROUGH ITS REAL PATH, not by calling the handler.

    Round 1 landed because the handler was only ever called directly, which skips the
    two checks that decide whether the supervisor can use it at all: `_refusal`'s
    subject-kind test (`subject_kind(alarm)` against `Remedy.subjects`) and
    `Daemon._alarm_subject`'s resolution of the subject from the alarm row. Both are
    exercised here, on the shape this alarm actually has — a work-order subject whose
    order has COMPLETED.
    """
    from jarvis.catalog import RemedyConfig
    from jarvis.neo_store import NeoStore

    alarm = _raised_alarm(started, store)
    carrier = store.get_work_order(alarm["wo_id"])
    argument = "Find what moves the prompt prefix between calls in proj_a."
    cfg = RemedyConfig(enabled=True, allowed=("file_work_order",))

    neo_store = NeoStore()
    try:
        outcome = remedies.propose(store, neo_store, "proj_a", carrier,
                                   store.get_alarm(alarm["id"]), "file_work_order",
                                   argument, cfg, reason=alarm["reason"])
    finally:
        neo_store.close()
    assert outcome["proposed"], outcome["reason"]
    approval_id = outcome["approval"]["id"]
    assert store.get_alarm(alarm["id"])["status"] == "proposed"

    # The reviewer says yes, and only then may anything be filed.
    store.decide_approval(approval_id, "approved", "go on", "test")
    subject = started._alarm_subject(store, store.get_alarm(alarm["id"]), remedies)
    assert subject["id"] == carrier["id"], "the subject did not resolve to the carrier"

    result = remedies.apply(store, started.central, "proj_a",
                            store.get_approval(approval_id),
                            store.get_alarm(alarm["id"]), subject)

    filed = [w for w in store.list_work_orders()
             if w["title"] == argument or w["title"].startswith("Ship the fix")]
    assert len(filed) == 2, result
    fix = next(w for w in filed if not w["title"].startswith("Ship the fix"))
    ship = next(w for w in filed if w["title"].startswith("Ship the fix"))
    assert store.dependencies(ship) == [fix["id"]]
    assert store.get_alarm(alarm["id"])["status"] == "acked"


def test_the_remedy_is_refused_when_the_project_has_not_armed_it(started, store):
    """The mirror of the test above, and it is what stops that one grading "propose
    always proposes". A refusal writes the reason on the alarm and files NOTHING."""
    from jarvis.catalog import RemedyConfig
    from jarvis.neo_store import NeoStore

    alarm = _raised_alarm(started, store)
    carrier = store.get_work_order(alarm["wo_id"])
    before = len(store.list_work_orders())

    neo_store = NeoStore()
    try:
        outcome = remedies.propose(store, neo_store, "proj_a", carrier,
                                   store.get_alarm(alarm["id"]), "file_work_order",
                                   "do the thing",
                                   RemedyConfig(enabled=True, allowed=("nudge",)))
    finally:
        neo_store.close()

    assert not outcome["proposed"]
    assert "supervisor.remedies.allowed" in outcome["reason"]
    assert store.get_alarm(alarm["id"])["status"] == "escalated"
    assert len(store.list_work_orders()) == before


# -- 4. the record renders it as what it is ---------------------------------------------


def test_the_timeline_does_not_claim_a_settled_order_is_still_running(started, store):
    """"Costing money while it runs" on an order that has completed is the class of
    false statement `inspection._spend_so_far` exists to prevent on the other surface.
    Both halves asserted: the turn-less row reads differently, the ordinary one is
    unchanged."""
    assert timeline.NO_TURN == NO_TURN

    standing, _ = timeline._describe("cost_alarm", {"seq": NO_TURN, "reason": "tax"})
    burning, _ = timeline._describe("cost_alarm", {"seq": 3, "reason": "long turn"})

    assert "runs" not in standing
    assert "standing" in standing
    assert burning == "Costing money while it runs"


def test_the_supervisor_is_told_these_two_are_not_burning_turns(started):
    """The judge's whole prompt says the turn is STILL RUNNING. An aggregate alarm on a
    settled order would be read as a turn to intervene in without this."""
    from jarvis import supervisor

    for persona in (supervisor.SUPERVISOR_PERSONA, supervisor.ALARM_REVIEWER_PERSONA):
        assert inspection.REWRITE_PREFIX_ALARM in persona
        assert inspection.REWRITE_TTL_ALARM in persona
        assert "EXEMPLAR" in persona


# -- 5. the thresholds are settings, validated where they were typed --------------------


def _catalog(**inspect):
    return {"os": {"inspect": inspect},
            "projects": [{"name": "p", "path": "/tmp/p"}]}


@pytest.mark.parametrize("bad, key", [
    ({"alarm_rewrite_prefix_share": 0}, "alarm_rewrite_prefix_share"),
    ({"alarm_rewrite_prefix_share": 1.5}, "alarm_rewrite_prefix_share"),
    ({"alarm_rewrite_ttl_share": -0.1}, "alarm_rewrite_ttl_share"),
    ({"alarm_rewrite_window_days": 0}, "alarm_rewrite_window_days"),
    ({"alarm_rewrite_min_orders": 0}, "alarm_rewrite_min_orders"),
    ({"alarm_rewrite_min_usd": -1}, "alarm_rewrite_min_usd"),
])
def test_a_bad_threshold_fails_at_boot_and_names_its_key(bad, key):
    """Refused where it was TYPED, the `os.cold_prefix_floor` precedent: a bad value
    would otherwise surface far from its cause, as an alarm that never fires or one that
    fires on everything — which reads as a finding rather than as a config error."""
    with pytest.raises(CatalogError) as e:
        parse_catalog(_catalog(**bad))
    assert key in str(e.value)


def test_the_legal_edges_are_accepted():
    """The mirror, and it is what stops the rule above being "refuse everything": a
    share may be exactly 1, and a money floor may be exactly 0 — a project may ask to
    hear about its tax however little it spent."""
    cfg = parse_catalog(_catalog(alarm_rewrite_prefix_share=1.0,
                                 alarm_rewrite_min_usd=0)).os.inspect
    assert cfg.alarm_rewrite_prefix_share == 1.0
    assert cfg.alarm_rewrite_min_usd == 0


def test_a_project_naming_one_key_inherits_the_rest(tmp_path):
    """Field-level inheritance, the same rule the other alarm thresholds have."""
    doc = {"os": {"inspect": {"alarm_rewrite_window_days": 30}},
           "projects": [{"name": "p", "path": str(tmp_path),
                         "inspect": {"alarm_rewrite_ttl_share": 0.4}}]}
    spec = parse_catalog(doc).projects[0]
    assert spec.inspect.alarm_rewrite_ttl_share == 0.4
    assert spec.inspect.alarm_rewrite_window_days == 30
    assert spec.inspect.alarm_rewrite_prefix_share == \
        InspectConfig().alarm_rewrite_prefix_share


# -- 6. the remedy: the supervisor can ask for the fix AND for its shipping -------------


def _alarm_with(store, argument):
    wo = store.create_work_order("the exemplar", status="completed")
    alarm = store.add_finding(wo["id"], kind=inspection.REWRITE_PREFIX_ALARM,
                              reason="24% of the bill was prefix invalidation",
                              seq=NO_TURN, source="cost")
    store.update_alarm(alarm["id"], remedy="file_work_order",
                       remedy_argument=argument)
    return store.get_alarm(alarm["id"]), store.get_work_order(wo["id"])


ARGUMENT = """Find what moves the prompt prefix between calls in proj_a.
The 15,862-token read plateau says the static head survived and the rest did not."""


def test_the_remedy_files_the_fix_and_the_order_that_ships_it(started, store):
    """ONE APPLICATION FILES BOTH, which is the remedy rather than two remedies: a fix
    nobody ships is not a fix (issue 164 item 1), and `--depends-on` is the OS's own
    idiom for ordering a two-step job in one go."""
    alarm, subject = _alarm_with(store, ARGUMENT)

    result = remedies._apply_file_work_order(store, started.central, "proj_a",
                                             subject, alarm)

    filed = [w for w in store.list_work_orders() if w["id"] != subject["id"]]
    assert len(filed) == 2
    fix = next(w for w in filed if not w["title"].startswith("Ship the fix"))
    ship = next(w for w in filed if w["title"].startswith("Ship the fix"))
    assert fix["id"] in result and ship["id"] in result

    # The title is the argument's first line; the WHOLE argument survives in the brief,
    # which is all the worker ever sees.
    assert fix["title"] == "Find what moves the prompt prefix between calls in proj_a."
    assert "15,862-token read plateau" in fix["description"]
    assert alarm["id"] in fix["description"]

    # The edge, and the gate. The ship order cannot run before the fix completes, and
    # when it does the release is still something a reviewer approves.
    assert store.dependencies(ship) == [fix["id"]]
    assert fix["id"] in ship["description"]
    assert "gated" in result
    assert "PRIVILEGED ACTION" in ship["description"]


def test_a_proposal_with_no_argument_is_refused_and_files_nothing(started, store):
    """`RemedyRefused` means NOTHING was done, so it is raised before the first create —
    a refusal after one order existed would be a false statement about the record."""
    alarm, subject = _alarm_with(store, "   ")
    before = len(store.list_work_orders())

    with pytest.raises(remedies.RemedyRefused):
        remedies._apply_file_work_order(store, started.central, "proj_a", subject, alarm)
    assert len(store.list_work_orders()) == before


def test_a_ship_order_that_cannot_be_filed_is_reported_not_raised(started, store,
                                                                  monkeypatch):
    """THE HALF-APPLIED PATH. By the time the ship order is attempted the fix order
    exists, so raising `RemedyRefused` — which asserts nothing was done — would write a
    lie into the record. The result string says what happened instead, including how to
    finish it by hand."""
    alarm, subject = _alarm_with(store, ARGUMENT)
    calls = {"n": 0}
    real = ops.create_work_order

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ops.OpsError("the project went away")
        return real(*args, **kwargs)

    monkeypatch.setattr(ops, "create_work_order", flaky)
    result = remedies._apply_file_work_order(store, started.central, "proj_a",
                                             subject, alarm)

    assert "could NOT be filed" in result
    assert "the project went away" in result
    assert "--depends-on" in result
    assert len([w for w in store.list_work_orders() if w["id"] != subject["id"]]) == 1
