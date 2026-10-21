"""An issue knows every work order that pointed at it, and the order knows its issues"""

from __future__ import annotations

from jarvis import cli, issues, ops
from tests.test_validation_follow_ups import (  # noqa: F401  (fixtures + helpers)
    ORIGIN, REPO, SERIES, filed, fleet, follow_up, judged, settle_again, tracker,
    verdict)
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


# -- 1. the relation, recorded where the prose used to be


def test_filing_a_follow_up_records_the_relation_as_well_as_the_footer(fleet, tracker):  # noqa: F811
    """BOTH, and the pairing is the point"""
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
    """THE CONSOLIDATION"""
    fleet.daemon.validator = Validator(verdict("passed", "Name the retry budget"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()
    # A SECOND SETTLE over the same order — what `jarvis validation force` produces
    settle_again(fleet, wo["id"], [follow_up("Fold the two parsers")])

    store = store_of(fleet)
    try:
        index = ops.issue_index(store, wo["id"])
    finally:
        store.close()
    assert [i["title"] for i in index["raised"]] == ["Name the retry budget",
                                                     "Fold the two parsers"]
    assert index["references"] == []


def test_the_list_reads_the_finding_even_when_the_tracker_may_not(fleet, tracker):  # noqa: F811
    """A PUBLIC TRACKER IS TOLD NOTHING, AND THE USER'S OWN LIST STILL READS"""
    tracker.set_private(False)
    fleet.daemon.validator = Validator(verdict("passed", "Name the retry budget"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = store_of(fleet)
    try:
        index = ops.issue_index(store, wo["id"])
        rows = store.issue_board()
    finally:
        store.close()

    assert index["raised"] == [], "an issue was opened on a tracker that may be public"
    assert [i["title"] for i in index["withheld"]] == ["Name the retry budget"]
    # ...and nothing was written into the tracker cache
    assert rows == []


def test_wo_show_lists_the_follow_ups_and_still_withholds_the_seat(fleet, tracker,  # noqa: F811
                                                                   capsys):
    """The definition of done for the terminal half — and the rule it must not break on the way"""
    wo = judged(fleet, "passed", "Fold the two parsers into one", seat="architect")
    capsys.readouterr()

    cli.main(["wo", "show", wo["id"]])

    out = capsys.readouterr().out
    assert "follow_ups_raised" in out
    assert "Fold the two parsers into one" in out
    assert "round 1" in out
    assert "architect" not in out, "a seat name reached a default surface"


# -- 2. what counts as a reference


def test_a_brief_that_cites_an_issue_by_url_is_a_reference(fleet, tracker):  # noqa: F811
    """The second admitting kind"""
    url = f"{SERIES}/41"
    wo = ops.create_work_order("proj_a", "Do the thing",
                               description=f"Background: {url}")

    assert [(r["issue_url"], r["kind"]) for r in links(fleet, wo["id"])] == \
        [(url, "cited")]


def test_a_bare_hash_number_counts_only_for_an_issue_already_tracked(fleet, tracker):  # noqa: F811
    """THE NARROWING THAT KEEPS THE SIGNAL HONEST"""
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
    """One rule doing two jobs"""
    wo = ops.create_work_order(
        "proj_a", "Do the thing",
        description="see https://github.com/someone/else/issues/7")

    assert links(fleet, wo["id"]) == []


def test_the_same_owner_and_repository_on_another_host_is_not_linked(fleet, tracker):  # noqa: F811
    """THE HOST IS PART OF THE IDENTITY, and this is the case that makes it a write path"""
    evil = f"https://evil.example/{REPO}/issues/7"

    cited = ops.create_work_order("proj_a", "Do the thing", description=f"see {evil}")
    assigned = ops.create_work_order("proj_a", "Fix the thing", issue_url=evil)

    assert links(fleet, cited["id"]) == []
    assert links(fleet, assigned["id"]) == []
    assert not ops.issues_on(evil, REPO)
    assert ops.issues_on(f"{SERIES}/7", REPO)


def test_one_order_counts_once_and_the_strongest_link_wins(fleet, tracker):  # noqa: F811
    """THE COUNT IS OF DISTINCT WORK ORDERS"""
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


# -- 3. what reaches the tracker


def test_the_sweep_comments_the_reference_and_labels_the_count(fleet, tracker):  # noqa: F811
    """The user's half: the count is on the issue, where they scan"""
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
    """`set_priority_label`'s rule"""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    assert "referenced: 1" in tracker.issue(url)["labels"]

    ops.create_work_order("proj_a", "Two", description=f"same as {url}")
    fleet.tick()

    got = [n for n in tracker.issue(url)["labels"] if n.startswith("referenced:")]
    assert got == ["referenced: 2"]


def test_nothing_is_written_to_an_issue_a_person_closed(fleet, tracker):  # noqa: F811
    """DO NOT RACE THE LIFECYCLE, and do not argue with a human"""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    tracker.set_issue(url, state="CLOSED", labels=tracker.issue(url)["labels"])
    before = len(tracker.issue(url).get("comments") or [])
    # The cached state is minutes old and good for an hour
    fleet.daemon.ISSUE_STATE_TTL = 0

    ops.create_work_order("proj_a", "Two", description=f"same as {url}")
    fleet.tick()

    issue = tracker.issue(url)
    assert issue["state"] == "CLOSED", "the OS reopened an issue a person closed"
    assert len(issue.get("comments") or []) == before
    assert "referenced: 2" not in issue["labels"]


def test_a_closed_issue_leaves_the_ranking(fleet, tracker):  # noqa: F811
    """The ranking is for choosing what to do next"""
    judged(fleet, "passed", "Fold the two parsers into one")
    url = filed(tracker)[0]["url"]
    fleet.tick()
    assert [r["issue_url"] for r in board(fleet)] == [url]

    tracker.set_issue(url, state="CLOSED")
    fleet.daemon.ISSUE_STATE_TTL = 0
    fleet.tick()

    assert board(fleet) == []


def test_the_state_ttl_is_what_keeps_the_sweep_cheap(fleet, tracker):  # noqa: F811
    """THE CEILING ON WHAT THIS COSTS, and the reason the two tests above have to expire it by hand"""
    judged(fleet, "passed", "Fold the two parsers into one")
    fleet.tick()
    before = len([c for c in tracker.calls if c["argv"][:2] == ["issue", "view"]])

    fleet.tick()
    fleet.tick()

    after = len([c for c in tracker.calls if c["argv"][:2] == ["issue", "view"]])
    assert after == before, "the sweep re-read a state it had just read"


def test_an_unreachable_tracker_costs_nothing_locally(fleet, tracker):  # noqa: F811
    """Every Jarvis surface reads the RECORD"""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    tracker.fail("could not resolve host")

    fleet.tick()

    assert [r["kind"] for r in links(fleet, wo["id"])] == ["raised"]
    assert len(board(fleet)) == 1


# -- 4. the surfaces


def test_the_project_page_ranks_the_issues_by_how_many_orders_hit_them(fleet, tracker):  # noqa: F811
    """WHERE THE USER PICKS THE NEXT THING TO WORK ON (Neo, question 410)"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    judged(fleet, "passed", "Fold the two parsers into one", "Name the retry budget")
    rows = filed(tracker)
    ops.create_work_order("proj_a", "Two", description=f"same as {rows[1]['url']}")
    fleet.tick()

    page = TestClient(create_app(), follow_redirects=False).get(
        "/project/proj_a").text
    shown = " ".join(page.split())

    assert "Tracker issues the fleet keeps hitting" in shown
    assert shown.index(rows[1]["title"]) < shown.index(rows[0]["title"]), \
        "the twice-referenced issue is not at the top"


def test_jarvis_issues_prints_the_count_and_who_pointed_at_it(fleet, tracker, capsys):  # noqa: F811
    """The terminal half of the same ranking — the CLI is the OS"""
    wo = judged(fleet, "passed", "Fold the two parsers into one")
    fleet.tick()
    capsys.readouterr()

    cli.main(["issues"])

    out = capsys.readouterr().out
    assert "Fold the two parsers into one" in out
    assert wo["id"] in out
    assert "(raised)" in out


def test_jarvis_issues_says_so_when_nothing_is_linked(fleet, capsys):  # noqa: F811
    """The empty case is a sentence, not a blank screen — every other listing's rule"""
    capsys.readouterr()

    cli.main(["issues"])

    assert "no tracker issue is linked" in capsys.readouterr().out


# -- 5. the per-round fragment kept what is genuinely per-round


def test_the_round_still_reports_what_never_became_an_issue(fleet, fake_gh):  # noqa: F811
    """WHAT MOVED AND WHAT DID NOT"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    fake_gh.issue_series(SERIES)  # no `origin` on the project: filing cannot happen
    wo = judged(fleet, "passed", "Name the retry budget")

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text
    shown = " ".join(page.split())

    assert "1 follow-up was kept on the internal record" in shown
    assert "Raised by this order" not in shown, "an issue that does not exist is listed"
    assert "Kept on the internal record" in shown, "the finding is nowhere on the page"
