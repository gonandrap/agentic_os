"""Token accounting read back from Claude Code's transcripts"""

from __future__ import annotations

import json
from typing import Sequence

import pytest

from jarvis import usage

#: `read_session` takes the cold-prefix floor as a REQUIRED argument
FLOOR = 5_000


def row(mid: str, *, write: int = 0, read: int = 0, out: int = 0, plain: int = 0,
        model: str = "claude-opus-5", ttl_1h: int = 0, ttl_5m: int = 0,
        at: str = "2026-08-09T00:00:00.000Z",
        stop_reason: str | None = "end_turn") -> dict:
    """One assistant row, in the shape Claude Code writes (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §1)"""
    usage_obj: dict = {
        "input_tokens": plain,
        "cache_creation_input_tokens": write,
        "cache_read_input_tokens": read,
        "output_tokens": out,
    }
    if ttl_1h or ttl_5m:
        usage_obj["cache_creation"] = {"ephemeral_1h_input_tokens": ttl_1h,
                                       "ephemeral_5m_input_tokens": ttl_5m}
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {"id": mid, "model": model, "usage": usage_obj,
                    "stop_reason": stop_reason},
    }


@pytest.fixture()
def transcripts(tmp_path, monkeypatch):
    """A fake `~/.claude/projects` tree, with a helper that writes one session into it"""
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


# -- the streaming duplicate


def test_streamed_duplicates_count_once_but_distinct_messages_both_count(transcripts):
    """The trap that inflated the first measurement of this very work order 2.6x"""
    transcripts("s1", [
        row("msg_a", write=500, read=0, out=1),
        row("msg_a", write=500, read=0, out=140),
        row("msg_a", write=500, read=0, out=140),
        row("msg_b", write=200, read=500, out=60),
    ])
    total = usage.read_session("s1", FLOOR).total

    assert total.messages == 2                 # not 4
    assert total.cache_write == 700            # 500 + 200, not 1500 + 200
    assert total.output == 200                 # 140 + 60: the max copy, not the first
    assert total.cache_read == 500


def test_a_message_whose_every_row_is_mid_stream_is_counted_not_estimated(transcripts):
    """The defect this parser can only DISCLOSE: a message with no sealed row at all (docs/superpowers/specs/2026-10-08-subagent-output-attribution.md §1)"""
    path = transcripts("s1", [
        row("msg_a", write=1_000, read=2_000, out=5, stop_reason=None),
        row("msg_a", write=1_000, read=2_000, out=5, stop_reason=None),
        row("msg_b", write=100, read=200, out=700),
    ])
    total = usage.read_session("s1", FLOOR).total

    assert total.placeholder_calls == 1
    assert total.placeholder_output == 5
    assert total.output == 705          # 5 + 700: the MAX merge, nothing synthesised
    assert [call.sealed for call in usage.calls_of(path)] == [False, True]
    assert total.as_dict()["placeholder_calls"] == 1


def test_output_takes_the_max_not_the_last_row(transcripts):
    """Rows are not guaranteed to arrive in ascending order of completeness"""
    transcripts("s1", [
        row("msg_a", out=900),
        row("msg_a", out=12),
    ])
    assert usage.read_session("s1", FLOOR).total.output == 900


# -- the re-write tax


def test_rewrite_excess_counts_only_what_was_written_twice(transcripts):
    """A session that re-writes its prefix reports the excess; a clean one reports zero"""
    # Clean: context grows monotonically, each token written exactly once
    transcripts("clean", [
        row("m1", write=1000, read=0),
        row("m2", write=500, read=1000),
        row("m3", write=300, read=1500),
    ])
    clean = usage.read_session("clean", FLOOR).total
    assert clean.context_peak == 1800          # 300 + 1500
    assert clean.cache_write == 1800
    assert clean.rewrite_excess == 0

    # Re-written: the third call re-sends the whole 1500-token prefix as a write
    transcripts("cold", [
        row("m1", write=1000, read=0),
        row("m2", write=500, read=1000),
        row("m3", write=1500, read=0),
    ])
    cold = usage.read_session("cold", FLOOR).total
    assert cold.context_peak == 1500
    assert cold.cache_write == 3000
    assert cold.rewrite_excess == 1500


