"""The knowledge base reaches workers as an index they query, not as a payload.

Pasting every relevant entry into the worker prompt made dispatch cost grow with the
size of the knowledge base — every work order in the fleet paying for every lesson ever
recorded — while the selector (most-recent-N) meant the entry that actually mattered
usually fell outside the window anyway. These tests pin the replacement: bounded prompt
cost, nothing silently invisible, and retrieval verbs that actually work.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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


def test_empty_base_also_removes_the_instructions_to_read_it(jarvis_home):
    """No index, no instruction to consult one.

    Telling a worker to look something up in a knowledge base that is not in its prompt
    is worse than saying nothing: it sent a subject in evals/llm/test_worker_judgment.py
    (which briefs with an empty base) off to `jarvis learn search "branch name"` for a
    branch-naming call it should simply have recorded. Both knowledge bullets are
    therefore conditional, and the WRITE bullet is not — a worker with nothing to read
    still has everything to record.
    """
    from jarvis.dispatch import build_worker_prompt

    central = CentralStore()
    wo = {"id": "wo-1", "title": "t", "description": "d"}
    empty = build_worker_prompt(wo, SPEC, central.knowledge_brief("p1"))
    assert "jarvis learn search" not in empty
    assert "jarvis learn show" not in empty
    assert "LOOK IT UP FIRST" not in empty
    assert "jarvis learn add" in empty          # writing never depends on the base
    assert "jarvis wo ask" in empty             # ...and asking Neo is untouched
    # "WRITE to it" only parses when a READ bullet precedes it to be the "it"
    assert "WRITE to it" not in empty

    central.add_knowledge("something worth knowing", project="p1", topic="ci")
    stocked = build_worker_prompt(wo, SPEC, central.knowledge_brief("p1"))
    assert "jarvis learn search" in stocked     # negative control
    assert "LOOK IT UP FIRST" in stocked


# -- the prompt tells the worker to go and read ----------------------------------------

def test_prompt_teaches_on_demand_retrieval(jarvis_home):
    central = CentralStore()
    central.add_knowledge("something", project="p1", topic="ci")
    prompt = _prompt(central.knowledge_brief("p1"))
    assert "jarvis learn search" in prompt
    assert "jarvis learn show <id>" in prompt
    assert "INDEX, not the knowledge" in prompt
    # and the contract itself must carry the read verb, not only the write verb.
    # The core contract now ends where the on-demand section index begins
    # (worker_brief.section_index) — the record prose moved behind `jarvis brief`.
    contract = prompt[prompt.index("# Operating contract"):prompt.index("# Full briefings")]
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


def test_every_indexed_entry_is_retrievable_by_its_own_headline(jarvis_home):
    """The index is only worth its space if what it advertises can be cashed in.

    A worker sees the headline and nothing else, so the words it has to search with are
    the words in the headline. Both routes are checked here — by id, and by a term
    lifted from the headline — because an index whose entries cannot be fetched back is
    strictly worse than the bulk injection it replaced. The behavioural counterpart
    (does a model actually do this?) is evals/llm/test_knowledge_retrieval_judgment.py.
    """
    central = CentralStore()
    bodies = {
        "deploy": "Schema migrations never run inside the deploy step, because the "
                  "blue/green swap leaves two versions live against one database.",
        "billing": "Billing figures are stored in integer cents and rounded only at "
                   "the presentation layer, never in an exporter's own arithmetic.",
        "jobs": "Scheduled jobs are registered with an explicit idempotency key, "
                "never as a bare cron entry, because delivery is at-least-once.",
    }
    for topic, body in bodies.items():
        central.add_knowledge(body + " " + "padding text. " * 40, project="p1",
                              topic=topic)

    for k in central.knowledge_brief("p1").digest:
        assert central.get_knowledge(k["id"]) is not None, "id in the index is dead"
        # the least distinctive thing a worker could reasonably search: the headline's
        # first real word
        term = k["headline"].split()[0]
        hits = central.search_knowledge(term, project="p1")
        assert any(h["id"] == k["id"] for h in hits), \
            f"headline word {term!r} does not retrieve {k['id']}"


def test_retracted_entries_leave_the_index_not_just_the_payload(jarvis_home):
    """Retraction has to remove a ruling from the MAP as well as the prompt text.

    A superseded headline left in the index is worse than a superseded entry pasted in
    full: the worker reads it, believes the OS knows something, and goes and fetches it.
    """
    central = CentralStore()
    live = central.add_knowledge("deploy with the release script", project="p1",
                                 topic="deploy")
    dead = central.add_knowledge("deploy by hand with systemctl", project="p1",
                                 topic="deploy")
    central.retract_knowledge(dead["id"], reason="superseded by the release script")

    brief = central.knowledge_brief("p1")
    indexed = {k["id"] for k in brief.digest}
    assert live["id"] in indexed          # negative control: the filter isn't a no-op
    assert dead["id"] not in indexed
    assert brief.total == 1
    prompt = _prompt(brief)
    assert "by hand with systemctl" not in prompt
    # ...and it is still on the record for the audit surfaces
    assert any(r["id"] == dead["id"] for r in central.search_knowledge("systemctl"))


def test_retracted_pinned_entry_stops_being_injected(jarvis_home):
    """Pinning is the loudest tier, so it is the one where a stale ruling does most
    damage: a retracted rail must stop riding along verbatim."""
    central = CentralStore()
    rail = central.add_knowledge("NEVER deploy on a Friday", project="p1", topic="deploy",
                                 tags=PINNED_TAG)
    keep = central.add_knowledge("ALWAYS run the smoke suite", project="p1",
                                 topic="deploy", tags=PINNED_TAG)
    central.retract_knowledge(rail["id"], reason="we deploy continuously now")

    prompt = _prompt(central.knowledge_brief("p1"))
    assert "ALWAYS run the smoke suite" in prompt   # negative control
    assert "NEVER deploy on a Friday" not in prompt


def test_retired_topics_do_not_advertise_themselves(jarvis_home):
    """A topic whose only entries were retracted must not appear in the overflow
    roll-call — it would send a worker looking for something that is not there."""
    central = CentralStore()
    gone = central.add_knowledge("the old way", project="p1", topic="legacy")
    central.add_knowledge("the current way", project="p1", topic="deploy")
    central.retract_knowledge(gone["id"], reason="removed")
    topics = dict(central.knowledge_topics("p1"))
    assert topics == {"deploy": 1}


def test_search_scopes_to_project_plus_global(jarvis_home):
    central = CentralStore()
    central.add_knowledge("shared secret handling", project="")
    central.add_knowledge("p1 secret handling", project="p1")
    central.add_knowledge("p2 secret handling", project="p2")
    got = {r["content"] for r in central.search_knowledge("secret", project="p1")}
    assert got == {"shared secret handling", "p1 secret handling"}
    # unscoped search still spans the fleet — cross-project learnings are often the point
    assert len(central.search_knowledge("secret")) == 3


def test_multi_word_search_ors_and_ranks(jarvis_home):
    """Agents search in phrases, not keywords.

    Under whole-phrase `LIKE '%cents rounding format%'` every one of these queries
    returned NOTHING, and the retrieval eval scored 2/7 for that reason alone — the
    worker asked the right question and the store said "never heard of it". Words are
    ORed and rows ranked by how many matched, so a phrase where only some words land
    still retrieves, best match first.
    """
    central = CentralStore()
    central.add_knowledge(
        "Billing figures are stored in integer cents and rounded for display ONLY at "
        "the presentation layer; exports must go through billing.format_amount.",
        project="p1", topic="billing")
    central.add_knowledge(
        "Generated artifacts go to the reports bucket under a dated prefix, never to "
        "the application's own filesystem.", project="p1", topic="storage")
    central.add_knowledge("Pull requests are squash-merged.", project="p1",
                          topic="process")

    # "rounding" matches nothing (the entry says "rounded") — the query still lands on
    # the strength of its other words
    hits = central.search_knowledge("cents rounding format", project="p1")
    assert hits and hits[0]["topic"] == "billing"
    hits = central.search_knowledge("artifacts storage report output", project="p1")
    assert hits and hits[0]["topic"] == "storage"

    # ranking, not just recall: the row matching more of the query comes first
    ranked = central.search_knowledge("billing cents exports", project="p1")
    assert ranked[0]["topic"] == "billing"

    # a single word behaves exactly as it always did, and a genuine miss still misses
    assert len(central.search_knowledge("squash-merged", project="p1")) == 1
    assert central.search_knowledge("kubernetes helm chart", project="p1") == []
    # the empty term stays the "everything" read that `learn list` and the UI rely on
    assert len(central.search_knowledge("", project="p1")) == 3
    # the score is how the list is ordered, not a field of an entry: it must not leak
    # into the UI, `--json` output, or anything that round-trips a row
    assert "_score" not in ranked[0]


def test_search_stems_so_a_different_inflection_still_retrieves(jarvis_home):
    """The gap FTS5 was added to close: "rounding" finding an entry that says "rounded".

    Under substring matching alone this query retrieved the entry only by riding along
    with its other words (kn-b02bd307). On its own it returned nothing.
    """
    central = CentralStore()
    central.add_knowledge("Figures are rounded at the presentation layer.",
                          project="p1", topic="billing")
    central.add_knowledge("Scheduled jobs register an idempotency key.",
                          project="p1", topic="jobs")

    assert [r["topic"] for r in central.search_knowledge("rounding", project="p1")] \
        == ["billing"]
    assert [r["topic"] for r in central.search_knowledge("scheduling", project="p1")] \
        == ["jobs"]


def test_search_ranks_by_bm25_not_by_how_many_words_appeared(jarvis_home):
    """Ranking is the other half, and it needs a case word-counting CANNOT get right.

    Both entries contain both query words, so the substring tier scores them equal and
    falls back to recency — which puts the padded one first. BM25 sees that the other
    says the same thing in a tenth of the space, and the assertion below is the
    difference: with the FTS tier off, this exact query returns them the other way up.
    """
    central = CentralStore()
    central.add_knowledge("Scheduled reports are chunked by day.",
                          project="p1", topic="reports")
    central.add_knowledge(
        "Nightly export. " + "unrelated padding sentence. " * 200
        + " It is scheduled by the daemon and lands beside the report.",
        project="p1", topic="exports")  # more recent, so recency alone puts it first

    assert central.search_knowledge(
        "scheduled report", project="p1")[0]["topic"] == "reports"

    central.fts = False  # the tier under test, removed
    assert central.search_knowledge(
        "scheduled report", project="p1")[0]["topic"] == "exports"


def test_fts_never_retrieves_less_than_substring_matching_did(jarvis_home):
    """The floor (spec §2, §3). Porter stems "deploy" and "deployment" apart, so pure
    FTS5 would answer this query with nothing — and a worker reads nothing as "the OS
    has never heard of this"."""
    central = CentralStore()
    central.add_knowledge("Deployment runs from a release tag, never from main.",
                          project="p1", topic="releases")
    central.add_knowledge("Kubernetes manifests live beside the chart.",
                          project="p1", topic="infra")

    assert [r["topic"] for r in central.search_knowledge("deploy", project="p1")] \
        == ["releases"]
    assert [r["topic"] for r in central.search_knowledge("kube", project="p1")] \
        == ["infra"]


