"""Token accounting read back from Claude Code's transcripts.

The module under test is a parser for a file format Jarvis does not own, so these
tests are written against the shapes that format actually produces — above all the
streaming duplicate (`test_streamed_duplicates_...`), which is not a hypothetical:
the first hand measurement of wo-cd73c537 reported 2.7M cache-write tokens where the
true figure is 1.03M, because every assistant message appears in the transcript two
or three times and summing the rows counts its input once per copy.

Most assertions here are PAIRED — the case that should count sits in the same test as
the case that should not. "Returns one message" is indistinguishable from "the parser
is broken and returns one message for everything" unless something in the same test
proves it can return two.
"""

from __future__ import annotations

import json

import pytest

from jarvis import usage


def row(mid: str, *, write: int = 0, read: int = 0, out: int = 0, plain: int = 0,
        model: str = "claude-opus-5") -> dict:
    """One assistant row, in the shape Claude Code writes."""
    return {
        "type": "assistant",
        "timestamp": "2026-08-09T00:00:00.000Z",
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": plain,
                "cache_creation_input_tokens": write,
                "cache_read_input_tokens": read,
                "output_tokens": out,
            },
        },
    }


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A fake `~/.claude/projects` tree, with a helper that writes one session into it."""
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))

    def write(session_id: str, rows: list[dict], *, slug: str = "-proj",
              subagents: dict[str, list[dict]] | None = None):
        project_dir = root / slug
        project_dir.mkdir(exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))
        for name, sub_rows in (subagents or {}).items():
            sub_dir = project_dir / session_id / "subagents"
            sub_dir.mkdir(parents=True, exist_ok=True)
            (sub_dir / f"{name}.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in sub_rows))
        return path

    return write


# -- the streaming duplicate ----------------------------------------------------------


def test_streamed_duplicates_count_once_but_distinct_messages_both_count(transcripts):
    """The trap that inflated the first measurement of this very work order 2.6x.

    One message id written three times as it streams: identical cache figures, growing
    `output_tokens`. It must contribute its input ONCE and its output at the FINAL
    value, not the first (the opening copy reports `output_tokens: 1`).

    Paired with a second, distinct message id in the same transcript — without it,
    `cache_write == 500` would also be satisfied by a parser that ignored every row
    after the first.
    """
    transcripts("s1", [
        row("msg_a", write=500, read=0, out=1),
        row("msg_a", write=500, read=0, out=140),
        row("msg_a", write=500, read=0, out=140),
        row("msg_b", write=200, read=500, out=60),
    ])
    total = usage.read_session("s1").total

    assert total.messages == 2                 # not 4
    assert total.cache_write == 700            # 500 + 200, not 1500 + 200
    assert total.output == 200                 # 140 + 60: the max copy, not the first
    assert total.cache_read == 500


def test_output_takes_the_max_not_the_last_row(transcripts):
    """Rows are not guaranteed to arrive in ascending order of completeness."""
    transcripts("s1", [
        row("msg_a", out=900),
        row("msg_a", out=12),
    ])
    assert usage.read_session("s1").total.output == 900


# -- the re-write tax -----------------------------------------------------------------


def test_rewrite_excess_counts_only_what_was_written_twice(transcripts):
    """A session that re-writes its prefix reports the excess; a clean one reports zero.

    Both halves in one test: `rewrite_excess == 0` on its own would pass just as well
    against a property that always returns zero.
    """
    # Clean: context grows monotonically, each token written exactly once.
    transcripts("clean", [
        row("m1", write=1000, read=0),
        row("m2", write=500, read=1000),
        row("m3", write=300, read=1500),
    ])
    clean = usage.read_session("clean").total
    assert clean.context_peak == 1800          # 300 + 1500
    assert clean.cache_write == 1800
    assert clean.rewrite_excess == 0

    # Re-written: the third call re-sends the whole 1500-token prefix as a write.
    transcripts("cold", [
        row("m1", write=1000, read=0),
        row("m2", write=500, read=1000),
        row("m3", write=1500, read=0),
    ])
    cold = usage.read_session("cold").total
    assert cold.context_peak == 1500
    assert cold.cache_write == 3000
    assert cold.rewrite_excess == 1500


def test_rewrite_cost_is_the_difference_between_writing_and_reading(transcripts):
    """The tax is what the re-sent tokens cost ABOVE reading them, not their full price."""
    transcripts("s1", [
        row("m1", write=1_000_000, read=0),
        row("m2", write=1_000_000, read=0),
    ])
    total = usage.read_session("s1").total
    assert total.rewrite_excess == 1_000_000
    # 1M tokens at Opus $5/MTok: written at 1.25x = $6.25, read at 0.1x = $0.50.
    assert total.rewrite_cost_usd == pytest.approx(5.75)


# -- turn boundaries ------------------------------------------------------------------


def test_a_boundary_is_the_cache_going_backwards_not_a_big_write(transcripts):
    """Boundaries are counted from cache_read DROPPING, so no threshold is involved.

    The rising-read call in the middle is the control: it writes just as much as the
    dropping one, and must not count.
    """
    transcripts("s1", [
        row("m1", write=900, read=0),      # first call: nothing to drop from
        row("m2", write=900, read=900),    # read rose — same write size, not a boundary
        row("m3", write=900, read=200),    # read dropped — a boundary
        row("m4", write=100, read=1100),   # rose again
        row("m5", write=900, read=300),    # dropped — a second boundary
    ])
    assert usage.read_session("s1").total.resume_boundaries == 2


def test_a_single_turn_session_has_no_boundaries(transcripts):
    transcripts("s1", [row("m1", write=100), row("m2", write=50, read=100)])
    assert usage.read_session("s1").total.resume_boundaries == 0


# -- subagents ------------------------------------------------------------------------


def test_subagents_are_counted_separately_and_included_in_the_total(transcripts):
    """A third of the planner's bill was subagents, so they get their own line.

    Asserting the split AND the sum: reporting them only inside the total would hide
    the thing worth acting on, and reporting them only separately would understate
    what the work order cost.
    """
    transcripts("s1", [row("m1", write=1000, out=10)], subagents={
        "agent-aaa": [row("s1m1", write=300, out=5)],
        "agent-bbb": [row("s2m1", write=200, out=7)],
    })
    session = usage.read_session("s1")

    assert session.subagent_count == 2
    assert session.main.cache_write == 1000
    assert session.subagents.cache_write == 500
    assert session.total.cache_write == 1500
    assert session.total.output == 22


def test_an_empty_subagent_transcript_is_not_counted_as_an_agent(transcripts):
    """Claude Code leaves behind stub files for agents that produced nothing."""
    transcripts("s1", [row("m1", write=100)], subagents={"agent-empty": []})
    assert usage.read_session("s1").subagent_count == 0


# -- missing evidence -----------------------------------------------------------------


def test_a_missing_transcript_is_not_found_rather_than_free(transcripts):
    """Unmeasurable and zero are different answers and must not render the same."""
    transcripts("present", [row("m1", write=100, out=10)])

    missing = usage.read_session("nope")
    assert missing.found is False
    assert missing.total.list_cost_usd == 0.0

    present = usage.read_session("present")
    assert present.found is True
    assert present.total.list_cost_usd > 0


def test_a_work_order_with_no_session_id_reports_not_found(transcripts):
    """A work order that never got a session must not match a transcript by accident."""
    transcripts("", [row("m1", write=100)], slug="-oddly-named")
    assert usage.read_session("").found is False


# -- pricing --------------------------------------------------------------------------


def test_synthetic_messages_are_free_but_unknown_models_are_not():
    """`<synthetic>` is Claude Code's own placeholder for an API error it rendered as a
    turn — never billed. An unrecognised REAL model must not take the same path: it
    falls back to the Opus rate, because silently pricing a new model at zero would
    make the report quietly wrong the week a model ships."""
    assert usage.price_for("<synthetic>") == (0.0, 0.0)
    assert usage.price_for("claude-opus-5") == (5.0, 25.0)
    assert usage.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert usage.price_for("claude-sonnet-5") == (3.0, 15.0)
    assert usage.price_for("claude-something-unreleased") == usage.DEFAULT_PRICE


def test_cost_is_priced_per_message_model_not_per_session(transcripts):
    """A session that mixes models must not price the cheap half at the dear rate."""
    transcripts("s1", [
        row("m1", plain=1_000_000, out=1_000_000, model="claude-opus-5"),
        row("m2", plain=1_000_000, out=1_000_000, model="claude-haiku-4-5"),
    ])
    total = usage.read_session("s1").total
    assert total.cost_by_model["claude-opus-5"] == pytest.approx(30.0)   # 5 + 25
    assert total.cost_by_model["claude-haiku-4-5"] == pytest.approx(6.0)  # 1 + 5
    assert total.list_cost_usd == pytest.approx(36.0)


def test_cache_reads_are_a_tenth_and_writes_are_a_quarter_more(transcripts):
    transcripts("s1", [row("m1", read=1_000_000)])
    assert usage.read_session("s1").total.list_cost_usd == pytest.approx(0.5)
    transcripts("s2", [row("m1", write=1_000_000)])
    assert usage.read_session("s2").total.list_cost_usd == pytest.approx(6.25)


# -- merging --------------------------------------------------------------------------


def test_merging_sums_the_tax_but_takes_the_peak():
    """Two sessions' re-write excesses add; their context peaks do not.

    Summing the peaks would let a merged total's excess be computed against a context
    no single session ever reached, which is why `rewrite_excess` is stored per session
    rather than derived from merged figures.
    """
    a = usage.Usage(cache_write=300, context_peak=100, rewrite_excess=200)
    b = usage.Usage(cache_write=500, context_peak=400, rewrite_excess=100)
    merged = a + b
    assert merged.rewrite_excess == 300
    assert merged.context_peak == 400
    assert merged.cache_write == 800


def test_rows_without_usage_are_ignored(transcripts):
    """Tool results, hooks and UI state make up most of a transcript.

    The `custom-title` row carries the literal string "usage" to exercise the cheap
    pre-filter `_assistant_messages` uses to skip most lines without parsing them: a
    row that survives the substring check must still be rejected on its type.
    """
    transcripts("s1", [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "attachment", "attachment": {"type": "task_reminder"}},
        {"type": "custom-title", "customTitle": 'the "usage" of the thing'},
        row("m1", write=100, out=10),
    ])
    assert usage.read_session("s1").total.messages == 1