def test_rewrite_cost_is_the_difference_between_writing_and_reading(transcripts):
    """The tax is what the re-sent tokens cost ABOVE reading them, not their full price"""
    transcripts("s1", [
        row("m1", write=1_000_000, read=0),
        row("m2", write=1_000_000, read=0),
    ])
    total = usage.read_session("s1", FLOOR).total
    assert total.rewrite_excess == 1_000_000
    # 1M tokens at Opus $5/MTok: written at 1.25x = $6.25, read at 0.1x = $0.50
    assert total.rewrite_cost_usd == pytest.approx(5.75)


# -- turn boundaries


def test_a_boundary_is_the_cache_going_backwards_not_a_big_write(transcripts):
    """Boundaries are counted from cache_read DROPPING, so no threshold is involved"""
    transcripts("s1", [
        row("m1", write=900, read=0),      # first call: nothing to drop from
        row("m2", write=900, read=900),    # read rose — same write size, not a boundary
        row("m3", write=900, read=200),    # read dropped — a boundary
        row("m4", write=100, read=1100),   # rose again
        row("m5", write=900, read=300),    # dropped — a second boundary
    ])
    assert usage.read_session("s1", FLOOR).total.resume_boundaries == 2


def test_a_single_turn_session_has_no_boundaries(transcripts):
    transcripts("s1", [row("m1", write=100), row("m2", write=50, read=100)])
    assert usage.read_session("s1", FLOOR).total.resume_boundaries == 0


# -- which of the two causes made a boundary cold

def test_a_boundary_inside_the_ttl_is_the_prefix_moving_however_little_it_read(
        transcripts):
    """The misreading this split exists to prevent, and its opposite in the same test"""
    transcripts("s1", [
        row("m1", write=9_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=9_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=9_000, read=10, at="2026-08-09T00:00:14.000Z"),   # 12s later
    ])
    near = usage.read_session("s1", FLOOR).total
    assert near.boundaries_ttl == 0
    assert near.rewrite_prefix_write == 9_000
    assert near.rewrite_ttl_share == 0.0

    transcripts("s2", [
        row("m1", write=9_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=9_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=9_000, read=10, at="2026-08-09T00:11:00.000Z"),   # 11min later
    ])
    far = usage.read_session("s2", FLOOR).total
    assert far.boundaries_ttl == 1
    assert far.rewrite_ttl_write == 9_000
    assert far.rewrite_ttl_share == 1.0


def test_an_hour_bought_prefix_is_not_expired_at_five_minutes(transcripts):
    """Issue #1291: the gap is judged against the TTL the prefix was last written at"""
    def session(gap_at: str) -> list[dict]:
        return [row("m1", write=9_000, ttl_1h=9_000, at="2026-08-09T00:00:00.000Z"),
                row("m2", read=9_000, at="2026-08-09T00:00:02.000Z"),  # a read keeps the hour
                row("m3", write=9_000, read=10, ttl_1h=9_000, at=gap_at)]

    transcripts("s1", session("2026-08-09T00:11:00.000Z"))   # 11 min: still warm
    assert usage.read_session("s1", FLOOR).total.boundaries_ttl == 0
    transcripts("s2", session("2026-08-09T01:05:00.000Z"))   # 65 min: gone
    assert usage.read_session("s2", FLOOR).total.boundaries_ttl == 1


def test_a_long_gap_that_still_read_the_static_prefix_is_not_an_expiry(transcripts):
    """An expired entry leaves NOTHING; a moved prefix still serves the system prompt"""
    transcripts("s1", [
        row("m1", write=60_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=60_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=40_000, read=20_000, at="2026-08-09T00:30:00.000Z"),
    ])
    total = usage.read_session("s1", FLOOR).total
    assert total.resume_boundaries == 1
    assert total.boundaries_ttl == 0
    assert total.rewrite_prefix_write == 40_000


def test_the_cause_split_is_a_partition_of_the_tax_not_a_second_count(transcripts):
    """`rewrite_excess` keeps its own threshold-free definition; the causes divide it"""
    transcripts("s1", [
        row("m1", write=50_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=50_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=50_000, read=20_000, at="2026-08-09T00:00:07.000Z"),  # prefix
        row("m4", write=100, read=70_000, at="2026-08-09T00:00:09.000Z"),
        row("m5", write=50_000, read=100, at="2026-08-09T00:20:00.000Z"),     # TTL
    ])
    total = usage.read_session("s1", FLOOR).total
    assert total.rewrite_ttl_share == 0.5
    assert total.rewrite_ttl_excess + (total.rewrite_excess - total.rewrite_ttl_excess) \
        == total.rewrite_excess
    assert 0 < total.rewrite_ttl_excess < total.rewrite_excess