def test_search_does_not_raise_on_what_a_user_actually_types(jarvis_home):
    """`-`, `:`, `*`, `"` and bare `(` are FTS5 SYNTAX. This is a read verb reached from
    the CLI, the dashboard and every worker: it returns rows or none, never a traceback.
    """
    central = CentralStore()
    central.add_knowledge("PRs #1-#2 are unmerged.", project="p1", topic="process")

    for hostile in ('PRs #1-', '-deploy', 'NEAR("a"', 'topic:billing', 'quote " mark',
                    'star*', '^caret', '---', '(', '*', ':', '"'):
        central.search_knowledge(hostile, project="p1")  # must not raise

    assert len(central.search_knowledge("PRs #1-", project="p1")) == 1


def test_the_index_tracks_every_writer_not_just_add_knowledge(jarvis_home):
    """Triggers, not a call in `add_knowledge` — `retract_knowledge`,
    `record_memory_file` and `set_knowledge_tags` write to the table directly (spec §4).
    """
    central = CentralStore()
    row = central.add_knowledge("Figures are rounded at the presentation layer.",
                                project="p1", topic="billing")

    central.retract_knowledge(row["id"], "superseded")
    hits = central.search_knowledge("rounding", project="p1")
    assert [h["id"] for h in hits] == [row["id"]], "retraction lost the row from the index"
    assert hits[0]["retired_reason"] == "superseded"

    central.record_memory_file("Exports are chunked by day.", project="p1", topic="mem")
    assert central.search_knowledge("chunking", project="p1")

    tagged = central.add_knowledge("Nightly export.", project="p1", topic="exports")
    central.set_knowledge_tags(tagged["id"], "nightly")
    assert [h["id"] for h in central.search_knowledge("nightly", project="p1")] \
        == [tagged["id"]]

    central.conn.execute("DELETE FROM knowledge WHERE id=?", (tagged["id"],))
    assert central.search_knowledge("nightly", project="p1") == []


