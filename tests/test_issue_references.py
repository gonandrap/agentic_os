"""An issue knows every work order that pointed at it, and the order knows its issues.

The relation `tests/test_validation_follow_ups.py` left in prose. That file proved a
follow-up gets FILED with a footer naming its origin; the origin lived only in that
footer, so "which issues did this order raise" could be answered only by scraping GitHub
and "how many orders have run into this issue" could not be answered at all. The second
is the one the user asked for: the count is a priority signal.

WHAT THIS FILE HOLDS, and each is a different failure:

* THE COUNT IS OF DISTINCT WORK ORDERS. An order that both raised an issue and cites it
  counts once, and the strongest link wins. A count that double-counts is not a weaker
  signal, it is a misleading one.
* THE ADMITTING SET IS NARROW (Neo, question 409). A bare `#N` for something the project
  does not track is not a reference — `#N` is also how a pull request is written.
* NOTHING IS WRITTEN TO AN ISSUE WHOSE STATE THE OS HAS NOT READ, and nothing at all to a
  closed one. The OS does not argue with a person who closed something.
* THE LIST IS PER ORDER, NOT PER ROUND. That is the whole of the UI change, and a test
  that only asserts "the issue is on the page" is satisfied by the per-round fragment it
  replaced — so the assertions count.

The fleet fixture and the injected validator are `test_validation_follow_ups`'s, for its
reasons: a test that reached this code through a real panel would be grading a model.
"""

from __future__ import annotations

from jarvis import cli, issues, ops
from tests.test_validation_follow_ups import (  # noqa: F401  (fixtures + helpers)
    ORIGIN, REPO, SERIES, filed, fleet, judged, tracker, verdict)
from tests.test_validation_loop import Validator, finish, write_catalog  # noqa: F401


def store_of(fleet):  # noqa: F811
    return fleet.store()


def links(fleet, unit_id: str) -> list[dict]:  # noqa: F811
    store = store_of(fleet)
    try:
        return store.issue_links_of(unit_id)
    finally:
        store.close()


def board(fleet) -> list[dict]:  # noqa: F811
    store = store_of(fleet)
    try:
        return ops.issue_board(store)
    finally:
        store.close()


# -- 1. the relation, recorded where the prose used to be -----------------------------


def test_filing_a_follow_up_records_the_relation_as_well_as_the_footer(fleet, tracker):  # noqa: F811
    """BOTH, and the pairing is the point. The footer is what a person with only GitHub
    reads; the row is what makes the same fact answerable locally. A change that replaced
    one with the other would pass half of this."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]

    assert "as a follow-up rather than a blocker" in filed(tracker)[0]["body"]
    assert f"`{wo['id']}`" in filed(tracker)[0]["body"]

    rows = links(fleet, wo["id"])
    assert [(r["issue_url"], r["kind"]) for r in rows] == [(url, "raised")]
    assert rows[0]["round"] == 1
    assert rows[0]["announced_at"], \
        "the filing already wrote that sentence; the sweep must not repeat it"


def test_the_order_lists_every_follow_up_across_every_round(fleet, tracker):  # noqa: F811
    """THE CONSOLIDATION. Two rounds, two issues, ONE list with both — which is the thing
    a per-round fragment cannot be however many rounds it renders."""
    fleet.daemon.validator = Validator(verdict("rejected", "Name the retry budget",
                                               reason="no test"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    fleet.tick()
    fleet.daemon.validator = Validator(verdict("passed", "Fold the two parsers"))
    fleet.change(wo["id"], "print('two')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = store_of(fleet)
    try:
        index = ops.issue_index(store, wo["id"])
    finally:
        store.close()
    assert [i["title"] for i in index["raised"]] == ["Name the retry budget",
                                                     "Fold the two parsers"]
    assert {i["round"] for i in index["raised"]} == {1, 2}, \
        "the rounds collapsed; a reader cannot tell which review raised which"
    assert index["references"] == []


def test_wo_show_lists_the_follow_ups_and_still_withholds_the_seat(fleet, tracker,  # noqa: F811
                                                                   capsys):
    """The definition of done for the terminal half — and the rule it must not break on
    the way. `jarvis validation show` is the deliberation surface; this is a default
    document and the seat that raised a finding does not belong on it."""
    wo = judged(fleet, "passed", "Fold the two parsers into one", seat="architect")
    capsys.readouterr()

    cli.main(["wo", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "follow_ups_raised" in out
    assert "Fold the two parsers into one" in out
    assert "round 1" in out
    assert "architect" not in out, "a seat name reached a default surface"


# -- 2. what counts as a reference ----------------------------------------------------


def test_a_brief_that_cites_an_issue_by_url_is_a_reference(fleet, tracker):  # noqa: F811
    """The second admitting kind. A URL on the project's OWN repository is unambiguous —
    `/issues/N` cannot be anything else — so it counts without the issue being known."""
    url = f"{SERIES}/41"
    wo = ops.create_work_order("proj_a", "Do the thing",
                               description=f"Background: {url}")

    assert [(r["issue_url"], r["kind"]) for r in links(fleet, wo["id"])] == \
        [(url, "cited")]


def test_a_bare_hash_number_counts_only_for_an_issue_already_tracked(fleet, tracker):  # noqa: F811
    """THE NARROWING THAT KEEPS THE SIGNAL HONEST. `#N` is how a pull request is written
    too, and the OS cannot tell them apart without a network call it must not make at
    creation — so an unknown number is left alone rather than becoming a link to an issue
    that may not exist."""
    unknown = ops.create_work_order("proj_a", "One", description="see #1234")
    assert links(fleet, unknown["id"]) == [], "a bare number became a phantom issue"

    raiser = judged(fleet, "passed", "Fold the two parsers into one")
    number = issues.issue_number(filed(tracker)[0]["url"])
    citer = ops.create_work_order("proj_a", "Two",
                                  description=f"this is the same thing as #{number}")

    assert [r["kind"] for r in links(fleet, citer["id"])] == ["cited"]
    assert [r["unit_id"] for r in board(fleet)[0]["units"]] == [raiser["id"],
                                                               citer["id"]]


def test_an_issue_on_another_repository_is_not_linked(fleet, tracker):  # noqa: F811
    """One rule doing two jobs. The repository comes from the checkout's own `origin`,
    which is what every write is later held to (`kn-2531869c`) — and it is also what keeps
    the count a fact about this project's tracker."""
    wo = ops.create_work_order(
        "proj_a", "Do the thing",
        description="see https://github.com/someone/else/issues/7")

    assert links(fleet, wo["id"]) == []


