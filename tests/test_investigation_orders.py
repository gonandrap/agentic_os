"""Investigation orders: an `inv-` feature-order row plus one read-only `investigator`.

docs/superpowers/specs/2026-09-27-investigation-orders.md. Three properties this file
exists to protect, in the spec's own order of how much they matter:

* **The no-write rule is ENFORCED** (§2.6). Two hooks, reachable through
  `preflight_decision` — a denial placed after the `is_jarvis_command_chain` auto-allow
  passes a direct unit test and does nothing in production.
* **The verdict is settled by `ops`, not by the model's diligence** (§2.5). The duplicate
  check and the filing happen in `ops`, and an ops-decided downgrade is recorded honestly.
* **An investigation never doubles up and never investigates itself** (§2.7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import claude_cli, cli, dispatch, hooks, ops, project_store, verdicts
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore
from jarvis.testing import a_verdict

SUBJECT = "wo-11111111"
WHY = ("wo-11111111 has been `validating` for six hours with no turn in flight and no "
       "open Neo question. Find out what is holding it.")


# -- §2.6: the no-write guarantee ------------------------------------------------------


@pytest.fixture()
def worktree(tmp_path) -> Path:
    wt = tmp_path / "proj" / ".claude" / "worktrees" / "wo-inv001"
    (wt / "src").mkdir(parents=True)
    return wt


def _env(kind: str = "investigator", **over) -> dict:
    return {"JARVIS_WO_ID": "wo-inv001",
            "JARVIS_WO_KIND": kind,
            claude_cli.TURN_TRANSPORT_ENV: claude_cli.TRANSPORT_HEADLESS,
            **over}


def _write(worktree: Path, path: str, tool: str = "Write") -> dict:
    return {"tool_name": tool, "cwd": str(worktree),
            "tool_input": {"file_path": str(worktree / path), "content": "x = 1"}}


def _bash(command: str, worktree: Path | None = None) -> dict:
    return {"tool_name": "Bash", "cwd": str(worktree or "/tmp"),
            "tool_input": {"command": command}}


def _decision(result):
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_investigator_may_not_write_a_source_file(worktree):
    for tool in ("Write", "Edit", "NotebookEdit"):
        result = hooks.investigator_write_decision(
            _write(worktree, "src/app.py", tool=tool), _env())
        assert _decision(result) == "deny", tool
        assert "verdict.json" in _reason(result)
        assert "jarvis investigate verdict" in _reason(result)


def test_the_verdict_is_the_one_file_it_may_write_and_only_whole(worktree):
    assert hooks.investigator_write_decision(
        _write(worktree, "verdict.json"), _env()) is None
    # Edit is refused on verdict.json too: a verdict is written whole, and a hook that
    # must reason about a fragment is `spec_shape_decision`'s argument for Write-only.
    refused = hooks.investigator_write_decision(
        _write(worktree, "verdict.json", tool="Edit"), _env())
    assert _decision(refused) == "deny"
    # …and not a verdict.json somewhere else in the tree.
    nested = hooks.investigator_write_decision(
        _write(worktree, "src/verdict.json"), _env())
    assert _decision(nested) == "deny"


def test_the_write_hook_leaves_every_other_kind_alone(worktree):
    """The negative control: nothing here narrows an ordinary worker or an analyst."""
    for kind in ("worker", "analyst", "planner", "manager"):
        assert hooks.investigator_write_decision(
            _write(worktree, "src/app.py"), _env(kind)) is None, kind


MUTATIONS = (
    "git commit -m 'fix it'",
    "git push origin HEAD",
    "gh pr create --fill",
    "gh pr merge 42 --squash",
    "sed -i 's/a/b/' src/jarvis/ops.py",
    "cat > src/app.py <<'EOF'\nx = 1\nEOF",
    "echo broken > src/app.py",
    # §3.1: both would bypass the duplicate check `ops` exists to run.
    "jarvis bug report 'a stale hold' -d x -e y -a z -p high",
    "jarvis issues start 790",
    "jarvis wo finish wo-inv001 --summary done",
)

READS = (
    "git log -5 --oneline",
    "git show HEAD --stat",
    "gh pr view https://github.com/x/y/pull/42",
    "gh pr diff 42",
    "jarvis wo show wo-11111111",
    "jarvis validation show wo-11111111",
    "jarvis inspect wo-11111111",
    "cd /tmp && jarvis fo list",
    "grep -rn 'panel gave up' src/jarvis/validation.py",
    # The four permitted mutations, and only these four.
    "jarvis wo ask wo-inv001 'is the hold re-derived?'",
    "jarvis wo assume wo-inv001 'the hold is written once'",
    "jarvis learn add 'a stale hold parks an order' --project proj_a",
    "jarvis investigate verdict inv-1234abcd --from-file verdict.json",
)


def test_an_investigator_may_not_mutate_anything():
    for command in MUTATIONS:
        result = hooks.investigator_bash_decision(_bash(command), _env())
        assert _decision(result) == "deny", command
        assert "investigation reads" in _reason(result).lower(), command


def test_an_investigator_may_read_everything_it_needs():
    for command in READS:
        assert _decision(
            hooks.investigator_bash_decision(_bash(command), _env())) == "allow", command


def test_a_permitted_read_is_an_explicit_allow_and_never_a_prompt():
    """`preflight_decision`'s docstring: these auto-approvals exist because a background
    session otherwise stalls on a permission prompt (verified live). An investigator's
    CORE evidence reads must not be the calls that can stall it — `None` means "no
    opinion" and falls through to the normal permission flow."""
    for command in ("git log --oneline", "gh pr view 5"):
        assert _decision(
            hooks.preflight_decision(_bash(command), _env())) == "allow", command
        # The control: an ordinary worker's identical read is untouched by this kind's
        # rule, so nothing here widens or narrows any other kind.
        assert hooks.investigator_bash_decision(
            _bash(command), _env("worker")) is None, command


