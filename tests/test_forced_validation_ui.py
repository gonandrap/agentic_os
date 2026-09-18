"""The dashboard half of `jarvis validation force`: the button, and the diagnosis.

docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §8.

THE POINT IS NOT THAT A BUTTON EXISTS. It is that the page and the command can never
come to disagree about which work orders may be re-judged, and that a user looking at a
parked pull request can see WHY it is parked without opening a terminal. Both claims are
about SHARING — one refusal resolver, one hold — so both are pinned structurally
(kn-4ea33fe6): the shared function is monkeypatched and each surface has to change its
answer. A behavioural assertion passes over two independent correct copies, which is
precisely the state this file exists to make impossible.

Its fixtures are `test_forced_validation`'s: a separate module because those are about
the verb and these are about the surfaces, and one 900-line file is where a reader stops
reading.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.ui.app import create_app

from test_forced_validation import (  # noqa: F401 — fixtures, used by name
    LIVE_HEAD, Panel, artifact, fleet, judge, parked, project, write_catalog,
)

#: The commit round 1 accepted, on the population where the head then MOVED — a branch
#: that had `origin/main` merged into it to clear a conflict (wo-752eced8). Distinct from
#: `LIVE_HEAD` by construction: the whole diagnosis is that the two differ.
JUDGED = "9ee728c7399ee728c7399ee728c7399ee728c739"

#: Wording ONLY this page's re-judge section emits. The work-order page renders the
#: validation panel AND the timeline, and both talk about forced rounds — so a
#: whole-page assertion is answered by the wrong surface unless it is anchored to
#: something only the right one says (kn-d51713af).
HEADING = "Re-judge this pull request"


def page_of(wo_id: str, query: str = "") -> str:
    """The page as a READER sees it, whitespace collapsed.

    Collapsed because a template wraps a sentence across source lines and nobody sees
    the break; an assertion against the raw HTML would pass or fail on where the editor
    happened to fold a line.
    """
    text = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo_id}{query}").text
    return " ".join(text.split())


def press(wo_id: str, reason: str):
    """The button, pressed. Redirects are NOT followed: where it sends the reader is
    half of what this route does."""
    return TestClient(create_app(), follow_redirects=False).post(
        f"/wo/proj_a/{wo_id}/validation/force", data={"reason": reason})


# -- the button is the command ---------------------------------------------------------


def test_the_button_opens_the_round_the_command_would_have(fleet, project, fake_gh):
    """`ops.force_validation`, reached from the page — not a second path into
    `submit_for_validation`. The `finished` count is the assertion that says so: a
    re-implementation that went back through `ops.finish` would write a second one and
    put a worker's account on the record for work no worker did."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    response = press(wo["id"], "round 1 predates head_sha")

    assert response.status_code == 303
    assert response.headers["location"] == f"/wo/proj_a/{wo['id']}?forced=2#rejudge"
    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None and forced["round"] == 2
    assert forced["forced_reason"] == "round 1 predates head_sha"
    assert store.get_work_order(wo["id"])["status"] == "validating"
    assert len(store.events_of_kind(wo["id"], "finished")) == 1


def test_the_page_reports_the_round_it_opened_rather_than_refreshing_in_silence(
        fleet, project, fake_gh):
    """The two lines are `ops.forced_round_lines`, which the CLI prints — so a user who
    forces a round from either surface is told the same two facts. Without them the page
    just redraws, and "did that do anything" is answered by scrolling."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    press(wo["id"], "the head moved under the pass")

    page = page_of(wo["id"], "?forced=2")

    assert f"{wo['id']} [proj_a]: round 2 opened by hand — the head moved under the pass" \
        in page
    assert "was waiting_pr_merge, now validating" in page
    # ...and it is a REPORT of one act, not a permanent banner: a reader arriving at the
    # page later is not told about a round they did not just open.
    assert "opened by hand" not in page_of(wo["id"])


def test_the_report_is_rebuilt_from_the_record_and_never_from_the_query(
        fleet, project, fake_gh):
    """`?forced=` selects an event; it never supplies text. A round number that names no
    forced round renders nothing at all, so the query string cannot put words on the
    page — and cannot claim a round was forced when none was."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    assert "opened by hand" not in page_of(wo["id"], "?forced=7")
    assert "opened by hand" not in page_of(wo["id"], "?forced=<script>")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