def test_one_order_counts_once_and_the_strongest_link_wins(fleet, tracker):  # noqa: F811
    """THE COUNT IS OF DISTINCT WORK ORDERS. An order that raised an issue and then cites
    it — a later brief quoting its own finding — must not count twice, and re-recording
    the weaker kind must not overwrite the stronger."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]

    store = store_of(fleet)
    try:
        assert store.link_issue(url, wo["id"], "cited") is False
        rows = store.issue_links_of(wo["id"])
        assert [r["kind"] for r in rows] == ["raised"], "a weaker kind overwrote it"
        assert ops.issue_board(store)[0]["refs"] == 1
    finally:
        store.close()


# -- 3. what reaches the tracker ------------------------------------------------------


def test_the_sweep_comments_the_reference_and_labels_the_count(fleet, tracker):  # noqa: F811
    """The user's half: the count is on the issue, where they scan.

    A COMMENT PER NEW REFERENCE and a label carrying the total. The issue body is never
    edited — it may carry text a person wrote, and clobbering it is not the OS's to do.
    """
    raiser = judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    number = issues.issue_number(url)
    citer = ops.create_work_order("proj_a", "Two", description=f"same as #{number}")

    fleet.tick()

    issue = tracker.issue(url)
    body = "\n".join(issue.get("comments") or [])
    assert citer["id"] in body, "the new reference was never said out loud"
    assert raiser["id"] not in body, \
        "the raising order was announced twice — it is already in the body"
    assert "referenced: 2" in issue["labels"]
    assert not any(c["argv"][:2] == ["issue", "edit"] and "--body" in c["argv"]
                   for c in tracker.calls), "the issue body was rewritten"


def test_the_label_replaces_the_stale_count_rather_than_accumulating(fleet, tracker):  # noqa: F811
    """`set_priority_label`'s rule. GitHub keeps every label you add, so an issue that
    went from one reference to two would carry both numbers and the scan the label exists
    for would read the wrong one."""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    assert "referenced: 1" in tracker.issue(url)["labels"]

    ops.create_work_order("proj_a", "Two", description=f"same as {url}")
    fleet.tick()

    got = [n for n in tracker.issue(url)["labels"] if n.startswith("referenced:")]
    assert got == ["referenced: 2"]


def test_nothing_is_written_to_an_issue_a_person_closed(fleet, tracker):  # noqa: F811
    """DO NOT RACE THE LIFECYCLE, and do not argue with a human. A closed issue is a
    decision somebody made; a reference recorded afterwards stays in Jarvis and never
    reaches the tracker."""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    tracker.set_issue(url, state="CLOSED", labels=tracker.issue(url)["labels"])
    before = len(tracker.issue(url).get("comments") or [])
    # The cached state is minutes old and good for an hour; a person closing an issue
    # out from under the OS is exactly the case the TTL delays, so the test expires it.
    fleet.daemon.ISSUE_STATE_TTL = 0

    ops.create_work_order("proj_a", "Two", description=f"same as {url}")
    fleet.tick()

    issue = tracker.issue(url)
    assert issue["state"] == "CLOSED", "the OS reopened an issue a person closed"
    assert len(issue.get("comments") or []) == before
    assert "referenced: 2" not in issue["labels"]


def test_a_closed_issue_leaves_the_ranking(fleet, tracker):  # noqa: F811
    """The ranking is for choosing what to do next. A closed issue's count is history and
    a list with one at the top is a list with nothing to act on."""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    assert [r["issue_url"] for r in board(fleet)] == [url]

    tracker.set_issue(url, state="CLOSED")
    fleet.daemon.ISSUE_STATE_TTL = 0
    fleet.tick()

    assert board(fleet) == []


def test_the_state_ttl_is_what_keeps_the_sweep_cheap(fleet, tracker):  # noqa: F811
    """THE CEILING ON WHAT THIS COSTS, and the reason the two tests above have to expire
    it by hand. A tick every thirty seconds must not re-read every linked issue: nothing
    the OS does with the state needs it fresher than an hour, and without the TTL this
    sweep would be one subprocess per issue per tick for ever."""
    judged(fleet, "passed", "Fold the two parsers into one")
    fleet.tick()
    before = len([c for c in tracker.calls if c["argv"][:2] == ["issue", "view"]])

    fleet.tick()
    fleet.tick()

    after = len([c for c in tracker.calls if c["argv"][:2] == ["issue", "view"]])
    assert after == before, "the sweep re-read a state it had just read"


def test_an_unreachable_tracker_costs_nothing_locally(fleet, tracker):  # noqa: F811
    """Every Jarvis surface reads the RECORD. A sweep that cannot reach `gh` leaves the
    relation intact and the listing working — the rule `filed_follow_ups` already holds,
    one relation out."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    tracker.fail("could not resolve host")

    fleet.tick()

    assert [r["kind"] for r in links(fleet, wo["id"])] == ["raised"]
    assert len(board(fleet)) == 1