def test_an_existing_os_db_is_backfilled_once(jarvis_home):
    """`executescript(SCHEMA)` is a no-op on a database that already has its tables, so
    a live `os.db` gets the index by rebuild — the same problem ADDED_COLUMNS solves for
    columns (spec §4)."""
    central = CentralStore()
    central.add_knowledge("Figures are rounded at the presentation layer.",
                          project="p1", topic="billing")
    # a store from before the index existed
    central.conn.executescript(
        "DROP TRIGGER knowledge_fts_ai; DROP TRIGGER knowledge_fts_au;"
        " DROP TRIGGER knowledge_fts_ad; DROP TABLE knowledge_fts;")
    central.conn.execute("DELETE FROM os_state WHERE key='knowledge_fts_built'")
    central.close()

    reopened = CentralStore()
    assert reopened.fts
    assert [r["topic"] for r in reopened.search_knowledge("rounding", project="p1")] \
        == ["billing"], "pre-existing entries were not indexed"

    # and the backfill is once, not on every open: a third open must not rebuild
    stamp = reopened.get_state("knowledge_fts_built")
    reopened.close()
    assert CentralStore().get_state("knowledge_fts_built") == stamp


def test_search_still_works_when_sqlite_has_no_fts5(jarvis_home, monkeypatch):
    """A search as good as yesterday's is not an outage; a store that will not open is
    (spec §8)."""
    central = CentralStore()
    central.add_knowledge("Figures are rounded at the presentation layer.",
                          project="p1", topic="billing")
    central.fts = False

    assert central.search_knowledge("rounded", project="p1")     # substring tier alone
    assert central.search_knowledge("rounding", project="p1") == []  # no stemming left
    assert len(central.search_knowledge("", project="p1")) == 1


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


