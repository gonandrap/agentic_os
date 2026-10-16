"""Questions to Neo must stay readable: skeleton plan reviews, section references.

Production questions #65–#67 reached 67–84KB because `build_plan_question` inlined
every child's full standalone brief, and worker questions grew because the contract
told workers to put everything inside the question text. The ruling (wo-e4a359cb):

* A plan-review question is a SKELETON — the ask verbatim, the summary, one line per
  child — never the full briefs. The briefs live in the stored plan, behind
  `jarvis fo show` and the dashboard.
* The design document is first-class: `plan.json` names it, `fo plan` snapshots it,
  and dispatch materialises it where every child worker can read it, so child briefs
  can reference its sections instead of duplicating it.
* A worker question is one paragraph that may reference a design artifact section
  in-text (`section 3 of design doc "docs/x.md"`); ops resolves the reference and
  hands Neo ONLY that section, not the whole document.
* An escalation inbox row is a headline LINKING to the question's dashboard page, never
  the verbatim question — the row reaches Telegram, where a command cannot be run.

Several assertions pair a "this must NOT appear" with a same-test control that the
text demonstrably existed at that moment — a skeleton is only evidence of dieting if
the fat was real.
"""

from __future__ import annotations

import pytest

from jarvis import db, ops, plans, sections
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC_BODY, fixture_spec_section


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def fat_child(key: str, needs: list[str] | None = None) -> dict:
    """A child whose brief is as fat as one may now be: right under
    `plans.MAX_DESCRIPTION_CHARS`, and still an order of magnitude more text than the
    one skeleton line Neo is meant to receive for it.

    It used to be several KB. The ceiling landed afterwards (wo-ed9af5b7) and dropped
    again to 600 when the spec became the artifact (wo-4580e7c1), so a brief that size is
    refused at submission now and could not reach these tests at all — but the diet these
    tests are about is the ratio between a brief and its skeleton line, not the absolute
    size, and even 600 characters against one line still demonstrates it."""
    body = (f"BRIEF-BODY-{key}: build the {key} piece of the exporter. "
            + "Context the planner repeated into every child. " * 11)
    assert len(body) <= plans.MAX_DESCRIPTION_CHARS, len(body)
    return {
        "key": key,
        "title": f"Build {key}",
        "description": body,
        "needs": needs or [],
        "spec_section": fixture_spec_section(key),
        "acceptance": f"tests for {key} pass",
    }


ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


#: The shared fixture document rather than a local copy of it. A plan is now refused
#: unless every child's `spec_section` resolves in the spec it names, so a private
#: document with three sections would cap this file's plans at three children and drift
#: from what `fixture_spec_section` hands out.
DESIGN_DOC = FIXTURE_DESIGN_DOC_BODY


# -- P1: the plan-review question is a skeleton -----------------------------------------


def test_plan_question_is_a_skeleton_and_fo_show_keeps_the_full_briefs():
    fo = {"id": "fo-x", "title": "CSV export", "description": ASK}
    plan = plans.parse_plan({
        "summary": "an exporter",
        "design_doc": "docs/superpowers/specs/exporter.md",
        "children": [fat_child("schema"), fat_child("api", needs=["schema"])],
    })
    question = plans.build_plan_question(fo, plan)
    full = "\n".join(plans.render_plan(plan))

    # Control first: the fat is real and the full renderer still carries it.
    assert "BRIEF-BODY-schema" in full and "BRIEF-BODY-api" in full
    # The question carries the case — ask, summary, every child's line — and no briefs.
    assert ASK in question
    assert "an exporter" in question
    assert "[schema] Build schema" in question
    assert "[api] Build api (needs schema)" in question
    assert "done when: tests for api pass" in question
    assert "BRIEF-BODY-schema" not in question
    assert "BRIEF-BODY-api" not in question
    # Whoever reads it is told where the full briefs live.
    assert "jarvis fo show fo-x" in question
    # The diet is the point: production plans hit 84KB. Asserted as INDEPENDENCE rather
    # than as a ratio — the same plan with briefs three times the size produces the
    # byte-identical question — because a ratio is a statement about today's
    # MAX_DESCRIPTION_CHARS and silently weakens every time that number moves.
    thin = plans.parse_plan({
        "summary": "an exporter",
        "design_doc": "docs/superpowers/specs/exporter.md",
        "children": [{**fat_child("schema"), "description": "a" * 200},
                     {**fat_child("api", needs=["schema"]), "description": "b" * 200}],
    })
    assert plans.build_plan_question(fo, thin) == question
    assert len("\n".join(plans.render_plan(thin))) < len(full)


