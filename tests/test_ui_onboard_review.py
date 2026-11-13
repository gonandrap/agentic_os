"""Review and apply an onboarding draft (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §5)"""

from __future__ import annotations

import html
import json
import os

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import interview, ops, wiring  # noqa: E402
from jarvis.testing import make_git_project  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402
from jarvis.ui.onboard_pages import section_url  # noqa: E402
from jarvis.ui.onboard_review import applied_url  # noqa: E402

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, and `ops.onboard_*` writes refuse one"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture(autouse=True)
def no_inventory(monkeypatch):
    """`wiring.discover` execs `claude`; the view reads one fixed lever instead"""
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file, tmp_path):
    ops.start_os(str(catalog_file), foreground=True)
    interview.new_draft(make_git_project(tmp_path / "work", NAME), NAME)
    return TestClient(create_app(), follow_redirects=False)


def head():
    return next((r for r in ops.config_history(limit=50) if r["head"]), None)


def defaults(client, section, name=NAME):
    r = client.post(f"/onboard/{name}/edit", data={
        "rev": ops.onboard_view(name)["rev"], "section": section, "verb": "defaults"})
    assert r.status_code == 303, r.text


def apply(client, name=NAME, **data):
    return client.post(f"/onboard/{name}/apply",
                       data={"rev": ops.onboard_view(name)["rev"], **data})


def test_review_is_not_shadowed_by_the_section_catch_all(client):
    r = client.get(f"/onboard/{NAME}/review")
    assert r.status_code == 200
    assert "review and apply" in r.text.lower()
    # Control: the catch-all still owns a real section and refuses an unknown one
    assert client.get(f"/onboard/{NAME}/evolution").status_code == 200
    assert client.get(f"/onboard/{NAME}/nosuch").status_code == 404


def test_each_changed_path_links_to_its_own_section(client, project):
    interview.new_draft(project, "proj_a")
    view = ops.onboard_view("proj_a")
    picked: dict[str, dict] = {}
    for s in view["sections"]:
        for k in s["knobs"]:
            if (s["key"] not in picked and len(picked) < 2 and not k["safety"]
                    and k["kind"] in ("asked", "classified")
                    and k["path"].startswith("projects.proj_a.")
                    and isinstance(k["value"], bool)):
                picked[s["key"]] = k
    assert len(picked) == 2
    for key, k in picked.items():
        r = client.post("/onboard/proj_a/edit", data={
            "rev": ops.onboard_view("proj_a")["rev"], "section": key, "verb": "set",
            "path": k["path"], "value": "false" if k["value"] else "true"})
        assert r.status_code == 303, r.text
    page = client.get("/onboard/proj_a/review").text
    assert len(ops.onboard_view("proj_a")["review"]["rows"]) == 2
    for key, k in picked.items():
        href = html.escape(section_url("proj_a", key, k["path"]))
        assert f'<a class="mono" href="{href}">{k["path"]}</a>' in page


def test_apply_refuses_open_must_haves_stale_rev_and_a_missing_reason(client):
    missing = ops.onboard_view(NAME)["progress"]["must_have_missing"]
    for key in missing[:-1]:
        defaults(client, key)
    before = head()
    r = apply(client)  # no reason yet: a given one is saved even when apply refuses
    assert r.status_code == 400 and f"decide first: {missing[-1]}" in html.unescape(r.text)
    assert head() == before

    defaults(client, missing[-1])
    r = client.post(f"/onboard/{NAME}/apply", data={"rev": "0" * 64})
    assert r.status_code == 400 and ops.ONBOARD_STALE in html.unescape(r.text)
    assert head() == before

    r = apply(client)
    with pytest.raises(ops.OpsError) as refusal:
        ops.onboard_apply(ops.onboard_view(NAME)["draft_path"], dry_run=True)
    assert r.status_code == 400 and str(refusal.value) in html.unescape(r.text)
    assert head() == before

    r = apply(client, reason="new project, defaults")
    after = head()
    assert after["id"] != (before or {}).get("id") and after["actor"] == "onboard"
    assert r.status_code == 303 and r.headers["location"] == applied_url(NAME, after["id"])


def ready(client):
    for key in ops.onboard_view(NAME)["progress"]["must_have_missing"]:
        defaults(client, key)


def test_apply_redirects_to_a_durable_page_built_from_the_version(client):
    """POST/redirect/GET: the result survives a reload (wo-6d9fbd23)"""
    assert client.get(f"/onboard/{NAME}/apply").headers["location"] == f"/onboard/{NAME}/review"
    ready(client)
    r = apply(client, reason="a new project")
    assert r.status_code == 303
    vid = head()["id"]
    page = client.get(r.headers["location"])
    assert page.status_code == 200
    text = html.unescape(page.text)
    assert vid in text and "a new project" in text
    assert f"{len(head()['changes'])} changes" in text
    # No daemon runs in the suite: the one working action is to start it
    assert 'data-state="down"' in text and "jarvis start --catalog" in text
    # A reload of the POST's own URL lands on the same page, not the section 404
    again = client.get(f"/onboard/{NAME}/apply")
    assert again.status_code == 303 and again.headers["location"] == applied_url(NAME, vid)
    # Controls: an unknown version and an unknown section are still 404s
    assert client.get(f"/onboard/{NAME}/applied/nosuch").status_code == 404
    assert client.get(f"/onboard/{NAME}/nosuch").status_code == 404
    assert f'/onboard/{NAME}/apply"' in client.get("/onboard").text