# -- 4. the surfaces ------------------------------------------------------------------


def test_the_project_page_ranks_the_issues_by_how_many_orders_hit_them(fleet, tracker):  # noqa: F811
    """WHERE THE USER PICKS THE NEXT THING TO WORK ON (Neo, question 410). A section on
    the page they already read, not a page of its own.

    TWO ISSUES, because "the most-referenced is first" is the claim and a page that
    printed them in any order satisfies "both are present".
    """
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    judged(fleet, "passed", "Fold the two parsers into one", "Name the retry budget")
    rows = filed(tracker)
    ops.create_work_order("proj_a", "Two", description=f"same as {rows[1]['url']}")
    fleet.tick()

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/project/{"proj_a"}").text
    shown = " ".join(page.split())

    assert "Tracker issues the fleet keeps hitting" in shown
    assert shown.index(rows[1]["title"]) < shown.index(rows[0]["title"]), \
        "the twice-referenced issue is not at the top"


def test_jarvis_issues_prints_the_count_and_who_pointed_at_it(fleet, tracker, capsys):  # noqa: F811
    """The terminal half of the same ranking — the CLI is the OS."""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    fleet.tick()
    capsys.readouterr()

    cli.main(["issues"])

    out = capsys.readouterr().out
    assert "Fold the two parsers into one" in out
    assert wo["id"] in out
    assert "(raised)" in out


def test_jarvis_issues_says_so_when_nothing_is_linked(fleet, capsys):  # noqa: F811
    """The empty case is a sentence, not a blank screen — every other listing's rule."""
    capsys.readouterr()

    cli.main(["issues"])

    assert "no tracker issue is linked" in capsys.readouterr().out


# -- 5. the per-round fragment kept what is genuinely per-round -----------------------


def test_the_round_still_reports_what_it_could_not_file(fleet, fake_gh):  # noqa: F811
    """WHAT MOVED AND WHAT DID NOT. The issues moved to the consolidated list; a finding
    that never BECAME an issue cannot appear there, so the failure count stays on the
    round that tried. Deleting it along with the links would lose the fact entirely."""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    fake_gh.issue_series(SERIES)  # no `origin` on the project: filing cannot happen
    wo = judged(fleet, "passed", "Name the retry budget")

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/{"proj_a"}/{wo['id']}").text
    shown = " ".join(page.split())

    assert "1 follow-up could not be filed as an issue" in shown
    assert "Raised by this order" not in shown, "an issue that does not exist is listed"