def test_the_bash_hook_leaves_every_other_kind_alone():
    for command in MUTATIONS:
        assert hooks.investigator_bash_decision(
            _bash(command), _env("worker")) is None, command


def test_the_denial_is_reachable_through_preflight_not_just_directly():
    """§2.6: the ordering is the whole point. `is_jarvis_command_chain` waves every
    `jarvis` verb through, so a denial placed after it passes a unit test and does
    nothing in production."""
    denied = hooks.preflight_decision(
        _bash("jarvis bug report 'x' -d x -e y -a z -p high"), _env())
    assert _decision(denied) == "deny"
    allowed = hooks.preflight_decision(
        _bash("jarvis investigate verdict inv-1234abcd --from-file verdict.json"),
        _env())
    assert _decision(allowed) == "allow"
    # A worker's identical call still gets the contract auto-allow.
    assert _decision(hooks.preflight_decision(
        _bash("jarvis bug report 'x' -d x -e y -a z -p high"),
        _env("worker"))) == "allow"


def test_the_write_denial_is_reachable_through_preflight(worktree):
    denied = hooks.preflight_decision(_write(worktree, "src/app.py"), _env())
    assert _decision(denied) == "deny"
    assert _decision(hooks.preflight_decision(
        _write(worktree, "verdict.json"), _env())) == "allow"


def test_jarvis_verbs_reads_each_segment_and_leaves_the_chain_predicate_alone():
    assert hooks.jarvis_verbs("cd /tmp && jarvis wo show wo-1") == (("wo", "show"),)
    assert hooks.jarvis_verbs("jarvis status") == (("status", ""),)
    assert hooks.jarvis_verbs("git log") == ()
    # Untouched, so no other kind's behaviour changes (§2.6).
    assert hooks.is_jarvis_command_chain("cd /tmp && jarvis bug report x")


#: A read the git/gh pair test clears on its FIRST TWO WORDS while the rest of the command
#: writes. §2.6: the pair exemption is segment-wise, so a later segment cannot ride in.
SMUGGLED = (
    "git log && sed -i 's/a/b/' src/jarvis/ops.py",
    "git status; git commit -am x && git push",
    "gh pr view 1; gh pr merge 1 --admin",
    "gh pr view 1 | sh",
    # No chaining at all: a git argument that writes a file.
    "git diff --output=src/a.py",
)

#: The same write, QUOTED or backslash-escaped. The shell strips both before git sees the
#: argument, so a test on unparsed text fails OPEN. §2.6.
QUOTED_SMUGGLED = (
    'git diff "--output=src/a.py"',
    "git diff '--output=src/a.py'",
    "git log -p \\--output=src/a.py",
    # git's parse-options accepts an unambiguous abbreviation, which writes too.
    "git diff --outp=src/a.py",
)

