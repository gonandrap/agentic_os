"""The recurrence ledger: a rule that already exists and did not hold.

Spec: docs/superpowers/specs/2026-09-27-self-evolution.md §8. Three properties carry the
weight, and §8 names the tests for each because a verdict derived wrong files the finding
against the wrong half of the rule.

1. **Every verdict is derived from a REAL fire record**, written through
   `CentralStore.record_rule_fire`, never from a hand-set dict field. A test that sets
   `{"outcome": "refused"}` by hand proves `rules.recurrence_verdict` reads a key, not
   that the ledger and the firing pass agree about what a fire is.
2. **The ledger is the source of truth and the tracker a mirror.** `gh` unreachable
   leaves the row written, `filed_note` carrying the failure, and `ops.record_recurrence`
   returning normally.
3. **The tracker search keys on the CAUSE, never on the order id** (kn-2b03830f, which
   retracted and reversed kn-5d8a396a), and keeps `--state all` so a closed original can
   still be reopened.

Nothing here reaches the real `gh` or the network: the `issues` functions are replaced,
and the test-isolation gate already blocks the binary underneath them.
"""

from __future__ import annotations

import pytest

from jarvis import bugreport, cli, issues, ops, rules
from jarvis.central_store import CentralStore
from jarvis.github import GhUnavailable, GitHubError
from jarvis.issues import IssueLifecycleError
from jarvis.testing import FIXTURE_ISSUE_URL

COND = {"all": [{"field": "status", "op": "eq", "value": "needs_review"},
                {"field": "seconds_in_status", "op": "gte", "value": 3600}]}

GAP = "stale-panel-hold"
ORDER = "wo-recur-1"


@pytest.fixture()
def store(jarvis_home):
    s = CentralStore()
    yield s
    s.close()


def _detector(store, **kw):
    kw.setdefault("gap_class", GAP)
    kw.setdefault("condition", COND)
    kw.setdefault("project", "proj_a")
    kw.setdefault("source", "io")
    kw.setdefault("io_id", "io-1")
    kw.setdefault("fix_wo_id", "wo-9")
    kw.setdefault("issue_url", FIXTURE_ISSUE_URL)
    return store.add_detector(**kw)


def _arm(store, det):
    """Arm a detector through `CentralStore.arm_detector`, the verb §6 brought.

    It refuses a detector with no live remedy row — "a rule may act" needs something to
    act WITH — so this adds the one the real flow always has before arming.
    """
    if not store.remedy_rules_for(det["id"]):
        store.add_remedy_rule(det["id"], "nudge", argument="ask where it got to")
    return store.arm_detector(det["id"], by="test", reason="armed for the fixture")


def _fire(store, det, outcome, **kw):
    kw.setdefault("project", "proj_a")
    kw.setdefault("order_id", ORDER)
    kw.setdefault("order_kind", "work_order")
    kw.setdefault("fingerprint", "fp-1")
    kw.setdefault("mode", rules.ARMED)
    return store.record_rule_fire(detector_id=det["id"], outcome=outcome, **kw)


class _Tracker:
    """What the tracker was asked to do, with no `gh` behind it."""

    def __init__(self):
        self.state = "OPEN"
        self.found: list[dict] = []
        self.labelled: list[tuple[str, str]] = []
        self.commented: list[tuple[str, str]] = []
        self.reopened: list[tuple[str, str]] = []
        self.ensured: list[str] = []
        self.searched: list[tuple[str, str]] = []

    def install(self, monkeypatch):
        monkeypatch.setattr(issues, "ensure_regression_label",
                            lambda repo: self.ensured.append(repo))
        monkeypatch.setattr(issues, "view", lambda url, repo=None: issues.Issue(
            url=url, number=7, state=self.state, labels=(), title="the original"))
        monkeypatch.setattr(issues, "comment", lambda url, body, repo=None:
                            self.commented.append((url, body)))
        monkeypatch.setattr(issues, "reopen", lambda url, body:
                            self.reopened.append((url, body)))
        monkeypatch.setattr(issues, "add_label", lambda url, label, repo=None:
                            self.labelled.append((url, label)))

        def _search(detector_id, gap_class, repo=None):
            self.searched.append((detector_id, gap_class))
            return list(self.found)

        monkeypatch.setattr(issues, "recurrence_issues", _search)
        return self


