"""What the catalog filter does not cover (docs/superpowers/specs/2026-10-28-offboard-a-project.md §6.2)"""

from __future__ import annotations

import json

import pytest

from jarvis import invariants, morpheus, ops
from jarvis.catalog import MorpheusConfig
from jarvis.central_store import ARCHIVED, CentralStore
from jarvis.neo_store import NeoStore

COND = {"field": "status", "op": "eq", "value": "needs_review"}


@pytest.fixture()
def central():
    s = CentralStore()
    yield s
    s.close()


@pytest.fixture()
def neo():
    s = NeoStore()
    yield s
    s.close()


def _queue(central, project, *, deadline_ts=0.0):
    return central.add_morpheus_item(
        project=project, repo="o/r", pr_number=7, pr_url="", pr_title="t", wo_id="",
        release_tag="", hints="", script="", script_source="", state="queued",
        deadline_ts=deadline_ts)


def offboarded() -> list:
    return list(invariants.check_offboarded_activity())


# -- INV-OFFBOARDED-ACTIVITY: only a project with NO `projects` row


def test_a_live_row_naming_an_unknown_project_is_reported(central):
    central.upsert_project("proj_a", "/tmp/proj_a")
    central.add_inbox("proj_a", "registered: fine")
    central.add_inbox("os", "the OS's own row: fine")
    central.add_backlog("gone", "left behind by a purge")

    found = offboarded()

    assert [(v.context["project"], v.context["table"], v.context["rows"])
            for v in found] == [("gone", "backlog", 1)]
    assert "jarvis offboard gone --purge --yes" in found[0].detail
    assert found[0].repaired is False


def test_a_fleet_wide_detector_and_a_gate_rule_are_never_reported(central):
    central.arm_detector(central.add_detector("fleet-gap", COND, source="user")["id"],
                         by="user", reason="test")
    central.add_gate_rule("match", "regex", r"\bfrob\b", kind="pr_merge", source="neo",
                          project="gone")

    assert offboarded() == []


@pytest.mark.parametrize("archived", [True, False], ids=["archived", "no-row"])
@pytest.mark.parametrize("seed", ["morpheus", "question", "detector"])
def test_a_clean_archive_is_never_a_violation(central, neo, seed, archived):
    """Archive leaves these on purpose (§6.3); with no `projects` row they are a leak"""
    if archived:
        central.upsert_project("proj_b", "/tmp/proj_b")
        central.set_project_status("proj_b", ARCHIVED)
    if seed == "morpheus":
        _queue(central, "proj_b", deadline_ts=9e12)
    elif seed == "question":
        neo.ask("proj_b", "wo-1", "which way?")
    else:
        central.arm_detector(
            central.add_detector("scoped-gap", COND, project="proj_b", source="user")["id"],
            by="user", reason="test")

    assert len(offboarded()) == (0 if archived else 1)


# -- the daemon's global queues skip an archived project


def test_morpheus_sweep_leaves_an_archived_projects_item_alone(central, tmp_path):
    """Neither run in another checkout nor timed out into a spend-filing hand-off"""
    item = _queue(central, "proj_b")
    morpheus.sweep(central, MorpheusConfig(), lambda i: tmp_path, skip={"proj_b"})
    assert central.morpheus_item(item["id"])["state"] == "queued"
    assert morpheus.wanting_scripts(central, skip={"proj_b"}) == []

    morpheus.sweep(central, MorpheusConfig(), lambda i: tmp_path)
    assert central.morpheus_item(item["id"])["state"] == "timedout"


def test_neo_never_claims_an_archived_projects_question(neo):
    neo.ask("proj_b", "wo-1", "older, offboarded")
    mine = neo.ask("proj_a", "wo-2", "newer, live")

    assert neo.claim_next({"proj_b"})["id"] == mine["id"]
    assert neo.claim_next({"proj_b"}) is None
    assert neo.claim_next()["project"] == "proj_b"


# -- a restart does not bring it back (§6.3, last paragraph)


def test_a_restart_does_not_rebootstrap_an_offboarded_project(
        jarvis_home, fake_claude, project, tmp_path, monkeypatch):
    path = tmp_path / "catalog.json"
    document = {"os": {"notifications": {"sinks": ["log"]}},
                "projects": [{"name": n, "path": str(project), "description": "test"}
                             for n in ("proj_a", "proj_b")]}
    path.write_text(json.dumps(document))
    ops.start_os(str(path), foreground=True)
    central = CentralStore()
    try:
        central.set_project_status("proj_b", ARCHIVED)
    finally:
        central.close()
    # What section 7's catalog rewrite leaves behind
    document["projects"] = document["projects"][:1]
    path.write_text(json.dumps(document))
    seen: list[str] = []
    real = ops.bootstrap_project
    monkeypatch.setattr(ops, "bootstrap_project",
                        lambda p, **kw: (seen.append(p.name), real(p, **kw))[1])

    ops.start_os(str(path), foreground=True)

    assert seen == ["proj_a"]
    central = CentralStore()
    try:
        assert central.get_project("proj_b")["status"] == ARCHIVED
    finally:
        central.close()
