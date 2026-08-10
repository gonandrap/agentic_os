"""Bug reporting: any agent in the fleet can file a Jarvis OS bug from one command.

A report becomes a GitHub issue on the OS repo (rendered from a fixed template that
always carries the running Jarvis version) and a Telegram ping carrying the issue link.
Both halves matter: a report that silently fails to file is worse than no report at all,
so failures raise instead of half-succeeding.
"""

import json as _json
import os as _os

import pytest

from jarvis import bugreport, cli


@pytest.fixture()
def started_project_wo(jarvis_home, fake_claude, catalog_file, project):
    """A started OS that has dispatched one work order, so the spawn call can be
    inspected for how the worker was wired up."""
    from jarvis import ops
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon
    ops.start_os(str(catalog_file), foreground=True)
    ops.create_work_order("proj_a", "add feature X")
    Daemon(load_catalog(catalog_file)).tick()
    return fake_claude, project


@pytest.fixture()
def reporting(jarvis_home, fake_claude, fake_gh, catalog_file):
    """A started OS with a fake `gh`, so reports have somewhere to go.

    `fake_claude` is required even though nothing here spawns a session: starting the
    OS refuses to proceed without a `claude` binary on PATH.
    """
    from jarvis import ops
    ops.start_os(str(catalog_file), foreground=True)
    return fake_gh


# -- the template ---------------------------------------------------------------


def test_body_carries_every_template_section():
    body = bugreport.render_body(
        description="wo send drops the message",
        expected="the worker receives it",
        actual="it stays queued forever",
        version="1.2.3",
        project="proj_a",
        wo_id="wo-1",
    )
    assert "wo send drops the message" in body
    assert "the worker receives it" in body
    assert "it stays queued forever" in body
    assert "1.2.3" in body
    for heading in ("Description", "Expected", "Actual", "Jarvis OS version"):
        assert heading in body, f"template lost its {heading!r} section"


def test_body_records_the_reporter_so_bugs_can_be_traced_back():
    body = bugreport.render_body(
        description="d", expected="e", actual="a", version="1.0.0",
        project="proj_a", wo_id="wo-42",
    )
    assert "proj_a" in body and "wo-42" in body


def test_body_omits_the_reporter_line_when_reported_by_a_human():
    body = bugreport.render_body(
        description="d", expected="e", actual="a", version="1.0.0",
        project="", wo_id="",
    )
    assert "wo-" not in body


def test_optional_steps_appear_only_when_given():
    without = bugreport.render_body(description="d", expected="e", actual="a",
                                    version="1.0.0", project="", wo_id="")
    assert "Steps to reproduce" not in without
    with_steps = bugreport.render_body(description="d", expected="e", actual="a",
                                       version="1.0.0", project="", wo_id="",
                                       steps="1. run jarvis status")
    assert "Steps to reproduce" in with_steps and "jarvis status" in with_steps


# -- which Jarvis is running ----------------------------------------------------
#
# The requirement under every test here: the reported string must never be a number
# that was never released. main's pyproject deliberately lags the shipped version (the
# bump lives only on release branches), so dist metadata alone invents releases in the
# dev checkout — issue #51, where a bug filed from dev recorded 0.1.1 while production
# ran 0.2.1.


@pytest.fixture(autouse=True)
def _uncached_version():
    """`jarvis_version` is cached for the dashboard's sake; tests need it re-resolved."""
    bugreport.jarvis_version.cache_clear()
    yield
    bugreport.jarvis_version.cache_clear()


def _git(cwd, *args):
    import subprocess
    env = {"HOME": str(cwd), "PATH": _os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def checkout(tmp_path, monkeypatch):
    """A throwaway git checkout laid out like this repo, with one release tag on a
    branch that is NOT an ancestor of main — exactly how shipit leaves the real repo."""
    root = tmp_path / "jarvis_os"
    (root / "src" / "jarvis").mkdir(parents=True)
    (root / "src" / "jarvis" / "__init__.py").write_text("")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "first")
    # The release cut: a branch off main carrying the bump commit and the tag, never
    # merged back. main therefore has no reachable jarvis-* tag, ever.
    _git(root, "checkout", "-q", "-b", "release/jarvis-1.2.3")
    (root / "pyproject.toml").write_text('version = "1.2.3"\n')
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "Release jarvis-1.2.3")
    _git(root, "tag", "jarvis-1.2.3")
    _git(root, "checkout", "-q", "main")
    monkeypatch.setattr(bugreport, "_source_dir", lambda: root / "src" / "jarvis")
    return root


def test_production_reports_the_release_tag_it_is_detached_at(checkout):
    """Production is `git checkout -f jarvis-X.Y.Z`, so HEAD *is* the release."""
    _git(checkout, "checkout", "-q", "jarvis-1.2.3")
    assert bugreport.jarvis_version() == "1.2.3"


def test_dev_checkout_reports_a_commit_never_a_version_number(checkout):
    """The issue-#51 regression: on main there is no reachable tag, and dist metadata
    would hand back a pyproject number that never shipped."""
    version = bugreport.jarvis_version()
    head = _git(checkout, "rev-parse", "--short", "HEAD")
    assert version == f"dev-{head}"
    assert "1.2.3" not in version, "a dev build must not claim a released version"