#: READERS THAT WRITE. Each is in `gate_rules._READERS` or is an interpreter, so the
#: `reads_only` fallback clears it — and each writes a file with no redirection at all.
#: §2.6: the no-write guarantee is this kind's own, so the refusal is here, not there.
WRITING_READERS = (
    "sed -n 'w src/a.py' README.md",
    "sed -n '1e touch x' README.md",
    "sort -o src/a.py README.md",
    "find . -delete",
    "find . -fprint src/a.py",
    'awk \'BEGIN{system("touch x")}\'',
    # BUNDLED SHORT OPTIONS: the writing letter is in a cluster, so a test on the
    # argument's PREFIX misses every one of these.
    "sort -nro src/a.py README.md",
    "sed -ni 'p' README.md",
    "sed -nf script.sed README.md",
    "sed -ne 'w src/a.py' README.md",
)

#: The pipelines an investigator actually needs, which the segment loop must keep.
PIPED_READS = (
    "git log --oneline -5 | head -20",
    "git diff HEAD~1 | grep def",
)

#: Legitimate quoted arguments: parsing must not turn the refusal into a false one.
QUOTED_READS = (
    'git log --grep="fix the bug" -5',
    'git diff "HEAD~1"',
    # Not a prefix of `--output` and writes nothing.
    "git log --output-indicator-new=X -1",
)

#: MCP write tools reach no `Edit`/`Write` branch, so without a branch of their own they
#: are seen by NO investigator hook. §2.6.
MCP_WRITES = (
    "mcp__serena__replace_symbol_body",
    "mcp__serena__create_text_file",
    "mcp__serena__insert_after_symbol",
    "mcp__serena__replace_content",
    "mcp__plugin_serena_serena__execute_shell_command",
)

MCP_READS = (
    "mcp__serena__find_symbol",
    "mcp__plugin_serena_serena__get_symbols_overview",
)


def test_a_read_may_not_smuggle_a_write_into_a_later_segment():
    for command in SMUGGLED:
        assert _decision(hooks.preflight_decision(_bash(command), _env())) == "deny", (
            command)
        # Directly too, so the refusal is this kind's own and not a gate firing first.
        assert _decision(
            hooks.investigator_bash_decision(_bash(command), _env())) == "deny", command


def test_a_quoted_write_argument_is_refused_too():
    for command in QUOTED_SMUGGLED:
        assert _decision(hooks.preflight_decision(_bash(command), _env())) == "deny", (
            command)
        assert _decision(
            hooks.investigator_bash_decision(_bash(command), _env())) == "deny", command


def test_a_quoted_read_still_runs():
    for command in QUOTED_READS:
        assert _decision(
            hooks.preflight_decision(_bash(command), _env())) == "allow", command


def test_a_reader_that_writes_a_file_is_refused():
    """§2.6: `gate_rules.reads_only` clears `sed`, `sort` and `awk` on their NAME, and
    each of them writes a file from its own arguments. The no-write guarantee cannot
    rest on a test of what a program usually does."""
    for command in WRITING_READERS:
        assert _decision(hooks.preflight_decision(_bash(command), _env())) == "deny", (
            command)
        assert _decision(
            hooks.investigator_bash_decision(_bash(command), _env())) == "deny", command
        # The negative control: no other kind's behaviour changes.
        assert hooks.investigator_bash_decision(
            _bash(command), _env("worker")) is None, command


def test_the_ordinary_reads_still_run():
    """The positive control for the rule above: refusing a whole program must not cost
    the investigator the reads it lives in."""
    for command in ("grep -rn foo src", "cat README.md", "sed -n '1,20p' README.md",
                    # The same clusters, one letter different, still reading.
                    "sed -ne '1,20p' README.md", "sed -nE '/err/p' README.md",
                    "sort -u README.md"):
        assert _decision(
            hooks.preflight_decision(_bash(command), _env())) == "allow", command


def test_an_investigator_may_not_use_an_mcp_write_tool():
    for tool in MCP_WRITES:
        payload = {"tool_name": tool, "cwd": "/tmp", "tool_input": {}}
        result = hooks.preflight_decision(payload, _env())
        assert _decision(result) == "deny", tool
        assert hooks.VERDICT_FILE in _reason(result), tool


def test_an_investigator_keeps_the_serena_read_tools():
    for tool in MCP_READS:
        payload = {"tool_name": tool, "cwd": "/tmp", "tool_input": {}}
        assert _decision(hooks.preflight_decision(payload, _env())) in (None, "allow"), (
            tool)