def test_plan_question_names_the_design_doc_when_the_plan_has_one():
    fo = {"id": "fo-x", "title": "CSV export", "description": ASK}
    plan = plans.parse_plan({
        "summary": "an exporter",
        "design_doc": "docs/superpowers/specs/exporter.md",
        "children": [fat_child("schema")],
    })
    question = plans.build_plan_question(fo, plan)
    assert 'docs/superpowers/specs/exporter.md' in question


def test_plan_reviewer_persona_no_longer_judges_text_it_does_not_receive():
    # The persona must not instruct Neo to judge the full descriptions — the skeleton
    # question does not carry them (prompt rule: never point an agent at a resource
    # that is not in its prompt).
    assert "Each description stands alone" not in plans.PLAN_REVIEWER_PERSONA
    assert "It is what was asked for" in plans.PLAN_REVIEWER_PERSONA


def test_the_persona_describes_the_outline_the_question_actually_carries():
    """Q1880: the question carries the spec's heading outline, so the persona must not
    promise spec text or a truncation marker that can no longer appear."""
    assert "HEADING OUTLINE" in plans.PLAN_REVIEWER_PERSONA
    assert "TRUNCATED" not in plans.PLAN_REVIEWER_PERSONA


# -- P2: the design document is first-class ---------------------------------------------


def test_parse_plan_normalises_design_doc_and_refuses_an_absolute_path():
    plan = plans.parse_plan({
        "summary": "s",
        "design_doc": "  docs/superpowers/specs/exporter.md  ",
        "children": [fat_child("a")],
    })
    assert plan["design_doc"] == "docs/superpowers/specs/exporter.md"
    with pytest.raises(plans.PlanError, match="design_doc"):
        plans.parse_plan({"design_doc": "/etc/passwd",
                          "children": [fat_child("a")]})


@pytest.fixture()
def planning(started, store):
    daemon = started
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    return daemon, store.get_feature_order(fo["id"])


def test_fo_plan_snapshots_the_design_doc_or_refuses(planning, store, project):
    daemon, fo = planning
    doc = project / "docs" / "superpowers" / "specs" / "exporter.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(DESIGN_DOC)

    with pytest.raises(ops.OpsError, match="missing.md"):
        ops.submit_plan(fo["id"], {
            "summary": "s", "design_doc": "docs/superpowers/specs/missing.md",
            "children": [fat_child("a")],
        })

    ops.submit_plan(fo["id"], {
        "summary": "s", "design_doc": "docs/superpowers/specs/exporter.md",
        "children": [fat_child("a")],
    })
    stored = db.from_json(store.get_feature_order(fo["id"])["plan"], {})
    assert stored["design_doc"] == "docs/superpowers/specs/exporter.md"
    assert stored["design_doc_content"] == DESIGN_DOC


def test_children_of_a_design_doc_plan_get_the_doc_materialised(planning, store,
                                                                project, fake_claude):
    daemon, fo = planning
    doc = project / "docs" / "superpowers" / "specs" / "exporter.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(DESIGN_DOC)
    ops.submit_plan(fo["id"], {
        "summary": "an exporter FORCE_APPROVE",
        "design_doc": "docs/superpowers/specs/exporter.md",
        "children": [fat_child("schema")],
    })
    daemon._neo_drain()
    daemon.tick()  # dispatches the child

    snapshot = project / ".jarvis" / "features" / fo["id"] / "exporter.md"
    assert snapshot.read_text() == DESIGN_DOC
    # ...and, separately, only this child's SECTION — the file the prompt names first.
    section = next((project / ".jarvis" / "features" / fo["id"] / "sections")
                   .glob("*.md"))
    which = fat_child("schema")["spec_section"]
    assert section.read_text() == sections.extract_section(DESIGN_DOC, which) + "\n"
    assert section.read_text() != DESIGN_DOC, "the section is a CUT, not the whole spec"
    # Two dispatch turns by now: the planner's, then the child's.
    calls = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=2)
    child_prompt = next(c["argv"][-1] for c in calls
                        if "BRIEF-BODY-schema" in c["argv"][-1])
    assert "# Your section of the feature spec" in child_prompt
    assert str(section) in child_prompt
    assert str(snapshot) in child_prompt
    # The SECTION's text is not pasted into the prompt: naming the file is the whole
    # saving, and a prompt carrying the section would only move the duplication.
    assert section.read_text().strip() not in child_prompt