def test_an_unclassified_tax_reports_no_share_rather_than_a_zero_one(transcripts):
    """A session with no boundary has NOT been measured at 0%"""
    transcripts("s1", [row("m1", write=500), row("m2", write=400, read=500)])
    total = usage.read_session("s1", FLOOR).total
    assert total.rewrite_ttl_share is None
    assert total.rewrite_ttl_excess == 0


def test_the_floor_comes_from_the_catalog_and_reclassifies_the_same_boundary(
        transcripts):
    """`os.cold_prefix_floor` is the knob, and this is the boundary it moves"""
    transcripts("s1", [
        row("m1", write=60_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=60_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=60_000, read=16_000, at="2026-08-09T00:30:00.000Z"),
    ])
    assert usage.read_session("s1", cold_prefix_floor=5_000).total.boundaries_ttl == 0
    assert usage.read_session("s1", cold_prefix_floor=20_000).total.boundaries_ttl == 1


def test_the_floor_has_no_default_anywhere_in_this_module():
    """There must be nothing to fall back TO"""
    assert not hasattr(usage, "COLD_PREFIX_FLOOR")
    assert not hasattr(usage, "resolved_cold_prefix_floor")
    with pytest.raises(TypeError):
        usage.read_session("s1")            # type: ignore[call-arg]


def test_merging_keeps_the_share_token_weighted_not_averaged():
    """Two sessions of very different size must not each get half a vote"""
    small = usage.Usage(rewrite_ttl_write=10, rewrite_prefix_write=0, boundaries_ttl=1,
                        resume_boundaries=1)
    large = usage.Usage(rewrite_ttl_write=0, rewrite_prefix_write=990,
                        boundaries_ttl=0, resume_boundaries=3)
    merged = small + large
    assert merged.boundaries_ttl == 1
    assert merged.resume_boundaries == 4
    assert merged.rewrite_ttl_share == 0.01


# -- subagents


def test_subagents_are_counted_separately_and_included_in_the_total(transcripts):
    """A third of the planner's bill was subagents, so they get their own line"""
    transcripts("s1", [row("m1", write=1000, out=10)], subagents={
        "agent-aaa": [row("s1m1", write=300, out=5)],
        "agent-bbb": [row("s2m1", write=200, out=7)],
    })
    session = usage.read_session("s1", FLOOR)

    assert session.subagent_count == 2
    assert session.main.cache_write == 1000
    assert session.subagents.cache_write == 500
    assert session.total.cache_write == 1500
    assert session.total.output == 22


def test_the_subagent_rewrite_tax_is_a_structural_zero_not_a_small_one(transcripts):
    """Spec 2026-10-02 §1.5, asserted as a PROPERTY and not as a constant"""
    transcripts(
        "structural",
        [row("m1", write=60_000, read=50_000, at="2026-08-09T00:00:00.000Z"),
         row("m2", write=60_000, read=1_000, at="2026-08-09T01:00:00.000Z"),
         row("m3", write=60_000, read=2_000, at="2026-08-09T02:00:00.000Z")],
        subagents={"agent-aaa": [
            row("s1", write=5_000, read=100_000, at="2026-08-09T00:10:00.000Z"),
            row("s2", write=5_000, read=120_000, at="2026-08-09T00:11:00.000Z")]})
    session = usage.read_session("structural", FLOOR)

    assert session.subagents.cache_write == 10_000
    assert session.subagents.rewrite_excess == 0
    assert session.subagents.resume_boundaries == 0
    assert session.main.rewrite_excess > 0 and session.main.resume_boundaries == 1
    # `Usage.__add__` sums both sides, so the total carries the main side's tax alone
    assert session.total.rewrite_excess == session.main.rewrite_excess
    assert session.total.resume_boundaries == session.main.resume_boundaries
    assert session.total.cache_write == (session.main.cache_write
                                         + session.subagents.cache_write)


