"""The per-feature agent type, and the section each child is handed.

A feature order's spec ends with an `Agent profile` appendix. The OS turns that appendix
into a Claude Code agent definition and every child of that feature runs AS it; when the
feature settles the definition is deleted. Design:
docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.

WHAT THESE TESTS ASSERT AGAINST, and why it is not a Python constant: the enforcement is
a FILE THE CLI READS plus a FLAG IN ARGV, so that is what is checked — the shipped
`.md` on disk and the recorded `claude` invocation. kn-44fb3e42 recorded this rule the
last time a seat's capabilities were shipped as markdown: a test over a Python list would
keep passing for ever while the file the CLI actually loads said something else.

`--agent` and its `--add-dir` are asserted TOGETHER in every case. Verified live with a
control on 2026-08-29: `--agent X` without the directory that supplies X is not a
degradation, it is an immediate CLI error — so a code path that emitted one without the
other would not run a worker with the wrong persona, it would fail to run one at all.
"""

from __future__ import annotations

import pytest

from jarvis import ops, specs
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import (
    FIXTURE_DESIGN_DOC,
    FIXTURE_DESIGN_DOC_BODY,
    fixture_spec_section,
)

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")

PROFILE_MARKER = "You are a Python engineer working on the exporter."


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def child(key: str) -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} piece. Do not touch the caller's public interface — the "
            f"sibling that owns it is doing that. Done when the existing suite is green."
        ),
        "needs": [],
        "spec_section": fixture_spec_section(key),
    }


def release(daemon, store, *keys: str, doc: str | None = None):
    """A feature order with a released plan. Returns the feature row."""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    if doc is not None:
        (store.project_path / FIXTURE_DESIGN_DOC).write_text(doc)
    ops.submit_plan(fo["id"], {"summary": "an exporter FORCE_APPROVE",
                               "design_doc": FIXTURE_DESIGN_DOC,
                               "children": [child(k) for k in keys]})
    daemon._neo_drain()
    return store.get_feature_order(fo["id"])


def definition(project, fo_id):
    return specs.agent_root(project, fo_id) / ".claude" / "agents" / f"{fo_id}.md"


# -- the definition on disk -------------------------------------------------------------


def test_releasing_a_plan_writes_the_features_agent_definition(started, store, project):
    fo = release(started, store, "schema")

    written = definition(project, fo["id"]).read_text()

    assert f"name: {fo['id']}" in written
    assert PROFILE_MARKER in written


def test_the_definition_declares_no_tools_key(started, store, project):
    """`tools:` is an ENFORCED capability restriction (kn-44fb3e42), so a per-feature
    profile that carried one would silently take a shell away from every child of that
    feature. Asserted against the shipped file, which is what the CLI reads."""
    fo = release(started, store, "schema")

    assert "tools:" not in definition(project, fo["id"]).read_text()


def test_a_spec_with_no_agent_profile_writes_nothing(project):
    """Degradation is total: no appendix, no agent, no exception. The plan validator
    refuses such a spec at submission, so this is the belt to that braces — a feature
    planned before the appendix existed still dispatches."""
    bare = "# Exporter\n\n## 1. Shape\n\nOne module.\n"

    assert specs.install_agent(project, "fo-none", "t", bare) is None
    assert not specs.agent_root(project, "fo-none").exists()


# -- the flag on the worker's command line ----------------------------------------------


def test_a_child_is_dispatched_as_its_features_agent(started, store, project,
                                                     fake_claude):
    fo = release(started, store, "schema")

    started.tick()

    call = fake_claude.wait_calls(
        lambda c: "--agent" in c["argv"] and "--session-id" in c["argv"])[0]
    argv = call["argv"]
    assert argv[argv.index("--agent") + 1] == fo["id"]
    # ...and the directory that supplies it, without which the flag is a hard error.
    assert str(specs.agent_root(project, fo["id"])) in argv


def test_a_work_order_with_no_feature_is_dispatched_with_no_agent(started, fake_claude):
    """The control. A standalone work order keeps the briefing it always had."""
    ops.create_work_order("proj_a", "unrelated", description="something else entirely")

    started.tick()

    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"])[0]
    assert "--agent" not in call["argv"]


