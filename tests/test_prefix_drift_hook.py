"""The prompt prefix must not move between turns of one conversation, and when it does,
the record must name what moved and refuse to call itself the measurement.

Finding 4 of docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md asked for
this and stated its own two objections. Both are tested here rather than argued with:

* IT IS A PROXY. A cache boundary is a fact the response reports and no hook sits in that
  path, so `invariants.check_prefix_stable` is the measurement and this is the early
  warning. The tests below pin that the relationship is written down where the number is
  computed — a precedence rule that lives only in a work order description ceases to
  exist when that order settles (kn-fafe92b7).
* IT RUNS EVERY SESSION FOR A RARE EVENT. So the cost is bounded by construction (no new
  process, no catalog import, a capped memory walk) and the bounds are asserted, not
  described. `scripts/bench_hook_cost.py` is the wall-clock half.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from jarvis import hooks, invariants, ops, timeline
from jarvis.hooks import (PREFIX_INGREDIENTS, PREFIX_UNKNOWN, handle_hook, note_prefix,
                          prefix_baseline, prefix_drift, prefix_fingerprint)
from jarvis.project_store import ProjectStore


@pytest.fixture()
def wo(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return ops.create_work_order("proj_a", "make the thing work", origin="jarvis",
                                 description="The user's original ask, verbatim.")


def env(project, wo_id):
    return {
        "JARVIS_WO_ID": wo_id,
        "JARVIS_PROJECT": "proj_a",
        "JARVIS_PROJECT_PATH": str(project),
    }


def session_start(source="resume", cwd=None):
    payload = {"hook_event_name": "SessionStart", "session_id": "sess-1", "source": source}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    return payload


def events(project, wo_id, kind="prefix_drift"):
    store = ProjectStore(project)
    try:
        return [e for e in store.list_events(wo_id) if e["kind"] == kind]
    finally:
        store.close()


def settings_file(project, wo_id):
    path = project / ".jarvis" / "worker-settings" / f"{wo_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


# -- 1. the comparison is within one conversation ----------------------------------------


def test_the_first_turn_has_nothing_to_compare_and_reports_nothing(wo, project):
    """A cold first turn writes its own cache entry whatever the prefix is. Reporting
    drift here would put a line on every work order ever dispatched."""
    settings_file(project, wo["id"]).write_text('{"includeGitInstructions": false}')

    handle_hook(session_start(source="startup", cwd=project), env(project, wo["id"]))

    assert events(project, wo["id"]) == []
    assert prefix_baseline(project, wo["id"]).exists(), "but the baseline is now recorded"


def test_a_prefix_that_holds_across_turns_says_nothing(wo, project):
    settings_file(project, wo["id"]).write_text('{"includeGitInstructions": false}')
    e = env(project, wo["id"])

    for _ in range(3):
        handle_hook(session_start(cwd=project), e)

    assert events(project, wo["id"]) == [], (
        "the quiet case is the common one — a hook that reported on it would be noise "
        "on every turn of every work order")


def test_the_settings_file_moving_mid_conversation_is_reported(wo, project):
    """The `includeGitInstructions` lever itself. Flipping it back on is the exact
    regression finding 4 says nothing today would notice."""
    settings = settings_file(project, wo["id"])
    settings.write_text('{"includeGitInstructions": false}')
    e = env(project, wo["id"])
    handle_hook(session_start(cwd=project), e)

    settings.write_text('{"includeGitInstructions": true}')
    handle_hook(session_start(cwd=project), e)

    [drift] = events(project, wo["id"])
    assert json.loads(drift["payload"])["changed"] == ["worker_settings"]


def test_a_projects_claude_md_moving_mid_conversation_is_reported(wo, project):
    settings_file(project, wo["id"]).write_text("{}")
    e = env(project, wo["id"])
    (project / "CLAUDE.md").write_text("# rules\nbe brief\n")
    handle_hook(session_start(cwd=project), e)

    (project / "CLAUDE.md").write_text("# rules\nbe brief\nand be kind\n")
    handle_hook(session_start(cwd=project), e)

    [drift] = events(project, wo["id"])
    assert json.loads(drift["payload"])["changed"] == ["memory"]


def test_two_work_orders_are_never_compared_with_each_other(jarvis_home, fake_claude,
                                                            catalog_file, project):
    """The cache's own scope, and the reason the baseline is per work order. Each
    conversation writes its own entry, so a prefix that differs BETWEEN two of them costs
    nothing — while the settings file differs between any two by construction, since it
    carries their ids and worktree paths.
    """
    ops.start_os(str(catalog_file), foreground=True)
    first = ops.create_work_order("proj_a", "one", origin="jarvis")
    second = ops.create_work_order("proj_a", "two", origin="jarvis")
    settings_file(project, first["id"]).write_text('{"JARVIS_WO_ID": "%s"}' % first["id"])
    settings_file(project, second["id"]).write_text('{"JARVIS_WO_ID": "%s"}' % second["id"])

    handle_hook(session_start(cwd=project), env(project, first["id"]))
    handle_hook(session_start(cwd=project), env(project, second["id"]))

    assert events(project, first["id"]) == []
    assert events(project, second["id"]) == []


# -- 1b. the one ingredient parsed out of the environment --------------------------------
#
# The other three are hashed from bytes and fail loudly. This one is READ from the install
# layout, so it can be wrong in a way that looks like working: a silent `?` is skipped on
# both sides of every comparison by design, and a CLI upgrade — the FIRST suspect in
# INV-PREFIX-DRIFT's own text — would then never be reported on that machine. Nothing in
# the ingredient tests above would notice, because they compare fingerprints to each other.


@pytest.fixture()
def install(tmp_path, monkeypatch):
    """A fake Claude install and a fake home, so these never read the test machine's own.

    Returns a helper that lays out one of the two shapes on demand. The native installer
    makes `claude` a symlink to a file whose NAME is the version — not a file inside a
    directory named for it — which is the detail the parse turns on.
    """
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # A user-level memory file, so `memory` is READABLE and the version is the only
    # ingredient these vary. Without it the fake home has nothing to hash, `memory` is
    # `?` too, and a test about one dead ingredient quietly grades two.
    (home / ".claude" / "CLAUDE.md").write_text("# standing instructions\nbe brief\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    bindir = tmp_path / "bin"
    bindir.mkdir()

    def lay_out(resolves_to: str | None = None, receipt: object = None):
        if resolves_to is not None:
            target = tmp_path / "versions" / resolves_to
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("#!/bin/sh\n")
            target.chmod(0o755)
            (bindir / "claude").symlink_to(target)
        if receipt is not None:
            (home / ".claude" / ".last-update-result.json").write_text(receipt)
        return {"PATH": str(bindir)}

    return lay_out


def test_the_version_comes_off_the_resolved_binary(install):
    """The native installer's layout, and the cheap path: one readlink, no subprocess."""
    env = install(resolves_to="9.9.72")

    assert hooks.claude_cli_version(env) == "9.9.72"