# -- it refuses exactly what the command refuses, and says so BEFORE the press ----------


@pytest.mark.parametrize("status", ["completed", "cancelled", "running", "pending"])
def test_the_control_is_disabled_on_the_page_rather_than_failing_on_submit(
        fleet, project, fake_gh, status):
    """The rule is taught by the page. A settled order would be reopened by a verdict
    that could change nothing; a live one would have its session claimed by the round
    machine mid-task — and a user learns neither from a button that looks pressable."""
    store, wo = parked(fleet, project)
    store.set_status(wo["id"], status)

    page = page_of(wo["id"])

    assert HEADING in page, "the control vanished instead of explaining itself"
    assert "disabled" in page.split(HEADING, 1)[1]
    assert f"No round can be forced here: {wo['id']} is {status}" in page


def test_a_work_order_with_no_pull_request_says_so_where_the_button_is(
        fleet, project, fake_gh):
    """The one refusal a "only render this beside a pull request" rule would have hidden
    — and it is the refusal that tells the user what to do next."""
    store, wo = parked(fleet, project, pr_url=None)

    page = page_of(wo["id"])

    assert "carries no pull request" in page
    assert "disabled" in page.split(HEADING, 1)[1]


def test_a_round_the_panel_still_owns_disables_the_button_and_names_the_round(
        fleet, project, fake_gh):
    """A second round underneath one about to run takes the MAX-round slot from it."""
    store, wo = parked(fleet, project, outcome="pending")

    page = page_of(wo["id"])

    assert f"round 1 on {wo['id']} is pending" in page
    assert "disabled" in page.split(HEADING, 1)[1]


def test_the_press_that_loses_the_race_is_flashed_and_opens_nothing(
        fleet, project, fake_gh):
    """The disabled control cannot prevent the panel opening a round between the render
    and the press, so the route refuses the same way and the reason reaches the page.
    Rendering the rule is not a substitute for enforcing it."""
    store, wo = parked(fleet, project, outcome="pending")

    response = press(wo["id"], "re-judge it")

    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    assert response.headers["location"].endswith("#rejudge")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1
    assert "the panel has not finished with it" in TestClient(
        create_app()).post(f"/wo/proj_a/{wo['id']}/validation/force",
                            data={"reason": "re-judge it"}).text


