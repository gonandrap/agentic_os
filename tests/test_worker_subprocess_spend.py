"""What a WORKER spends below itself, and whether the work order can see it.

`tests/test_agent_usage.py` covers the calls JARVIS makes on a work order's behalf — Neo,
the panel's seats, the digest. This covers the opposite direction, and the one issue #103
was filed about: the `claude` processes a worker's own tool call spawns. wo-52a6164d ran
the opt-in LLM eval suite twice while shipping 0.5.4 and reported 3.4M tokens; not one of
those scenario calls was in the figure, and that figure is what prompted someone to ask
why shipping was so expensive.

The dead end is the same one that forced `agent_usage` to exist. A descendant call gets a
session id Jarvis never minted, writes its transcript under whatever cwd it ran in (for
the eval suites, a pytest tmp dir nowhere near the worktree), and names no work order
anywhere. So it is recorded when it returns or it is lost, and what is worth a test is:

* it is RECORDED, keyed on the one thing that reaches every descendant — `JARVIS_WO_ID`
  in the environment — and not recorded when there is no work order to bill;
* the OS's own call sites do NOT double-record through the same seam;
* the accounting reaches the ledger the report reads, even from inside a test process
  whose isolation gate has moved `JARVIS_HOME` — which is exactly where the eval suites
  run, and where the whole fix would otherwise be a no-op;
* the report keeps it as a THIRD CLASS rather than folding it into Jarvis's overhead, and
  says out loud that it is still only a floor.
"""

from __future__ import annotations

import json

import pytest

from jarvis import agent_usage, claude_cli, cli, dispatch, ops, testing
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon

#: What `testing.FAKE_CLAUDE`'s `emit_headless` reports for every one-shot call.
FAKE_CALL = {"input": 5, "cache_write": 200, "cache_read": 800, "output": 60,
             "cost_usd": 0.002}


def calls(home=None, **filters) -> list[dict]:
    central = CentralStore(home / "os.db" if home else None)
    try:
        return central.agent_calls(**filters)
    finally:
        central.close()


# -- the recording seam ---------------------------------------------------------------


def test_a_call_from_inside_a_work_order_is_billed_to_it(jarvis_home, fake_claude,
                                                         monkeypatch):
    """The fix. An eval suite, a script, anything the worker's own tool call runs comes
    through this transport, and `JARVIS_WO_ID` is the only thing about it that says which
    work order is paying — nothing in the call's own session or transcript does."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")

    claude_cli.run_headless("summarise this")

    (row,) = calls(wo_id="wo-abc123")
    assert row["kind"] == agent_usage.WORKER_SUBPROCESS
    assert row["project"] == "proj_a" and row["ok"]
    assert (row["input"], row["output"]) == (FAKE_CALL["input"], FAKE_CALL["output"])
    assert row["cost_usd"] == FAKE_CALL["cost_usd"]


def test_a_call_outside_a_work_order_records_nothing(jarvis_home, fake_claude,
                                                     monkeypatch):
    """A human at a terminal, or the daemon before it has a question in hand. There is no
    work order to bill, and a row filed against `''` would land in the fleet's
    unattributed-overhead line claiming to be OS work it is not."""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)

    claude_cli.run_headless("summarise this")

    assert calls() == []


def test_the_label_records_what_ran_the_call(jarvis_home, fake_claude, monkeypatch):
    """One `pytest evals/llm` is forty calls. Grouping them by the program that ran them
    is what turns forty near-identical rows into the one line a reader wants."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(claude_cli.sys, "argv", ["/usr/bin/pytest", "evals/llm"])

    claude_cli.run_headless("scenario 1")

    assert calls(wo_id="wo-abc123")[0]["label"] == "pytest"


def test_an_unparseable_reply_is_recorded_as_a_call_that_failed(jarvis_home, monkeypatch):
    """A call the CLI answered with something that carried no envelope was PAID FOR just
    the same. A zero-token row saying so is a different fact from no row at all — the same
    reason `add_agent_call` writes one for a call with no usage."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **kw: "not json at all")

    claude_cli.run_headless("summarise this")

    (row,) = calls(wo_id="wo-abc123")
    assert not row["ok"] and row["output"] == 0


def test_attribution_can_be_switched_off(jarvis_home, fake_claude, monkeypatch):
    """The escape hatch the OS's own call sites use. Without it, a Neo answer made from a
    process that happens to carry a work order would be billed twice for one call."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")

    claude_cli.run_headless("summarise this", attribute=False)

    assert calls(wo_id="wo-abc123") == []