@pytest.fixture()
def tracker(monkeypatch):
    return _Tracker().install(monkeypatch)


# -- one test per verdict, each from a real fire record ---------------------------------


def test_a_dry_run_detector_gives_not_armed(store, tracker):
    det = _detector(store)
    _fire(store, det, rules.RECORDED, mode=rules.DRY_RUN)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["recorded"] is True
    assert out["verdict"] == rules.NOT_ARMED
    assert out["detector"]["id"] == det["id"]
    assert "arming" in out["verdict_note"]
    assert out["recurrence"]["original_fix_wo_id"] == "wo-9"
    assert out["recurrence"]["original_issue_url"] == FIXTURE_ISSUE_URL


def test_an_armed_detector_with_no_fire_on_the_order_gives_missed(store, tracker):
    det = _arm(store, _detector(store))
    # A fire on a DIFFERENT order is not evidence about this one.
    _fire(store, det, rules.PROPOSED, order_id="wo-someone-else")
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["verdict"] == rules.MISSED
    assert "condition" in out["verdict_note"]


@pytest.mark.parametrize("outcome", [rules.PROPOSED, rules.REFUSED, rules.APPLIED])
def test_an_armed_detector_that_fired_gives_remedy_failed(store, tracker, outcome):
    det = _arm(store, _detector(store))
    _fire(store, det, outcome)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["verdict"] == rules.REMEDY_FAILED
    assert "REMEDY" in out["verdict_note"]


def test_an_unreadable_fire_gives_its_own_verdict_and_never_missed(store, tracker):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.UNREADABLE)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["verdict"] == rules.UNREADABLE_RECURRENCE
    assert out["verdict"] != rules.MISSED


def test_the_callers_note_and_the_verdict_note_are_different_keys(store, tracker):
    """Two sentences with two audiences, so two names: the row carries what the CALLER
    passed and `verdict_note` is the OS's sentence about the verdict (spec §8)."""
    det = _arm(store, _detector(store))
    _fire(store, det, rules.PROPOSED)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER,
                                note="what the caller saw")
    assert out["recurrence"]["note"] == "what the caller saw"
    assert "REMEDY" in out["verdict_note"]
    assert "note" not in out


def test_a_cleared_fire_is_still_the_evidence_the_verdict_reads(store, tracker):
    """`open_rule_fire` sees only UNCLEARED fires, and a cleared one still happened."""
    det = _arm(store, _detector(store))
    fire = _fire(store, det, rules.PROPOSED)
    store.close_rule_fire(fire["id"])
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["verdict"] == rules.REMEDY_FAILED


# -- the fourth path, which has no verdict: the tracker could not be reached ------------


def test_gh_unreachable_leaves_the_row_written_and_says_so(store, tracker, monkeypatch):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.PROPOSED)

    def _boom(url, repo=None):
        raise GhUnavailable("no gh on PATH", GitHubError.NO_GH)

    monkeypatch.setattr(issues, "view", _boom)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["recorded"] is True
    assert out["verdict"] == rules.REMEDY_FAILED
    assert GitHubError.NO_GH in out["filed_note"]
    assert "no gh on PATH" in out["filed_note"]
    # The ledger kept it, and kept the failure with it.
    rows = store.list_recurrences(detector_id=det["id"])
    assert len(rows) == 1 and rows[0]["filed_note"] == out["filed_note"]


def test_a_refusal_from_gh_is_recorded_rather_than_raised(store, tracker, monkeypatch):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.REFUSED)
    monkeypatch.setattr(issues, "comment", lambda url, body, repo=None: (_ for _ in ()
                        ).throw(IssueLifecycleError("gh refused", GitHubError.REFUSED)))
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert out["recorded"] is True and "gh refused" in out["filed_note"]


# -- the search: keyed on the cause, never on the order id -----------------------------


