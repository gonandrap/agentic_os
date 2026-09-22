"""A non-blocking finding becomes a GitHub issue on the project's own tracker.

Section 4 of docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md, whose
§4.2 said `CentralStore.add_backlog`. THE USER OVERRULED IT on 2026-09-16: a follow-up is
filed as an issue on the repository under review. Nothing else in §4 moved — not the
"before the outcome branch" placement, not the cap, not the silence toward the submitter.

THE PAIRING THIS FILE RUNS ON. "It was filed" and "the submitter was not told" are the
two halves of the design and each is satisfied by the wrong code alone: a machine that
files nothing passes every silence assertion, and a machine that pushes every nit at the
worker passes every filing assertion. So the filing tests read the TRACKER and the
message the worker received, in the same test, off the same round.

AND THE HALF THAT ONLY EXISTS BECAUSE THE DESTINATION IS REMOTE. Filing now crosses a
network, so a finding can be raised and land nowhere — a project with no `origin`, a `gh`
that cannot authenticate, a rate limit. Every one of those must be COUNTED and shown,
because a finding that evaporated silently is indistinguishable from a round that raised
none. The `failed` tests are not error handling; they are the record staying honest.

The validator is INJECTED, as everywhere else in this lineage: what the seats classify is
§3's measurement and the eval's, and a test that reached this code through a real panel
would be grading a model rather than the filing.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from jarvis import cli, issues, ops, timeline
from jarvis.daemon import Daemon
from tests.test_validation_loop import (  # noqa: F401  (the fixture and its helpers)
    Fleet, Validator, fleet, finish, write_catalog)

#: The project fixture's `origin`, and therefore the tracker every follow-up here lands
#: on. Deliberately not the OS's own bug tracker: the whole point of the ruling is that a
#: finding about a project's code goes on THAT project's repository.
ORIGIN = "https://github.com/acme/proj_a.git"
REPO = "acme/proj_a"
SERIES = f"https://github.com/{REPO}/issues"


# -- the panel, driven by the test ----------------------------------------------------


def _seats(verdict: str) -> list[dict]:
    return [{"seat": seat, "status": "ok", "verdict": verdict, "model": "sonnet",
             "latency_ms": 12, "reply": f"the {seat} seat says {verdict}"}
            for seat in ("tester", "security")]


def follow_up(title: str, detail: str = "", seat: str = "maintainer", *,
              file: str = "src/proj/a.py", symbol: str = "run",
              failure: str = "run({}) returns None instead of raising") -> dict:
    """One ADMISSIBLE entry of `decide`'s `follow_ups` key, as §3 returns it.

    `file` and `failure` are what `ops.follow_up_admissible` requires — a follow-up names
    a behaviour that is wrong, in code that exists — so the default here is a finding
    that may become a ticket. `nit()` below is the other kind.
    """
    return {"seat": seat, "title": title, "detail": detail or f"about {title}",
            "round": 1, "file": file, "symbol": symbol, "failure": failure}


def nit(title: str, seat: str = "tester") -> dict:
    """A finding that names no wrong behaviour: "no test covers this", a stale docstring.

    Never a tracker item, and never dropped either — it reaches the submitter through
    `ops.follow_up_feedback` in the round that is going back anyway.
    """
    return {"seat": seat, "title": title, "detail": f"about {title}", "round": 1,
            "file": "", "symbol": "", "failure": ""}


def verdict(outcome: str, *titles: str, reason: str = "", seat: str = "maintainer",
            **kw) -> dict:
    return {"outcome": outcome, "reason": reason, "seats": _seats(
        "pass" if outcome == "passed" else "reject"),
        "follow_ups": [follow_up(t, seat=seat) for t in titles], **kw}


@pytest.fixture()
def tracker(fleet, fake_gh):  # noqa: F811
    """The fleet's project with a GitHub `origin`, and a fake tracker behind it.

    `make_git_project` adds no remote, so without this `follow_up_repo` returns "" and
    every test here would exercise the no-origin path while appearing to test filing.
    `issue_series` is what makes two follow-ups in one round come back as two issues —
    see its docstring for why the single-url default hides a broken dedupe.
    """
    subprocess.run(["git", "-C", str(fleet.project), "remote", "add", "origin", ORIGIN],
                   check=True, capture_output=True)
    fake_gh.issue_series(SERIES)
    fake_gh.set_labels([])  # a project tracker has never heard of the follow-up label
    return fake_gh


def filed(tracker) -> list[dict]:
    """Every issue on the tracker, oldest first, as a person would read it."""
    return [{"url": u, **r} for u, r in sorted(
        tracker.issues.items(), key=lambda kv: int(kv[0].rsplit("/", 1)[-1]))]


#: The per-finding digest every published title carries, in both shapes (spec §9). Its
#: exact wording is pinned once, by `test_the_private_title_carries_the_token`; the tests
#: below are about WHICH findings were filed, so they read the title with it removed.
TOKEN = re.compile(r"\s*\[vf-[0-9a-f]{8}\]$")


def titles(tracker) -> list[str]:
    """Every filed issue's title without its digest token, oldest first."""
    return [TOKEN.sub("", r["title"]) for r in filed(tracker)]


def sent(fleet, wo_id: str) -> str:  # noqa: F811
    """Everything the submitter has been told. `user_to_agent` only: the worker's own
    turns land in the same table in the other direction."""
    store = fleet.store()
    try:
        return "\n".join(r["content"] for r in store.conn.execute(
            "SELECT content FROM wo_messages WHERE wo_id=? "
            "AND direction='user_to_agent'", (wo_id,)).fetchall())
    finally:
        store.close()


def judged(fleet, outcome: str, *titles: str, **kw) -> dict:  # noqa: F811
    """One work order through one real round with these follow-ups. Returns it."""
    fleet.daemon.validator = Validator(verdict(outcome, *titles, **kw))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    return wo


def payload(fleet, wo_id: str) -> dict:  # noqa: F811
    """The last filing event's payload, or {}."""
    store = fleet.store()
    try:
        rows = store.events_of_kind(wo_id, ops.FOLLOW_UPS_EVENT)
    finally:
        store.close()
    return json.loads(rows[-1]["payload"]) if rows else {}


