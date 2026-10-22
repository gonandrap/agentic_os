"""A malformed model id is refused at creation, and a worker claude rejected says so"""

from __future__ import annotations

import pytest

from jarvis import claude_cli, invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

#: Verbatim from the claude CLI 2.1.308 bundle (`error:"model_not_found"`)
REJECTED = ("There's an issue with the selected model (claude-opus-5.5). It may not exist "
            "or you may not have access to it. Run --model to pick a different model.")


@pytest.fixture()
def daemon(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


# -- the shape check


@pytest.mark.parametrize("good", [
    "opus", "sonnet", "haiku", "fable", "opus[1m]", "claude-opus-5-5",
    "claude-opus-4-8", "claude-sonnet-4-6[1m]", "claude-haiku-4-5-20251001",
    "claude-mythos-5", None, "",
])
def test_good_ids_pass(good):
    ops.check_model_id(good)


def test_a_dotted_id_is_refused_with_the_dashed_spelling():
    with pytest.raises(ops.OpsError) as e:
        ops.check_model_id("claude-opus-5.5")
    assert str(e.value) == ("unknown model id 'claude-opus-5.5' — did you mean "
                            "'claude-opus-5-5'?")


def test_a_typo_is_refused_with_the_closest_known_id():
    with pytest.raises(ops.OpsError) as e:
        ops.check_model_id("claude-opsu-5")
    assert "'claude-opsu-5'" in str(e.value)
    assert "did you mean 'claude-opus-5'?" in str(e.value)


def test_suggestions_cover_aliases_and_known_ids():
    ids = ops.suggested_model_ids()
    assert {"opus", "sonnet", "haiku", "fable", "claude-opus-4-8"} <= set(ids)


# -- the chokepoint


def test_create_refuses_a_bad_model_and_persists_nothing(daemon, store):
    with pytest.raises(ops.OpsError, match="did you mean 'claude-opus-5-5'"):
        ops.create_work_order("proj_a", "build it", model="claude-opus-5.5")
    assert store.list_work_orders() == []


def test_the_browser_form_bounces_a_bad_model(daemon, store, live_daemon):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    client = TestClient(create_app(), follow_redirects=False)
    r = client.post("/wo/create", data={"project": "proj_a", "title": "t",
                                        "model": "claude-opus-5.5"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/?error=")
    assert store.list_work_orders() == []
    page = client.get("/").text
    assert 'list="model-ids"' in page and '<option value="claude-opus-4-8">' in page


# -- the failure says which


def test_claude_rejecting_the_model_is_read_off_its_text():
    assert claude_cli.model_rejected(REJECTED) == "claude-opus-5.5"
    assert claude_cli.model_rejected("Claude AI usage limit reached|1") is None
    assert claude_cli.model_rejected(None) is None


def test_a_rejected_model_is_flagged_as_such(daemon, store):
    wo = ops.create_work_order("proj_a", "build it", model="sonnet")
    store.set_status(wo["id"], "running")
    turn = store.create_turn(wo["id"], "dispatch", "do the work")
    store.finish_turn(turn["id"], "failed", error=REJECTED)

    daemon.settle_work_order(daemon.catalog.project("proj_a"), store,
                             store.get_work_order(wo["id"]))

    row = store.get_work_order(wo["id"])
    want = invariants.model_rejected_blocker(wo["id"], "claude-opus-5.5")
    assert row["status"] == "failed"
    assert row["attention_reason"] == want
    assert invariants.true_blockers(store, row)[0] == want
    assert want == (f"worker failed — model 'claude-opus-5.5' rejected by claude; "
                    f"`jarvis wo retry {wo['id']} --model <valid>`")


# -- the retry


def a_failed_order(daemon, store) -> dict:
    wo = ops.create_work_order("proj_a", "build it", model="sonnet")
    daemon.tick()
    store.set_status(wo["id"], "failed")
    return wo


def test_retry_with_a_model_switches_the_order(daemon, store):
    wo = a_failed_order(daemon, store)
    ops.retry(wo["id"], project_name="proj_a", model="claude-opus-5-5")
    assert store.get_work_order(wo["id"])["model"] == "claude-opus-5-5"


def test_the_cli_takes_a_retry_model():
    from jarvis import cli

    args = cli.build_parser().parse_args(["wo", "retry", "wo-1", "--model", "opus"])
    assert args.model == "opus"


def test_retry_refuses_a_bad_model_and_changes_nothing(daemon, store):
    wo = a_failed_order(daemon, store)
    with pytest.raises(ops.OpsError, match="did you mean"):
        ops.retry(wo["id"], project_name="proj_a", model="claude-opus-5.5")
    assert store.get_work_order(wo["id"])["model"] == "sonnet"
    assert store.list_messages(wo["id"]) == []
