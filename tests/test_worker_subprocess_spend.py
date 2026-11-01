"""What a WORKER spends below itself, and whether the work order can see it (#103)"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

import pytest

from jarvis import agent_usage, claude_cli, cli, dispatch, ops, seats, structured, testing
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon

#: What `testing.FAKE_CLAUDE`'s `emit_headless` reports for every one-shot call
FAKE_CALL = {"input": 5, "cache_write": 200, "cache_read": 800, "output": 60,
             "cost_usd": 0.002}


def calls(home=None, **filters) -> list[dict]:
    central = CentralStore(home / "os.db" if home else None)
    try:
        return central.agent_calls(**filters)
    finally:
        central.close()


# -- the recording seam


def test_a_call_from_inside_a_work_order_is_billed_to_it(jarvis_home, fake_claude,
                                                         monkeypatch):
    """The fix"""
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
    """A human at a terminal, or the daemon before it has a question in hand"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)

    claude_cli.run_headless("summarise this")

    assert calls() == []


def test_the_label_records_what_ran_the_call(jarvis_home, fake_claude, monkeypatch):
    """One `pytest evals/llm` is forty calls"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(claude_cli.sys, "argv", ["/usr/bin/pytest", "evals/llm"])

    claude_cli.run_headless("scenario 1")

    assert calls(wo_id="wo-abc123")[0]["label"] == "pytest"


def test_an_unparseable_reply_is_recorded_as_a_call_that_failed(jarvis_home, monkeypatch):
    """A call the CLI answered with something that carried no envelope was PAID FOR just the same"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **kw: "not json at all")

    claude_cli.run_headless("summarise this")

    (row,) = calls(wo_id="wo-abc123")
    assert not row["ok"] and row["output"] == 0


def _compiled_in(module: str, source: str):
    """A function whose FRAME reports `module`, for driving the caller check"""
    glb: dict = {"__name__": module}
    exec(source, glb)
    return glb["go"]


def test_an_outside_caller_cannot_switch_attribution_off(jarvis_home, fake_claude,
                                                         monkeypatch):
    """The hole #749 was spent through"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")

    with pytest.raises(claude_cli.AttributionRefused, match="outside the jarvis package"):
        claude_cli.run_headless("summarise this", records_itself="neo_answer")

    assert calls(wo_id="wo-abc123") == []       # nothing ran, so there is nothing to bill


def test_an_eval_shaped_caller_naming_a_real_kind_is_still_refused(jarvis_home,
                                                                   fake_claude,
                                                                   monkeypatch):
    """The #749 shape exactly: 713 calls from `evals.llm.test_stakes_classifier_ab`, none recorded"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    go = _compiled_in("evals.llm.test_stakes_classifier_ab",
                      "def go(cli):\n"
                      "    return cli.run_headless('q', records_itself='neo_answer')\n")

    with pytest.raises(claude_cli.AttributionRefused) as e:
        go(claude_cli)

    assert "evals.llm.test_stakes_classifier_ab" in str(e.value)
    assert calls(wo_id="wo-abc123") == []


def test_the_refusal_happens_before_any_subprocess_is_spawned(jarvis_home, monkeypatch):
    """The whole value of checking at the call: a misuse costs zero dollars"""
    monkeypatch.setattr(claude_cli, "_run",
                        lambda *a, **kw: pytest.fail("the subprocess ran"))

    with pytest.raises(claude_cli.AttributionRefused):
        claude_cli.run_headless_result("summarise this", records_itself="neo_answer")


# -- the forwarders, which are not the OS frame that authorises


def test_an_eval_cannot_declare_through_structured_request(jarvis_home, fake_claude,
                                                           monkeypatch):
    """`structured.request` FORWARDS a declaration"""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    go = _compiled_in("evals.llm.test_stakes_classifier_ab",
                      "def go(structured, on_usage):\n"
                      "    return structured.request('q', validate=lambda d: d,\n"
                      "                              records_itself='digest',\n"
                      "                              on_usage=on_usage)\n")

    with pytest.raises(claude_cli.AttributionRefused, match="outside the jarvis package"):
        go(structured, lambda usage: None)

    assert calls(wo_id="wo-abc123") == []