def settle_again(fleet, wo_id: str, findings: list[dict], **cfg) -> dict:  # noqa: F811
    """A SECOND SETTLE on one order, over the round it already has.

    What `jarvis validation force` produces: the loop settled once, the user re-judged
    the same pull request, and the filer runs again. The per-order cap and the dedupe
    both have to hold ACROSS settles, which one round can never show.
    """
    store = fleet.store()
    try:
        rnd = store.latest_validation_round(wo_id=wo_id)
        conf = fleet.spec.validation
        return ops.file_validation_follow_ups(store, fleet.spec, dict(rnd), findings,
                                              conf, wo_id=wo_id)
    finally:
        store.close()


def kept_rows(fleet, unit_id: str) -> list[dict]:  # noqa: F811
    """The withheld follow-ups on the internal record, oldest first."""
    store = fleet.store()
    try:
        return store.internal_follow_ups(unit_id)
    finally:
        store.close()


# -- 1. the filing ---------------------------------------------------------------------


def test_a_rejection_with_rounds_left_files_nothing_and_sends_back_its_blockers(fleet,
                                                                                tracker):
    """FILED AT SETTLE, NEVER MID-LOOP. Round 1's findings used to be filed at once and
    then fixed in round 2, leaving issues open against code that no longer exists — 20 of
    them on wo-0a9ba9b3. The submitter is still sent back over its blocker alone.

    Both halves, because either is satisfied by the wrong machine: a panel that files
    nothing ever passes "the worker was told only the reason"; a panel that pushed every
    nit at the worker passes "no issue exists"."""
    fleet.reconfigure(max_rounds=9)
    wo = judged(fleet, "rejected", "Name the retry budget in the docstring",
                reason="the new branch has no test")
    fleet.tick()  # deliver the feedback

    assert filed(tracker) == [], "an intermediate round filed against unsettled code"
    told = sent(fleet, wo["id"])
    assert "the new branch has no test" in told
    assert "retry budget" not in told, "a follow-up reached the submitter"
    assert "github.com/acme" not in told, "an issue link reached the submitter"


def test_the_last_round_of_a_rejected_order_still_files(fleet, tracker):
    """The loop is over and there is no round left to fix anything in, so the findings
    are filed rather than lost with it — Neo's ruling on this order: the user decides the
    blockers, and holding the follow-ups risks losing them entirely."""
    fleet.reconfigure(max_rounds=1)
    wo = judged(fleet, "rejected", "Name the retry budget in the docstring",
                reason="the new branch has no test")

    assert titles(tracker) == ["Name the retry budget in the docstring"]
    assert filed(tracker)[0]["url"].startswith(SERIES)
    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_it_is_filed_on_the_project_under_review_not_the_os_tracker(fleet, tracker):
    """The ruling's substance. `issues.py` wrote only to `bugreport.bug_repo()` before
    this; a finding about a project's code belongs on that project's repository, and the
    repository name comes from the CHECKOUT's own `origin` rather than from anything
    stored or model-written."""
    judged(fleet, "passed", "Fold the two parsers into one")

    created = [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]]
    assert len(created) == 1
    argv = created[0]["argv"]
    assert argv[argv.index("--repo") + 1] == REPO
    assert REPO != tracker.repo, "the fixture no longer distinguishes the two trackers"


def test_a_passed_round_files_its_follow_ups_too(fleet, tracker):
    """The case that makes this a feature rather than a nicer rejection. A finding the
    seats judged the work shippable WITHOUT is exactly the finding that used to be lost —
    conditioning the filing on an outcome the seat could not see when it wrote would
    rebuild the treadmill in miniature."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")

    assert titles(tracker) == ["Fold the two parsers into one"]
    store = fleet.store()
    try:
        assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"
    finally:
        store.close()


def test_the_issue_body_names_the_seat_the_round_and_the_pull_request(fleet, tracker):
    """THE ONE DOOR DELIBERATION LEAVES BY, and it is deliberate (§4.2). The tracker is
    the project's own record, read later by a person deciding whether to act, and which
    reviewer said this is exactly what they need — while the same fact may never reach
    the submitter, which the first test pins."""
    wo = judged(fleet, "passed", "Fold the two parsers into one", seat="architect")

    body = filed(tracker)[0]["body"]
    assert "about Fold the two parsers into one" in body      # the seat's own detail
    assert "`architect` seat" in body
    assert "round 1" in body and wo["id"] in body
    assert "github.com" in body, "the pull request is not on the issue"
    assert "follow-up rather than a blocker" in body


def test_every_follow_up_is_labelled_and_the_label_is_created_first(fleet, tracker):
    """`gh` refuses `--label` for a label the repository does not define (`kn-eefc35a8`
    fact 1), and unlike the OS's own tracker these repositories have never been asked to
    carry one — so the FIRST follow-up on every project in the fleet fails without this.

    The fixture starts the repository with NO labels at all, which is what makes the
    assertion real: remove `ensure_follow_up_label` and the create refuses."""
    judged(fleet, "passed", "Fold the two parsers into one")

    order = [tuple(c["argv"][:2]) for c in tracker.calls]
    assert ("label", "create") in order
    assert order.index(("label", "create")) < order.index(("issue", "create"))
    assert filed(tracker)[0]["labels"] == [issues.FOLLOW_UP_LABEL]


def test_a_verdict_with_no_follow_ups_key_at_all_files_nothing(fleet, tracker):
    """THE OLD SHAPE. Every row already in `validation_opinions` is it, a model will
    sometimes answer in it anyway, and `Daemon.validator` is injectable — several suites
    inject fakes returning only the three keys that predate this."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass")})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert filed(tracker) == []
    assert [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]] == []
    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
        assert store.events_of_kind(wo["id"], ops.FOLLOW_UPS_EVENT) == []
    finally:
        store.close()