def test_a_blank_reason_is_refused_by_the_page_as_it_is_by_the_flag(
        fleet, project, fake_gh):
    """`--reason` is required and `required` is on the box, but whitespace passes both:
    the refusal that matters is `ops`', and it has to reach the reader."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    response = press(wo["id"], "   ")

    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


def test_the_page_and_the_command_cannot_drift_about_what_may_be_forced(
        fleet, project, fake_gh, monkeypatch):
    """THE STRUCTURAL PIN, and the reason this file exists (kn-4ea33fe6). Two carefully
    written copies of "which statuses may be re-judged" pass every behavioural assertion
    above and give opposite answers the day one of them is edited. So the single
    resolver is replaced and BOTH surfaces are required to change their answer — a test
    that fails the moment somebody re-splits them, which no behavioural assertion does.
    """
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    monkeypatch.setattr(ops, "force_validation_refusal",
                        lambda *a, **k: "the resolver says no")

    assert "the resolver says no" in page_of(wo["id"])
    with pytest.raises(ops.OpsError, match="the resolver says no"):
        ops.force_validation(wo["id"], reason="try anyway")


def test_a_project_with_the_panel_off_gets_no_control_at_all(
        tmp_path, jarvis_home, fake_claude, project, fake_gh):
    """`automerge_state`'s rule, one authority along: a control for a mechanism a project
    never switched on would spend the user's attention on the absence of a feature they
    did not ask for. Every other refusal is about THIS order and is rendered; this one is
    about the project, and the honest rendering of it is nothing."""
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    store, wo = parked(daemon, project)
    assert HEADING in page_of(wo["id"])  # ...with the panel ON, it is there

    data = json.loads(catalog_path.read_text())
    data["os"]["validation"]["enabled"] = False
    catalog_path.write_text(json.dumps(data))

    assert HEADING not in page_of(wo["id"])


# -- the diagnosis: why this pull request is parked ------------------------------------


def parked_on_a_moved_head(fleet, project, fake_gh):
    """The production shape: round 1 passed on one commit, the branch moved, and the poll
    recorded the hold that followed."""
    store, wo = parked(fleet, project, judged=JUDGED)
    artifact(fake_gh)  # ...whose head is LIVE_HEAD
    spec = fleet.catalog.project("proj_a")
    spec.validation.enabled = spec.validation.auto_merge = True
    fleet.poll_pull_requests(spec, store)
    assert store.events_of_kind(wo["id"], "automerge_held"), "no hold was recorded"
    return store, wo


def test_the_page_names_the_commit_that_was_judged_and_the_one_that_is_there_now(
        fleet, project, fake_gh):
    """THE HALF THAT MAKES THE BUTTON USEFUL. A user looking at a parked order could see
    that it was parked and not why. Both commits, and the mismatch said in words rather
    than left to be spotted by comparing two hex strings."""
    store, wo = parked_on_a_moved_head(fleet, project, fake_gh)

    page = page_of(wo["id"])

    assert f"The panel judged {JUDGED[:10]}" in page
    assert f"the head of the pull request is now {LIVE_HEAD[:10]}" in page
    assert "not the same commit" in page
    # The round's own judged commit, on the round — the other half of the same question,
    # and the surface a reader checks against when there is more than one round.
    assert f"@{JUDGED[:10]}" in page


def test_the_diagnosis_is_the_hold_the_merge_machine_recorded_not_a_second_opinion(
        fleet, project, fake_gh, monkeypatch):
    """Structural, for the diagnosis what the resolver test is for the rule.
    `automerge.decide` worked this out on the tick that declined to merge, and a page
    deriving its own answer would need a `gh` call from a web request and would describe
    a different moment. Take the recorded hold away and the page has nothing to say."""
    store, wo = parked_on_a_moved_head(fleet, project, fake_gh)
    assert "The panel judged" in page_of(wo["id"])

    monkeypatch.setattr(ops, "automerge_state", lambda store, wo: None)

    page = page_of(wo["id"])
    assert "The panel judged" not in page
    # ...and the control is still there: a hold nobody recorded is not a refusal.
    assert HEADING in page


def test_a_hold_a_fresh_round_cannot_clear_is_not_diagnosed_as_one_that_can(
        fleet, project, fake_gh, monkeypatch):
    """A red build is not what this button fixes. Wording every hold as something to
    force a round over is how a user comes to spend round numbers on a failing CI run —
    so only the two codes a recorded commit actually clears are diagnosed."""
    store, wo = parked(fleet, project, judged=JUDGED)
    artifact(fake_gh)
    monkeypatch.setattr(ops, "automerge_state", lambda store, wo: {
        "kind": "automerge_held", "code": "checks_not_green", "round": 1,
        "judged_sha": JUDGED, "head_sha": JUDGED,
        "line": "held — CI has not finished a unanimous pass on this commit"})

    page = page_of(wo["id"])

    assert "The panel judged" not in page
    assert "not the same commit" not in page


def test_a_round_that_recorded_no_commit_is_diagnosed_in_its_own_words(
        fleet, project, fake_gh):
    """The population the command was written for: 73 rounds judged before `head_sha`
    shipped, holding on `sha_unrecorded` for ever. "The head moved" would be a lie about
    it — nothing moved, nothing was ever written down."""
    store, wo = parked(fleet, project)  # judged='', as every pre-0.10.0 round is
    artifact(fake_gh)
    spec = fleet.catalog.project("proj_a")
    spec.validation.enabled = spec.validation.auto_merge = True
    fleet.poll_pull_requests(spec, store)

    page = page_of(wo["id"])

    assert "never recorded WHICH commit it judged" in page
    assert "not the same commit" not in page


def test_forcing_the_round_from_the_page_is_what_lets_the_merge_arm(
        fleet, project, fake_gh):
    """End to end, from the button: the diagnosis is on the page, the press opens the
    round, the panel passes it, and the condition that held the merge is satisfied. The
    recovery the user has been asking an operator for, without an operator."""
    store, wo = parked_on_a_moved_head(fleet, project, fake_gh)
    spec = fleet.catalog.project("proj_a")
    assert store.list_approvals(wo["id"]) == []

    press(wo["id"], "origin/main was merged in to clear a conflict")
    judge(fleet, store, Panel("passed"))
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    fleet.poll_pull_requests(spec, store)
    assert [a["kind"] for a in store.list_approvals(wo["id"])] == ["auto_merge"]