@pytest.mark.parametrize("site", ["neo", "panel_seat", "panel_chair", "digest"])
def test_every_os_call_site_opts_out_of_the_transports_attribution(site):
    """Each of these records itself, with the work order AND the question it was made
    for — detail this seam cannot know. Leaving both on would write two rows for one call
    the moment the OS's own process carried a `JARVIS_WO_ID`, which is precisely what
    happens when the LLM evals drive Neo in-process inside a worker.

    Asserted against the source rather than by driving each path, because what must hold
    is that NO site forgets — a behavioural test per site proves only the sites someone
    remembered to write one for.
    """
    import inspect

    from jarvis import digest, neo, panel

    if site == "digest":
        assert digest.CALL.keywords.get("attribute") is False
        return
    source = {"neo": inspect.getsource(neo.answer_question),
              "panel_seat": inspect.getsource(panel._run_seat),
              "panel_chair": inspect.getsource(panel._run_chair)}[site]
    assert "attribute=False" in source


def test_recording_a_subprocess_call_never_breaks_the_call(jarvis_home, fake_claude,
                                                           monkeypatch):
    """Accounting observes. An eval suite must not fail because a row could not be
    written — the cost of a broken store is a missing row, and every total is a floor
    anyway."""
    def explode(*a, **kw):
        raise RuntimeError("the store is broken")

    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(CentralStore, "add_agent_call", explode)

    assert claude_cli.run_headless("summarise this")   # the answer still comes back
    assert calls(wo_id="wo-abc123") == []              # only the row is lost


# -- the ledger it reaches ------------------------------------------------------------


def test_accounting_follows_the_spend_home_not_the_jarvis_home(jarvis_home, fake_claude,
                                                               monkeypatch, tmp_path):
    """The half of the fix that makes it reach the case in the issue. The repo-root
    isolation gate redirects `JARVIS_HOME` for `evals/` too, so without a separately
    pinned sink an opt-in LLM eval spends real money into a tmp directory that is deleted
    at teardown."""
    real = tmp_path / "real-home"
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(real))

    claude_cli.run_headless("scenario 1")

    assert len(calls(home=real, wo_id="wo-abc123")) == 1
    assert calls(home=jarvis_home, wo_id="wo-abc123") == []


def test_the_spend_home_opens_the_usage_row_path_and_nothing_else(monkeypatch, tmp_path):
    """The condition the carve-out was granted under. `JARVIS_SPEND_HOME` must not become
    a second route by which a sandboxed process reaches live state: no other central-store
    write follows it, and no notification path does."""
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(tmp_path / "elsewhere"))
    import inspect

    from jarvis import central_store, notify, paths

    for module in (paths, central_store, notify):
        assert agent_usage.SPEND_HOME_ENV not in inspect.getsource(module)


def test_the_isolation_gate_redirects_the_spend_home_by_default(tmp_path, monkeypatch):
    """A suite running against the fake `claude` bills nothing real, so a row it wrote
    into live state would be an invented number in someone's cost report."""
    monkeypatch.delenv("JARVIS_EVALS_LLM", raising=False)
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)

    env = testing.gate_environment(tmp_path)

    assert env[agent_usage.SPEND_HOME_ENV] == env["JARVIS_HOME"]


@pytest.mark.parametrize("llm,wo,redirected", [
    ("", "", True),            # a plain test run: nothing real is being spent
    ("1", "", True),           # a human running the evals by hand: no work order pays
    ("", "wo-1", True),        # a worker's ordinary test run: the calls are all faked
    ("1", "wo-1", False),      # a worker running the LLM evals: real tokens, real payer
])
def test_the_gate_lifts_the_redirect_only_for_real_tokens_a_work_order_pays_for(
        tmp_path, monkeypatch, llm, wo, redirected):
    """Both halves are required. `JARVIS_EVALS_LLM` says the run reaches the real model —
    the same signal that already stops the gate replacing the `claude` binary. `JARVIS_WO_ID`
    says who is being charged. Either alone is a run whose spend belongs in the sandbox."""
    monkeypatch.setenv("JARVIS_EVALS_LLM", llm) if llm else monkeypatch.delenv(
        "JARVIS_EVALS_LLM", raising=False)
    monkeypatch.setenv("JARVIS_WO_ID", wo) if wo else monkeypatch.delenv(
        "JARVIS_WO_ID", raising=False)

    env = testing.gate_environment(tmp_path)

    assert (agent_usage.SPEND_HOME_ENV in env) is redirected


def test_a_dispatched_worker_carries_the_spend_home(jarvis_home, project):
    """Set beside `JARVIS_WO_ID`, and by the same mechanism: a `--settings` env block
    reaches the CLI's own process env, so every subprocess the worker spawns inherits
    both — however deep in the tree it is."""
    from jarvis.catalog import ProjectSpec
    from jarvis.paths import jarvis_home as home_of

    spec = ProjectSpec(name="proj_a", path=project, description="")
    path = dispatch._write_worker_settings(spec, {"id": "wo-abc123"})

    env = json.loads(path.read_text())["env"]
    assert env[agent_usage.SPEND_HOME_ENV] == str(home_of())
    assert env["JARVIS_WO_ID"] == "wo-abc123"