def test_a_non_version_resolution_falls_through_to_the_updater_receipt(install):
    """npm, a distro package, a wrapper script — anything whose `claude` does not resolve
    to a version-named file. The updater writes down what it installed, so there is a
    second source that does not depend on the layout."""
    env = install(resolves_to="claude-wrapper",
                  receipt='{"version_from": "9.9.71", "version_to": "9.9.72"}')

    assert hooks.claude_cli_version(env) == "9.9.72"


def test_no_claude_on_the_path_still_reads_the_receipt(install):
    """The hook runs with the worker's PATH, not a login shell's."""
    env = install(receipt='{"version_to": "9.9.72"}')

    assert hooks.claude_cli_version(env) == "9.9.72"


@pytest.mark.parametrize("receipt", [
    None,                              # no receipt at all
    "not json at all",                 # malformed
    '{"version_from": "9.9.71"}',     # right file, missing key
    '{"version_to": ""}',              # present and empty
    '{"version_to": 2.1}',             # present and not a string
])
def test_an_unreadable_version_is_unknown_and_never_a_guess(install, receipt):
    env = install(resolves_to="claude-wrapper", receipt=receipt)

    assert hooks.claude_cli_version(env) == PREFIX_UNKNOWN


def test_the_fingerprint_carries_the_real_version_not_the_unknown(install, wo, project):
    """The branch tests above prove the parse; this proves the fingerprint actually USES
    it. A version silently stuck at `?` is skipped on both sides of every comparison, so
    the ingredient would be dead while every other test still passed."""
    env = install(resolves_to="9.9.72")

    assert prefix_fingerprint(project, project, wo, env)["cli_version"] == "9.9.72"


