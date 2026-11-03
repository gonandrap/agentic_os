"""The steward reviews backlog proposals (2026-11-03-backlog-operability §3)"""

from __future__ import annotations

import itertools

import pytest

from jarvis import db, ops, steward
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon

REF = {"kind": "neo_question", "id": 4}
#: Stored fine (normalise only checks keys) but `ops.origin_url` cannot resolve it
LOST_REF = {"kind": "gate", "project": "proj_a", "id": "not-a-number"}


@pytest.fixture()
def clock(monkeypatch):
    ticks = itertools.count(1000)
    monkeypatch.setattr(db, "now", lambda: float(next(ticks)))


@pytest.fixture()
def central(jarvis_home, clock):
    c = CentralStore()
    yield c
    c.close()


def propose(central, title, description="", *, refs=None, **kw):
    fields = {"item_type": "task", "severity": "medium", "suggested_kind": "wo", **kw}
    return central.propose_backlog("proj_a", title, description,
                                   origin_refs=[REF] if refs is None else refs, **fields)


def review(central, item):
    item = central.get_backlog(item["id"])
    r = steward.review_proposal(central, item)
    steward.apply_review(central, item, r)
    return central.get_backlog(item["id"])


def test_a_clean_proposal_opens(central):
    got = review(central, propose(central, "dashboard shows stale cost totals"))
    assert (got["status"], got["review_rule"]) == ("open", "accepted")
    assert got["review_note"] and got["reviewed_at"]


def test_a_user_filed_item_is_not_reviewed(central):
    mine = central.add_backlog("proj_a", "a user idea")
    r = steward.review_proposal(central, mine)
    assert r.rule == "origin-user"
    steward.apply_review(central, mine, r)
    got = central.get_backlog(mine["id"])
    assert (got["status"], got["review_rule"], got["reviewed_at"]) == ("open", "", None)


def test_an_exact_duplicate_is_dropped_and_folds_its_sightings(central):
    kept = central.add_backlog("proj_a", "Fix the  login-bug!",
                               origin_refs=[{"kind": "order", "id": "wo-11111111"}])
    dup = propose(central, "fix the login bug", refs=[REF])
    got = review(central, dup)
    assert (got["status"], got["review_rule"], got["duplicate_of"]) == \
        ("dropped", "exact-duplicate", kept["id"])
    assert central.get_backlog(kept["id"])["origin_refs"] == [
        {"kind": "order", "id": "wo-11111111"}, REF]
    # Folding the same sighting twice does not repeat it
    central.append_backlog_origin_refs(kept["id"], [REF])
    assert len(central.get_backlog(kept["id"])["origin_refs"]) == 2


def test_a_different_title_is_not_an_exact_duplicate(central):
    central.add_backlog("proj_a", "fix the login bug")
    other = review(central, propose(central, "fix the logout timer"))
    assert other["status"] == "open" and other["duplicate_of"] is None


def test_a_duplicate_of_a_promoted_item_names_its_order(central):
    kept = central.add_backlog("proj_a", "fix the login bug")
    central.mark_backlog(kept["id"], "promoted", promoted_wo_id="wo-22222222")
    got = review(central, propose(central, "fix the login bug"))
    assert got["duplicate_of"] == kept["id"] and "wo-22222222" in got["review_note"]


def test_a_settled_or_other_project_item_is_no_duplicate(central):
    done = central.add_backlog("proj_a", "fix the login bug")
    central.mark_backlog(done["id"], "done")
    central.add_backlog("proj_b", "fix the login bug")
    assert review(central, propose(central, "fix the login bug"))["status"] == "open"


def test_of_two_identical_proposals_the_older_is_kept(central):
    first = propose(central, "fix the login bug")
    second = propose(central, "fix the login bug")
    assert review(central, first)["status"] == "open"
    assert review(central, second)["duplicate_of"] == first["id"]


def test_an_unresolved_ref_quarantines(central):
    got = review(central, propose(central, "something real", refs=[REF, LOST_REF]))
    assert (got["status"], got["review_rule"]) == ("quarantined", "unresolved-ref")
    assert "not found" in got["review_note"]


def test_a_near_restatement_is_quarantined_naming_the_candidate(central):
    kept = central.add_backlog("proj_a", "cost dashboard totals stale after midnight rollover")
    got = review(central, propose(central, "cost dashboard totals stale after rollover"))
    assert (got["status"], got["review_rule"]) == ("quarantined", "possible-duplicate")
    assert kept["id"] in got["review_note"]
    assert got["duplicate_of"] is None  # never auto-folded