def test_a_work_order_with_no_feature_carries_no_spec_section(started, store, project,
                                                              fake_claude):
    """The null case, and the control for the test above: an ordinary work order has no
    parent, so nothing is snapshotted and its prompt has no spec heading at all."""
    ops.create_work_order("proj_a", "unrelated", description="something else entirely")

    started.tick()

    call = fake_claude.wait_calls(lambda c: "--session-id" in c["argv"], count=1)[0]
    assert "# Your section of the feature spec" not in call["argv"][-1]
    assert not (project / ".jarvis" / "features").exists()


# -- P3: in-text section references, and the length guardrail ---------------------------


def test_find_refs_reads_the_documented_shapes():
    text = ('Should rounding live in the writer? My question is from section 3 of '
            'design doc "docs/superpowers/specs/exporter.md": the doc says errors raise.')
    assert sections.find_refs(text) == [("docs/superpowers/specs/exporter.md", "3")]
    text2 = 'Per section "Data model" of the design doc "docs/superpowers/specs/exporter.md", ...'
    assert sections.find_refs(text2) == [("docs/superpowers/specs/exporter.md", "Data model")]
    assert sections.find_refs("no references here") == []


def test_unreferenced_docs_are_the_md_paths_no_reference_covers():
    covered = ('From section 3 of design doc "docs/superpowers/specs/exporter.md": '
               'docs/superpowers/specs/exporter.md says errors raise.')
    assert sections.unreferenced_docs(covered) == []
    bare = "I read docs/superpowers/specs/exporter.md and went with CSV."
    assert sections.unreferenced_docs(bare) == ["docs/superpowers/specs/exporter.md"]
    # Only markdown, and only once per path.
    assert sections.unreferenced_docs("edited src/jarvis/ops.py and tests/t.py") == []
    assert sections.unreferenced_docs("a.md, a.md and b.md") == ["a.md", "b.md"]


def test_heading_outline_is_every_heading_line_and_nothing_else():
    """Neo ruling Q1880: a structural fingerprint for plan review. Same matcher
    (`HEADING_RE`) as `find_heading`, so the outline and a resolved section agree."""
    outline = sections.heading_outline(DESIGN_DOC)

    assert outline[:3] == ["# Exporter design", "## 1. Shape", "## 2. Data model"]
    assert outline == [f"{m.group(1)} {m.group(2)}"
                       for m in sections.HEADING_RE.finditer(DESIGN_DOC)]
    assert "The exporter is one module with one entry point." not in outline
    assert sections.heading_outline("prose with no heading at all") == []


def test_extract_section_by_number_and_name():
    by_number = sections.extract_section(DESIGN_DOC, "3")
    assert by_number is not None
    assert "Failure handling" in by_number
    assert "An empty result set" in by_number
    assert "Rows are dicts" not in by_number  # neighbouring section stays out
    by_name = sections.extract_section(DESIGN_DOC, "data model")
    assert by_name is not None and "union of keys" in by_name
    assert sections.extract_section(DESIGN_DOC, "no such heading") is None


def test_find_heading_is_the_one_matcher_extract_section_uses():
    """The §3 split: one rule, so a child's brief and its spec link cannot disagree."""
    heads = [m.group(2) for m in sections.HEADING_RE.finditer(DESIGN_DOC)]

    by_number = sections.find_heading(DESIGN_DOC, "3")
    assert by_number is not None and "Failure handling" in heads[by_number]
    by_name = sections.find_heading(DESIGN_DOC, "data model")
    assert by_name is not None and "Data model" in heads[by_name]
    assert sections.find_heading(DESIGN_DOC, "no such heading") is None
    # And the slice still starts at the heading the matcher chose.
    section = sections.extract_section(DESIGN_DOC, "3")
    assert section is not None and section.startswith("#")
    assert heads[by_number] in section.splitlines()[0]