def test_the_tracker_search_keys_on_the_cause_and_never_on_the_order(store, monkeypatch):
    det = _arm(store, _detector(store, issue_url=""))
    _fire(store, det, rules.PROPOSED)
    seen: list[list[str]] = []

    def _run(args, *, url, tolerate="", stdin=None):
        seen.append(list(args))
        return "[]"

    monkeypatch.setattr(issues, "_run", _run)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    listing = [a for a in seen if a[:2] == ["issue", "list"]]
    assert len(listing) == 1
    argv = listing[0]
    assert "--state" in argv and argv[argv.index("--state") + 1] == "all"
    search = argv[argv.index("--search") + 1]
    assert det["id"] in search and GAP in search
    assert ORDER not in search
    assert not any(ORDER in a for a in argv)
    # Nothing was found, so nothing was filed, and the row still exists.
    assert out["recorded"] is True
    assert "no original issue" in out["filed_note"]


def test_a_hit_from_the_search_is_the_issue_that_is_commented(store, tracker):
    det = _arm(store, _detector(store, issue_url=""))
    _fire(store, det, rules.PROPOSED)
    tracker.found = [{"url": FIXTURE_ISSUE_URL, "number": 7, "state": "OPEN"}]
    ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert tracker.searched == [(det["id"], GAP)]
    assert [u for u, _ in tracker.commented] == [FIXTURE_ISSUE_URL]


# -- reopen or comment, and the label either way ---------------------------------------


def test_a_closed_original_is_reopened_and_never_duplicated(store, tracker):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.PROPOSED)
    tracker.state = "CLOSED"
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert [u for u, _ in tracker.reopened] == [FIXTURE_ISSUE_URL]
    assert tracker.commented == []
    assert tracker.labelled == [(FIXTURE_ISSUE_URL, issues.REGRESSION_LABEL)]
    assert "reopened" in out["filed_note"] and FIXTURE_ISSUE_URL in out["filed_note"]


def test_an_open_original_is_commented_on_and_not_reopened(store, tracker):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.PROPOSED)
    out = ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    assert [u for u, _ in tracker.commented] == [FIXTURE_ISSUE_URL]
    assert tracker.reopened == []
    assert tracker.labelled == [(FIXTURE_ISSUE_URL, issues.REGRESSION_LABEL)]
    assert "commented" in out["filed_note"]
    # The redaction boundary: what reaches the public tracker is the four fields only.
    body = tracker.commented[0][1]
    assert ORDER in body and GAP in body and det["id"] in body
    assert "needs_review" not in body


# -- no detector at all: a new gap, and never an invented verdict -----------------------


def test_no_detector_for_the_gap_class_is_a_new_gap(store, tracker):
    out = ops.record_recurrence(gap_class="never-seen-before", project="proj_a",
                                order_id=ORDER)
    assert out == {"recorded": False, "detector": None, "verdict": None,
                   "recurrence": None, "filed_note": "",
                   "verdict_note": out["verdict_note"]}
    assert "NEW gap" in out["verdict_note"]
    assert "never-seen-before" in out["verdict_note"]
    assert store.list_recurrences() == []
    assert tracker.commented == [] and tracker.reopened == []


# -- the store verbs ------------------------------------------------------------------


def test_add_recurrence_increments_the_detector_counter(store):
    det = _detector(store)
    assert det["recurrences"] == 0
    store.add_recurrence(gap_class=GAP, detector_id=det["id"], project="proj_a",
                         order_id=ORDER, verdict=rules.MISSED)
    assert store.get_detector(det["id"])["recurrences"] == 1
    store.add_recurrence(gap_class=GAP, detector_id=det["id"], project="proj_a",
                         order_id="wo-2", verdict=rules.NOT_ARMED)
    assert store.get_detector(det["id"])["recurrences"] == 2


def test_an_unknown_verdict_is_refused(store):
    det = _detector(store)
    with pytest.raises(ValueError, match="unknown recurrence verdict"):
        store.add_recurrence(gap_class=GAP, detector_id=det["id"], project="proj_a",
                             order_id=ORDER, verdict="probably_fine")
    assert store.list_recurrences() == []