def test_a_cli_upgrade_mid_conversation_is_reported_end_to_end(install, wo, project):
    """The whole point of the ingredient, through `handle_hook` rather than through the
    fingerprint: this is the case finding 4 names first and the one INV-PREFIX-DRIFT sends
    the reader to check first."""
    settings_file(project, wo["id"]).write_text("{}")
    e = {**env(project, wo["id"]), **install(resolves_to="9.9.71")}
    handle_hook(session_start(cwd=project), e)

    (Path(e["PATH"]) / "claude").unlink()
    upgraded = install(resolves_to="9.9.72")
    handle_hook(session_start(cwd=project), {**e, **upgraded})

    [drift] = events(project, wo["id"])
    payload = json.loads(drift["payload"])
    assert payload["changed"] == ["cli_version"]
    assert payload["before"]["cli_version"] == "9.9.71"
    assert payload["after"]["cli_version"] == "9.9.72"


# -- 2. an unreadable ingredient is not a changed one ------------------------------------


def test_an_unknown_ingredient_never_counts_as_drift():
    """"I could not tell" and "it changed" are different claims. A hook that conflates
    them reports drift every time a file is briefly unreadable — and then again when it
    comes back."""
    holds = {name: "abc" for name in PREFIX_INGREDIENTS}
    went_dark = {**holds, "worker_settings": PREFIX_UNKNOWN}

    assert prefix_drift(holds, went_dark) == ()
    assert prefix_drift(went_dark, holds) == ()
    assert prefix_drift(holds, {**holds, "worker_settings": "def"}) == ("worker_settings",)


def test_a_missing_settings_file_is_unknown_rather_than_empty(wo, project):
    """Not yet written and written-as-empty must not share a token, or a work order whose
    settings arrive late reports drift on its second turn for no reason."""
    fingerprint = prefix_fingerprint(project, project, wo, {})

    assert fingerprint["worker_settings"] == PREFIX_UNKNOWN