def test_an_empty_subagent_transcript_is_not_counted_as_an_agent(transcripts):
    """Claude Code leaves behind stub files for agents that produced nothing"""
    transcripts("s1", [row("m1", write=100)], subagents={"agent-empty": []})
    assert usage.read_session("s1", FLOOR).subagent_count == 0


# -- sessions split across project directories


def test_a_session_split_across_project_dirs_sums_every_segment(transcripts):
    """Claude Code keys the transcript directory on the segment's cwd"""
    transcripts("split", [row("m1", write=100, read=0, out=10)], slug="-repo")
    transcripts("split", [row("m2", write=50, read=400, out=20)],
                slug="-repo-worktree")
    transcripts("solo", [row("m3", out=5)], slug="-repo")

    split = usage.read_session("split", FLOOR).total
    assert split.messages == 2
    assert split.cache_write == 150
    assert split.cache_read == 400
    assert split.output == 30
    # A second file exists only because the session was resumed somewhere else
    assert split.resume_boundaries == 1

    assert usage.read_session("solo", FLOOR).total.output == 5


def test_subagents_are_found_beside_every_segment(transcripts):
    """Each segment directory can hold its own subagent transcripts"""
    transcripts("split", [row("m1", write=100)], slug="-repo",
                subagents={"agent-aaa": [row("sa1", out=5)]})
    transcripts("split", [row("m2", write=50)], slug="-repo-worktree",
                subagents={"agent-bbb": [row("sb1", out=7)]})

    session = usage.read_session("split", FLOOR)
    assert session.subagent_count == 2
    assert session.subagents.output == 12


# -- missing evidence


def test_a_missing_transcript_is_not_found_rather_than_free(transcripts):
    """Unmeasurable and zero are different answers and must not render the same"""
    transcripts("present", [row("m1", write=100, out=10)])

    missing = usage.read_session("nope", FLOOR)
    assert missing.found is False
    assert missing.total.list_cost_usd == 0.0

    present = usage.read_session("present", FLOOR)
    assert present.found is True
    assert present.total.list_cost_usd > 0


def test_a_work_order_with_no_session_id_reports_not_found(transcripts):
    """A work order that never got a session must not match a transcript by accident"""
    transcripts("", [row("m1", write=100)], slug="-oddly-named")
    assert usage.read_session("", FLOOR).found is False


# -- pricing