# -- `jarvis learn search` is an index, not a payload ------------------------------------
#
# Spec: docs/superpowers/specs/2026-10-02-learn-search-returns-an-index.md. `search` was
# the one retrieval verb with no index form, and it printed every matching body in full.


BODY = ("deploy runs from the tag\n"
        "the second line names the gate it must pass\n"
        + "padding that nobody should ever be charged for. " * 40)


def test_search_rows_never_carry_a_body(jarvis_home, capsys):
    """Spec test 1 — the acceptance test. A truncated body would still be a body."""
    central = CentralStore()
    central.add_knowledge(BODY, project="p1", topic="releases")
    _run(["--json", "learn", "search", "deploy", "--project", "p1"])
    out = capsys.readouterr().out
    rows = json.loads(out)
    assert rows and all("content" not in r for r in rows)
    assert "padding that nobody should ever be charged for" not in out
    assert rows[0]["headline"] == "deploy runs from the tag"


def test_search_still_records_a_hit_on_every_entry_it_named(jarvis_home, capsys):
    """Spec test 3 — `search` stays in AIMED_VERBS."""
    central = CentralStore()
    row = central.add_knowledge(BODY, project="p1", topic="releases")
    _run(["--json", "learn", "search", "deploy", "--project", "p1"])
    capsys.readouterr()
    assert central.knowledge_hit_counts() == {row["id"]: 1}