# -- what the report does with it ------------------------------------------------------


@pytest.fixture()
def spent(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    """A dispatched work order that asked Neo once and ran four calls of its own."""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "ship the release")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    daemon._neo_drain()

    monkeypatch.setenv("JARVIS_WO_ID", wo["id"])
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")
    monkeypatch.setattr(claude_cli.sys, "argv", ["/usr/bin/pytest", "evals/llm"])
    for i in range(4):
        claude_cli.run_headless(f"scenario {i}")
    return daemon, wo


def test_subprocess_spend_is_its_own_class_not_jarvis_overhead(spent):
    """The ruling this was built to. An eval suite and a Neo question are different shapes
    of spending, and a single column would say they were the same — which is exactly the
    distinction someone reading an expensive work order is looking for."""
    _, wo = spent

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert unit["os_calls"] == 1                    # Neo, and only Neo
    assert unit["subproc_calls"] == 4               # the worker's own processes
    assert unit["subproc_cost_usd"] > 0
    assert [k["kind"] for k in unit["os_by_kind"]] == ["neo_answer"]


def test_the_total_counts_what_the_worker_spent_below_itself(spent):
    """It IS the work order's cost, so it has to be in the number that answers "what did
    this cost" — reported apart, added in."""
    _, wo = spent

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert unit["total_cost_usd"] == pytest.approx(round(
        unit["list_cost_usd"] + unit["os_cost_usd"] + unit["subproc_cost_usd"], 4))
    assert unit["subproc_recorded_cost_usd"] == pytest.approx(4 * FAKE_CALL["cost_usd"])


def test_the_fleet_rollup_carries_it_too(spent):
    totals = ops.cost_report()["totals"]

    assert totals["subproc_calls"] == 4
    assert totals["total_cost_usd"] == pytest.approx(round(
        totals["list_cost_usd"] + totals["os_cost_usd"] + totals["subproc_cost_usd"], 2))


def test_the_detail_groups_by_what_ran_the_calls(spent):
    """Grouped, not listed: the OS's per-call table is right for five panel seats and
    wrong for forty eval scenarios, which would bury the seats under `pytest` rows."""
    _, wo = spent

    report = ops.cost_report(target=wo["id"], project="proj_a")
    (group,) = report["subproc_detail"]
    assert group["label"] == "pytest" and group["calls"] == 4
    assert group["list_cost_usd"] > 0 and not group["failed"]
    # And it stays OUT of the OS's own per-call table, which is about Jarvis's overhead.
    assert [c["kind"] for c in report["os_calls_detail"]] == ["neo_answer"]


def test_a_work_order_whose_whole_bill_is_subprocesses_is_still_measurable(
        jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    """`measurable` gates whether a unit is worth showing at all. A work order with a
    pruned transcript that never asked Neo but spent forty dollars on an eval suite must
    not read as nothing to see."""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "run the evals")
    daemon.tick()
    monkeypatch.setenv("JARVIS_WO_ID", wo["id"])
    claude_cli.run_headless("scenario 1")

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert not unit["found"] and not unit["os_calls"]
    assert unit["measurable"] and unit["total_cost_usd"] == unit["subproc_cost_usd"]


# -- the floor -------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["fleet", "work order"])
def test_the_report_declares_itself_a_floor_unconditionally(spent, scope):
    """UNCONDITIONAL, and that is the design. Some descendants cannot be caught at all — a
    bare `claude -p` from a shell comes through no seam Jarvis owns — and a heuristic that
    guessed whether any had escaped would be blind in exactly the cases it was meant to
    catch. A flat statement is always true and costs one line."""
    _, wo = spent

    report = (ops.cost_report() if scope == "fleet"
              else ops.cost_report(target=wo["id"], project="proj_a"))

    assert report["floor"] is True
    assert report["floor_reason"] == ops.COST_FLOOR_NOTE


def test_the_floor_is_declared_even_with_nothing_to_declare_it_about(
        jarvis_home, fake_claude, catalog_file, project):
    """The point of unconditional: a fleet that has recorded no subprocess call yet is not
    a fleet that proved there were none."""
    ops.start_os(str(catalog_file), foreground=True)

    assert ops.cost_report()["floor_reason"] == ops.COST_FLOOR_NOTE


def test_the_cli_shows_the_class_and_says_the_figure_is_a_floor(spent, capsys):
    """Both facts have to survive into the surface a person actually reads."""
    _, wo = spent

    cli.main(["cost", wo["id"]])

    out = capsys.readouterr().out
    assert "claude processes this worker spawned" in out
    assert "pytest" in out
    assert "subprocesses  ~$" in out
    assert "Every figure above is a floor" in out