def test_list_recurrences_is_newest_first_and_filters(store):
    det = _detector(store)
    other = _detector(store, gap_class="other-gap")
    first = store.add_recurrence(gap_class=GAP, detector_id=det["id"],
                                 project="proj_a", order_id="wo-1",
                                 verdict=rules.MISSED)
    second = store.add_recurrence(gap_class=GAP, detector_id=det["id"],
                                  project="proj_a", order_id="wo-2",
                                  verdict=rules.NOT_ARMED)
    store.add_recurrence(gap_class="other-gap", detector_id=other["id"],
                         project="proj_b", order_id="wo-3", verdict=rules.MISSED)
    assert [r["id"] for r in store.list_recurrences(detector_id=det["id"])] == [
        second["id"], first["id"]]
    assert [r["order_id"] for r in store.list_recurrences(project="proj_b")] == ["wo-3"]
    assert [r["id"] for r in store.list_recurrences(order_id="wo-1")] == [first["id"]]
    assert [r["id"] for r in store.list_recurrences(verdict=rules.NOT_ARMED)] == [
        second["id"]]
    assert store.get_recurrence(first["id"])["verdict"] == rules.MISSED
    assert store.get_recurrence(10_000) is None


def test_the_filed_note_is_written_in_place_afterwards(store):
    det = _detector(store)
    row = store.add_recurrence(gap_class=GAP, detector_id=det["id"], project="proj_a",
                               order_id=ORDER, verdict=rules.MISSED)
    assert row["filed_note"] == ""
    note = "commented on " + FIXTURE_ISSUE_URL
    assert store.set_recurrence_filed_note(row["id"], note)["filed_note"] == note
    with pytest.raises(KeyError):
        store.set_recurrence_filed_note(10_000, "nothing to write on")


def test_a_live_database_reaches_the_new_table(jarvis_home):
    """The property `test_rules_store.py`'s docstring names: a new table is untested
    until something writes it, closes the store and reads it back from the file."""
    first = CentralStore()
    try:
        det = _detector(first)
        first.add_recurrence(gap_class=GAP, detector_id=det["id"], project="proj_a",
                             order_id=ORDER, verdict=rules.REMEDY_FAILED,
                             note="read me back")
    finally:
        first.close()
    second = CentralStore()
    try:
        rows = second.list_recurrences(detector_id=det["id"])
    finally:
        second.close()
    assert [r["note"] for r in rows] == ["read me back"]
    assert rows[0]["verdict"] == rules.REMEDY_FAILED


# -- the reader the verdict owes (Neo question 974) -------------------------------------


def test_rules_show_carries_the_recurrences_and_the_cli_prints_them(store, tracker,
                                                                    capsys):
    det = _arm(store, _detector(store))
    _fire(store, det, rules.UNREADABLE)
    ops.record_recurrence(gap_class=GAP, project="proj_a", order_id=ORDER)
    data = ops.rules_show(det["id"])
    assert [r["verdict"] for r in data["recurrences"]] == [rules.UNREADABLE_RECURRENCE]
    assert cli.main(["rules", "show", det["id"]]) == 0
    line = [ln for ln in capsys.readouterr().out.splitlines()
            if ln.strip().startswith("recurrence ")][0]
    assert rules.UNREADABLE_RECURRENCE in line and ORDER in line
    assert "commented" in line


def test_a_detector_with_no_recurrences_prints_none(store, capsys):
    det = _detector(store)
    assert ops.rules_show(det["id"])["recurrences"] == []
    assert cli.main(["rules", "show", det["id"]]) == 0
    assert "recurrence " not in capsys.readouterr().out


# -- the gh client: one wrapper, and the regression label ------------------------------


def test_reopen_and_the_label_go_through_the_existing_run(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(issues, "_run",
                        lambda args, *, url, tolerate="", stdin=None:
                        seen.append(list(args)) or "")
    # `checked_issue_url` holds an unqualified URL to the OS's OWN tracker, so the URL
    # here is built from it rather than from the fixture repo.
    url = f"https://github.com/{bugreport.bug_repo()}/issues/7"
    issues.reopen(url, "it came back")
    issues.ensure_regression_label("owner/repo")
    assert seen[0][:2] == ["issue", "reopen"]
    assert seen[0][-2:] == ["--comment", "it came back"]
    assert seen[1][:3] == ["label", "create", issues.REGRESSION_LABEL]
    assert issues.REGRESSION_COLOUR not in (issues.FOLLOW_UP_COLOUR,
                                            issues.LABEL_COLOUR)


def test_the_search_tolerates_output_that_is_not_json(monkeypatch):
    monkeypatch.setattr(issues, "_run",
                        lambda args, *, url, tolerate="", stdin=None: "not json")
    assert issues.recurrence_issues("dt-1", GAP, "owner/repo") == []