def test_overlap_of_the_smaller_wording_alone_is_not_a_duplicate(central):
    # 3 of the proposal's 3 words are shared (same_defect would fold) but 3/8 of the larger
    central.add_backlog("proj_a", "cost dashboard totals wrong during release freeze window weekend")
    got = review(central, propose(central, "cost dashboard totals"))
    assert steward.same_defect("cost dashboard totals",
                               "cost dashboard totals wrong during release freeze window weekend")
    assert got["status"] == "open"


def test_a_thin_wording_never_matches(central):
    central.add_backlog("proj_a", "the and for")  # no content words at all
    assert steward.same_defect("the and for", "for the and")  # the vacuous rule would fold
    assert review(central, propose(central, "for the and"))["status"] == "open"


@pytest.mark.parametrize("severity", ["critical", "blocker"])
def test_an_urgent_claim_is_quarantined(central, severity):
    got = review(central, propose(central, "workers lose messages", severity=severity))
    assert (got["status"], got["review_rule"]) == ("quarantined", "severity-claim")


def test_major_is_not_an_urgent_claim(central):
    assert review(central, propose(central, "workers lose messages",
                                   severity="major"))["status"] == "open"


def test_a_big_work_order_may_be_a_feature(central):
    long = review(central, propose(central, "rework one", "x" * 2501))
    files = " ".join(f"src/m{i}.py" for i in range(7))
    many = review(central, propose(central, "rework two", f"touches {files}"))
    for got in (long, many):
        assert (got["status"], got["review_rule"]) == ("quarantined", "kind-mismatch")
        assert got["review_note"] == "may be a feature order"


def test_within_bounds_or_already_a_feature_is_not_a_mismatch(central):
    six = " ".join(f"src/m{i}.py src/m{i}.py" for i in range(6))  # repeats count once
    assert review(central, propose(central, "rework three", f"{six} {'x' * 2000}"))["status"] == "open"
    assert review(central, propose(central, "rework four", "x" * 3000,
                                   suggested_kind="fo"))["status"] == "open"


def test_a_dangling_dependency_quarantines(central):
    gone = central.add_backlog("proj_a", "an abandoned idea")
    central.mark_backlog(gone["id"], "dropped")
    live = central.add_backlog("proj_a", "a live idea")
    got = review(central, propose(central, "builds on it", depends_on=[gone["id"]]))
    assert (got["status"], got["review_rule"]) == ("quarantined", "dangling-dependency")
    assert gone["id"] in got["review_note"]
    assert review(central, propose(central, "builds on live",
                                   depends_on=[live["id"]]))["status"] == "open"


def test_the_first_rule_wins(central):
    central.add_backlog("proj_a", "workers lose messages")
    got = review(central, propose(central, "workers lose messages", severity="critical",
                                  refs=[LOST_REF]))
    assert got["review_rule"] == "exact-duplicate"


@pytest.fixture()
def daemon(jarvis_home, fake_claude, catalog_file, project, clock):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def test_the_daemon_reviews_oldest_first_capped_without_the_ledger(daemon, monkeypatch):
    monkeypatch.setattr(steward, "BACKLOG_REVIEW_PER_TICK", 2)
    c = daemon.central
    items = [propose(c, t) for t in ("ledger migration stalls", "cache eviction leaks",
                                      "notifier retry storms")]
    assert daemon.review_backlog_proposals() == 2
    assert [c.get_backlog(i["id"])["status"] for i in items] == ["open", "open", "proposed"]
    assert daemon.review_backlog_proposals() == 1
    assert c.get_backlog(items[2]["id"])["status"] == "open"
    assert c.steward_decisions() == [] and c.agent_calls() == []


def test_a_failing_review_leaves_the_item_proposed_and_moves_on(daemon, monkeypatch):
    c = daemon.central
    bad, good = propose(c, "parser crashes on empty"), propose(c, "exporter timeout ignored")
    real = steward.review_proposal

    def flaky(central, item):
        if item["id"] == bad["id"]:
            raise RuntimeError("boom")
        return real(central, item)

    monkeypatch.setattr(steward, "review_proposal", flaky)
    assert daemon.review_backlog_proposals() == 1
    assert c.get_backlog(bad["id"])["status"] == "proposed"
    assert c.get_backlog(good["id"])["status"] == "open"
