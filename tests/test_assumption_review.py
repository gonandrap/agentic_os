"""Per-assumption review: one work order, one verdict per assumption.

The case that forced this: a work order came back with two assumptions, one worth
accepting and one worth rejecting, and the only controls were "accept all" and
"reject all". These tests pin both the ability to split the verdict and the settling
rule that keeps a split verdict from quietly landing in `completed`.
"""

from __future__ import annotations

import pytest

from jarvis import cli, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.invariants import check_project
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def two_assumptions(daemon, title: str = "risky refactor") -> tuple[dict, list[dict]]:
    """A finished work order sitting in needs_review with two pending assumptions."""
    wo = ops.create_work_order("proj_a", title)
    daemon.tick()
    ops.assume(wo["id"], "assumed the API is v2")
    ops.assume(wo["id"], "assumed we can drop python 3.10")
    assert ops.finish(wo["id"], "done-ish")["status"] == "needs_review"
    return wo, pending(wo["id"])


def store_for(wo_id: str) -> ProjectStore:
    return ProjectStore(ops.find_work_order(wo_id)[1])


def pending(wo_id: str) -> list[dict]:
    store = store_for(wo_id)
    try:
        return store.pending_assumptions(wo_id)
    finally:
        store.close()


def fresh(wo_id: str) -> dict:
    return ops.find_work_order(wo_id)[2]


# -- the split verdict -----------------------------------------------------------


def test_accepting_one_leaves_the_other_pending(started):
    daemon = started
    wo, assumptions = two_assumptions(daemon)

    out = ops.review_assumption(wo["id"], assumptions[0]["id"], accept=True)
    assert out["settled"] == "pending"
    assert out["pending_left"] == 1
    assert out["content"] == "assumed the API is v2"

    still = pending(wo["id"])
    assert [a["content"] for a in still] == ["assumed we can drop python 3.10"]

    # nothing settles while a decision is still owed, and the flag counts down
    record = fresh(wo["id"])
    assert record["status"] == "needs_review"
    assert record["needs_attention"] == 1
    assert record["attention_reason"] == "1 assumption pending your review"


def test_mixed_verdict_does_not_complete_the_work_order(started):
    """The whole point: accept one, reject the other, and the work order stays open."""
    daemon = started
    wo, assumptions = two_assumptions(daemon)

    ops.review_assumption(wo["id"], assumptions[0]["id"], accept=True)
    out = ops.review_assumption(wo["id"], assumptions[1]["id"], accept=False)

    assert out["settled"] == "rejected"
    assert out["pending_left"] == 0
    record = fresh(wo["id"])
    assert record["status"] == "needs_review"  # NOT completed
    assert record["needs_attention"] == 1
    assert "rejected" in record["attention_reason"]

    # and the verdicts are on the record, one each
    store = store_for(wo["id"])
    try:
        rows = {a["content"]: a["status"] for a in store.all_assumptions(wo["id"])}
    finally:
        store.close()
    assert rows == {"assumed the API is v2": "accepted",
                    "assumed we can drop python 3.10": "rejected"}


def test_a_mixed_verdict_survives_the_reconciler(started):
    """Attention is re-derived every tick, so the settling rule only counts if it
    outlives a tick: the invariants must not relabel or retire a rejected round."""
    daemon = started
    wo, assumptions = two_assumptions(daemon)
    ops.review_assumption(wo["id"], assumptions[0]["id"], accept=True)
    ops.review_assumption(wo["id"], assumptions[1]["id"], accept=False)

    daemon.tick()
    store = store_for(wo["id"])
    try:
        check_project(store, repair=True)
    finally:
        store.close()

    record = fresh(wo["id"])
    assert record["status"] == "needs_review"
    assert record["needs_attention"] == 1
    assert "rejected" in record["attention_reason"]


def test_accepting_every_assumption_one_by_one_completes(started):
    daemon = started
    wo, assumptions = two_assumptions(daemon)

    ops.review_assumption(wo["id"], assumptions[0]["id"], accept=True)
    out = ops.review_assumption(wo["id"], assumptions[1]["id"], accept=True)

    assert out["settled"] == "completed"
    record = fresh(wo["id"])
    assert record["status"] == "completed"
    assert record["needs_attention"] == 0


def test_bulk_accept_cannot_undo_a_rejection_in_the_same_round(started):
    """Reject one, then hit "Accept all" — the rejection still owns the outcome."""
    daemon = started
    wo, assumptions = two_assumptions(daemon)

    ops.review_assumption(wo["id"], assumptions[0]["id"], accept=False)
    out = ops.review_work_order(wo["id"], accept=True)

    assert out["settled"] == "rejected"
    assert fresh(wo["id"])["status"] == "needs_review"


