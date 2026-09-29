"""A release order opens no validation round, at EITHER submission site — issue #838 §2.

docs/superpowers/specs/2026-09-29-a-release-blocked-by-a-red-main-retries-itself.md

Defect (b) of the live case: a blocked release authors no files and stages no tag, so
`evidence.nothing_to_judge` hit row 2 of its table, the round escalated with "nothing to
review", and `autoreview.HELD_PANEL_GAVE_UP` went on the assumption — so not even Neo
could clear it. The post-condition for a release is a machine check
(`Daemon.settle_shipped_releases`), so no panel is opened at all.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import autoreview, db, ops, release
from tests.test_release_staging import FakeRunner
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish)

ISSUE = "https://github.com/acme/proj/issues/826"


@pytest.fixture(autouse=True)
def no_real_systemd(monkeypatch):
    """These stage a release, so a tick would reach `release.maybe_restart` and ask the
    DEVELOPER's systemd to restart the fleet."""
    monkeypatch.setattr(release, "SystemdRunner", FakeRunner)


def release_order(fleet, *, status: str = "running", assume: str = "") -> str:
    store = fleet.store()
    try:
        wo = store.create_work_order("Ship the fix", status=status,
                                     metadata={release.BATCH_KEY: [ISSUE]})
        if assume:
            store.add_assumption(str(wo["id"]), assume)
        return str(wo["id"])
    finally:
        store.close()


def stage_release(wo_id: str, version: str = "0.10.26") -> None:
    """The marker `scripts/shipit.sh --stage` writes: the release WAS delivered."""
    path = release.marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "wo_id": wo_id, "project": "proj_a", "version": version,
        "tag": f"jarvis-{version}", "staged_at": 1_758_000_000, "state": "staged"}))


def rounds(fleet, wo_id: str):
    store = fleet.store()
    try:
        return store.validation_rounds(wo_id=wo_id)
    finally:
        store.close()


def row(fleet, wo_id: str) -> dict:
    store = fleet.store()
    try:
        return store.get_work_order(wo_id)
    finally:
        store.close()


def events(fleet, wo_id: str, kind: str) -> list[dict]:
    store = fleet.store()
    try:
        return [db.from_json(e["payload"], {})
                for e in store.events_of_kind(wo_id, kind)]
    finally:
        store.close()


def test_release_order_opens_no_round_on_finish(fleet):
    """§2 at `ops.finish`, and defect (b): validation is enabled and the release still
    reaches no panel. `land_when_cleared` is TOLD (`panel_cleared`), so it does not
    re-read a round that was never opened and park the release in `validating` for ever.
    """
    rel = release_order(fleet)
    stage_release(rel)

    result = ops.finish(rel, "shipped jarvis-0.10.26")

    store = fleet.store()
    try:
        assert store.latest_validation_round(wo_id=rel) is None
    finally:
        store.close()
    assert rounds(fleet, rel) == []
    assert result["status"] != "validating"
    assert row(fleet, rel)["status"] != "validating"
    assert events(fleet, rel, "validation_escalated") == []


def test_release_order_opens_no_round_on_review(fleet):
    """§2 at `_validates_on_review` — the catch-up path `review_work_order` reaches
    through `_land_after_acceptance`. A release order parked before two-gates shipped
    must not be sent to a panel now either."""
    rel = release_order(fleet, status="needs_review",
                        assume="shipped without the changelog entry")
    stage_release(rel)

    result = ops.review_work_order(rel, accept=True)

    assert rounds(fleet, rel) == []
    assert result["status"] == "completed"
    assert row(fleet, rel)["status"] == "completed"


def green_base() -> None:
    """A fresh GREEN stored reading, so `defer_red_release` falls through and the
    submission reaches the validation block this file is about."""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.set_base_health("proj_a", {"red": False, "base": "main",
                                           "checked_at": time.time()})
    finally:
        central.close()


def test_a_release_that_delivered_nothing_reaches_no_panel(fleet, monkeypatch):
    """Defect (b) EXACTLY: the blocked release — no staged tag, no attested effect, so
    nothing voids the packet — with validation enabled. `evidence.nothing_to_judge` used
    to escalate it with "nothing to review" and `autoreview.HELD_PANEL_GAVE_UP` put a
    hold even Neo could not clear. No round opens, so that guard is never reached."""
    from jarvis import evidence as evidence_mod

    assert ops.validation_config("proj_a").enabled
    monkeypatch.setattr(evidence_mod, "nothing_to_judge",
                        lambda *a, **k: pytest.fail("the empty-packet guard was reached"))
    rel = release_order(fleet)
    green_base()

    result = ops.finish(rel, "Release BLOCKED on a red main")

    assert rounds(fleet, rel) == []
    assert result["status"] != "validating"
    assert events(fleet, rel, "validation_escalated") == []
    holds = [e.get("reason") for e in events(fleet, rel, "autoreview_held")]
    assert autoreview.HELD_PANEL_GAVE_UP not in holds


def test_a_release_with_a_pull_request_reaches_no_panel_either(fleet):
    """§2's predicate is the ORDER, not the artifact: a release that opened a PR (a
    version bump, a changelog) is still judged by `settle_shipped_releases`."""
    rel = release_order(fleet)
    stage_release(rel)

    result = ops.finish(rel, "shipped jarvis-0.10.26",
                        pr_url="https://github.com/x/y/pull/731")

    assert rounds(fleet, rel) == []
    assert result["status"] != "validating"


def test_the_predicate_is_the_batch_keys_presence(fleet):
    """An EMPTY batch is still a release order — the key's presence is the whole test,
    and a release whose fixes were all overtaken must not be sent to a panel. A missing
    key, or one holding something that is not a list, is not a release order at all."""
    assert release.is_release_order({"metadata": db.to_json({release.BATCH_KEY: []})})
    assert not release.is_release_order({"metadata": db.to_json({})})
    assert not release.is_release_order({"metadata": db.to_json({"other": [ISSUE]})})
    assert not release.is_release_order(
        {"metadata": db.to_json({release.BATCH_KEY: ISSUE})})
    assert not release.is_release_order({"metadata": None})


def test_a_non_list_batch_key_still_opens_a_round(fleet):
    """The other side of it, at the call site: only a real release order is exempt."""
    wo = fleet.dispatch()
    store = fleet.store()
    try:
        store.update_work_order(wo["id"],
                                metadata=db.to_json({release.BATCH_KEY: "yes"}))
    finally:
        store.close()
    fleet.change(wo["id"], "print('two')\n")

    result = finish(fleet, wo["id"], pr="https://github.com/x/y/pull/2")

    assert result["status"] == "validating"
    assert len(rounds(fleet, wo["id"])) == 1


def test_an_ordinary_work_order_still_opens_one(fleet):
    """The other side of the predicate: nothing about validation changed for the work
    orders it was written for."""
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")

    result = finish(fleet, wo["id"], pr="https://github.com/x/y/pull/1")

    assert result["status"] == "validating"
    assert len(rounds(fleet, wo["id"])) == 1