def test_an_eval_cannot_declare_through_run_blind(jarvis_home, monkeypatch, tmp_path):
    """The seats path, same hole: `jarvis.seats` forwarded `kind` into the transport"""
    monkeypatch.setattr(claude_cli, "_run",
                        lambda *a, **kw: pytest.fail("the subprocess ran"))
    go = _compiled_in("evals.llm.test_neo_panel_judgment",
                      "def go(seats, cwd):\n"
                      "    return seats.run_blind({'tester': ('s', 'u')}, models={},\n"
                      "                           timeout=5, cwd=cwd,\n"
                      "                           kind='panel_seat')\n")

    with pytest.raises(claude_cli.AttributionRefused):
        go(seats, tmp_path)


def test_an_eval_cannot_declare_through_prime_cache(jarvis_home, monkeypatch, tmp_path):
    """`prime_cache` never raises on a transport failure, so a refusal it swallowed would be a free pass"""
    monkeypatch.setattr(claude_cli, "_run",
                        lambda *a, **kw: pytest.fail("the subprocess ran"))
    go = _compiled_in("evals.llm.test_validation_judgment",
                      "def go(seats, cwd):\n"
                      "    return seats.prime_cache('s', 'u', 'haiku', timeout=5,\n"
                      "                             cwd=cwd, kind='validation_seat')\n")

    with pytest.raises(claude_cli.AttributionRefused):
        go(seats, tmp_path)


def test_a_declaration_with_no_recorder_records_nothing_so_it_is_refused(monkeypatch):
    """Even from an OS frame"""
    monkeypatch.setattr(claude_cli, "_run",
                        lambda *a, **kw: pytest.fail("the subprocess ran"))
    go = _compiled_in("jarvis.digest",
                      "def go(structured):\n"
                      "    return structured.request('q', validate=lambda d: d,\n"
                      "                              records_itself='digest')\n")

    with pytest.raises(claude_cli.AttributionRefused, match="on_usage"):
        go(structured)


def test_an_outside_caller_cannot_mint_an_authorisation(tmp_path):
    """The token IS the check (§3): minting it runs the frame walk"""
    mint = _compiled_in("evals.llm.test_neo_panel_judgment",
                        "def go(fn, kind):\n    return fn(kind)\n")

    with pytest.raises(claude_cli.AttributionRefused):
        mint(claude_cli.authorise, "panel_seat")
    with pytest.raises(claude_cli.AttributionRefused):
        mint(claude_cli.Authorisation, "panel_seat")


def test_an_os_frame_mints_a_token_the_transport_takes_as_checked():
    """The other half: panel and validation mint on their own thread"""
    mint = _compiled_in("jarvis.panel", "def go(fn, kind):\n    return fn(kind)\n")

    token = mint(claude_cli.authorise, "panel_seat")

    assert token.kind == "panel_seat"


def test_a_jarvis_frame_beyond_the_frame_cap_does_not_authorise():
    """`_CALLER_FRAME_CAP` refuses rather than accepts past the horizon: a stack this deep is not one of..."""
    deep = _compiled_in("evals.llm.deep_chain",
                        "def go(check, kind, depth):\n"
                        "    if depth:\n"
                        "        return go(check, kind, depth - 1)\n"
                        "    return check(kind)\n")
    os_frame = _compiled_in("jarvis.neo",
                            "def go(deep, check, kind):\n"
                            "    return deep(check, kind, 25)\n")

    with pytest.raises(claude_cli.AttributionRefused):
        os_frame(deep, claude_cli._check_records_itself, "neo_answer")


#: Every declaration the OS makes of the `agent_calls.kind` it writes for its own call: the function...
OS_DECLARATIONS = [
    ("neo.answer_question", "jarvis.neo",
     "claude_cli.run_headless_result", "records_itself", "neo_answer"),
    ("panel._round", "jarvis.panel", "seats.run_blind", "kind", "panel_seat"),
    ("panel._run_chair", "jarvis.panel",
     "claude_cli.run_headless_result", "records_itself", "panel_seat"),
    ("validation.decide/prime_cache", "jarvis.validation",
     "seats.prime_cache", "kind", "validation_seat"),
    ("validation.decide/run_blind", "jarvis.validation",
     "seats.run_blind", "kind", "validation_seat"),
    ("validation._run_chair", "jarvis.validation",
     "claude_cli.run_headless_result", "records_itself", "validation_seat"),
    ("digest.summarise", "jarvis.digest",
     "structured.request", "records_itself", "digest"),
    ("supervisor.review", "jarvis.supervisor",
     "structured.request", "records_itself", "supervisor"),
    ("supervisor.review_health", "jarvis.supervisor",
     "structured.request", "records_itself", "health"),
    ("daemon._classify_stakes", "jarvis.daemon",
     "claude_cli.run_headless_result", "records_itself", "stakes_classifier"),
]


