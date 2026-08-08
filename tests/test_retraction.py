"""Retracting a superseded ruling: it leaves the prompt, it stays in the record.

Both of the OS's ledgers — Neo's learnings and the central knowledge base — were
INSERT-only. A ruling the user reversed therefore stayed in every prompt beside its
replacement for ever, and the live production learnings accumulated a CONFLICT NOTICE
entry quoting three mutually unsatisfiable rulings of the user's own.

Retraction is an UPDATE, never a DELETE. The property that keeps it honest is that a
retired row is still returned by the AUDIT reads (`NeoStore.all_learnings`,
`CentralStore.search_knowledge`) carrying `retired_at` and `retired_reason`, while the
PROMPT reads (`NeoStore.learnings` -> `neo.build_system_prompt`, and
`CentralStore.relevant_knowledge` -> the worker prompt) skip it.

EVERY FILTER TEST HERE CARRIES ITS NEGATIVE CONTROL IN THE SAME TEST, because a filter
that returned nothing at all would satisfy "the retired one is gone" perfectly. So each
one names a second, non-retired entry that MUST still be there. (Same rule as
`test_neo.py::test_a_seat_scoped_learning_does_not_move_neos_cached_prefix`.)
"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis.central_store import MEMORY_TAG, CentralStore
from jarvis.neo_store import NeoStore


@pytest.fixture()
def neo(jarvis_home):
    store = NeoStore()
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def central(jarvis_home):
    store = CentralStore()
    try:
        yield store
    finally:
        store.close()


# -- learnings ---------------------------------------------------------------------


def test_a_retracted_learning_leaves_neos_prompt_and_the_survivors_do_not_move(neo):
    """The point of the whole feature, and the one property that pins it.

    THIS IS A DELIBERATE EXCEPTION TO THE APPEND-ONLY PROMPT PREFIX that
    `test_neo.py::test_learnings_shape_future_answers` protects, and it is worth being
    explicit rather than letting it read as drift: a retraction genuinely DOES rewrite
    that prefix, because the whole purpose is to remove text the user has reversed. It
    costs one Anthropic prompt-cache miss, once, at the moment the user changes their
    mind — which is exactly when a stale cached prefix is the thing you least want.

    What it must NOT do is reorder the rows that survive. Oldest-first ordering is what
    makes an ordinary new learning an append; if a retraction reshuffled the survivors,
    every subsequent prompt would be a fresh miss rather than one.
    """
    a = neo.add_learning("Always default to CSV", project="proj_a")
    b = neo.add_learning("Never bundle two decisions in one PR", project="proj_a")
    c = neo.add_learning("Squash merges only", project="proj_a")
    before = [r["content"] for r in neo.learnings("proj_a")]
    assert before == [a["content"], b["content"], c["content"]]

    neo.retract_learning(b["id"], "reversed: small stacked PRs are fine now")

    prompt = neo_mod.build_system_prompt(neo, "proj_a")
    assert "Never bundle two decisions in one PR" not in prompt
    # the negative control: the filter did not simply return nothing
    assert "Always default to CSV" in prompt
    assert "Squash merges only" in prompt
    # and the survivors keep their order, so the prefix shifts once and then holds
    assert [r["content"] for r in neo.learnings("proj_a")] == [a["content"], c["content"]]


def test_a_retracted_learning_is_still_in_the_audit_trail(neo):
    """RETIRED IS NOT DELETED. This is the assertion that stops anyone implementing
    retraction as a DELETE the next time the prompt needs shortening."""
    row = neo.add_learning("Always default to CSV", project="proj_a")
    neo.retract_learning(row["id"], "the user moved to JSON in 2026-08")

    audit = {r["id"]: r for r in neo.all_learnings()}
    assert row["id"] in audit
    retired = audit[row["id"]]
    assert retired["content"] == "Always default to CSV"
    assert retired["retired_reason"] == "the user moved to JSON in 2026-08"
    assert retired["retired_at"] is not None
    # and it is reachable through the store's own opt-in, which is what the CLI uses
    assert row["id"] in {r["id"] for r in neo.learnings("proj_a", include_retired=True)}


def test_the_seat_scoped_query_also_drops_retired_learnings(neo):
    """The default-safe filter lives in `NeoStore.learnings`, not in
    `neo.build_system_prompt`, precisely so the panel's per-seat prompt builder — which
    lives in another module and another work order — inherits it for free.

    Three controls in one call, because a seat query returning `[]` proves nothing:
    the surviving global row and the surviving seat row must both still be there.
    """
    glob = neo.add_learning("Always default to CSV", project="proj_a")
    dead = neo.add_learning("A grep naming shipit ships nothing",
                            project="proj_a", seat="blast")
    live = neo.add_learning("A PR body quoting a path ships nothing",
                            project="proj_a", seat="blast")
    neo.retract_learning(dead["id"], "the recogniser was fixed; this misleads now")

    seen = [r["content"] for r in neo.learnings("proj_a", seat="blast")]
    assert dead["content"] not in seen
    assert seen == [glob["content"], live["content"]]


def test_retracting_a_learning_needs_a_reason(neo):
    row = neo.add_learning("Always default to CSV", project="proj_a")
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="reason"):
            neo.retract_learning(row["id"], blank)
    # and the refusal left the learning standing, not half-retired
    assert [r["content"] for r in neo.learnings("proj_a")] == [row["content"]]


def test_retracting_a_learning_that_does_not_exist_raises(neo):
    with pytest.raises(KeyError):
        neo.retract_learning(4242, "superseded")


def test_retracting_a_learning_twice_is_refused(neo):
    """Refuse rather than re-stamp. The first reason and timestamp record WHEN the user
    changed their mind; an idempotent second retraction would overwrite both, and the
    audit trail is the only thing retraction has instead of a delete."""
    row = neo.add_learning("Always default to CSV", project="proj_a")
    first = neo.retract_learning(row["id"], "the user moved to JSON")
    with pytest.raises(ValueError, match="already retired"):
        neo.retract_learning(row["id"], "a second, different reason")

    after = {r["id"]: r for r in neo.all_learnings()}[row["id"]]
    assert after["retired_reason"] == "the user moved to JSON"
    assert after["retired_at"] == first["retired_at"]


# -- knowledge ---------------------------------------------------------------------


def test_a_retracted_knowledge_entry_leaves_the_worker_prompt_feed(central):
    live = central.add_knowledge("always run make lint", project="proj_a", topic="ci")
    dead = central.add_knowledge("deploy from the deploy branch", project="proj_a",
                                 topic="ci")
    glob = central.add_knowledge("prefer uv over pip", project="")
    central.retract_knowledge(dead["id"], "we deploy from tags now")

    offered = [r["content"] for r in central.relevant_knowledge("proj_a")]
    assert "deploy from the deploy branch" not in offered
    # the negative controls: project-scoped AND global entries still arrive
    assert live["content"] in offered
    assert glob["content"] in offered


def test_a_retracted_knowledge_entry_is_still_in_the_audit_trail(central):
    row = central.add_knowledge("deploy from the deploy branch", project="proj_a")
    central.retract_knowledge(row["id"], "we deploy from tags now")

    found = central.search_knowledge("deploy from the deploy branch")
    assert len(found) == 1
    assert found[0]["retired_reason"] == "we deploy from tags now"
    assert found[0]["retired_at"] is not None
    # and the opt-in the CLI listing uses reaches it too
    assert row["id"] in {
        r["id"] for r in central.relevant_knowledge("proj_a", include_retired=True)}


def test_retracting_knowledge_needs_a_reason(central):
    row = central.add_knowledge("deploy from the deploy branch", project="proj_a")
    for blank in ("", "   ", "\n\t"):
        with pytest.raises(ValueError, match="reason"):
            central.retract_knowledge(row["id"], blank)
    assert [r["content"] for r in central.relevant_knowledge("proj_a")] == [row["content"]]


def test_retracting_knowledge_that_does_not_exist_raises(central):
    with pytest.raises(KeyError):
        central.retract_knowledge("kn-deadbeef", "superseded")


def test_retracting_knowledge_twice_is_refused(central):
    row = central.add_knowledge("deploy from the deploy branch", project="proj_a")
    first = central.retract_knowledge(row["id"], "we deploy from tags now")
    with pytest.raises(ValueError, match="already retired"):
        central.retract_knowledge(row["id"], "a second, different reason")

    after = central.search_knowledge("deploy from")[0]
    assert after["retired_reason"] == "we deploy from tags now"
    assert after["retired_at"] == first["retired_at"]


# -- the mirrored memory file ---------------------------------------------------------


def test_a_retracted_memory_mirror_does_not_capture_the_next_rewrite_in_place(central):
    """`record_memory_file` replaces its row in place so a rewritten memory file does
    not accumulate near-duplicates. Its SELECT skips retired rows.

    The alternative — updating the retired row in place — would turn one retraction
    into a permanent silent mute: every later version of that file written into a row
    no prompt reads, with nothing telling anyone why their memory stopped arriving
    (ruled by Neo, question 56). A retraction is a statement about the TEXT.
    """
    assert central.record_memory_file("PRs #1-#2 are unmerged.", project="proj_a",
                                      topic="project_tesis")
    original = central.search_knowledge("PRs #1-#2")[0]
    central.retract_knowledge(original["id"], "the thesis project was archived")

    # the retraction holds against the CURRENT text
    assert "PRs #1-#2 are unmerged." not in [
        r["content"] for r in central.relevant_knowledge("proj_a")]

    # a genuinely new version of the file is a new statement: it lands live, in a new row
    assert central.record_memory_file("PRs #1-#3 are merged.", project="proj_a",
                                      topic="project_tesis")
    offered = [r["content"] for r in central.relevant_knowledge("proj_a")]
    assert offered == ["PRs #1-#3 are merged."]
    # both rows survive in the audit trail, and exactly one of them is retired
    rows = central.search_knowledge("PRs #1-")
    assert len(rows) == 2
    assert sum(r["retired_at"] is not None for r in rows) == 1


def test_retraction_does_not_make_the_mirror_start_accumulating_rows(central):
    """The replace-in-place behaviour is untouched for the ordinary case: rewriting a
    memory file that was never retracted still updates ONE row."""
    for text in ("first version", "second version", "third version"):
        central.record_memory_file(text, project="proj_a", topic="project_tesis")

    rows = [r for r in central.search_knowledge("", limit=50)
            if r["tags"] == MEMORY_TAG]
    assert len(rows) == 1
    assert rows[0]["content"] == "third version"
    assert rows[0]["retired_at"] is None


# -- the CLI ---------------------------------------------------------------------------


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    from jarvis.cli import main

    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_retracts_a_learning_and_keeps_listing_it(jarvis_home, capsys):
    store = NeoStore()
    try:
        dead = store.add_learning("Always default to CSV", project="proj_a")
        live = store.add_learning("Squash merges only", project="proj_a")
    finally:
        store.close()

    code, out, _ = _run(["neo", "retract", str(dead["id"]),
                         "--reason", "the user moved to JSON"], capsys)
    assert code == 0

    # the listing is an audit surface: it shows the retired row, marked, with the reason
    code, out, _ = _run(["neo", "learnings", "--project", "proj_a"], capsys)
    assert code == 0
    assert "the user moved to JSON" in out
    assert "Always default to CSV" in out
    assert "Squash merges only" in out            # the negative control

    # and Neo's prompt has lost exactly the one
    store = NeoStore()
    try:
        prompt = neo_mod.build_system_prompt(store, "proj_a")
    finally:
        store.close()
    assert "Always default to CSV" not in prompt
    assert live["content"] in prompt


def test_cli_refuses_a_learning_retraction_with_a_blank_reason(jarvis_home, capsys):
    store = NeoStore()
    try:
        row = store.add_learning("Always default to CSV", project="proj_a")
    finally:
        store.close()

    code, _, err = _run(["neo", "retract", str(row["id"]), "--reason", "   "], capsys)
    assert code == 1
    assert "reason" in err

    store = NeoStore()
    try:
        assert [r["content"] for r in store.learnings("proj_a")] == [row["content"]]
    finally:
        store.close()


def test_cli_refuses_a_learning_retraction_of_an_unknown_id(jarvis_home, capsys):
    code, _, err = _run(["neo", "retract", "4242", "--reason", "superseded"], capsys)
    assert code == 1
    assert "4242" in err and "not found" in err


def test_cli_retracts_knowledge_and_keeps_listing_it(jarvis_home, capsys):
    store = CentralStore()
    try:
        dead = store.add_knowledge("deploy from the deploy branch", project="proj_a")
        store.add_knowledge("always run make lint", project="proj_a")
    finally:
        store.close()

    code, _, _ = _run(["learn", "retract", dead["id"],
                       "--reason", "we deploy from tags now"], capsys)
    assert code == 0

    code, out, _ = _run(["learn", "list", "--project", "proj_a"], capsys)
    assert code == 0
    assert "we deploy from tags now" in out
    assert "deploy from the deploy branch" in out
    assert "always run make lint" in out          # the negative control

    store = CentralStore()
    try:
        offered = [r["content"] for r in store.relevant_knowledge("proj_a")]
    finally:
        store.close()
    assert offered == ["always run make lint"]


def test_cli_refuses_a_knowledge_retraction_with_a_blank_reason(jarvis_home, capsys):
    store = CentralStore()
    try:
        row = store.add_knowledge("deploy from the deploy branch", project="proj_a")
    finally:
        store.close()

    code, _, err = _run(["learn", "retract", row["id"], "--reason", ""], capsys)
    assert code == 1
    assert "reason" in err

    store = CentralStore()
    try:
        assert len(store.relevant_knowledge("proj_a")) == 1
    finally:
        store.close()


def test_cli_refuses_a_knowledge_retraction_of_an_unknown_id(jarvis_home, capsys):
    code, _, err = _run(["learn", "retract", "kn-deadbeef", "--reason", "superseded"],
                        capsys)
    assert code == 1
    assert "kn-deadbeef" in err and "not found" in err


def test_cli_refuses_a_second_retraction(jarvis_home, capsys):
    store = NeoStore()
    try:
        row = store.add_learning("Always default to CSV", project="proj_a")
    finally:
        store.close()

    assert _run(["neo", "retract", str(row["id"]), "--reason", "moved to JSON"],
                capsys)[0] == 0
    code, _, err = _run(["neo", "retract", str(row["id"]), "--reason", "again"], capsys)
    assert code == 1
    assert "already retired" in err
