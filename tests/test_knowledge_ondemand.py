"""The knowledge base reaches workers as an index they query, not as a payload.

Pasting every relevant entry into the worker prompt made dispatch cost grow with the
size of the knowledge base — every work order in the fleet paying for every lesson ever
recorded — while the selector (most-recent-N) meant the entry that actually mattered
usually fell outside the window anyway. These tests pin the replacement: bounded prompt
cost, nothing silently invisible, and retrieval verbs that actually work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis.catalog import OsConfig, ProjectSpec, WorkerDefaults
from jarvis.central_store import PINNED_TAG, CentralStore, has_tag, headline
from jarvis.cli import build_parser, cmd_learn

SPEC = ProjectSpec(name="p1", path=Path("/tmp/p1"), worker=WorkerDefaults())


def _prompt(brief):
    from jarvis.dispatch import build_worker_prompt
    return build_worker_prompt({"id": "wo-1", "title": "t", "description": "d"}, SPEC, brief)


# -- headlines ----------------------------------------------------------------------

def test_headline_collapses_a_whole_memory_file_to_one_line():
    doc = "# Deploy runbook\n\n" + ("body line that goes on and on. " * 200)
    line = headline(doc)
    assert line == "# Deploy runbook"
    assert "\n" not in line


def test_headline_truncates_long_single_line_entries():
    line = headline("x" * 500)
    assert len(line) <= 160 and line.endswith("…")


# -- the brief ----------------------------------------------------------------------

def test_prompt_cost_is_bounded_by_the_budget_not_the_base_size(jarvis_home):
    """The whole point: 1000 entries must not cost more prompt than 40."""
    central = CentralStore()
    for i in range(40):
        central.add_knowledge(f"entry {i} " + "padding " * 60, project="p1",
                              topic=f"t{i % 5}")
    small = len(_prompt(central.knowledge_brief("p1")))
    for i in range(1000):
        central.add_knowledge(f"bulk {i} " + "padding " * 60, project="p1",
                              topic=f"t{i % 5}")
    big = len(_prompt(central.knowledge_brief("p1")))
    assert central.count_knowledge("p1") == 1040
    # 26x the knowledge, and the prompt must not have grown by more than a rounding
    # error (only the entry count and overflow tallies change width).
    assert big - small < 200, f"prompt grew {big - small} chars with the base"


def test_digest_covers_every_topic_rather_than_the_busiest_one(jarvis_home):
    """Round-robin selection, not straight recency: a map that omits whole topics
    cannot tell a worker that there is anything to look up."""
    central = CentralStore()
    central.add_knowledge("the one deploy rule", project="p1", topic="deploy")
    for i in range(200):  # a very busy topic recorded much later
        central.add_knowledge(f"ci note {i}", project="p1", topic="ci")
    brief = central.knowledge_brief("p1", digest_limit=20)
    topics = {k["topic"] for k in brief.digest}
    assert topics == {"deploy", "ci"}
    assert any("the one deploy rule" in k["headline"] for k in brief.digest)


def test_overflow_names_what_did_not_fit(jarvis_home):
    central = CentralStore()
    for i in range(60):
        central.add_knowledge(f"note {i}", project="p1", topic="ci")
    brief = central.knowledge_brief("p1", digest_limit=10)
    assert len(brief.digest) == 10
    assert brief.overflow == [("ci", 50)]
    assert brief.overflow_count == 50
    assert "50 further entries" in _prompt(brief)


def test_pinned_entries_are_injected_in_full(jarvis_home):
    central = CentralStore()
    long_rule = "NEVER force-push to main. " + "Here is the long rationale. " * 20
    central.add_knowledge(long_rule, project="p1", topic="git", tags=PINNED_TAG)
    central.add_knowledge("ordinary lesson " + "detail " * 40 + "TAIL-MARKER",
                          project="p1", topic="ci")
    brief = central.knowledge_brief("p1")
    assert [k["content"] for k in brief.pinned] == [long_rule]
    prompt = _prompt(brief)
    assert long_rule in prompt              # pinned: verbatim, however long
    assert "TAIL-MARKER" not in prompt      # unpinned: truncated to a headline
    assert "ordinary lesson" in prompt      # ...but still discoverable by id


def test_pinned_limit_caps_the_verbatim_tier(jarvis_home):
    central = CentralStore()
    for i in range(10):
        central.add_knowledge(f"rule {i}", project="p1", tags=PINNED_TAG)
    brief = central.knowledge_brief("p1", pinned_limit=3)
    assert len(brief.pinned) == 3
    # the ones that lost the cap fall back to the index rather than disappearing
    assert len(brief.pinned) + len(brief.digest) + brief.overflow_count == 10


def test_global_entries_reach_every_project_and_are_labelled(jarvis_home):
    central = CentralStore()
    central.add_knowledge("fleet-wide rule", project="", topic="ci")
    central.add_knowledge("p2 only", project="p2", topic="ci")
    prompt = _prompt(central.knowledge_brief("p1"))
    assert "fleet-wide rule" in prompt and "(global)" in prompt
    assert "p2 only" not in prompt


def test_empty_base_renders_no_block(jarvis_home):
    central = CentralStore()
    brief = central.knowledge_brief("p1")
    assert not brief
    assert "# Knowledge base" not in _prompt(brief)


# -- the prompt tells the worker to go and read ----------------------------------------

def test_prompt_teaches_on_demand_retrieval(jarvis_home):
    central = CentralStore()
    central.add_knowledge("something", project="p1", topic="ci")
    prompt = _prompt(central.knowledge_brief("p1"))
    assert "jarvis learn search" in prompt
    assert "jarvis learn show <id>" in prompt
    assert "INDEX, not the knowledge" in prompt
    # and the contract itself must carry the read verb, not only the write verb
    contract = prompt[prompt.index("# Operating contract"):prompt.index("# What the outside")]
    assert "jarvis learn search" in contract
    assert "jarvis learn add" in contract


# -- pinning ---------------------------------------------------------------------------

def test_pin_and_unpin_round_trip(jarvis_home):
    central = CentralStore()
    row = central.add_knowledge("rule", project="p1", tags="ci")

    def tags() -> str:
        fresh = central.get_knowledge(row["id"])
        assert fresh is not None
        return fresh["tags"]

    assert not has_tag(tags(), PINNED_TAG)
    central.pin_knowledge(row["id"])
    assert has_tag(tags(), PINNED_TAG) and has_tag(tags(), "ci")
    central.pin_knowledge(row["id"], pinned=False)
    assert not has_tag(tags(), PINNED_TAG) and has_tag(tags(), "ci")  # other tags survive
    assert central.pin_knowledge("kn-nope") is None


# -- retrieval verbs -------------------------------------------------------------------

def _run(argv: list[str]) -> None:
    cmd_learn(build_parser().parse_args(argv))


def test_search_scopes_to_project_plus_global(jarvis_home):
    central = CentralStore()
    central.add_knowledge("shared secret handling", project="")
    central.add_knowledge("p1 secret handling", project="p1")
    central.add_knowledge("p2 secret handling", project="p2")
    got = {r["content"] for r in central.search_knowledge("secret", project="p1")}
    assert got == {"shared secret handling", "p1 secret handling"}
    # unscoped search still spans the fleet — cross-project learnings are often the point
    assert len(central.search_knowledge("secret")) == 3


def test_search_filters_by_topic(jarvis_home):
    central = CentralStore()
    central.add_knowledge("deploy note", project="p1", topic="deploy")
    central.add_knowledge("ci note", project="p1", topic="ci")
    got = [r["content"] for r in central.search_knowledge("note", topic="ci")]
    assert got == ["ci note"]


def test_cli_show_returns_full_text_and_rejects_unknown_ids(jarvis_home, capsys):
    from jarvis.ops import OpsError
    central = CentralStore()
    body = "a long entry\nwith a second line that the index would have dropped"
    row = central.add_knowledge(body, project="p1")
    _run(["--json", "learn", "show", row["id"]])
    assert "second line" in capsys.readouterr().out
    with pytest.raises(OpsError):
        _run(["--json", "learn", "show", row["id"], "kn-missing"])


def test_cli_list_is_a_digest_unless_full(jarvis_home, capsys):
    central = CentralStore()
    central.add_knowledge("headline here\nhidden body text", project="p1")
    _run(["--json", "learn", "list", "--project", "p1"])
    out = capsys.readouterr().out
    assert "headline here" in out and "hidden body text" not in out
    _run(["--json", "learn", "list", "--project", "p1", "--full"])
    assert "hidden body text" in capsys.readouterr().out


def test_cli_topics_counts_entries(jarvis_home, capsys):
    central = CentralStore()
    central.add_knowledge("a", project="p1", topic="ci")
    central.add_knowledge("b", project="p1", topic="ci")
    central.add_knowledge("c", project="p1", topic="")
    _run(["--json", "learn", "topics", "--project", "p1"])
    out = capsys.readouterr().out
    assert '"topic": "ci"' in out and '"entries": 2' in out
    assert '"(no topic)"' in out


def test_cli_add_pin_flag(jarvis_home, capsys):
    _run(["--json", "learn", "add", "rule", "--project", "p1", "--pin", "--tags", "git"])
    central = CentralStore()
    row = central.search_knowledge("rule", project="p1")[0]
    assert has_tag(row["tags"], PINNED_TAG) and has_tag(row["tags"], "git")


# -- catalog knobs -----------------------------------------------------------------------

def test_catalog_exposes_the_budget(jarvis_home):
    from jarvis.catalog import parse_catalog
    cat = parse_catalog({"os": {"knowledge_inject_limit": 2,
                                "knowledge_digest_limit": 7,
                                "knowledge_digest_chars": 300}, "projects": []})
    assert (cat.os.knowledge_inject_limit, cat.os.knowledge_digest_limit,
            cat.os.knowledge_digest_chars) == (2, 7, 300)
    assert OsConfig().knowledge_digest_limit == 40