def test_an_ordinary_worker_is_untouched_by_the_mcp_rule():
    for tool in MCP_WRITES + MCP_READS:
        payload = {"tool_name": tool, "cwd": "/tmp", "tool_input": {}}
        assert hooks.preflight_decision(payload, _env(kind="worker")) is None, tool


def test_a_piped_read_still_runs():
    for command in PIPED_READS:
        assert _decision(
            hooks.preflight_decision(_bash(command), _env())) == "allow", command


# -- §2.7: filing one -----------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, fake_gh, catalog_file, project):
    """A started OS whose `proj_a` IS the tracker's project, so a GAP's `--expedite`
    cascade actually reaches a work order — `issues.tracker_project`'s one condition is
    the checkout's `origin` (tests/test_issue_lifecycle.py's `_origin`)."""
    import subprocess

    from jarvis.testing import FIXTURE_BUG_REPO
    subprocess.run(["git", "remote", "add", "origin",
                    f"https://github.com/{FIXTURE_BUG_REPO}.git"],
                   cwd=project, check=True)
    ops.start_os(str(catalog_file), foreground=True)
    return fake_gh


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _subject(store) -> str:
    wo = store.create_work_order("ship the CSV export", description="the ask")
    store.update_work_order(wo["id"], status="validating")
    return wo["id"]


def _investigating(store, inv: dict) -> str:
    """The state the daemon leaves an order in: one investigator, status `planning`.

    Written through the store rather than through `Daemon.plan_features`, which is the
    next pass' seam (§2.7) — this pass proves `ops.submit_verdict`, not the loop.
    """
    child = store.create_work_order(f"investigate {inv['id']}", kind="investigator",
                                    parent_id=inv["id"])
    store.update_feature_order(inv["id"], plan_wo_id=child["id"])
    store.set_feature_status(inv["id"], "planning")
    return child["id"]