def test_an_empty_title_is_not_an_issue(fleet, tracker):
    """A finding with nothing to call it cannot be an issue anybody can act on, and an
    untitled one is worse than none. Dropped silently rather than counted as failed: a
    later round retrying it would produce the same nothing."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [{"seat": "tester", "title": "   ", "detail": "something"}]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert filed(tracker) == []
    assert payload(fleet, wo["id"]) == {}


# -- 2. the dedupe ---------------------------------------------------------------------


def _round_two(fleet, first: dict, second: dict, **cfg) -> dict:  # noqa: F811
    """Two real rounds on one work order, with different evidence each time.

    `**cfg` goes in WITH `max_rounds` rather than in a second call: `reconfigure`
    rewrites the whole catalog, so a later call silently drops an earlier one's field.
    """
    fleet.reconfigure(max_rounds=9, **cfg)
    validator = Validator(first, second)
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # deliver the feedback; the work order is running again
    fleet.change(wo["id"], "print('one')\nprint('two')\n")
    finish(fleet, wo["id"], summary="fixed")
    fleet.drain()
    assert [c["round"] for c in validator.calls] == [1, 2], "round 2 never ran"
    return wo


def test_round_two_re_raising_round_ones_nit_files_it_once(fleet, tracker):
    """Mechanism 1.2: a fresh reviewer re-reads the whole artifact and says the same
    thing again. Without this the feature converts a rejection treadmill into a tracker
    full of duplicates."""
    _round_two(fleet,
               verdict("rejected", "Name the retry budget", reason="no test"),
               verdict("passed", "Name the retry budget"))

    assert titles(tracker) == ["Name the retry budget"]


def test_the_title_is_matched_after_whitespace_is_collapsed(fleet, tracker):
    """The house normalisation — strip, collapse runs of whitespace — so a seat that
    wraps its sentence differently in round 2 does not file it twice."""
    _round_two(fleet,
               verdict("rejected", "Name the retry budget", reason="no test"),
               verdict("passed", "  Name   the\n  retry budget  "))

    assert titles(tracker) == ["Name the retry budget"]


def test_an_issue_the_user_has_since_closed_is_still_not_re_filed(fleet, tracker):
    """THE TRAP THAT IS INVISIBLE UNTIL PRODUCTION, one layer out from where it was.
    `gh issue list` defaults to OPEN issues only, exactly as `CentralStore.list_backlog`
    defaulted to `status='open'`; a dedupe on that default stops seeing a follow-up the
    user has closed and re-files it on every subsequent round, for ever.

    Paired with the round-2 test above, which proves the dedupe works at all — this one
    alone would pass on a machine that never files anything twice because it never files
    anything."""
    wo = judged(fleet, "passed", "Name the retry budget")
    rows = filed(tracker)
    assert len(rows) == 1
    # The user reads it and closes it, which is what a tracker is for.
    tracker.add_issue(rows[0]["url"], title=rows[0]["title"], body=rows[0]["body"],
                      state="CLOSED", labels=rows[0]["labels"])

    settle_again(fleet, wo["id"], [follow_up("Name the retry budget")])

    assert [r["state"] for r in filed(tracker)] == ["CLOSED"], (
        "a closed follow-up was filed again")


def test_the_dedupe_reads_only_this_units_follow_ups(fleet, tracker):
    """Searched by LABEL and by the unit id in the body, so a person's own issue that
    happens to share a title is neither matched nor suppressed."""
    tracker.add_issue(f"{SERIES}/900", title="Name the retry budget",
                      body="I noticed this myself", labels=[])
    judged(fleet, "passed", "Name the retry budget")

    assert titles(tracker).count("Name the retry budget") == 2, (
        "a human's issue suppressed the panel's, or vice versa")


def test_the_same_title_twice_in_one_round_is_one_issue(fleet, tracker):
    """Two seats can reach the same nit in the same round, and the dedupe against the
    tracker cannot see an issue this round has not filed yet."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [follow_up("Name the retry budget", seat="tester"),
                        follow_up("Name the retry budget", seat="architect")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert titles(tracker) == ["Name the retry budget"]


# -- 3. the cap ------------------------------------------------------------------------


SIX = ("one", "two", "three", "four", "five", "six")


def test_only_five_follow_ups_are_filed_and_the_rest_are_counted(fleet, tracker):
    """The bound itself. What it bounds is the ORDER, which the next test pins."""
    wo = judged(fleet, "passed", *SIX)

    assert titles(tracker) == list(SIX[:5])
    assert payload(fleet, wo["id"])["dropped"] == 1


def test_the_cap_is_applied_before_the_network(fleet, tracker):
    """A round with more findings than the cap must not open a connection per finding to
    discover it may keep five. Counted in `gh` CALLS, which is the only place the
    difference shows."""
    fleet.reconfigure(max_follow_ups=2)
    judged(fleet, "passed", *SIX)

    created = [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]]
    assert len(created) == 2, "the cap was applied after filing, not before"


def test_the_cap_is_a_per_project_setting(fleet, tracker):
    """Every threshold a surface judges by is a catalog field with this block's ordinary
    field-level inheritance, never a module constant — and a project that names one keeps
    the fleet answer for every other field."""
    fleet.reconfigure(max_follow_ups=9, project_validation={"max_follow_ups": 2})
    judged(fleet, "passed", *SIX)

    assert titles(tracker) == ["one", "two"]


def test_the_cap_bounds_the_order_and_not_one_settle(fleet, tracker):
    """THE ARITHMETIC THAT PRODUCED TWENTY ISSUES. Applied per round, a unit judged four
    times filed four times the cap — 4 x 5 on wo-0a9ba9b3. What the order has already
    raised is subtracted, so a second settle with entirely fresh findings files nothing
    once the cap is spent.

    Asserted on FRESH titles, not on re-raised ones: a machine whose dedupe worked and
    whose cap did not would pass a repeat."""
    fleet.reconfigure(max_follow_ups=2)
    wo = judged(fleet, "passed", *SIX)
    assert titles(tracker) == ["one", "two"]

    result = settle_again(fleet, wo["id"],
                          [follow_up(t) for t in ("seven", "eight")])

    assert titles(tracker) == ["one", "two"], "a second settle filed past the cap"
    assert result["dropped"] == 2


# -- 4. what cannot be filed, and is therefore counted ---------------------------------


def test_a_project_with_no_github_remote_keeps_the_finding_on_the_record(fleet,
                                                                        fake_gh):
    """Several projects in the fleet are local-only. No repository is no proof of
    privacy, so the finding takes the same path a public tracker's does: kept whole,
    internally, and nothing published. It used to be counted as a failure, which read as
    a finding lost to a broken network rather than one deliberately not published."""
    fake_gh.issue_series(SERIES)  # no `origin` is added: that is the case under test
    wo = judged(fleet, "passed", "Name the retry budget")

    assert [c for c in fake_gh.calls if c["argv"][:2] == ["issue", "create"]] == []
    p = payload(fleet, wo["id"])
    assert p["failed"] == 0 and p["items"] == []
    assert [i["title"] for i in p["withheld"]] == ["Name the retry budget"]
    assert kept_rows(fleet, wo["id"])[0]["detail"] == "about Name the retry budget"