def test_a_failing_git_describe_falls_through_instead_of_propagating(checkout):
    """Guards the reason this code asks `--exact-match` rather than describing HEAD.

    Release tags are descendants of main, so on a dev HEAD `git describe` does not
    return a stale answer — it exits non-zero with "No tags can describe". That fatal
    must become the dev string, never an exception and never an empty version.
    """
    import subprocess
    described = subprocess.run(
        ["git", "-C", str(checkout), "describe", "--tags", "--match", "jarvis-*"],
        capture_output=True, text=True)
    assert described.returncode != 0, "fixture no longer reproduces the real topology"
    assert "No tags can describe" in described.stderr

    version = bugreport.jarvis_version()
    assert version.startswith("dev-")
    assert "fatal" not in version and "1.2.3" not in version


def test_uncommitted_changes_are_flagged_on_a_release_too(checkout):
    """A hand-edited production checkout is not the release it claims to be."""
    _git(checkout, "checkout", "-q", "jarvis-1.2.3")
    (checkout / "pyproject.toml").write_text('version = "9.9.9"\n')
    assert bugreport.jarvis_version() == "1.2.3-dirty"


def test_untracked_scratch_files_do_not_count_as_dirty(checkout):
    """Workers leave scratch files everywhere; only tracked changes alter the code."""
    (checkout / "notes.md").write_text("scratch")
    assert bugreport.jarvis_version().endswith(_git(checkout, "rev-parse", "--short",
                                                    "HEAD"))


def test_a_wheel_install_falls_back_to_dist_metadata(tmp_path, monkeypatch):
    """No checkout, no git answer — and dist metadata is correct there anyway, because
    the wheel was built from the tag whose pyproject carries the bump."""
    from importlib.metadata import version as dist_version
    site = tmp_path / "site-packages" / "jarvis"
    site.mkdir(parents=True)
    monkeypatch.setattr(bugreport, "_source_dir", lambda: site)
    assert bugreport.jarvis_version() == dist_version("jarvis-os")


def test_an_unrelated_enclosing_repo_is_not_mistaken_for_a_jarvis_checkout(
        tmp_path, monkeypatch):
    """A venv under someone else's project puts our package inside THEIR repo. Reporting
    their commit as the Jarvis version would be the same lie in a new costume."""
    from importlib.metadata import version as dist_version
    other = tmp_path / "someones_project"
    site = other / ".venv" / "lib" / "python3.13" / "site-packages" / "jarvis"
    site.mkdir(parents=True)
    _git(other, "init", "-q", "-b", "main")
    (other / "README.md").write_text("not jarvis")
    _git(other, "add", "-A")
    _git(other, "commit", "-qm", "first")
    monkeypatch.setattr(bugreport, "_source_dir", lambda: site)
    assert bugreport.jarvis_version() == dist_version("jarvis-os")


def test_version_never_raises_even_when_git_explodes(monkeypatch):
    """Three surfaces render this string, one of them a web page. It must not throw."""
    def boom(*_a, **_k):
        raise RuntimeError("git went sideways")
    monkeypatch.setattr(bugreport, "_jarvis_checkout", boom)
    assert bugreport.jarvis_version()


# -- filing the issue -----------------------------------------------------------


def test_report_files_a_github_issue_labelled_bug(reporting):
    bugreport.report_bug(title="wo send is lost", description="d",
                         expected="e", actual="a")
    call = reporting.calls[-1]
    assert call["argv"][:3] == ["issue", "create", "--repo"]
    assert call["argv"][3] == bugreport.DEFAULT_BUG_REPO
    assert "--label" in call["argv"]
    assert call["argv"][call["argv"].index("--label") + 1] == "bug"


def test_report_sends_the_body_over_stdin_not_argv(reporting):
    """Bodies carry stack traces and logs; argv has a length limit, stdin does not."""
    bugreport.report_bug(title="t", description="d" * 5000, expected="e", actual="a")
    call = reporting.calls[-1]
    assert "--body-file" in call["argv"]
    assert call["argv"][call["argv"].index("--body-file") + 1] == "-"
    assert "d" * 5000 in call["stdin"]


def test_report_returns_the_issue_url(reporting):
    result = bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    assert result["url"] == reporting.issue_url
    assert result["title"] == "t"


def test_report_targets_an_overridable_repo(reporting, monkeypatch):
    monkeypatch.setenv(bugreport.BUG_REPO_ENV, "someone/elsewhere")
    bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    assert reporting.calls[-1]["argv"][3] == "someone/elsewhere"


# -- notifying the user ---------------------------------------------------------


def test_report_notifies_the_user_with_the_issue_link(reporting):
    from jarvis.central_store import CentralStore
    result = bugreport.report_bug(title="wo send is lost", description="d",
                                  expected="e", actual="a", project="proj_a")
    central = CentralStore()
    try:
        items = central.unacked_inbox()
    finally:
        central.close()
    assert any(result["url"] in (i["body"] or "") for i in items), \
        "the user must get the issue link, not just a 'bug filed' note"
    assert any("wo send is lost" in i["title"] for i in items)