def test_an_investigation_is_an_inv_row_carrying_its_subject(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    assert inv["kind"] == "investigation"
    assert inv["id"].startswith("inv-")
    row = store.get_feature_order(inv["id"])
    assert json.loads(row["metadata"])[ops.SUBJECT_KEY] == subject
    assert row["status"] == "pending"
    # §2.8: the daemon is the caller, so an uncapped default would be an uncapped loop.
    assert row["budget_usd"] == pytest.approx(2.00)
    shown = ops.show_investigation_order(inv["id"])
    assert shown["subject"] == subject
    assert shown["status_label"] == "pending"
    assert [r["id"] for r in ops.list_investigation_orders("proj_a")] == [inv["id"]]


def test_an_investigation_needs_a_why(started, store):
    with pytest.raises(ops.OpsError, match="why"):
        ops.create_investigation_order("proj_a", _subject(store), "")


def test_a_second_live_investigation_on_one_subject_is_refused(started, store):
    subject = _subject(store)
    first = ops.create_investigation_order("proj_a", subject, WHY)
    with pytest.raises(ops.OpsError) as e:
        ops.create_investigation_order("proj_a", subject, WHY)
    assert first["id"] in str(e.value)
    # The control: a SETTLED one does not block a new one (§6 — the subject is still
    # stuck? open a new one).
    store.set_feature_status(first["id"], "completed")
    assert ops.create_investigation_order("proj_a", subject, WHY)["id"] != first["id"]


def test_an_investigation_never_investigates_the_diagnostician(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    with pytest.raises(ops.OpsError, match="investigat"):
        ops.create_investigation_order("proj_a", inv["id"], WHY)
    investigator = _investigating(store, inv)
    with pytest.raises(ops.OpsError, match="investigat"):
        ops.create_investigation_order("proj_a", investigator, WHY)


# -- §2.5: the verdict, and who decides the classification ----------------------------


def _submitted(started, store, classification: str, **over):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    investigator = _investigating(store, inv)
    out = ops.submit_verdict(inv["id"],
                             a_verdict(classification, subject=subject, **over))
    return inv["id"], investigator, out


def test_a_gap_is_filed_by_ops_as_an_expedited_bug(started, store):
    inv_id, investigator, out = _submitted(started, store, "GAP")
    assert out["classification"] == "GAP"
    assert out["filed"]["issue_url"] == started.issue_url
    assert out["filed"]["wo_id"]          # --expedite dispatched one (§2.5)
    row = store.get_feature_order(inv_id)
    assert row["status"] == "completed"
    assert not row["needs_attention"], "only WAITING_ON_USER raises attention"
    stored = json.loads(row["plan"])
    assert stored["classified_by"] == "investigator"
    assert stored["filed"]["issue_url"] == started.issue_url
    assert store.get_work_order(investigator)["status"] == "completed"
    assert store.events_of_kind(investigator, "verdict_submitted")


def test_a_gap_whose_duplicate_ops_finds_is_downgraded_honestly(started, store):
    """§2.5: the record must never read as though the investigator classified it that
    way, and never as though it was wrong — finding the duplicate was never its job."""
    subject = _subject(store)
    # The body names THE CAUSE, which is what the tracker half keys on (§2.5) — the
    # subject id in a body says nothing about whether this cause is already filed.
    cause = a_verdict("GAP")["proposed_fix"]["title"]
    started.add_issue("https://github.com/x/y/issues/790",
                      title="stale panel hold", state="CLOSED",
                      body=f"{cause} — reported from a panel hold that never clears")
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    out = ops.submit_verdict(inv["id"], a_verdict("GAP", subject=subject))

    assert out["classification"] == "ALREADY_TRACKED"
    stored = json.loads(store.get_feature_order(inv["id"])["plan"])
    assert stored["classified_by"] == "ops"
    assert stored["submitted_classification"] == "GAP"
    assert stored["duplicate_of"] == "https://github.com/x/y/issues/790"
    assert stored["filed"] is None
    assert not any(c["argv"][:2] == ["issue", "create"] for c in started.calls), \
        "a duplicate must not be filed again — #792 duplicated #790"


def test_an_issue_that_merely_names_the_subject_is_not_the_duplicate(started, store):
    """§2.5: the tracker half keys on the CAUSE. Issues naming an order id are common —
    every validation follow-up the panel filed on it — and are about other causes, so
    keying on the id downgraded a real GAP and filed nothing."""
    subject = _subject(store)
    started.add_issue("https://github.com/x/y/issues/791",
                      title="follow-up: docstring does not name the round number",
                      body=f"raised by the validation panel on {subject}; the round "
                           f"docstring omits the round number")
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    out = ops.submit_verdict(inv["id"], a_verdict("GAP", subject=subject))

    assert out["classification"] == "GAP"
    assert out["filed"]["issue_url"]
    assert out["filed"]["wo_id"]
    assert out["classified_by"] != "ops"
    stored = json.loads(store.get_feature_order(inv["id"])["plan"])
    assert stored["classified_by"] != "ops"
    assert stored["filed"]["issue_url"] == out["filed"]["issue_url"]


def test_a_live_order_already_on_the_subject_counts_as_the_duplicate(started, store):
    """The second half of §2.5's check: the tracker holds nothing, but an order is
    already open about this subject, so nothing new is filed."""
    subject = _subject(store)
    already = store.create_work_order(
        "clear the stale panel hold",
        description=f"the hold on {subject} is never re-derived; re-derive it on the tick")
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    out = ops.submit_verdict(inv["id"], a_verdict("GAP", subject=subject))

    assert out["classification"] == "ALREADY_TRACKED"
    stored = json.loads(store.get_feature_order(inv["id"])["plan"])
    assert stored["duplicate_of"] == already["id"]
    assert stored["classified_by"] == "ops"
    assert not any(c["argv"][:2] == ["issue", "create"] for c in started.calls)


def test_waiting_on_user_is_the_only_classification_that_raises_attention(started,
                                                                         store):
    inv_id, _investigator, out = _submitted(started, store, "WAITING_ON_USER")
    row = store.get_feature_order(inv_id)
    assert row["status"] == "completed"
    assert row["needs_attention"]
    assert "as-4" in row["attention_reason"]
    assert out["filed"] is None


def test_a_transient_settles_silently(started, store):
    inv_id, _investigator, out = _submitted(started, store, "TRANSIENT")
    row = store.get_feature_order(inv_id)
    assert row["status"] == "completed"
    assert not row["needs_attention"]
    assert out["filed"] is None
    assert json.loads(row["plan"])["unsticks"]["when"]


def test_an_already_tracked_verdict_keeps_what_the_investigator_named(started, store):
    inv_id, _investigator, out = _submitted(started, store, "ALREADY_TRACKED")
    row = store.get_feature_order(inv_id)
    assert row["status"] == "completed"
    assert not row["needs_attention"]
    stored = json.loads(row["plan"])
    assert stored["duplicate_of"] == "#790"
    assert stored["classified_by"] == "investigator"
    assert "submitted_classification" not in stored
    assert out["filed"] is None


def test_a_bad_verdict_stores_nothing_and_names_every_problem(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    broken = a_verdict("GAP", subject=subject)
    broken.pop("proposed_fix")
    with pytest.raises(ops.OpsError) as e:
        ops.submit_verdict(inv["id"], broken)
    assert "proposed_fix" in str(e.value)
    row = store.get_feature_order(inv["id"])
    assert row["plan"] is None and row["status"] == "planning"


def test_a_second_verdict_is_refused_by_the_status_check(started, store):
    inv_id, _investigator, _out = _submitted(started, store, "TRANSIENT")
    subject = ops.show_investigation_order(inv_id)["subject"]
    with pytest.raises(ops.OpsError, match="completed"):
        ops.submit_verdict(inv_id, a_verdict("TRANSIENT", subject=subject))


def test_a_verdict_about_another_subject_is_refused(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    with pytest.raises(ops.OpsError, match="wo-99999999"):
        ops.submit_verdict(inv["id"], a_verdict("TRANSIENT", subject="wo-99999999"))


def test_a_filing_failure_is_never_silent(started, store, monkeypatch):
    """§2.5's one attention case that is not a classification: `gh` unreachable must not
    settle the order."""
    from jarvis import bugreport

    def boom(**_kw):
        raise bugreport.BugReportError("gh: could not resolve host github.com")

    monkeypatch.setattr(bugreport, "report_bug", boom)
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    out = ops.submit_verdict(inv["id"], a_verdict("GAP", subject=subject))

    row = store.get_feature_order(inv["id"])
    assert row["status"] == "planning", "an error is not a verdict"
    assert row["needs_attention"]
    assert "could not resolve host" in row["attention_reason"]
    assert "jarvis investigate verdict" in row["attention_reason"]
    assert json.loads(row["plan"])["filing_error"]
    assert out["filing_error"]
    from jarvis.central_store import CentralStore
    central = CentralStore()
    try:
        assert any(inv["id"] in (i["title"] or "") for i in central.unacked_inbox())
    finally:
        central.close()


def test_cancelling_one_settles_it_and_stops_the_investigator(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    investigator = _investigating(store, inv)
    ops.cancel_investigation_order(inv["id"])
    assert store.get_feature_order(inv["id"])["status"] == "cancelled"
    assert store.get_work_order(investigator)["status"] == "cancelled"


def test_the_verbs_refuse_a_row_of_another_kind(started, store, improvement_order):
    with pytest.raises(ops.OpsError, match="improvement order"):
        ops.show_investigation_order(improvement_order["id"])


# -- §2.7: the daemon seam ------------------------------------------------------------


def _investigators(store) -> list[dict]:
    return [w for w in store.list_work_orders(limit=200)
            if w.get("kind") == "investigator"]


def test_the_daemon_opens_the_investigator_with_no_cli_in_the_way(started, store,
                                                                 catalog_file):
    """§2.7: the assertion that the companion fleet-health order HAS a seam — it calls
    `ops.create_investigation_order` from the daemon and never shells out to `jarvis`."""
    from jarvis.daemon import Daemon

    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    daemon = Daemon(load_catalog(catalog_file))
    daemon.plan_features(daemon.catalog.projects[0], store)

    opened = _investigators(store)
    assert len(opened) == 1, opened
    assert opened[0]["parent_id"] == inv["id"]
    assert opened[0]["description"] == WHY   # the `why` verbatim
    row = store.get_feature_order(inv["id"])
    assert row["status"] == "planning"
    assert row["plan_wo_id"] == opened[0]["id"]


def test_a_second_tick_files_no_second_investigator(started, store, catalog_file):
    from jarvis.daemon import Daemon

    ops.create_investigation_order("proj_a", _subject(store), WHY)
    daemon = Daemon(load_catalog(catalog_file))
    daemon.plan_features(daemon.catalog.projects[0], store)
    daemon.plan_features(daemon.catalog.projects[0], store)

    assert len(_investigators(store)) == 1


def test_a_feature_and_an_improvement_order_in_the_same_tick_keep_their_own_children(
        started, store, catalog_file, improvement_order):
    """The sibling loops read one table, so a kind filter dropped on any side is
    invisible from the others (src/jarvis/daemon.py's own reason for three loops)."""
    from jarvis.daemon import Daemon

    ops.create_investigation_order("proj_a", _subject(store), WHY)
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon = Daemon(load_catalog(catalog_file))
    daemon.plan_features(daemon.catalog.projects[0], store)

    kinds = sorted(w["kind"] for w in store.list_work_orders(limit=200)
                   if w["kind"] in ("analyst", "planner", "investigator"))
    assert kinds == ["analyst", "investigator", "planner"], kinds
    assert _investigators(store)[0]["parent_id"].startswith("inv-")


# -- §2.7: the CLI, a thin wrapper over those ops functions ---------------------------


def test_the_cli_drives_the_whole_surface(started, store, tmp_path, capsys):
    subject = _subject(store)
    assert cli.main(["investigate", subject, "--why", WHY, "--json"]) == 0
    inv_id = json.loads(capsys.readouterr().out)["created"]
    assert inv_id.startswith("inv-")

    assert cli.main(["investigate", "list", "--json"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [inv_id]

    assert cli.main(["investigate", "show", inv_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["subject"] == subject

    _investigating(store, {"id": inv_id})
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(a_verdict("TRANSIENT", subject=subject)))
    assert cli.main(["investigate", "verdict", inv_id,
                     "--from-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["classification"] == "TRANSIENT"
    assert store.get_feature_order(inv_id)["status"] == "completed"

    # …and it stays out of the feature-order listing throughout
    assert cli.main(["fo", "list", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_a_sub_verb_is_never_read_as_a_subject(started, store, capsys):
    """`jarvis investigate list` is the listing, not "investigate the subject named
    list" — the sub-verbs are a closed set and anything else is a subject."""
    assert cli.main(["investigate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    assert cli.main(["investigate", "cancel", inv["id"], "--json"]) == 0
    capsys.readouterr()
    assert store.get_feature_order(inv["id"])["status"] == "cancelled"


def test_the_cli_refuses_a_subjectless_create_and_a_whyless_one(started, store, capsys):
    with pytest.raises(SystemExit):
        cli.main(["investigate", _subject(store)])   # --why is required
    assert cli.main(["investigate", "wo-nosuchid", "--why", WHY, "--json"]) != 0


def test_a_bad_verdict_file_is_named_and_not_a_traceback(started, store, tmp_path,
                                                         capsys):
    inv = ops.create_investigation_order("proj_a", _subject(store), WHY)
    _investigating(store, inv)
    path = tmp_path / "verdict.json"
    path.write_text("{not json")
    assert cli.main(["investigate", "verdict", inv["id"], "--from-file", str(path)]) != 0
    assert cli.main(["investigate", "verdict", inv["id"],
                     "--from-file", str(tmp_path / "missing.json")]) != 0


# -- §2.2/§2.3: the investigator is never handed the worker contract -------------------


def _prompt(store, kind: str, project_spec) -> str:
    wo = store.create_work_order(f"diagnose {SUBJECT}", description=WHY, kind=kind)
    feature = None
    if kind in ("investigator", "analyst"):
        inv = store.create_feature_order(
            f"investigate {SUBJECT}", description=WHY,
            kind="investigation" if kind == "investigator" else "improvement",
            metadata={ops.SUBJECT_KEY: SUBJECT})
        store.update_work_order(wo["id"], parent_id=inv["id"])
        wo = store.get_work_order(wo["id"])
        feature = {"fo": inv, "children": []}
    return dispatch.build_worker_prompt(wo, project_spec, feature=feature)


#: Words that turn a line into a prohibition. An assertion that a prompt does not TELL a
#: session to open a pull request has to read past the line telling it not to.
_NEGATIONS = ("not", "never", " no ", "cannot", "refus", "nothing")


def _without_prohibitions(prompt: str) -> str:
    """The prompt with its prohibitions and its GAP-CLASS REGISTRY removed.

    The registry too (A.2): its entries DESCRIBE what a stuck order looks like, so
    `red-main`'s symptom says "a pull request whose checks fail" — vocabulary the
    investigator reads about, never an instruction to it."""
    from jarvis import gaps

    body = prompt.replace(gaps.render_registry(), "")
    return "\n".join(line for line in body.splitlines()
                     if not any(w in line.lower() for w in _NEGATIONS))


@pytest.fixture()
def project_spec(catalog_file):
    return load_catalog(catalog_file).projects[0]


def test_the_investigator_prompt_is_a_fourth_branch_and_not_the_worker_contract(
        store, project_spec):
    """Per kn-31f0f450 the assertion needs the worker and the analyst beside it: an
    assertion about the investigator alone passes just as well when the branch is
    missing and the worker contract leaked in."""
    investigator = _prompt(store, "investigator", project_spec)
    worker = _prompt(store, "worker", project_spec)
    analyst = _prompt(store, "analyst", project_spec)

    assert "INVESTIGATOR" in investigator
    assert "jarvis investigate verdict" in investigator
    assert "verdict.json" in investigator
    # Both strings appear inside PROHIBITIONS, so strip those lines first and then assert
    # the bare instruction is absent (kn-31f0f450).
    body = _without_prohibitions(investigator)
    assert "jarvis wo finish" not in body
    assert "open a PR" not in body and "pull request" not in body.lower()
    # The controls: the worker IS told to open one and to finish, and the analyst is its
    # own branch.
    assert "open a PR" in worker and "jarvis wo finish" in worker
    assert "jarvis io report" in analyst and "jarvis investigate verdict" not in analyst


def test_the_prompt_names_the_four_classifications_and_what_it_cannot_file(
        store, project_spec):
    prompt = _prompt(store, "investigator", project_spec)
    for classification in verdicts.CLASSIFICATIONS:
        assert classification in prompt
    # §3.1: an unrelated Jarvis bug found mid-investigation goes in the verdict.
    assert "jarvis bug report" in prompt
    assert str(verdicts.MAX_VERDICT_CHARS) in prompt
    assert str(verdicts.MIN_QUOTE_CHARS) in prompt


def test_serena_reaches_the_investigator_read_only():
    """§2.6's final paragraph: never grant Serena wholesale — it ships
    `execute_shell_command`, `create_text_file` and `replace_symbol_body`."""
    rules = dispatch.serena_allow_rules()
    assert any(r.endswith("find_symbol") for r in rules)
    for banned in ("execute_shell_command", "create_text_file", "replace_symbol_body"):
        assert not any(banned in r for r in rules), banned


def test_the_burning_turn_judge_knows_what_an_investigator_is():
    from jarvis import supervisor

    said = supervisor._what_it_is({"kind": "investigator", "parent_id": "inv-1234abcd"})
    assert "INVESTIGATOR" in said and "inv-1234abcd" in said
    assert "read" in said.lower()
    assert said != supervisor._what_it_is({"kind": "worker"})


def test_the_investigation_knobs_ship_cheap_and_are_one_edit():
    from jarvis import catalog

    assert catalog.DEFAULT_INVESTIGATION_BUDGET_USD == 2.00
    defaults = catalog.WorkerDefaults()
    assert defaults.investigation_budget_usd == 2.00
    assert defaults.investigation_model and defaults.investigation_effort


def test_the_investigator_is_dispatched_on_the_investigation_model(store, project_spec):
    wo = store.create_work_order("diagnose it", kind="investigator")
    resolved = dispatch.resolved_choices(project_spec, wo)
    assert resolved["model"] == project_spec.worker.investigation_model
    assert resolved["effort"] == project_spec.worker.investigation_effort
    worker = store.create_work_order("ship it")
    assert dispatch.resolved_choices(project_spec, worker)["model"] == \
        project_spec.worker.model


def test_the_investigation_kind_is_not_in_the_feature_listings(store):
    """§2.4 of the improvement-orders spec, one kind further on: every listing filters
    POSITIVELY, so a new kind is excluded for free — pinned rather than reasoned."""
    fo = store.create_feature_order("CSV export", description="the ask")
    inv = store.create_feature_order("investigate wo-1", description=WHY,
                                     kind="investigation")
    assert [r["id"] for r in store.list_feature_orders()] == [fo["id"]]
    assert [r["id"] for r in
            store.list_feature_orders(kind="investigation")] == [inv["id"]]
    assert project_store.feature_status_label("investigation", "planning") == \
        "investigating"


def test_the_investigator_prompt_demands_a_gap_class_and_shows_the_registry(
        store, project_spec):
    """Appendix A.3: one renderer for the prompt and the fix order's brief, for
    `remedies.render_catalogue`'s reason — a model shown a different list from the one the
    code enforces asks for things that are refused."""
    from jarvis import gaps

    prompt = _prompt(store, "investigator", project_spec)
    assert gaps.render_registry() in prompt
    assert '"gap_class"' in prompt
    assert '"detector"' in prompt and '"remedy"' in prompt
    lowered = prompt.lower()
    assert "mechanical" in lowered and "self-heal" in lowered
    # The control: the worker's prompt carries none of it.
    assert "gap_class" not in _prompt(store, "worker", project_spec)