def test_a_tracker_that_cannot_be_read_files_nothing_rather_than_duplicating(
        fleet, tracker, monkeypatch):
    """The dedupe's read is a PRECONDITION of the writes, not an optimisation. Filing
    without it would duplicate every follow-up this unit already has, which is the exact
    flood the dedupe exists to prevent — so an unreadable tracker costs the round its
    follow-ups and says so."""
    def refuse(repo, unit_id):
        raise issues.IssueLifecycleError("HTTP 403: rate limit exceeded", "REFUSED")

    monkeypatch.setattr(issues, "follow_ups_filed", refuse)
    wo = judged(fleet, "passed", "Name the retry budget")

    assert [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]] == []
    p = payload(fleet, wo["id"])
    assert p["failed"] == 1 and p["items"] == []
    assert "could not be read" in p["reason"]


def test_a_filing_that_fails_costs_that_finding_and_not_the_round(fleet, tracker,
                                                                  monkeypatch):
    """One finding filed, the next refused. The round still reaches its verdict, the
    successful issue is still on the record, and the failure is counted beside it —
    `_validate_work_order`'s own `except` would have abandoned the round half-settled."""
    real = issues.file_follow_up
    calls: list[str] = []

    def flaky(repo, title, body):
        calls.append(title)
        if len(calls) > 1:
            raise issues.IssueLifecycleError("gh said no", "REFUSED")
        return real(repo, title, body)

    monkeypatch.setattr(issues, "file_follow_up", flaky)
    wo = judged(fleet, "passed", "one", "two")

    p = payload(fleet, wo["id"])
    assert len(p["items"]) == 1 and p["failed"] == 1
    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
    finally:
        store.close()


def test_the_filer_cannot_take_a_round_down(fleet, tracker, monkeypatch):
    """The blanket guard, for the failures `ops` does not expect. The verdict has been
    paid for and every seat is already recorded by the time this runs."""
    def boom(*a, **k):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(ops, "file_validation_follow_ups", boom)
    wo = judged(fleet, "rejected", "Name the retry budget", reason="no test")

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "rejected"
    finally:
        store.close()
    assert filed(tracker) == []


# -- 5. the switch ---------------------------------------------------------------------


def test_with_filing_off_the_finding_is_discarded_and_still_does_not_reject(fleet,
                                                                            tracker):
    """`follow_ups: false` does NOT restore the old behaviour: §3 made the rejection
    change unconditional and this knob does not reach it. So `False` only chooses to
    throw the record away, which is why it ships True."""
    fleet.reconfigure(follow_ups=False)
    wo = judged(fleet, "passed", "Name the retry budget")

    assert filed(tracker) == []
    assert tracker.calls == [], "a `gh` call was made with filing switched off"
    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "passed"
    finally:
        store.close()


def test_it_ships_on(fleet):
    """The ruled carve-out to `ValidationConfig`'s ships-disabled rule (Neo, question
    309): this knob gates no behaviour change, only whether the record survives, and
    discarding is the one outcome nobody wants. Asserted on a catalog that names neither
    the field nor a project override, which is every fleet's."""
    from jarvis.catalog import ValidationConfig

    assert ValidationConfig().follow_ups is True
    assert fleet.spec.validation.follow_ups is True


# -- 6. the timeline -------------------------------------------------------------------


def test_the_filing_is_readable_on_the_timeline(fleet, tracker):
    """`timeline.event_level` returns `signal` for a kind it does not know, so an
    unregistered kind RENDERS — as the bare kind beside a JSON blob — and looks fine
    (`kn-3f133363`). One branch covers `jarvis wo show` and the dashboard, because both
    call `build_timeline`."""
    wo = judged(fleet, "passed", "Name the retry budget")

    line = _timeline(fleet, wo["id"])
    assert line["label"] == "Review filed 1 follow-up issue"
    assert line["detail"].startswith("#")
    # NO SEAT NAME: the payload carries them and this surface is read by the submitter.
    assert "maintainer" not in line["label"] + line["detail"]


def test_the_timeline_says_when_a_follow_up_could_not_be_filed(fleet, tracker,
                                                               monkeypatch):
    """The label changes, not just the detail. A round that raised follow-ups and filed
    none must not read as a round that raised none — that is the whole difference a
    remote destination introduces."""
    monkeypatch.setattr(issues, "follow_ups_filed", _unreadable)
    wo = judged(fleet, "passed", "Name the retry budget")

    line = _timeline(fleet, wo["id"])
    assert line["label"] == "Review raised follow-ups it could not file"
    assert "1 could not be filed" in line["detail"]


def test_the_timeline_distinguishes_kept_from_could_not_be_filed(fleet, fake_gh):
    """THE THIRD OUTCOME, and it is not a failure: nothing was published because nothing
    may be, and the finding is whole on the record. A reader told "could not be filed"
    goes looking for a broken tracker that is working perfectly."""
    fake_gh.issue_series(SERIES)  # no `origin`
    wo = judged(fleet, "passed", "Name the retry budget")

    line = _timeline(fleet, wo["id"])
    assert line["label"] == "Review kept 1 follow-up off the tracker"
    assert "kept on the internal record" in line["detail"]
    assert "could not be filed" not in line["detail"]


def _timeline(fleet, wo_id: str) -> dict:  # noqa: F811
    store = fleet.store()
    try:
        rows = timeline.build_timeline(store.get_work_order(wo_id),
                                       store.list_events(wo_id), [])
    finally:
        store.close()
    return next(e for e in rows if e["kind"] == ops.FOLLOW_UPS_EVENT)


def test_the_timeline_is_not_folded_away_as_debug():
    """`DEBUG_KINDS` is what `jarvis wo show` hides by default. A filing is an outcome of
    the round, not plumbing."""
    assert ops.FOLLOW_UPS_EVENT not in timeline.DEBUG_KINDS


# -- 7. the surfaces -------------------------------------------------------------------


def test_the_named_key_list_carries_follow_ups_on_every_round(fleet, tracker):
    """`ops.validation_rounds` projects a NAMED KEY LIST — a key not in it is a key
    `jarvis wo show`, `jarvis fo show` and both dashboard pages can never see. ALWAYS
    PRESENT, even empty, which is the rule `assumptions` and `alarms` already follow."""
    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),          # files nothing
                    verdict("passed", "Name the retry budget"))

    store = fleet.store()
    try:
        rounds = ops.validation_rounds(store, wo_id=wo["id"])
    finally:
        store.close()
    assert rounds[0]["follow_ups"]["filed"] == []
    assert [i["title"] for i in rounds[1]["follow_ups"]["filed"]] == \
        ["Name the retry budget"]
    assert rounds[1]["follow_ups"]["filed"][0]["seat"] == "maintainer"
    assert rounds[1]["follow_ups"]["filed"][0]["url"].startswith(SERIES)
    assert all(r["follow_ups"]["dropped"] == 0 for r in rounds)