@pytest.mark.parametrize("published, managed, state, command", [
    (None, False, "unknown", ""),
    ([NAME], False, "live", f'jarvis restart --reason "onboarded {NAME}"'),
    (["other"], False, "not_loaded", f'jarvis restart --reason "onboarded {NAME}"'),
    (["other"], True, "not_loaded", f'jarvis restart --reason "onboarded {NAME}"'),
])
def test_live_status_reads_what_the_running_daemon_published(
        jarvis_home, monkeypatch, published, managed, state, command):
    from jarvis import service_manager
    from jarvis.central_store import CentralStore
    from jarvis.daemon import DAEMON_PROJECTS_KEY

    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    monkeypatch.setattr(ops, "daemon_running", lambda: 4242)
    central = CentralStore()
    try:
        central.set_state(DAEMON_PROJECTS_KEY, "" if published is None else json.dumps(
            {"projects": published, "managed": managed}))
    finally:
        central.close()
    live = ops.onboard_live(NAME, "C")
    assert (live["state"], live["command"]) == (state, command)


# -- `jarvis restart` and its button (io-74eab254)

def publish(projects, managed):
    from jarvis.central_store import CentralStore
    from jarvis.daemon import DAEMON_PROJECTS_KEY

    central = CentralStore()
    try:
        central.set_state(DAEMON_PROJECTS_KEY, json.dumps(
            {"projects": projects, "managed": managed}))
    finally:
        central.close()


@pytest.fixture()
def systemctl(monkeypatch):
    """The fake systemctl: every argv the systemd runner would have executed"""
    from jarvis import release, service_manager

    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    argvs: list[list[str]] = []
    monkeypatch.setattr(release.SystemdRunner, "_run",
                        lambda self, argv: argvs.append(argv) or "")
    return argvs


def test_the_restart_button_issues_a_detached_restart_under_systemd(
        client, monkeypatch, systemctl):
    ready(client)
    page_url = apply(client, reason="a new project").headers["location"]
    monkeypatch.setattr(ops, "daemon_running", lambda: 4242)
    publish(["other"], managed=True)

    text = html.unescape(client.get(page_url).text)
    assert 'data-state="not_loaded"' in text and "Restart now" in text
    assert "never restarts" not in text

    r = client.post("/os/restart", data={"reason": f"onboarded {NAME}", "next": page_url})
    assert r.status_code == 303 and r.headers["location"] == page_url
    [argv] = systemctl
    assert argv[:3] == ["systemd-run", "--user", "--collect"]
    assert argv[-1] == "sleep 3; systemctl --user restart jarvis.service"


def test_no_restart_button_once_the_project_is_live(client, monkeypatch, systemctl):
    ready(client)
    page_url = apply(client, reason="a new project").headers["location"]
    monkeypatch.setattr(ops, "daemon_running", lambda: 4242)
    publish([NAME], managed=True)
    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:  # the daemon booted after this version: nothing restart-class is pending
        central.set_state("daemon_started_at", str(head()["ts"] + 1))
    finally:
        central.close()
    assert "Restart now" not in client.get(page_url).text


def test_restart_refuses_without_a_reason_or_a_running_os(jarvis_home, systemctl):
    with pytest.raises(ops.OpsError, match="say why"):
        ops.restart_os("  ")
    with pytest.raises(ops.OpsError, match="not running"):
        ops.restart_os("apply settings")
    assert systemctl == []


def test_an_unmanaged_restart_stops_then_starts_from_the_registered_catalog(
        jarvis_home, monkeypatch, systemctl):
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        central.set_state("catalog_path", "/c.json")
    finally:
        central.close()
    publish(["p"], managed=False)
    monkeypatch.setattr(ops, "daemon_running", lambda: 4242)
    calls: list[str] = []
    monkeypatch.setattr(ops, "stop_os", lambda: calls.append("stop") or {"status": "stopped"})
    monkeypatch.setattr(ops, "start_os", lambda c: calls.append(f"start {c}") or {"daemon": {}})
    assert ops.restart_os("apply settings")["status"] == "restarted"
    assert calls == ["stop", "start /c.json"] and systemctl == []


def test_config_shows_restart_now_only_while_a_restart_is_pending(client, monkeypatch):
    monkeypatch.setattr(ops, "restart_pending", lambda: [])
    assert 'id="cfg-restart"' not in client.get("/config").text
    monkeypatch.setattr(ops, "restart_pending", lambda: ["os.max_concurrent_turns"])
    page = client.get("/config").text
    assert 'id="cfg-restart"' in page and 'action="/os/restart"' in page


def test_a_worker_calling_restart_hits_the_service_restart_gate():
    from jarvis import gate_rules

    match = gate_rules.RuleSet.from_seeds().decide(
        'jarvis restart --reason "x"', gate_rules.KIND_NAMES).match
    assert match is not None and match.kind == "service_restart"


def test_no_os_text_claims_start_restarts():
    assert "jarvis start" not in ops.APPLY_NOTES["restart"]
    assert "jarvis start` writes" not in ops.SETTINGS_OVERRIDES_NOTE