def test_the_planner_is_not_dispatched_as_the_agent_it_has_not_written_yet(
        started, store, fake_claude):
    """A planner carries `parent_id` too, and it is the session that WRITES the profile.
    Running it as a persona derived from its own unwritten output is circular."""
    ops.create_feature_order("proj_a", "CSV export", description=ASK)

    started.tick()

    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"])[0]
    assert "You are the PLANNER" in call["argv"][-1]
    assert "--agent" not in call["argv"]


# -- the lifecycle ----------------------------------------------------------------------


def test_settling_the_feature_deletes_its_agent(started, store, project):
    """Written into `set_feature_status`, the one line every settle path goes through —
    a persona outliving its feature is one a later, unrelated order could be handed."""
    fo = release(started, store, "schema")
    assert definition(project, fo["id"]).exists()

    ops.cancel_feature_order(fo["id"])

    assert not specs.agent_root(project, fo["id"]).exists()


def test_the_agent_can_be_rebuilt_from_the_spec_after_the_feature_settled(
        started, store, project):
    """The snapshot outlives the agent: the plan is never deleted, so `jarvis fo agent`
    hands the persona back to a session opened by hand."""
    fo = release(started, store, "schema")
    ops.cancel_feature_order(fo["id"])

    out = ops.rebuild_feature_agent(fo["id"])

    assert out["agent"] == fo["id"]
    assert PROFILE_MARKER in definition(project, fo["id"]).read_text()


def test_rebuilding_says_so_when_there_is_no_spec_to_build_from(started, store):
    """An unplanned feature has no snapshot. Refused with the reason, never a silent
    empty definition that would then be selected with `--agent` and fail at spawn."""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)

    with pytest.raises(ops.OpsError, match="no spec snapshot"):
        ops.rebuild_feature_agent(fo["id"])


def test_a_deleted_definition_heals_on_the_next_dispatch(started, store, project,
                                                         fake_claude):
    """Generated, never authored — the same property `bootstrap._rebuild` gives the
    skills tree. This is also what gives a feature released before this change an agent."""
    fo = release(started, store, "schema")
    definition(project, fo["id"]).unlink()

    started.tick()

    assert PROFILE_MARKER in definition(project, fo["id"]).read_text()


# -- what the panel is shown ------------------------------------------------------------


def test_the_panels_packet_carries_the_childs_section(started, store, project):
    from jarvis import evidence, validation

    fo = release(started, store, "schema")
    wo = store.feature_children(fo["id"])[0]

    packet = evidence.collect_work_order(project, wo, declared="ran the suite",
                                         spec=specs.spec_of(store, wo))
    prompt = validation.build_packet_prompt(packet)

    assert packet.spec_ref.startswith(FIXTURE_DESIGN_DOC)
    assert packet.spec_section and packet.spec_section in prompt
    assert "THE SPEC THIS WAS BUILT TO" in prompt


def test_a_packet_with_no_section_renders_as_it_always_did(project):
    """The null case is the panel's existing behaviour, unchanged — no heading, no
    empty section, nothing for a seat to read as "the spec said nothing"."""
    from jarvis import evidence, validation

    packet = evidence.collect_work_order(
        project, {"id": "wo-x", "title": "t", "description": "d"}, declared="")

    assert packet.spec_ref == "" and packet.spec_section == ""
    assert "THE SPEC THIS WAS BUILT TO" not in validation.build_packet_prompt(packet)


def test_the_stored_section_is_what_the_plan_named(started, store):
    """`work_orders.spec_section` is a column because three readers need it. Its value
    comes from the plan and nothing re-derives it."""
    fo = release(started, store, "schema")

    wo = store.feature_children(fo["id"])[0]

    assert wo["spec_section"] == child("schema")["spec_section"]


# -- the spec's own text -----------------------------------------------------------------


def test_the_profile_is_the_appendix_and_not_the_rest_of_the_spec():
    profile = specs.agent_profile(FIXTURE_DESIGN_DOC_BODY)

    assert PROFILE_MARKER in profile
    assert "The exporter is one module with one entry point." not in profile