def test_the_projection_never_asks_github_what_a_round_filed(fleet, tracker):
    """Read from the EVENT alone. A surface that asked the tracker would make
    `jarvis wo show` fail when GitHub is unreachable, and would re-read one issue per
    round per page view."""
    wo = judged(fleet, "passed", "Name the retry budget")
    before = len(tracker.calls)

    store = fleet.store()
    try:
        rounds = ops.validation_rounds(store, wo_id=wo["id"])
        ops.validation_detail(store, wo_id=wo["id"])
    finally:
        store.close()

    assert rounds[-1]["follow_ups"]["filed"], "the fixture filed nothing"
    assert len(tracker.calls) == before, "a surface called `gh`"


def test_round_line_counts_them_and_stays_silent_at_zero(fleet, tracker):
    """The one-line formatter both `show` commands share, so a count belongs there or
    nowhere. Silent at zero, which is every round on a fleet that has not turned the
    panel on — and it names no seat, because the submitter reads this."""
    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),
                    verdict("passed", "Name the retry budget"))

    store = fleet.store()
    try:
        lines = [ops.round_line(r)
                 for r in ops.validation_rounds(store, wo_id=wo["id"])]
    finally:
        store.close()
    assert "follow-up" not in lines[0]
    assert "1 follow-up issue filed" in lines[1]
    assert "maintainer" not in lines[1]


def test_round_line_says_what_went_over_the_cap(fleet, tracker):
    fleet.reconfigure(max_follow_ups=2)
    wo = judged(fleet, "passed", *SIX)

    assert _line(fleet, wo).endswith(
        "· 2 follow-up issues filed, 4 over the cap"), _line(fleet, wo)


def test_round_line_says_what_could_not_be_filed_at_all(fleet, tracker, monkeypatch):
    """A round that filed NOTHING and dropped nothing still has something to say, and
    this is the shape that came out as `· , · 6 not filed` when the clauses were appended
    to a string instead of joined: the cap is never reached on this path, so the first
    two clauses are empty and only the third is not."""
    monkeypatch.setattr(issues, "follow_ups_filed", _unreadable)
    wo = judged(fleet, "passed", *SIX)

    assert _line(fleet, wo).endswith("· 6 not filed"), _line(fleet, wo)


def test_round_line_says_what_was_kept_off_the_tracker(fleet, tracker):
    """The third outcome on the same line: nothing was published, nothing failed, and
    the findings are on the record in full."""
    tracker.set_private(False)
    wo = judged(fleet, "passed", "one", "two")

    assert "2 kept internally" in _line(fleet, wo), _line(fleet, wo)


def _unreadable(repo, unit_id):
    raise issues.IssueLifecycleError("HTTP 403: rate limit exceeded", "REFUSED")


def _line(fleet, wo) -> str:  # noqa: F811
    store = fleet.store()
    try:
        return ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[-1])
    finally:
        store.close()