def _site_function(site: str):
    """The function a site id names — `decide` for `validation.decide/run_blind`"""
    from jarvis import daemon, digest, neo, panel, supervisor, validation

    module, _, rest = site.partition(".")
    owner = {"neo": neo, "panel": panel, "validation": validation, "digest": digest,
             "supervisor": supervisor, "daemon": daemon}[module]
    name = rest.split("/")[0]
    if not hasattr(owner, name) and module == "daemon":
        owner = daemon.Daemon
    return getattr(owner, name)


def _called_name(node: ast.Call) -> str:
    """`seats.run_blind` for an attribute call, `request` for a bare one"""
    func = node.func
    if isinstance(func, ast.Attribute):
        prefix = func.value.id + "." if isinstance(func.value, ast.Name) else ""
        return prefix + func.attr
    return getattr(func, "id", "")


def _kind_literal(value: ast.expr):
    """The kind a declaration names, through the conditional `digest.summarise` uses"""
    if isinstance(value, ast.IfExp):
        return _kind_literal(value.body)
    return value.value if isinstance(value, ast.Constant) else None


def _call_aliases(tree: ast.AST, call: str) -> set[str]:
    """Local names bound to `call` for injection"""
    return {node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.BoolOp) and isinstance(node.value.op, ast.Or)
            and any(isinstance(v, ast.Attribute) and _called_name(ast.Call(
                func=v, args=[], keywords=[])) == call for v in node.value.values)}


def _declared_kinds(fn, call: str, keyword: str) -> list[str]:
    """Every literal `fn`'s own source passes as `keyword` to `call`, or to a local alias of it"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    names = {call} | _call_aliases(tree, call)
    return [kind
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and _called_name(node) in names
            for kw in node.keywords
            if kw.arg == keyword
            for kind in [_kind_literal(kw.value)] if kind is not None]


@pytest.mark.parametrize("site,module,call,keyword,kind", OS_DECLARATIONS)
def test_every_os_site_declares_the_kind_its_own_row_uses(site, module, call, keyword,
                                                          kind):
    """Read out of each site's REAL source: the literal it passes is the kind its own row is written under"""
    assert _declared_kinds(_site_function(site), call, keyword) == [kind]


@pytest.mark.parametrize("site,module,call,keyword,kind", OS_DECLARATIONS)
def test_the_transport_accepts_every_kind_the_os_declares(site, module, call, keyword,
                                                          kind):
    """The other half: the transport takes each of those kinds from the declaring module's own frame"""
    go = _compiled_in(module, "def go(check, kind):\n    check(kind)\n")

    go(claude_cli._check_records_itself, kind)     # raises AttributionRefused if it drifts


def test_no_source_file_in_the_repo_still_opts_out_of_attribution():
    """Catches a half-done migration: a site left on the deleted boolean would raise a `TypeError` only..."""
    from pathlib import Path

    import jarvis

    repo = Path(jarvis.__file__).parent.parent.parent
    roots = [repo / "src" / "jarvis", repo / "evals", repo / "scripts"]
    # `evals/` and `scripts/` too: the keyword #749 was spent through was typed in an eval
    assert [str(f) for root in roots if root.is_dir()
            for f in root.rglob("*.py") if "attribute=False" in f.read_text()] == []


@pytest.mark.parametrize("kind", ["neo_answers", "  ", "Neo_answer", "anything",
                                  agent_usage.WORKER_SUBPROCESS])
def test_a_declaration_of_an_unknown_or_subprocess_kind_is_refused(kind):
    """`KIND_LABELS` stays open for RECORDING and is closed for DECLARING: a made-up kind is the one place..."""
    go = _compiled_in("jarvis.neo", "def go(check, kind):\n    check(kind)\n")

    with pytest.raises(claude_cli.AttributionRefused):
        go(claude_cli._check_records_itself, kind)