def test_a_broken_read_costs_a_data_point_and_not_the_session(wo, project, monkeypatch):
    """The hook's whole contract with the session: the measurement is elsewhere, so
    nothing here is worth failing a turn over."""
    monkeypatch.setattr(hooks, "prefix_fingerprint",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    result = handle_hook(session_start(cwd=project), env(project, wo["id"]))

    assert result == {"wo_id": wo["id"], "event": "SessionStart"}


# -- 3. the cost, since it is paid on every session --------------------------------------


def test_the_hook_adds_no_new_process_to_a_session():
    """Finding 4's second con, answered by arithmetic rather than by a promise: the
    marginal cost of the fingerprint is what it does INSIDE a hook invocation that was
    already happening on every SessionStart."""
    from jarvis.bootstrap import build_settings

    starts = build_settings({})["hooks"]["SessionStart"]

    assert len(starts) == 1 and len(starts[0]["hooks"]) == 1, (
        "the fingerprint rides the SessionStart hook Jarvis already installs; a second "
        "entry here would double the per-session process cost")


def test_the_fingerprint_does_not_import_the_catalog(tmp_path):
    """~60ms against a ~155ms hook — a 39% tax on every session, to watch a field only a
    human edit moves. The work order's own override is covered instead, free, because
    that row is already loaded.

    Asserted in a fresh interpreter, because the cost is an IMPORT: in this process the
    module is long since loaded, so every in-process form of this check passes whether
    the claim is true or not.
    """
    import subprocess
    import sys

    probe = (
        "import sys;"
        "from jarvis.hooks import prefix_fingerprint;"
        "from pathlib import Path;"
        f"prefix_fingerprint(Path({str(tmp_path)!r}), Path({str(tmp_path)!r}),"
        " {'id': 'wo-1', 'model': 'claude-opus-5'}, {});"
        "sys.exit('jarvis.catalog was imported' if 'jarvis.catalog' in sys.modules else 0)"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)

    assert done.returncode == 0, done.stderr + done.stdout


def test_the_memory_walk_is_bounded(wo, project, monkeypatch, tmp_path):
    """So the hook's cost cannot grow with the size of somebody's rules directory."""
    monkeypatch.setattr(hooks, "PREFIX_MEMORY_FILE_CAP", 3)
    deep = project
    for i in range(10):
        deep = deep / f"d{i}"
        deep.mkdir()
        (deep / "CLAUDE.md").write_text(f"level {i}")

    assert len(hooks.memory_files(project, deep)) == 3


def test_the_walk_stops_at_the_project_and_does_not_climb_out_of_it(wo, project):
    """A worker's cwd is `<root>/.claude/worktrees/<id>`, so the walk passes through two
    directories before it reaches the root it must stop at. Climbing past it would hash
    whatever CLAUDE.md happens to sit above the checkout — a file no worker's prompt
    contains, and one the machine's other projects would move under it."""
    (project.parent / "CLAUDE.md").write_text("belongs to something else")
    (project / "CLAUDE.md").write_text("the project's own")
    worktree = project / ".claude" / "worktrees" / wo["id"]
    worktree.mkdir(parents=True)
    # The root by a DIFFERENT SPELLING of the same directory, which is what a catalog
    # path through a symlink gives: an unresolved stop never equals a resolved candidate,
    # so the walk runs past it without one.
    by_link = project.parent / "as-linked"
    by_link.symlink_to(project)

    walked = hooks.memory_files(by_link, worktree)

    assert project / "CLAUDE.md" in walked
    assert project.parent / "CLAUDE.md" not in walked


def test_the_work_orders_own_standing_instructions_are_covered(wo, project):
    """`briefing_for` composes the git briefing with the work order's override, so a
    change to either moves the same block of the prompt."""
    before = prefix_fingerprint(project, project, wo, {})
    after = prefix_fingerprint(project, project, {**wo, "append_system_prompt": "haiku"},
                               {})

    assert before["git_briefing"] != after["git_briefing"]


# -- 4. what the record says it is -------------------------------------------------------


def test_the_timeline_names_the_ingredient_and_the_version_it_moved_to():
    """An unregistered event kind renders as a bare name beside a JSON blob, which is
    written-and-not-read (kn-3f133363). A digest's before/after is the question restated;
    a version's is the answer, so only the version is quoted."""
    label, detail = timeline._describe("prefix_drift", {
        "changed": ["cli_version"],
        "before": {"cli_version": "2.1.271"},
        "after": {"cli_version": "2.1.272"}})

    assert "2.1.271 → 2.1.272" in detail
    assert "early warning, not a measurement" in label


def test_every_ingredient_has_a_label_a_reader_can_act_on():
    """A payload key names the code; the timeline is read by whoever has to fix it."""
    assert set(timeline.PREFIX_INGREDIENT_LABEL) == set(PREFIX_INGREDIENTS)


def test_the_hook_defers_to_the_authoritative_check_where_it_computes_its_number():
    """The amendment from wo-9722bb7b, and the other half of the rule wo-1d5cefc8 landed
    in `check_prefix_stable`'s own docstring. Descriptions settle and stop being read; if
    the deference lives only there, the next reader meets two prefix-drift signals with
    nothing saying which is the measurement (kn-376c88eb, kn-a066e10c).
    """
    import inspect

    source = inspect.getsource(hooks)
    section = source[source.index("# -- the prompt prefix"):source.index("def note_prefix")]

    assert "`invariants.check_prefix_stable` IS THE MEASUREMENT" in section
    assert "PROXY" in section
    assert "check_prefix_stable` wins" in section, "and which one wins is the whole rule"
    # The shortfall against finding 4's "caught at the moment it happens", stated as
    # plainly as the deference — a reader who takes this for the detector the finding
    # asked for stops looking, which is the expensive way to be wrong (Neo q363).
    assert "NOT A DETECTOR" in section
    assert "CANNOT TELL A REGRESSION FROM AN EDIT" in section
    assert "NOTHING IS PAGED" in section
    assert "invariants.check_prefix_stable" in (note_prefix.__doc__ or ""), (
        "the function that writes the event has to carry it too — the section comment is "
        "one scroll away from whoever is reading `note_prefix` in isolation")


def test_the_hook_does_not_raise_its_own_violation(wo, project):
    """Neo q363. An ingredient changing is ORDINARY — an edit to a project's CLAUDE.md
    legitimately moves the prefix for every worker in it. A proxy that alarmed here would
    fire on routine edits AND would stand beside the authoritative check as a second
    verdict with no rule saying which to believe."""
    names = [check.__name__ for check in invariants.OS_INVARIANTS]

    assert "check_prefix_stable" in names
    assert not [n for n in names if "prefix" in n and n != "check_prefix_stable"]


# -- 5. the doctor cites what the hook saw -----------------------------------------------


def test_the_measurement_names_a_cause_when_the_hook_recorded_one(wo, project,
                                                                  catalog_file):
    """This is the whole of "reports INTO it". `check_prefix_stable` knows the prefix got
    worse and has only a suspect list to offer about why; the hook knows what moved and
    cannot say what it cost. Read together they are one answer."""
    settings = settings_file(project, wo["id"])
    settings.write_text("{}")
    e = env(project, wo["id"])
    handle_hook(session_start(cwd=project), e)
    settings.write_text('{"includeGitInstructions": true}')
    handle_hook(session_start(cwd=project), e)

    from jarvis.ops import resolve_catalog

    witness = invariants._prefix_witness(resolve_catalog(str(catalog_file)).os)

    assert "worker's settings file" in witness
    assert "early warning and not this measurement" in witness


def test_the_witness_narrows_the_suspect_list_and_never_replaces_it(jarvis_home,
                                                                    fake_claude,
                                                                    catalog_file, project):
    """Most of the fleet's settled orders predate the hook, so an absence here is not
    evidence that nothing moved — and a violation that dropped the checklist on the
    strength of that absence would be worse than the one it replaced."""
    ops.start_os(str(catalog_file), foreground=True)
    from jarvis.ops import resolve_catalog

    cfg = resolve_catalog(str(catalog_file)).os

    assert invariants._prefix_witness(cfg) == ""
    assert "The usual suspects" in inspect.getsource(invariants.check_prefix_stable)


def test_a_dead_ingredient_is_named_rather_than_left_as_silence(install, wo, project,
                                                                catalog_file):
    """Review round 1's finding. `cli_version` is the only ingredient parsed out of the
    environment, so on a machine whose install layout is not recognised it is `?` for
    ever — and `?` is skipped on both sides of every comparison, so a CLI upgrade is never
    reported. That is not silence: this check tells the reader a crossing with nothing
    named points at MCP, so a dead ingredient sends them to the WRONG suspect.
    """
    settings_file(project, wo["id"]).write_text("{}")
    unreadable = install(resolves_to="claude-wrapper")  # no receipt either
    handle_hook(session_start(cwd=project), {**env(project, wo["id"]), **unreadable})

    from jarvis.ops import resolve_catalog

    witness = invariants._prefix_witness(resolve_catalog(str(catalog_file)).os)

    assert "could not read the Claude Code version" in witness
    assert "neither confirmed nor ruled out" in witness


def test_a_readable_ingredient_is_not_reported_as_dark(install, wo, project, catalog_file):
    """The other half, or the warning above is on every violation for ever and stops
    meaning anything."""
    settings_file(project, wo["id"]).write_text("{}")
    handle_hook(session_start(cwd=project),
                {**env(project, wo["id"]), **install(resolves_to="9.9.72")})

    from jarvis.ops import resolve_catalog

    witness = invariants._prefix_witness(resolve_catalog(str(catalog_file)).os)

    assert "could not read" not in witness


def test_the_measurement_says_it_is_blind_to_mcp(jarvis_home, fake_claude, catalog_file,
                                                 project):
    """35% of the post-fix prefix re-writes by volume, and the hook cannot see any of it:
    tool definitions render at position 0 and a server connects MID-turn, after
    SessionStart has run. An unexplained crossing with nothing named is the case to
    suspect a server in, and the violation has to say so or the absence reads as an
    all-clear."""
    ops.start_os(str(catalog_file), foreground=True)
    wo_row = ops.create_work_order("proj_a", "one", origin="jarvis")
    settings = settings_file(project, wo_row["id"])
    settings.write_text("{}")
    e = env(project, wo_row["id"])
    handle_hook(session_start(cwd=project), e)
    settings.write_text('{"x": 1}')
    handle_hook(session_start(cwd=project), e)

    from jarvis.ops import resolve_catalog

    witness = invariants._prefix_witness(resolve_catalog(str(catalog_file)).os)

    assert "blind to the MCP tool set" in witness
