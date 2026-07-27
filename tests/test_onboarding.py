"""Onboarding: the bootstrap work order that produces a launcher contract, and the
staleness signals that ask for a fresh one.
"""

from __future__ import annotations

import json
import time

import pytest

from jarvis import launcher as lm
from jarvis import onboarding, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return load_catalog(catalog_file)


# -- the bootstrap prompt --------------------------------------------------------------


def test_bootstrap_prompt_is_not_a_work_order_prompt(started, project):
    spec = started.project("proj_a")
    prompt = onboarding.build_bootstrap_prompt(spec, "wo-abc")

    # It says what it is, and carries the protocol the session has to implement.
    assert "onboarding session" in prompt
    assert "This is NOT a work order" in prompt
    assert str(project / ".jarvis" / "launcher.json") in prompt
    assert "schema_version" in prompt and "state_map" in prompt
    assert "jarvis launcher verify proj_a --live" in prompt
    assert "jarvis wo finish wo-abc" in prompt
    # …and none of the worker contract, which would send it off writing code.
    assert "open a PR" not in prompt
    assert "jarvis wo assume" not in prompt


def test_reonboarding_hands_over_the_current_contract_to_amend(started, project,
                                                               fake_wrapper):
    fake_wrapper.install(project)
    spec = started.project("proj_a")
    contract = lm.load_contract(project / ".jarvis" / "launcher.json")
    prompt = onboarding.build_bootstrap_prompt(spec, "wo-abc", existing=contract,
                                               reason="the wrapper changed")
    assert "Amend this rather than starting over" in prompt
    assert '"name": "bgwrap"' in prompt
    assert "the wrapper changed" in prompt


# -- raising the work order ------------------------------------------------------------


def test_first_onboarding_waits_for_a_human_because_jarvis_cannot_spawn_one(
        started, project):
    result = ops.onboard("proj_a")

    assert result["dispatch"] is False
    assert result["amending"] is False
    prompt_path = project / ".jarvis" / "onboarding" / f"{result['wo_id']}.md"
    assert prompt_path.is_file()
    assert str(prompt_path) == result["prompt_path"]

    store = ProjectStore(project)
    wo = store.get_work_order(result["wo_id"])
    assert wo["kind"] == "bootstrap"
    # Not pending: nothing may try to dispatch it with a launcher that isn't ready.
    assert wo["status"] == "waiting_input"
    assert wo["needs_attention"] == 1
    assert "by hand" in wo["attention_reason"]


def test_a_bootstrap_order_is_never_dispatched_by_accident(started, project,
                                                           fake_claude, catalog_file):
    ops.onboard("proj_a")
    Daemon(load_catalog(catalog_file)).tick()
    assert not [c for c in fake_claude.calls if "--bg" in c["argv"]]


def test_an_unstarted_bootstrap_order_is_not_declared_failed(started, project,
                                                             catalog_file):
    """It is waiting on a person, not on a session — the reconciler's "the worker's
    session never appeared" timeout must not apply to something never spawned."""
    result = ops.onboard("proj_a")
    store = ProjectStore(project)
    store.update_work_order(result["wo_id"], updated_at=time.time() - 3600)

    daemon = Daemon(load_catalog(catalog_file))
    daemon.tick()
    daemon.tick_count = 0
    daemon.tick()

    wo = store.get_work_order(result["wo_id"])
    assert wo["status"] == "waiting_input"
    assert "by hand" in (wo["attention_reason"] or "")


def test_reonboarding_dispatches_once_a_verified_launcher_exists(
        started, project, fake_wrapper, catalog_file):
    fake_wrapper.install(project)
    assert ops.launcher_verify("proj_a", live=True)["ok"] is True

    result = ops.onboard("proj_a", reason="drift")
    assert result["dispatch"] is True and result["amending"] is True

    Daemon(load_catalog(catalog_file)).tick()
    store = ProjectStore(project)
    wo = store.get_work_order(result["wo_id"])
    assert wo["status"] == "running"
    # The session got the bootstrap prompt, not the worker contract.
    spawn = [c for c in fake_wrapper.calls if c["argv"][0] == "run"][-1]
    prompt = spawn["argv"][spawn["argv"].index("--") + 1]
    assert "This is NOT a work order" in prompt


# -- verification and staleness ----------------------------------------------------------


def test_static_verification_is_not_verification(started, project, fake_wrapper):
    fake_wrapper.install(project)
    report = ops.launcher_verify("proj_a", live=False)
    assert report["ok"] is True
    assert onboarding.launcher_state("proj_a").get("verified_at") is None
    assert any("never passed a live verification" in p
               for p in onboarding.launcher_health(started.project("proj_a"))["problems"])

    ops.launcher_verify("proj_a", live=True)
    assert onboarding.launcher_state("proj_a")["verified_at"] > 0
    assert onboarding.launcher_health(started.project("proj_a"))["problems"] == []


def test_verification_stamps_the_source_digests_it_will_later_compare(
        started, project, fake_wrapper):
    path = project / ".jarvis" / "launcher.json"
    contract = dict(fake_wrapper.contract)
    contract["provenance"] = {"sources": [{"path": str(fake_wrapper.bin),
                                           "sha256": "auto"}]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(contract))

    ops.launcher_verify("proj_a", live=False)
    stamped = json.loads(path.read_text())
    assert stamped["provenance"]["sources"][0]["sha256"] not in ("auto", "missing")


def test_a_changed_wrapper_asks_for_a_new_onboarding_session(started, project,
                                                             fake_wrapper):
    fake_wrapper.install(project)
    ops.launcher_verify("proj_a", live=True)
    spec = started.project("proj_a")
    assert onboarding.launcher_health(spec)["problems"] == []

    fake_wrapper.bin.write_text("#!/bin/sh\necho the user rewrote their wrapper\n")
    health = onboarding.launcher_health(spec)
    assert any("has changed" in p for p in health["problems"])
    assert health["drift"] == [str(fake_wrapper.bin)]


def test_a_contract_verified_long_ago_goes_stale(started, project, fake_wrapper):
    fake_wrapper.install(project)
    ops.launcher_verify("proj_a", live=True)
    state = onboarding.launcher_state("proj_a")
    state["verified_at"] = time.time() - (lm.REVERIFY_AFTER_DAYS + 1) * 86400
    onboarding._write_state("proj_a", state)

    problems = onboarding.launcher_health(started.project("proj_a"))["problems"]
    assert any("days ago" in p for p in problems)


def test_the_native_launcher_is_never_nagged_about(started, project):
    health = onboarding.launcher_health(started.project("proj_a"))
    assert health["launcher"] == "native"
    assert health["problems"] == []


def test_launcher_problems_reach_the_pulse_check(started, project, fake_wrapper):
    fake_wrapper.install(project)  # installed but never live-verified
    attention = ops.os_status(started)["attention"]
    launcher_items = [a for a in attention if a["title"] == "launcher contract"]
    assert launcher_items
    assert "jarvis onboard proj_a" in launcher_items[0]["reason"]