def test_a_test_double_between_the_os_and_the_transport_still_records_itself(
        jarvis_home, fake_claude, monkeypatch):
    """Spec §3 step 3, pinned so a later tightening to the nearest frame cannot happen silently: it would..."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    meter = _compiled_in("evals.llm.test_validation_judgment",
                         "def go(real, **kwargs):\n    return real('p', **kwargs)\n")
    os_path = _compiled_in(
        "jarvis.validation",
        "def go(meter, real):\n"
        "    return meter(real, records_itself='validation_seat')\n")

    assert os_path(meter, claude_cli.run_headless_result).usage    # accepted, and it ran
    assert calls(wo_id="wo-abc123") == []       # the declaring caller writes its own row


def test_recording_a_subprocess_call_never_breaks_the_call(jarvis_home, fake_claude,
                                                           monkeypatch):
    """Accounting observes"""
    def explode(*a, **kw):
        raise RuntimeError("the store is broken")

    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setattr(CentralStore, "add_agent_call", explode)

    assert claude_cli.run_headless("summarise this")   # the answer still comes back
    assert calls(wo_id="wo-abc123") == []              # only the row is lost


# -- the ledger it reaches


def test_accounting_follows_the_spend_home_not_the_jarvis_home(jarvis_home, fake_claude,
                                                               monkeypatch, tmp_path):
    """The half of the fix that makes it reach the case in the issue"""
    real = tmp_path / "real-home"
    monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(real))

    claude_cli.run_headless("scenario 1")

    assert len(calls(home=real, wo_id="wo-abc123")) == 1
    assert calls(home=jarvis_home, wo_id="wo-abc123") == []


def test_the_spend_home_opens_the_usage_row_path_and_nothing_else(monkeypatch, tmp_path):
    """The condition the carve-out was granted under"""
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(tmp_path / "elsewhere"))
    import inspect

    from jarvis import central_store, notify, paths

    for module in (paths, central_store, notify):
        assert agent_usage.SPEND_HOME_ENV not in inspect.getsource(module)


def test_the_isolation_gate_redirects_the_spend_home_by_default(tmp_path, monkeypatch):
    """A suite running against the fake `claude` bills nothing real"""
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
    """Both halves are required"""
    monkeypatch.setenv("JARVIS_EVALS_LLM", llm) if llm else monkeypatch.delenv(
        "JARVIS_EVALS_LLM", raising=False)
    monkeypatch.setenv("JARVIS_WO_ID", wo) if wo else monkeypatch.delenv(
        "JARVIS_WO_ID", raising=False)

    env = testing.gate_environment(tmp_path)

    assert (agent_usage.SPEND_HOME_ENV in env) is redirected


def test_a_dispatched_worker_carries_the_spend_home(jarvis_home, project):
    """Set beside `JARVIS_WO_ID`, and by the same mechanism: a `--settings` env block reaches the CLI's..."""
    from jarvis.catalog import ProjectSpec
    from jarvis.paths import jarvis_home as home_of

    spec = ProjectSpec(name="proj_a", path=project, description="")
    path = dispatch._write_worker_settings(spec, {"id": "wo-abc123"})

    env = json.loads(path.read_text())["env"]
    assert env[agent_usage.SPEND_HOME_ENV] == str(home_of())
    assert env["JARVIS_WO_ID"] == "wo-abc123"
    assert env[agent_usage.LIVE_HOME_ENV] == str(home_of())
    assert env[agent_usage.LIVE_CODE_ENV] == str(agent_usage.CODE_DIR)


# -- code from another checkout never writes the live fleet's books (#754)


@pytest.fixture()
def live(tmp_path, monkeypatch):
    """A dispatched worker tree whose live fleet runs code from ANOTHER checkout"""
    home = tmp_path / "live-home"
    monkeypatch.setenv(agent_usage.LIVE_HOME_ENV, str(home))
    monkeypatch.setenv(agent_usage.LIVE_CODE_ENV, str(tmp_path / "production" / "jarvis"))
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(home))
    monkeypatch.delenv("JARVIS_EVALS_LLM", raising=False)
    monkeypatch.setattr(agent_usage, "_scratch_home", None)
    monkeypatch.setattr(agent_usage, "_warned", set())
    return home


def test_a_script_with_its_own_home_records_there_not_in_the_live_one(live, jarvis_home,
                                                                       capsys):
    """The issue's case: a temp JARVIS_HOME, JARVIS_SPEND_HOME inherited from dispatch"""
    agent_usage.record("neo_answer", wo_id="io-x")
    agent_usage.record("neo_answer", wo_id="io-x")

    assert calls(home=live) == []
    assert len(calls(home=jarvis_home, wo_id="io-x")) == 2
    assert capsys.readouterr().err.count("#754") == 1