def test_an_excerpt_quotes_the_body_line_that_matched(jarvis_home, capsys):
    """Spec test 4, case 1."""
    central = CentralStore()
    central.add_knowledge(BODY, project="p1", topic="releases")
    _run(["--json", "learn", "search", "gate", "--project", "p1"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["excerpt"] == "the second line names the gate it must pass"


def test_a_long_matching_line_is_bounded_like_a_headline(jarvis_home, capsys):
    """Spec test 4, case 2 — `headline()` IS the bound; no new constant."""
    central = CentralStore()
    central.add_knowledge("short first line\n" + "gate " * 200, project="p1")
    _run(["--json", "learn", "search", "gate", "--project", "p1"])
    row = json.loads(capsys.readouterr().out)[0]
    assert len(row["excerpt"]) <= 160 and row["excerpt"].endswith("…")


def test_a_match_only_in_the_first_line_yields_no_excerpt(jarvis_home, capsys):
    """Spec test 4, case 3 — the headline already shows it."""
    central = CentralStore()
    central.add_knowledge("deploy from the tag\nnothing else matches here", project="p1")
    _run(["--json", "learn", "search", "deploy", "--project", "p1"])
    row = json.loads(capsys.readouterr().out)[0]
    assert "excerpt" not in row and row["headline"] == "deploy from the tag"


def test_a_hit_with_no_matching_body_line_still_returns_the_row(jarvis_home, capsys):
    """Spec test 4, case 4 — a topic hit has nothing to quote."""
    central = CentralStore()
    central.add_knowledge("one line only", project="p1", topic="releases")
    _run(["--json", "learn", "search", "releases", "--project", "p1"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and "excerpt" not in rows[0]


def test_an_index_row_prices_the_fetch(jarvis_home, capsys):
    """Spec test 6 — `chars` is the body length, so `show <id>` can be priced first."""
    central = CentralStore()
    central.add_knowledge(BODY, project="p1", topic="releases")
    _run(["--json", "learn", "search", "deploy", "--project", "p1"])
    row = json.loads(capsys.readouterr().out)[0]
    assert row["chars"] == len(BODY)


def test_no_prompt_promises_that_search_returns_full_text(jarvis_home):
    """Spec test 7 — every prompt string that advertised bodies from `search`."""
    from jarvis import dispatch, worker_brief

    central = CentralStore()
    central.add_knowledge("something worth knowing", project="p1", topic="ci")
    brief = central.knowledge_brief("p1")
    rendered = [
        _prompt(brief),
        "\n".join(dispatch.render_knowledge_block(brief, "p1")),
        worker_brief.knowledge_section("p1"),
        worker_brief.contract_section("wo-brief01", "p1"),
    ]
    for text in rendered:
        assert "full text of matches" not in text
    # `show` keeps the promise, and the contract keeps the literal substring
    assert "full text of specific entries" in rendered[1]
    assert "jarvis learn search" in rendered[3]


# -- half 2: title-matched hints at dispatch ---------------------------------------------


def _stocked(central, n=60, project="p1"):
    """A base deep enough that the oldest entries fall into overflow."""
    for i in range(n):
        central.add_knowledge(f"bulk {i} " + "padding " * 20, project=project,
                              topic=f"t{i % 5}")


def test_a_brief_with_no_title_has_no_hints(jarvis_home):
    """Spec test 8 — the default must stay byte-identical (validation.py, _index_cost)."""
    central = CentralStore()
    central.add_knowledge("deploy runs from the tag", project="p1", topic="releases")
    brief = central.knowledge_brief("p1")
    assert brief.hints == []
    assert "TITLE" not in _prompt(brief)


def test_a_title_matched_overflow_entry_reaches_the_prompt(jarvis_home):
    """Spec test 9 — the 586-entry overflow is where the relevant entry was hiding."""
    central = CentralStore()
    target = central.add_knowledge(
        "the gate matches the shipit command byte for byte", project="p1", topic="gates")
    _stocked(central)
    plain = central.knowledge_brief("p1", digest_limit=5, digest_chars=600)
    assert target["id"] not in {r["id"] for r in plain.digest}

    hinted = central.knowledge_brief("p1", digest_limit=5, digest_chars=600,
                                     title="the shipit gate matches byte for byte")
    assert target["id"] in {r["id"] for r in hinted.hints}
    assert target["id"] in _prompt(hinted)


def test_dispatch_passes_the_work_order_title_and_the_hint_bounds(
        jarvis_home, fake_claude, project, catalog_file):
    """Spec test 9, the wiring half: the real launch path asks for hints."""
    from jarvis import dispatch, ops
    from jarvis.catalog import load_catalog
    from jarvis.project_store import ProjectStore

    ops.start_os(str(catalog_file), foreground=True)
    cat = load_catalog(catalog_file)
    ops.create_work_order("proj_a", "the shipit gate matches byte for byte")
    store = ProjectStore(project)
    central = CentralStore()
    central.add_knowledge("the gate matches the shipit command byte for byte",
                          project="proj_a", topic="gates")
    seen: dict[str, Any] = {}
    real = central.knowledge_brief

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    central.knowledge_brief = spy  # type: ignore[method-assign]
    wo = store.claim_next_pending()
    try:
        dispatch.dispatch_work_order(store, central, cat.projects[0], wo,
                                     os_config=cat.os)
    finally:
        store.close()
        central.close()
    assert seen["title"] == "the shipit gate matches byte for byte"
    assert seen["hint_limit"] == cat.os.knowledge_hint_limit
    assert seen["hint_chars"] == cat.os.knowledge_hint_chars


def test_an_entry_already_in_the_index_is_not_repeated_as_a_hint(jarvis_home):
    """Spec test 10."""
    central = CentralStore()
    indexed = central.add_knowledge("the shipit gate matches byte for byte",
                                    project="p1", topic="gates")
    pinned = central.add_knowledge("never ship a byte without the gate", project="p1",
                                   topic="gates", tags=PINNED_TAG)
    brief = central.knowledge_brief("p1", title="the shipit gate matches byte for byte")
    assert indexed["id"] in {r["id"] for r in brief.digest}
    assert pinned["id"] in {r["id"] for r in brief.pinned}
    assert brief.hints == []


def test_the_hint_budget_is_separate_from_the_digest_budget(jarvis_home):
    """Spec test 11 — bounds hold and the digest is the same size either way."""
    central = CentralStore()
    for i in range(6):
        central.add_knowledge(f"the shipit gate matches byte for byte, case {i} "
                              + "padding " * 20, project="p1", topic="gates")
    _stocked(central)
    plain = central.knowledge_brief("p1", digest_limit=5, digest_chars=600)
    hinted = central.knowledge_brief("p1", digest_limit=5, digest_chars=600,
                                     title="the shipit gate matches byte for byte",
                                     hint_limit=3, hint_chars=400)
    assert len(hinted.hints) <= 3
    assert sum(len(h["headline"]) for h in hinted.hints) <= 400
    assert [r["id"] for r in hinted.digest] == [r["id"] for r in plain.digest]

    tight = central.knowledge_brief("p1", digest_limit=5, digest_chars=600,
                                    title="the shipit gate matches byte for byte",
                                    hint_limit=3, hint_chars=40)
    assert len(tight.hints) < len(hinted.hints)


def test_a_short_title_gets_no_hints(jarvis_home):
    """Spec test 12 — below MISSED_MIN_WORDS the query matches half the base."""
    from jarvis.central_store import MISSED_MIN_WORDS

    central = CentralStore()
    central.add_knowledge("the shipit gate matches byte for byte", project="p1",
                          topic="gates")
    _stocked(central)                      # pushes the target out of the digest
    bounds = {"digest_limit": 5, "digest_chars": 600}
    short = " ".join("gate matches byte for byte".split()[:MISSED_MIN_WORDS - 1])
    assert central.knowledge_brief("p1", title=short, **bounds).hints == []
    assert central.knowledge_brief("p1", title="gate matches byte", **bounds).hints != []


def test_a_hint_is_not_filtered_by_when_the_order_was_created(jarvis_home):
    """Spec test 13 — the report's `ts <= created_at` filter does NOT apply here."""
    central = CentralStore()
    _stocked(central)
    fresh = central.add_knowledge("the shipit gate matches byte for byte", project="p1",
                                  topic="gates")
    # a newer entry in the same topic, so round-robin indexes that one and `fresh` is in
    # overflow — the case this hint tier exists for
    central.add_knowledge("gates are reviewed by Neo", project="p1", topic="gates")
    brief = central.knowledge_brief("p1", digest_limit=5, digest_chars=600,
                                    title="the shipit gate matches byte for byte")
    assert fresh["id"] in {r["id"] for r in brief.hints}


def test_the_hint_block_says_title_match_and_says_hint(jarvis_home):
    """Spec test 14 — a lexical coincidence must not read as an instruction."""
    from jarvis.dispatch import render_knowledge_block

    central = CentralStore()
    central.add_knowledge("the shipit gate matches byte for byte\n" + "body " * 100,
                          project="p1", topic="gates")
    _stocked(central)
    brief = central.knowledge_brief("p1", digest_limit=5, digest_chars=600,
                                    title="the shipit gate matches byte for byte")
    text = "\n".join(render_knowledge_block(brief, "p1"))
    block = text[text.index("TITLE"):]
    assert "HINT" in block and "not an instruction" in block
    assert brief.hints and all(h["content"] not in text for h in brief.hints)