def test_synthetic_messages_are_free_but_unknown_models_are_not():
    """`<synthetic>` is Claude Code's own placeholder for an API error it rendered as a turn — never billed"""
    assert usage.price_for("<synthetic>") == (0.0, 0.0)
    assert usage.price_for("claude-opus-5") == (5.0, 25.0)
    assert usage.price_for("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert usage.price_for("claude-sonnet-5") == (3.0, 15.0)
    assert usage.price_for("claude-something-unreleased") == usage.DEFAULT_PRICE


def test_cost_is_priced_per_message_model_not_per_session(transcripts):
    """A session that mixes models must not price the cheap half at the dear rate"""
    transcripts("s1", [
        row("m1", plain=1_000_000, out=1_000_000, model="claude-opus-5"),
        row("m2", plain=1_000_000, out=1_000_000, model="claude-haiku-4-5"),
    ])
    total = usage.read_session("s1", FLOOR).total
    assert total.cost_by_model["claude-opus-5"] == pytest.approx(30.0)   # 5 + 25
    assert total.cost_by_model["claude-haiku-4-5"] == pytest.approx(6.0)  # 1 + 5
    assert total.list_cost_usd == pytest.approx(36.0)


def test_cache_reads_are_a_tenth_and_writes_are_a_quarter_more(transcripts):
    transcripts("s1", [row("m1", read=1_000_000)])
    assert usage.read_session("s1", FLOOR).total.list_cost_usd == pytest.approx(0.5)
    transcripts("s2", [row("m1", write=1_000_000)])
    assert usage.read_session("s2", FLOOR).total.list_cost_usd == pytest.approx(6.25)


def test_a_one_hour_cache_write_costs_twice_input_not_a_quarter_more(transcripts):
    """The TTL is a PRICE, and Jarvis bought the expensive one for months"""
    transcripts("s1", [row("m1", write=1_000_000, ttl_1h=1_000_000)])
    assert usage.read_session("s1", FLOOR).total.list_cost_usd == pytest.approx(10.0)
    transcripts("s2", [row("m1", write=1_000_000, ttl_5m=1_000_000)])
    assert usage.read_session("s2", FLOOR).total.list_cost_usd == pytest.approx(6.25)
    # Half and half prices in between, and a message with no split at all stays on the 5-minute rate
    transcripts("s3", [row("m1", write=1_000_000, ttl_1h=500_000, ttl_5m=500_000)])
    assert usage.read_session("s3", FLOOR).total.list_cost_usd == pytest.approx(8.125)


def test_the_split_is_a_ratio_when_it_covers_only_part_of_the_write(transcripts):
    """A turn's envelope reports the split for PART of the turn while `modelUsage` reports the whole"""
    priced = usage.priced("claude-opus-5", cache_write=1_000_000,
                          cache_1h=80_000, cache_5m=20_000)
    # 80% of the sample was 1h, so the whole million is priced at 1.25 + 0.8*0.75
    assert priced.list_cost_usd == pytest.approx(1_000_000 * 5 * 1.85 / 1e6)


def test_the_bill_says_what_each_class_of_token_cost(transcripts):
    """The line items"""
    transcripts("s1", [row("m1", plain=1_000, write=100_000, read=2_000_000,
                           out=10_000)])
    classes = usage.read_session("s1", FLOOR).total.cost_by_class
    assert classes["input"] == pytest.approx(0.005)
    assert classes["cache_write"] == pytest.approx(0.625)
    assert classes["cache_read"] == pytest.approx(1.0)
    assert classes["output"] == pytest.approx(0.25)
    assert sum(classes.values()) == pytest.approx(
        usage.read_session("s1", FLOOR).total.list_cost_usd)


# -- merging


def test_merging_sums_the_tax_but_takes_the_peak():
    """Two sessions' re-write excesses add; their context peaks do not"""
    a = usage.Usage(cache_write=300, context_peak=100, rewrite_excess=200)
    b = usage.Usage(cache_write=500, context_peak=400, rewrite_excess=100)
    merged = a + b
    assert merged.rewrite_excess == 300
    assert merged.context_peak == 400
    assert merged.cache_write == 800


def test_a_transcript_path_may_be_a_plain_string(transcripts):
    """`_assistant_messages` is the module's useful handle for ad-hoc measurement"""
    path = transcripts("s1", [row("m1", write=100, out=10)])
    assert usage._assistant_messages(str(path)) == usage._assistant_messages(path)


def test_rows_without_usage_are_ignored(transcripts):
    """Tool results, hooks and UI state make up most of a transcript"""
    transcripts("s1", [
        {"type": "user", "message": {"content": "hello"}},
        {"type": "attachment", "attachment": {"type": "task_reminder"}},
        {"type": "custom-title", "customTitle": 'the "usage" of the thing'},
        row("m1", write=100, out=10),
    ])
    assert usage.read_session("s1", FLOOR).total.messages == 1


# --- the tool_use/tool_result walk (spec 2026-10-06-fleet-cost-per-tool.md §10.2-§10.3)


def call_row(mid: str, *, read: int = 0, out: int = 0, write: int = 0, plain: int = 0,
             tools: Sequence[tuple[str, str]] = (),
             at: str = "2026-10-06T00:00:00.000Z") -> dict:
    """One assistant row with `usage` and optional `tool_use` blocks"""
    return {
        "type": "assistant",
        "timestamp": at,
        "message": {
            "id": mid, "model": "claude-opus-5",
            "usage": {"input_tokens": plain, "cache_creation_input_tokens": write,
                      "cache_read_input_tokens": read, "output_tokens": out},
            "content": [{"type": "tool_use", "id": tid, "name": name,
                         "input": {"command": f"echo {tid}"}}
                        for tid, name in tools],
        },
    }


def result_row(*results: tuple[str, str], at: str = "2026-10-06T00:00:01.000Z",
               is_error: bool = False) -> dict:
    """One user row carrying `tool_result` blocks — (tool_use_id, text) pairs"""
    return {
        "type": "user",
        "timestamp": at,
        "message": {"content": [{"type": "tool_result", "tool_use_id": tid,
                                 "content": text, "is_error": is_error}
                                for tid, text in results]},
    }


def write_jsonl(path, rows_out: Sequence[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows_out))
    return path


def test_tool_results_pairs_a_use_with_its_result_and_sizes_it_by_context_delta(
        tmp_path):
    """The pair, and the unmatched call beside it so "one result" cannot be a stub"""
    path = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50, tools=[("t1", "Read")]),
        result_row(("t1", "x" * 40)),
        call_row("m2", read=1200, out=10, tools=[("t2", "Bash")]),
    ])
    found = usage.tool_results(path)
    assert [(r.tool_use_id, r.name, r.matched) for r in found] == [
        ("t1", "Read", True), ("t2", "Bash", False)]
    first, unmatched = found
    # 1200 - 1000 - 50: the tokens call m2 was asked to read that m1 was not
    assert (first.tokens, first.token_basis) == (150, usage.BASIS_CONTEXT_DELTA)
    assert (first.chars, first.is_error, first.caller) == (40, False, usage.CALLER_MAIN)
    assert (unmatched.tokens, unmatched.token_basis) == (0, "")