@pytest.fixture()
def dispatched(started, project):
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    return daemon, wo


def test_a_referenced_section_reaches_neo_and_the_rest_of_the_doc_does_not(
        dispatched, project):
    daemon, wo = dispatched
    doc = project / "docs" / "superpowers" / "specs" / "exporter.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(DESIGN_DOC)
    ops.ask_question(
        wo["id"],
        'From section 3 of design doc "docs/superpowers/specs/exporter.md": should an empty '
        'result set also log a warning? I recommend no — errors raise, quiet '
        'otherwise.')
    neo = NeoStore()
    try:
        q = neo.get(1)
    finally:
        neo.close()
    assert "An empty result set writes the header" in q["context"]
    assert "union of keys" not in q["context"]  # the rest of the doc stayed home


# -- one section, resolved or refused, for BOTH verbs ----------------------------------
#
# User decision 2026-10-14 (Neo learning #102): Neo never receives a full spec, only a
# pointer to ONE specific section resolved to that section's text. A reference that does
# not resolve is a REFUSAL, never a note in the context and never the whole file.


def _doc(project):
    doc = project / "docs" / "superpowers" / "specs" / "exporter.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(DESIGN_DOC)
    return doc


def _nothing_recorded(project, wo_id):
    neo = NeoStore()
    try:
        assert neo.get(1) is None
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        assert store.all_assumptions(wo_id) == []
    finally:
        store.close()


def _both_verbs(wo_id):
    return (lambda text: ops.ask_question(wo_id, text),
            lambda text: ops.assume(wo_id, text))


def test_an_unresolvable_reference_is_refused_in_both_verbs(dispatched, project):
    """The file is missing, or the heading is. Either way the asker is told which, and
    nothing is recorded: a silently dropped reference is a question argued from nothing."""
    daemon, wo = dispatched
    _doc(project)

    for verb in _both_verbs(wo["id"]):
        with pytest.raises(ops.OpsError, match="no such file"):
            verb('From section 9 of design doc "docs/superpowers/specs/ghost.md": '
                 'which default?')
        with pytest.raises(ops.OpsError, match="no such heading"):
            verb('From section "Nowhere at all" of design doc '
                 '"docs/superpowers/specs/exporter.md": which default?')
    _nothing_recorded(project, wo["id"])


def test_a_document_named_with_no_section_is_refused_with_the_shape_named(dispatched,
                                                                         project):
    daemon, wo = dispatched
    _doc(project)

    for verb in _both_verbs(wo["id"]):
        with pytest.raises(ops.OpsError, match="section 3 of design doc"):
            verb("I read docs/superpowers/specs/exporter.md and went with CSV.")
    _nothing_recorded(project, wo["id"])


TWO_REFS = ('Errors raise per section 3 of design doc '
            '"docs/superpowers/specs/exporter.md", and the header is the union of keys '
            'per section "Data model" of design doc '
            '"docs/superpowers/specs/exporter.md".')


def test_two_cited_sections_both_resolve_in_both_verbs(dispatched, project):
    """Neo ruling Q1883: NO cardinality cap. `validation.assumption_spec_chars` bounds the
    text, never a count of sections."""
    daemon, wo = dispatched
    _doc(project)

    for verb in _both_verbs(wo["id"]):
        verb(TWO_REFS)


def test_assume_stores_every_cited_section_under_one_labelled_snapshot(dispatched,
                                                                      project):
    """Q1883. Three columns, N sections: each body preceded by its own
    `<path> § <which>:` line, `spec_path`/`spec_which` from the FIRST reference."""
    daemon, wo = dispatched
    _doc(project)

    ops.assume(wo["id"], TWO_REFS)

    store = ProjectStore(project)
    try:
        (row,) = store.all_assumptions(wo["id"])
    finally:
        store.close()
    assert (row["spec_path"], row["spec_which"]) == (
        "docs/superpowers/specs/exporter.md", "3")
    assert "docs/superpowers/specs/exporter.md § 3:" in row["spec_text"]
    assert "docs/superpowers/specs/exporter.md § Data model:" in row["spec_text"]
    assert "An empty result set writes the header" in row["spec_text"]
    assert "union of keys" in row["spec_text"]
    assert "one module with one entry point" not in row["spec_text"]  # §1 stayed home