def test_wo_show_prints_the_count(fleet, tracker, capsys):
    """`jarvis wo show`'s human view goes through a THIRD reader — `cli._readable_rounds`
    — which collapses each round to `ops.round_line`. Fetching it through the command is
    what proves the chain."""
    wo = judged(fleet, "passed", "Name the retry budget")
    capsys.readouterr()

    cli.main(["wo", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "1 follow-up issue filed" in out
    assert "maintainer" not in out, "a seat name reached a default surface"


def test_validation_show_classifies_each_finding_and_links_the_issue(fleet, tracker,
                                                                     capsys):
    """§4.7.3: making "why was this rejected" answerable without reading five JSON blobs.

    This is the DELIBERATION surface, so it is also the one place besides the issue body
    where a seat's name sits beside what it said."""
    fleet.daemon.validator = Validator({
        "outcome": "rejected", "reason": "the new branch has no test",
        "seats": [{"seat": "tester", "status": "ok", "verdict": "reject",
                   "model": "sonnet", "latency_ms": 12,
                   "reply": '{"verdict": "reject", "reason": "no test", "asks": [],'
                            ' "findings": ['
                            '{"severity": "blocker", "title": "No test covers retry",'
                            ' "detail": "add one"},'
                            '{"severity": "follow_up", "title": "Name the retry budget",'
                            ' "detail": "in the docstring"}]}'}],
        "follow_ups": [follow_up("Name the retry budget", seat="tester")]})
    fleet.reconfigure(max_rounds=1)  # so this rejection SETTLES the loop and files
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    url = filed(tracker)[0]["url"]
    capsys.readouterr()

    cli.main(["validation", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "blocked: No test covers retry" in out
    assert f"follow-up {url}: Name the retry budget" in out
    assert "tester" in out, "the deliberation surface withheld the seat"


def test_the_dashboard_page_lists_the_issue_once_for_the_whole_order(fleet, tracker):
    """THE TEMPLATE IS A SURFACE YOU GO AND COUNT (`kn-99e37a4b`). The page does NOT read
    `ops.validation_rounds` — `_issues.html` is fed `ops.issue_index`, a projection of its
    own — and `{% if index.raised %}` is SILENTLY FALSY: drop the key and the whole
    section disappears with no error and a green suite.

    ONCE FOR THE ORDER, NOT ONCE PER ROUND, which is the change: two rounds ran and the
    issue is listed one time, with a count beside it. The per-round fragment it replaced
    could show neither.
    """
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo = _round_two(fleet,
                    verdict("rejected", reason="no test"),
                    verdict("passed", "Name the retry budget"))
    url = filed(tracker)[0]["url"]

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text
    shown = " ".join(page.split())

    assert shown.count("Raised by this order") == 1, "the list is per round again"
    assert page.count(f'href="{url}"') == 1, "the issue is listed twice"
    assert "1 follow-up" in shown, "the consolidated list has no count"
    assert "Name the retry budget" in page
    assert ops.FOLLOW_UPS_EVENT not in page      # the raw kind never reaches a reader
    assert "maintainer" not in page              # nor does the seat


def test_the_dashboard_section_bites_when_the_projection_loses_the_key(fleet, tracker,
                                                                       monkeypatch):
    """The mutation that proves the assertion above is load-bearing. `issue_index` is the
    projection THAT section reads; mutating `validation_rounds` or `validation_detail`
    instead would leave this test green, which is how you learn they were never one
    surface."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    wo = judged(fleet, "passed", "Name the retry budget")
    real = ops.issue_index
    monkeypatch.setattr(ops, "issue_index",
                        lambda *a, **k: {**real(*a, **k), "raised": []})

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert "Raised by this order" not in page


def test_the_dashboard_says_what_could_not_be_filed(fleet, tracker, monkeypatch):
    """The failure is a rendered fact, not only a log line — and it is TWO facts, so it
    is asserted as two.

    The work-order page renders the validation panel AND the timeline, and both of them
    report a failure in words containing "could not be filed". Asked only whether the
    PAGE says it, this test passed with the panel's branch gutted to `{% if false %}`:
    the timeline entry alone satisfied it. A check that asks whether SOME surface has a
    property is answered by the wrong one (`kn-99e37a4b`); each is now named by the
    wording only it emits.
    """
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    monkeypatch.setattr(issues, "follow_ups_filed", _unreadable)
    wo = judged(fleet, "passed", "Name the retry budget")

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text
    # Collapsed, because the sentences wrap across source lines and a reader sees the
    # collapsed text — asserting the source's line breaks would pin the indentation.
    shown = " ".join(page.split())

    assert "1 follow-up could not be filed as an issue" in shown, "the PANEL is silent"
    assert "1 could not be filed &mdash;" in shown or "1 could not be filed —" in shown, \
        "the TIMELINE is silent"
    assert shown.count("could not be read") == 2, "only one of the two gave the reason"


# -- 8. the feature order, and what must not widen -------------------------------------


def test_both_validation_loops_file_through_the_one_function(fleet):
    """`_round_config`'s docstring records Neo's ruling (question 176) that the two loops
    are held identical. A STRUCTURAL claim about the daemon, not a restatement of one
    function in terms of itself: each loop reaches the shared filer, and does so INSIDE
    the outcome branch — at settle, twice, once where the round passed and once where it
    was refused with no round left."""
    src = Path(Daemon._validate_work_order.__code__.co_filename).read_text()
    tree = ast.parse(src)
    for name in ("_validate_work_order", "_validate_feature"):
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        calls = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_file_follow_ups"]
        assert len(calls) == 2, f"{name} does not file at both settle points"
        branch = next(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Assign)
                      and any(getattr(t, "id", "") == "outcome" for t in n.targets))
        assert min(calls) > branch, f"{name} files before the outcome is known"


def test_a_feature_orders_follow_up_is_filed_on_the_same_tracker(fleet, tracker):
    """The other unit. The event goes to the MANAGER's timeline through
    `ops.feature_event` — a feature order has no timeline of its own — and the issue
    lands on the same project repository, because the unit under review is the project
    either way."""
    from tests.test_feature_orders import ASK, a_plan, child

    fo = ops.create_feature_order("proj_a", "the exporter", description=ASK)
    fleet.daemon.tick()
    ops.submit_plan(fo["id"], a_plan(child("alpha")))
    ops.review_plan(fo["id"], accept=True, decided_by="user")
    store = fleet.store()
    try:
        manager = store.manager_work_order(fo["id"])
        assert manager is not None
        rnd = store.open_validation_round(fo_id=fo["id"], fingerprint="ffff",
                                          summary="built it", evidence="ran pytest")
        result = ops.file_validation_follow_ups(
            store, fleet.spec, dict(rnd), [follow_up("Name the retry budget")],
            fleet.spec.validation, fo_id=fo["id"])
        assert len(result["items"]) == 1
        events = ops.feature_events_of_kind(store, fo["id"], ops.FOLLOW_UPS_EVENT)
        assert len(events) == 1 and events[0]["wo_id"] == manager["id"]
        rounds = ops.validation_rounds(store, fo_id=fo["id"])
        assert [i["title"] for i in rounds[0]["follow_ups"]["filed"]] == \
            ["Name the retry budget"]
    finally:
        store.close()

    body = filed(tracker)[0]["body"]
    assert fo["id"] in body, "the issue does not say which feature raised it"


def test_a_filed_follow_up_never_becomes_the_submitters_side_effect(fleet, tracker):
    """`ops.side_effects_of` tells the panel what durable work the SUBMITTER did outside
    the repository. An issue the PANEL filed is the panel's own output: a later round
    that saw it there would judge its own remarks as the submitter's deliverable — and
    `side_effects_sha` is in `evidence.fingerprint`, so it would break the repeat guard
    too."""
    wo = judged(fleet, "passed", "Name the retry budget")

    assert filed(tracker), "the fixture filed nothing, so the claim is vacuous"
    store = fleet.store()
    try:
        assert ops.side_effects_of(store, wo["id"]) == []
    finally:
        store.close()


def test_nothing_reaches_the_projects_backlog_any_more(fleet, tracker):
    """The ruling replaced the destination rather than adding to it. A row left behind
    here would be the same finding recorded twice, in two places that then disagree the
    moment the user closes one."""
    from jarvis.central_store import CentralStore

    judged(fleet, "passed", "Name the retry budget")

    central = CentralStore()
    try:
        assert central.list_backlog("proj_a", status=None) == []
    finally:
        central.close()
    assert filed(tracker), "the fixture filed nothing, so the claim is vacuous"


# -- 9. what may leave the machine -----------------------------------------------------
#
# Spec §9 of docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md, carrying
# over §8 of docs/superpowers/specs/2026-09-14-a-filed-bug-runs-itself.md: NO MODEL PROSE
# IS EVER PUBLISHED. The seat's words go out only where the OS has POSITIVELY established
# the tracker is private; public and "could not tell" reach the same branch.

#: The two shapes that make the leak test about something real rather than about a word:
#: a credential quoted back by a seat, and an absolute path off the machine the panel ran
#: on. Both are exactly the kind of remark a reviewer files rather than blocks on.
CREDENTIAL = "sk-live-9f3a2b7c1d4e5f60"
INTERNAL_PATH = "/srv/" + "jarvis-prod/checkouts/acme/secrets/fixtures/tokens.json"
LEAKY_DETAIL = (f"the fixture at {INTERNAL_PATH} still carries what looks like a live "
                f"token, {CREDENTIAL} — rotate it when convenient")

#: Substrings, not only the whole strings (`kn-bfbb2a3a`): a body that published half the
#: token, or the directory without the file, has published it.
DISTINCTIVE = ("sk-live-9f3a2b7c", "9f3a2b7c1d4e5f60", "jarvis-prod",
               "secrets/fixtures", "tokens.json", "rotate it when convenient")


def leaky(seat: str = "security") -> dict:
    """One finding whose detail and title both carry text that must not be published."""
    return {"seat": seat, "round": 1,
            "title": f"Rotate the credential in {INTERNAL_PATH}",
            "detail": LEAKY_DETAIL, "file": "src/proj/a.py", "symbol": "load_tokens",
            "failure": "load_tokens() reads the fixture token in production"}


def _judge_leaky(fleet) -> dict:  # noqa: F811
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [leaky()]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    return wo


def _everything_gh_was_told(tracker) -> str:
    """Every argument and every byte of stdin of every `gh` call the test made.

    READ OFF THE RECORDED CALLS, never off a return value: what leaked is what reached
    the process, and a body assembled safely and then handed to the wrong call is still
    published.
    """
    return "\n".join(" ".join(c["argv"]) + "\n" + (c["stdin"] or "")
                     for c in tracker.calls)


def test_a_public_tracker_is_told_nothing_at_all(fleet, tracker):
    """THE LEAK TEST, AND THE EMPTY-ISSUE TEST, on one finding. A finding quoting a
    credential and an absolute internal path, on a repository the privacy read reports
    PUBLIC: no part of either reaches `gh` — and no issue is opened either.

    The issue this used to file said the text was withheld and carried nothing else;
    twenty of those landed on a public tracker from one order (#523-#527, #538-#552) and
    a reader could not tell them apart. What the OS may not publish, it keeps.

    Paired with the control below — a filer that broke and did nothing at all passes the
    silence half of this on its own."""
    tracker.set_private(False)
    wo = _judge_leaky(fleet)

    told = _everything_gh_was_told(tracker)
    assert CREDENTIAL not in told and INTERNAL_PATH not in told
    assert LEAKY_DETAIL not in told
    for fragment in DISTINCTIVE:
        assert fragment not in told, f"{fragment!r} was published"
    assert [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]] == []

    # KEPT WHOLE, and readable: the finding, its detail, the seat and the code it names.
    row = kept_rows(fleet, wo["id"])[0]
    assert row["title"] == ops.follow_up_key(leaky()["title"])
    assert row["detail"] == LEAKY_DETAIL
    assert row["seat"] == "security" and row["round"] == 1
    assert row["file"] == "src/proj/a.py"
    item = payload(fleet, wo["id"])["withheld"][0]
    assert item["title"] == row["title"] and item["digest"] == row["digest"]
    assert payload(fleet, wo["id"])["items"] == []


def test_a_private_tracker_still_gets_the_finding_in_full(fleet, tracker):
    """THE CONTROL, on the identical finding. `set_private(True)` is the fixture's own
    default and is named here anyway: the claim is about what the privacy read ANSWERED,
    not about what the fixture happened to be left at."""
    tracker.set_private(True)
    wo = _judge_leaky(fleet)

    row = filed(tracker)[0]
    assert LEAKY_DETAIL in row["body"], "the private branch withheld the finding"
    assert row["title"].startswith(f"Rotate the credential in {INTERNAL_PATH}")
    assert "`security` seat" in row["body"] and wo["id"] in row["body"]
    assert kept_rows(fleet, wo["id"]) == [], "it was both filed and kept"


def test_the_issue_body_carries_the_failure_scenario_and_the_code(fleet, tracker):
    """What makes a filed issue actionable, and what the admissibility gate demanded
    before it could be filed at all: the file it is wrong in and how it goes wrong."""
    judged(fleet, "passed", "Name the retry budget")

    body = filed(tracker)[0]["body"]
    assert "src/proj/a.py" in body and "run" in body
    assert "returns None instead of raising" in body


@pytest.mark.parametrize("break_it", ["fail_privacy_read", "garble_privacy_read"])
def test_a_privacy_read_that_does_not_answer_keeps_the_finding(fleet, tracker,
                                                               break_it):
    """FAIL CLOSED, pinned on both ways of not answering: a `gh` that RAISES and a `gh`
    that exits 0 with something unparseable. Different code paths — `_run` raises for one
    and `json.loads` for the other — and one `except` is what is being verified, so the
    plain-public case above cannot stand in for either.

    ONLY the privacy read is broken. `fail()` would take the dedupe read down too and
    every assertion here would pass for that reason instead of for this one."""
    getattr(tracker, break_it)()
    wo = _judge_leaky(fleet)

    told = _everything_gh_was_told(tracker)
    for fragment in (CREDENTIAL, INTERNAL_PATH, *DISTINCTIVE):
        assert fragment not in told, f"{fragment!r} was published on an unknown tracker"
    assert [c for c in tracker.calls if c["argv"][:2] == ["issue", "create"]] == []
    assert len(kept_rows(fleet, wo["id"])) == 1
    assert payload(fleet, wo["id"])["withheld"], "the finding was lost with the read"


def test_the_title_carries_the_token_the_dedupe_reads(fleet, tracker):
    """The digest is derived from `follow_up_claim` — the file, the symbol and the title
    — and from nothing else, so a seat that rewords its sentence in a later round does
    not file the same observation twice. Pinned here rather than inside a flip test so a
    change to the derivation fails in one obvious place."""
    judged(fleet, "passed", "Name the retry budget")

    digest = ops.follow_up_digest_of(follow_up("  Name the   retry budget "))
    assert digest == ops.follow_up_digest_of(follow_up("Name the retry budget"))
    assert filed(tracker)[0]["title"] == f"Name the retry budget [{digest}]"
    assert ops.follow_up_token(filed(tracker)[0]["title"]) == digest
    assert ops.follow_up_token("Name the retry budget") == "", (
        "a pre-§9 title must report no token, or the fallback never runs")


def test_the_same_claim_reworded_is_not_filed_twice(fleet, tracker):
    """THE DUPLICATE THAT WALKED THROUGH. The same observation was filed three times on
    wo-0a9ba9b3 — #538 "CI ran nothing on PR #520, so the declared suite counts are
    unverifiable", #546 "No CI ran on PR #520, so the declared suite result is the only
    account of testing", #552 "CI ran no checks on PR #520" — because the key was the
    sha256 of the sentence. The key is the (file, symbol) the finding names plus its
    claim, so the rewording has to move the code it points at to file again."""
    wo = judged(fleet, "passed", "Name the retry budget")
    same = follow_up("Name the retry budget", file="src/proj/a.py", symbol="run")
    reworded = {**same, "title": "Name the retry budget"}

    settle_again(fleet, wo["id"], [reworded])
    assert len(filed(tracker)) == 1, "a reworded duplicate was filed"

    settle_again(fleet, wo["id"],
                 [follow_up("Name the retry budget", file="src/proj/b.py")])
    assert len(filed(tracker)) == 2, "a different file is a different finding"


def test_a_flip_to_public_neither_re_files_nor_re_records(fleet, tracker):
    """THE TRAP THIS FEATURE WOULD OTHERWISE SET, now across both destinations. Settle 1
    files A on a private tracker; the repository is flipped open; settle 2 re-raises A
    and adds B. A must not be filed again and must not be copied onto the internal
    record either, and B — which may no longer travel — must be kept."""
    a, b = "Name the retry budget", "Fold the two parsers into one"
    wo = judged(fleet, "passed", a)
    assert titles(tracker) == [a]

    tracker.set_private(False)
    settle_again(fleet, wo["id"], [follow_up(a), follow_up(b)])

    assert titles(tracker) == [a], "the flip re-filed a follow-up"
    assert [r["title"] for r in kept_rows(fleet, wo["id"])] == [b], (
        "the flip copied a filed finding onto the internal record, or lost the new one")


def test_a_follow_up_filed_before_this_shipped_is_not_filed_again(fleet, tracker):
    """The other half of the same trap: every issue already on a tracker was filed under
    a bare finding title or under a title-derived token, and neither matches the claim
    digest. Without both fallbacks this change re-files the fleet's entire standing
    backlog of follow-ups on the next settle each unit reaches.

    Both shapes in one test, because they fall back through different comparisons: the
    pre-§9 issue carries no token at all, and the §9 issue carries `follow_up_digest`.
    """
    wo = judged(fleet, "passed", "Name the retry budget")
    old = filed(tracker)[0]
    tracker.add_issue(old["url"], title="Name the retry budget", body=old["body"],
                      labels=old["labels"])                     # the pre-§9 shape
    other = "Fold the two parsers into one"
    tracker.add_issue(f"{SERIES}/901",
                      title=f"Validation follow-up {ops.follow_up_digest(other)}",
                      body=f"raised on {wo['id']}", labels=[issues.FOLLOW_UP_LABEL])

    settle_again(fleet, wo["id"],
                 [follow_up("Name the retry budget"), follow_up(other)])

    assert len(filed(tracker)) == 2, "a follow-up already on the tracker was filed again"


# -- 10. the admissibility gate --------------------------------------------------------
#
# Roughly half of the forty issues wo-0a9ba9b3 and wo-2005a89b filed were not defects in
# the deliverable: "CI has not run on the PR", "the PR body does not name…", "the
# docstring is stale", and coverage nits of the form "X has no test" — an unbounded
# supply in any codebase. A follow-up must name a behaviour that IS WRONG, in a file,
# with a concrete failure. The rest are worth telling the submitter and are told.


def test_a_finding_that_names_no_wrong_behaviour_is_not_an_issue(fleet, tracker):
    """The gate itself, on the four shapes that filled the tracker."""
    fleet.reconfigure(max_rounds=1)
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [nit("No test covers the repair path"),
                        nit("budget.spent still documents itself as two queries"),
                        nit("The PR body does not say which change owns the repair"),
                        nit("CI ran no checks on PR #520")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert filed(tracker) == []
    assert kept_rows(fleet, wo["id"]) == [], "an inadmissible finding was kept as a nit"
    assert payload(fleet, wo["id"]) == {}


def test_an_admissible_finding_beside_them_is_still_filed(fleet, tracker):
    """THE PAIRING. A gate that refused everything would pass the test above, and this
    order is explicitly not a licence to reject everything — #549 and #525 were real."""
    fleet.daemon.validator = Validator(
        {"outcome": "passed", "reason": "", "seats": _seats("pass"),
         "follow_ups": [nit("No test covers the repair path"),
                        follow_up("The stale-row repair repeats for ever")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    assert titles(tracker) == ["The stale-row repair repeats for ever"]


def test_the_inadmissible_ones_reach_the_submitter_instead(fleet, tracker):
    """NOT DROPPED, ROUTED. In a round that is going back anyway, a stale docstring costs
    nothing and is fixed in the same session; on the tracker it outlives the session and
    nobody acts on it. The blocker's own reason still leads."""
    fleet.reconfigure(max_rounds=9)
    fleet.daemon.validator = Validator(
        {"outcome": "rejected", "reason": "the new branch has no test",
         "seats": _seats("reject"),
         "follow_ups": [nit("The docstring for budget.spent is stale"),
                        follow_up("The stale-row repair repeats for ever")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()  # deliver the feedback

    told = sent(fleet, wo["id"])
    assert "the new branch has no test" in told
    assert "The docstring for budget.spent is stale" in told
    assert "The stale-row repair repeats for ever" not in told, (
        "a tracker item was pushed at the submitter")


def test_the_round_record_does_not_carry_the_aside(fleet, tracker):
    """The note is for whoever is fixing this, not for the record: writing it into the
    round's outcome would restate it on every surface that prints a round."""
    fleet.reconfigure(max_rounds=9)
    fleet.daemon.validator = Validator(
        {"outcome": "rejected", "reason": "the new branch has no test",
         "seats": _seats("reject"),
         "follow_ups": [nit("The docstring for budget.spent is stale")]})
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = fleet.store()
    try:
        rnd = store.latest_validation_round(wo_id=wo["id"])
    finally:
        store.close()
    assert rnd["reason"] == "the new branch has no test"


# -- 11. the surfaces of a withheld follow-up ------------------------------------------


def test_jarvis_issues_lists_the_kept_ones_marked_internal(fleet, tracker, capsys):
    """A finding kept off the tracker is still something the project might pick up, and
    `jarvis issues` is where the user chooses what to do next. Marked, because a row that
    looked like a tracker issue would send them to GitHub for a number that is not
    there."""
    tracker.set_private(False)
    judged(fleet, "passed", "The stale-row repair repeats for ever")
    capsys.readouterr()

    cli.main(["issues"])

    out = capsys.readouterr().out
    assert "The stale-row repair repeats for ever" in out
    assert "internal" in out
    assert "no tracker issue was opened" in out


def test_the_work_order_page_and_show_carry_them(fleet, tracker, capsys):
    """Both ends of `ops.issue_index`, which is what the page and the command share."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    tracker.set_private(False)
    wo = judged(fleet, "passed", "The stale-row repair repeats for ever")

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text
    shown = " ".join(page.split())
    assert "Kept on the internal record" in shown
    assert "The stale-row repair repeats for ever" in shown
    assert "no tracker issue was opened" in shown

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"]])
    out = capsys.readouterr().out
    assert "follow_ups_kept" in out
    assert "internal only: The stale-row repair repeats for ever" in out