def test_a_script_aimed_wholly_at_the_live_home_records_in_a_scratch_home(live, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(live))

    agent_usage.record("neo_answer", wo_id="io-x")

    assert calls(home=live) == []
    assert len(calls(home=agent_usage._scratch_home, wo_id="io-x")) == 1


@pytest.mark.parametrize("why", ["same code", "real tokens"])
def test_the_live_fleets_own_code_and_real_eval_tokens_still_reach_the_live_home(
        live, monkeypatch, why):
    if why == "same code":
        monkeypatch.setenv(agent_usage.LIVE_CODE_ENV, str(agent_usage.CODE_DIR))
    else:
        monkeypatch.setenv("JARVIS_EVALS_LLM", "1")
        monkeypatch.setenv("JARVIS_WO_ID", "wo-abc123")

    agent_usage.record("neo_answer", wo_id="wo-abc123")

    assert len(calls(home=live, wo_id="wo-abc123")) == 1


# -- what the report does with it


@pytest.fixture()
def spent(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    """A dispatched work order that asked Neo once and ran four calls of its own"""
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
    """The ruling this was built to"""
    _, wo = spent

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert unit["os_calls"] == 1                    # Neo, and only Neo
    assert unit["subproc_calls"] == 4               # the worker's own processes
    assert unit["subproc_cost_usd"] > 0
    assert [k["kind"] for k in unit["os_by_kind"]] == ["neo_answer"]


def test_the_total_counts_what_the_worker_spent_below_itself(spent):
    """It IS the work order's cost, so it has to be in the number that answers "what did this cost\""""
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
    """Grouped, not listed: the OS's per-call table is right for five panel seats and wrong for forty eval..."""
    _, wo = spent

    report = ops.cost_report(target=wo["id"], project="proj_a")
    (group,) = report["subproc_detail"]
    assert group["label"] == "pytest" and group["calls"] == 4
    assert group["list_cost_usd"] > 0 and not group["failed"]
    # And it stays OUT of the OS's own per-call table, which is about Jarvis's overhead
    assert [c["kind"] for c in report["os_calls_detail"]] == ["neo_answer"]


def test_a_work_order_whose_whole_bill_is_subprocesses_is_still_measurable(
        jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    """`measurable` gates whether a unit is worth showing at all"""
    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "run the evals")
    daemon.tick()
    monkeypatch.setenv("JARVIS_WO_ID", wo["id"])
    claude_cli.run_headless("scenario 1")

    unit = ops.cost_report(target=wo["id"], project="proj_a")["units"][0]
    assert not unit["found"] and not unit["os_calls"]
    assert unit["measurable"] and unit["total_cost_usd"] == unit["subproc_cost_usd"]


# -- the floor


@pytest.mark.parametrize("scope", ["fleet", "work order"])
def test_the_report_declares_itself_a_floor_unconditionally(spent, scope):
    """UNCONDITIONAL, and that is the design"""
    _, wo = spent

    report = (ops.cost_report() if scope == "fleet"
              else ops.cost_report(target=wo["id"], project="proj_a"))

    assert report["floor"] is True
    assert report["floor_reason"] == ops.COST_FLOOR_NOTE


def test_the_floor_is_declared_even_with_nothing_to_declare_it_about(
        jarvis_home, fake_claude, catalog_file, project):
    """The point of unconditional: a fleet that has recorded no subprocess call yet is not a fleet that..."""
    ops.start_os(str(catalog_file), foreground=True)

    assert ops.cost_report()["floor_reason"] == ops.COST_FLOOR_NOTE


def test_the_cli_shows_the_class_and_says_the_figure_is_a_floor(spent, capsys):
    """Both facts have to survive into the surface a person actually reads"""
    _, wo = spent

    cli.main(["cost", wo["id"]])

    out = capsys.readouterr().out
    assert "claude processes the worker spawned itself" in out
    assert "pytest" in out
    # Its own class on the bill, never folded into Jarvis's overhead (#103)
    assert "what Jarvis spent on this order" in out
    # This work order was dispatched but its turn never settled
    assert "not on this bill" in out
    assert "nothing measurable" in out
    assert "Every figure above is a floor" in out