def test_bulk_review_still_settles_the_simple_cases(started):
    """The all-at-once path is an addition's baseline, not its casualty."""
    daemon = started
    wo, _ = two_assumptions(daemon)
    assert ops.review_work_order(wo["id"], accept=True)["settled"] == "completed"
    assert fresh(wo["id"])["status"] == "completed"

    wo2, _ = two_assumptions(daemon, title="second one")
    assert ops.review_work_order(wo2["id"], accept=False)["settled"] == "rejected"
    record = fresh(wo2["id"])
    assert record["status"] == "needs_review"
    assert "rejected" in record["attention_reason"]


def test_a_rejection_does_not_haunt_the_next_round(started):
    """After guidance and rework, a clean round must be able to complete.

    Without a notion of "this round" a rejected work order could never reach
    `completed` again — and there is no manual complete command to dig it out with.
    """
    daemon = started
    wo, assumptions = two_assumptions(daemon)
    ops.review_work_order(wo["id"], accept=False)
    assert fresh(wo["id"])["status"] == "needs_review"

    ops.send_message(wo["id"], "use v1, and keep 3.10")   # guidance closes the round
    ops.assume(wo["id"], "assumed v1 pagination is cursor-based")
    ops.finish(wo["id"], "reworked")

    reopened = pending(wo["id"])
    assert len(reopened) == 1
    out = ops.review_assumption(wo["id"], reopened[0]["id"], accept=True)
    assert out["settled"] == "completed"
    assert fresh(wo["id"])["status"] == "completed"


def test_reviewing_early_decides_without_settling(started):
    """A worker still running owns its own status; reviewing only stamps the row."""
    daemon = started
    wo = ops.create_work_order("proj_a", "long job")
    daemon.tick()
    ops.assume(wo["id"], "assumed UTC everywhere")
    a = pending(wo["id"])[0]

    out = ops.review_assumption(wo["id"], a["id"], accept=True)
    assert out["settled"] == "unchanged"
    assert fresh(wo["id"])["status"] != "completed"
    assert pending(wo["id"]) == []


# -- guardrails ------------------------------------------------------------------


def test_unknown_or_already_decided_assumption_is_refused(started):
    daemon = started
    wo, assumptions = two_assumptions(daemon)
    ops.review_assumption(wo["id"], assumptions[0]["id"], accept=True)

    with pytest.raises(ops.OpsError) as e:
        ops.review_assumption(wo["id"], assumptions[0]["id"], accept=False)
    assert "not pending" in str(e.value)
    assert str(assumptions[1]["id"]) in str(e.value)   # names what IS pending

    with pytest.raises(ops.OpsError):
        ops.review_assumption(wo["id"], 999_999, accept=True)

    # the refusal changed nothing
    assert len(pending(wo["id"])) == 1


def test_timeline_names_the_assumption_that_was_decided(started):
    daemon = started
    wo, assumptions = two_assumptions(daemon)
    ops.review_assumption(wo["id"], assumptions[1]["id"], accept=False)

    from jarvis.timeline import build_timeline
    store = store_for(wo["id"])
    try:
        entries = build_timeline(store.get_work_order(wo["id"]),
                                 store.list_events(wo["id"]),
                                 store.list_messages(wo["id"]))
    finally:
        store.close()
    hit = [e for e in entries if e["label"] == "Assumption rejected"]
    assert hit and hit[0]["detail"] == "assumed we can drop python 3.10"


# -- CLI parity ------------------------------------------------------------------


def test_cli_can_target_one_assumption(started, capsys):
    import json
    daemon = started
    wo, assumptions = two_assumptions(daemon)

    cli.main(["wo", "review", wo["id"], "--assumption", str(assumptions[0]["id"]),
              "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["accepted"] is True and out["pending_left"] == 1

    cli.main(["wo", "review", wo["id"], "--assumption", str(assumptions[1]["id"]),
              "--reject", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["accepted"] is False and out["settled"] == "rejected"
    assert fresh(wo["id"])["status"] == "needs_review"


def test_cli_without_the_flag_is_still_bulk(started, capsys):
    import json
    daemon = started
    wo, _ = two_assumptions(daemon)
    cli.main(["wo", "review", wo["id"], "--json"])
    out = json.loads(capsys.readouterr().out)
    assert out["reviewed"] == 2 and out["settled"] == "completed"