def test_assume_resolves_the_section_once_and_stores_the_snapshot(dispatched, project):
    """kn-924730d7: the worker's worktree is gone by the time the review packet is built,
    so the pointer is resolved HERE and the section stored on the row."""
    daemon, wo = dispatched
    _doc(project)

    ops.assume(wo["id"],
               'Errors raise, per section 3 of design doc '
               '"docs/superpowers/specs/exporter.md".')

    store = ProjectStore(project)
    try:
        (row,) = store.all_assumptions(wo["id"])
    finally:
        store.close()
    assert row["spec_path"] == "docs/superpowers/specs/exporter.md"
    assert row["spec_which"] == "3"
    assert "An empty result set writes the header" in row["spec_text"]
    assert "union of keys" not in row["spec_text"]  # the rest of the doc stayed home


def test_an_assumption_with_no_reference_at_all_is_recorded_as_before(dispatched,
                                                                      project):
    daemon, wo = dispatched

    ops.assume(wo["id"], "used tabs, not spaces")

    store = ProjectStore(project)
    try:
        (row,) = store.all_assumptions(wo["id"])
    finally:
        store.close()
    assert (row["spec_path"], row["spec_which"], row["spec_text"]) == (None, None, None)


def test_a_question_over_the_cap_is_refused_with_the_fix_named(dispatched):
    daemon, wo = dispatched
    with pytest.raises(ops.OpsError, match="section"):
        ops.ask_question(wo["id"], "x" * (ops.QUESTION_MAX_CHARS + 1))
    neo = NeoStore()
    try:
        assert neo.get(1) is None  # nothing was queued
    finally:
        neo.close()


def test_a_long_but_legal_question_carries_a_warning(dispatched):
    daemon, wo = dispatched
    out = ops.ask_question(wo["id"], "y" * (ops.QUESTION_WARN_CHARS + 1))
    assert "paragraph" in out.get("warning", "")
    out2 = ops.ask_question(wo["id"], "short and sharp?")
    assert "warning" not in out2


def test_the_contracts_teach_the_reference_shape_not_paste_everything():
    from jarvis.catalog import ProjectSpec
    from jarvis.dispatch import build_worker_prompt
    from pathlib import Path
    project = ProjectSpec(name="p", path=Path("/tmp/p"))
    worker = build_worker_prompt({"id": "wo-1", "title": "t", "kind": "worker",
                                  "description": "d"}, project)
    planner = build_worker_prompt({"id": "wo-2", "title": "t", "kind": "planner",
                                   "description": "d", "parent_id": "fo-1"}, project)
    for prompt in (worker, planner):
        assert "everything needed to decide INSIDE the question text" not in prompt
        assert "one paragraph" in prompt
    # The planner keeps its full prompt; the worker's core is compressed and the
    # reference example moved into the full contract section behind `jarvis brief`
    # (worker_brief.contract_section — single source with the CLI).
    from jarvis import worker_brief
    worker_contract = worker_brief.render_section("contract", wo_id="wo-1", project="p")
    for text in (planner, worker_contract):
        assert 'section 3 of design doc "docs/superpowers/specs/feature.md"' in text
    # The planner is told the design doc carries the shared context now.
    assert "design_doc" in planner
    assert "Repetition is cheap" not in planner


# -- P4: escalation inbox rows are headlines --------------------------------------------


def test_an_escalated_question_lands_in_the_inbox_as_a_headline(dispatched, project):
    daemon, wo = dispatched
    long_tail = " ".join(f"context sentence {n}." for n in range(40))
    ops.ask_question(wo["id"], f"FORCE_ESCALATE: may I rotate the key? {long_tail}")
    daemon._neo_drain()
    central = CentralStore()
    try:
        item = next(i for i in central.unacked_inbox()
                    if "Neo escalated" in i["title"])
    finally:
        central.close()
    # Control: the question really was long, and its head really is in the body.
    assert "may I rotate the key?" in item["body"]
    assert "context sentence 39." in long_tail
    assert "context sentence 39." not in item["body"]  # the tail stayed out
    assert "/neo/question/1" in item["body"]
    assert f"jarvis neo answer 1" in item["body"]
    assert len(item["body"]) < 600