# -- failing loudly -------------------------------------------------------------


def test_a_failed_filing_raises_rather_than_reporting_success(reporting):
    reporting.fail("gh: could not authenticate")
    with pytest.raises(bugreport.BugReportError) as e:
        bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    assert "authenticate" in str(e.value)


def test_a_failed_filing_does_not_notify(reporting):
    """A Telegram ping with no issue behind it trains the user to distrust the pings."""
    from jarvis.central_store import CentralStore
    reporting.fail("boom")
    with pytest.raises(bugreport.BugReportError):
        bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    central = CentralStore()
    try:
        assert central.unacked_inbox() == []
    finally:
        central.close()


def test_a_missing_gh_explains_how_to_fix_it(reporting, monkeypatch):
    monkeypatch.setenv("JARVIS_GH_BIN", "/nonexistent/gh")
    with pytest.raises(bugreport.BugReportError) as e:
        bugreport.report_bug(title="t", description="d", expected="e", actual="a")
    msg = str(e.value)
    assert "gh" in msg and ("install" in msg or "not found" in msg)


# -- the CLI --------------------------------------------------------------------


def test_cli_reports_a_bug_and_prints_the_url(reporting, capsys):
    rc = cli.main(["bug", "report", "wo send is lost",
                   "--description", "messages never arrive",
                   "--expected", "worker receives it",
                   "--actual", "stays queued", "--json"])
    assert rc == 0
    out = _json.loads(capsys.readouterr().out)
    assert out["url"] == reporting.issue_url
    assert out["version"] == bugreport.jarvis_version()


def test_cli_requires_expected_and_actual(reporting):
    """The template is the point — a freeform 'it broke' report is not acceptable."""
    with pytest.raises(SystemExit):
        cli.main(["bug", "report", "t", "--description", "d"])


def test_cli_reports_the_failure_instead_of_exiting_zero(reporting, capsys):
    reporting.fail("gh exploded")
    rc = cli.main(["bug", "report", "t", "--description", "d",
                   "--expected", "e", "--actual", "a"])
    assert rc == 1
    assert "gh exploded" in capsys.readouterr().err


# -- getting the skill in front of every agent ----------------------------------


SKILL = "report-jarvis-bug"


def test_installing_agent_skills_lays_them_out_where_add_dir_finds_them(project):
    """`--add-dir X` loads skills from X/.claude/skills/ (verified against the CLI),
    which is the only injection point that reaches a worker in a fresh worktree."""
    from jarvis.bootstrap import install_agent_assets
    root = install_agent_assets(project)[0]
    assert (root / ".claude" / "skills" / SKILL / "SKILL.md").is_file()


def test_installing_agent_skills_is_idempotent_and_self_healing(project):
    from jarvis.bootstrap import install_agent_assets
    root = install_agent_assets(project)[0]
    skill = root / ".claude" / "skills" / SKILL / "SKILL.md"
    skill.write_text("locally mangled")
    install_agent_assets(project)
    assert skill.read_text() != "locally mangled"


def test_agent_skills_live_under_the_gitignored_state_dir(project):
    """They are generated, not authored — they must never show up in `git status`."""
    from jarvis.bootstrap import install_agent_assets
    root = install_agent_assets(project)[0]
    assert ".jarvis" in root.relative_to(project).parts


def test_the_skill_warns_that_the_tracker_is_public():
    from jarvis.bootstrap import ASSETS
    text = (ASSETS / "skills" / SKILL / "SKILL.md").read_text()
    assert "PUBLIC" in text, "agents must be told the issue tracker is public"
    assert "jarvis bug report" in text


def test_dispatch_points_the_worker_at_the_skill(started_project_wo):
    """A skill the worker cannot load is not wired to anything."""
    fake_claude, project_path = started_project_wo
    spawn = fake_claude.wait_calls(
        lambda c: "-p" in c["argv"] and "--session-id" in c["argv"])[-1]
    argv = spawn["argv"]
    assert "--add-dir" in argv, "workers are never told where the OS skills live"
    added = argv[argv.index("--add-dir") + 1]
    from pathlib import Path
    assert (Path(added) / ".claude" / "skills" / SKILL / "SKILL.md").is_file()


def test_this_repo_ships_the_skill_to_its_own_agents():
    """agentic_os is itself a managed project; Jarvis and workers here need the skill
    too, and for a worktree to see it, it must be git-tracked at the repo root."""
    from pathlib import Path

    from jarvis.bootstrap import ASSETS
    repo = Path(__file__).resolve().parent.parent
    shipped = repo / ".claude" / "skills" / SKILL / "SKILL.md"
    assert shipped.is_file()
    assert shipped.read_text() == (ASSETS / "skills" / SKILL / "SKILL.md").read_text(), \
        "the repo's own copy of the skill has drifted from the asset agents are given"