def test_parallel_results_split_char_proportionally_and_sum_to_the_delta(tmp_path):
    """K results between one call pair share ONE exact delta, 1:1:2 by characters"""
    path = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50,
                 tools=[("t1", "Read"), ("t2", "Read"), ("t3", "Bash")]),
        result_row(("t1", "a" * 10), ("t2", "b" * 10), ("t3", "c" * 20)),
        call_row("m2", read=1200),
    ])
    found = usage.tool_results(path)
    assert sum(r.tokens for r in found) == 150
    assert sorted(r.tokens for r in found) == [37, 38, 75]
    assert {r.token_basis for r in found} == {usage.BASIS_CONTEXT_DELTA}


def test_a_compaction_between_the_calls_falls_back_to_chars(tmp_path):
    """The context goes DOWN at a `compact_boundary`, so the delta is not the result"""
    path = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50, tools=[("t1", "Read")]),
        result_row(("t1", "x" * 40)),
        {"type": "system", "subtype": "compact_boundary",
         "timestamp": "2026-10-06T00:00:02.000Z",
         "compactMetadata": {"trigger": "auto", "preTokens": 1000, "postTokens": 100}},
        call_row("m2", read=1200),
    ])
    only, = usage.tool_results(path)
    assert (only.tokens, only.token_basis) == (10, usage.BASIS_CHARS)
    other, = usage.tool_results(path, chars_per_token=2.0)
    assert other.tokens == 20


def test_a_user_prompt_between_the_calls_falls_back_to_chars(tmp_path):
    """Otherwise the user's own typing is charged to the tool"""
    path = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50, tools=[("t1", "Read")]),
        result_row(("t1", "x" * 40)),
        {"type": "user", "timestamp": "2026-10-06T00:00:02.000Z",
         "message": {"content": "now do the other thing"}},
        call_row("m2", read=1200),
    ])
    only, = usage.tool_results(path)
    assert (only.tokens, only.token_basis) == (10, usage.BASIS_CHARS)


def test_a_non_positive_delta_falls_back_to_chars(tmp_path):
    """A context that did not grow cannot be evidence of what the tool appended"""
    path = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50, tools=[("t1", "Read")]),
        result_row(("t1", "x" * 40)),
        call_row("m2", read=900),
    ])
    only, = usage.tool_results(path)
    assert (only.tokens, only.token_basis) == (10, usage.BASIS_CHARS)


def test_a_sidechain_result_is_attributed_to_the_subagent_not_the_main_chain(tmp_path):
    """Caller is the FILE the row came from — `read_session`'s own split (§10.3)"""
    parent = write_jsonl(tmp_path / "s1.jsonl", [
        call_row("m1", read=1000, out=50, tools=[("t1", "Read")]),
        result_row(("t1", "x" * 40)),
        call_row("m2", read=1200),
    ])
    sub = write_jsonl(tmp_path / "s1" / "subagents" / "agent-a.jsonl", [
        call_row("n1", read=500, out=10, tools=[("u1", "Grep")]),
        result_row(("u1", "y" * 8)),
        call_row("n2", read=600),
    ])
    assert [r.caller for r in usage.tool_results(parent)] == [usage.CALLER_MAIN]
    sub_result, = usage.tool_results(sub)
    assert sub_result.caller == usage.CALLER_SUBAGENT
    # 600 - 500 - 10, measured inside the subagent's own chain
    assert (sub_result.tokens, sub_result.name) == (90, "Grep")
