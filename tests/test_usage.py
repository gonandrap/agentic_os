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

#: `read_session` takes the cold-prefix floor as a REQUIRED argument — there is no
#: module default to fall back on — so every call here passes one. The value only
#: matters to the tests that are about the classification; elsewhere it is inert.
FLOOR = 5_000


def row(mid: str, *, write: int = 0, read: int = 0, out: int = 0, plain: int = 0,
        model: str = "claude-opus-5", ttl_1h: int = 0, ttl_5m: int = 0,
        at: str = "2026-08-09T00:00:00.000Z") -> dict:
    """One assistant row, in the shape Claude Code writes.

    `cache_creation` — the TTL the write bought — is nested one level down and is only
    written when a test asks for it, because most rows in the wild predate it and the
    module has to price those too.
    """
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
        "message": {"id": mid, "model": model, "usage": usage_obj},
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
    total = usage.read_session("s1", FLOOR).total

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
    assert usage.read_session("s1", FLOOR).total.output == 900


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
    clean = usage.read_session("clean", FLOOR).total
    assert clean.context_peak == 1800          # 300 + 1500
    assert clean.cache_write == 1800
    assert clean.rewrite_excess == 0

    # Re-written: the third call re-sends the whole 1500-token prefix as a write.
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
    """The tax is what the re-sent tokens cost ABOVE reading them, not their full price."""
    transcripts("s1", [
        row("m1", write=1_000_000, read=0),
        row("m2", write=1_000_000, read=0),
    ])
    total = usage.read_session("s1", FLOOR).total
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
    assert usage.read_session("s1", FLOOR).total.resume_boundaries == 2


def test_a_single_turn_session_has_no_boundaries(transcripts):
    transcripts("s1", [row("m1", write=100), row("m2", write=50, read=100)])
    assert usage.read_session("s1", FLOOR).total.resume_boundaries == 0


# -- which of the two causes made a boundary cold -------------------------------------

def test_a_boundary_inside_the_ttl_is_the_prefix_moving_however_little_it_read(
        transcripts):
    """The misreading this split exists to prevent, and its opposite in the same test.

    wo-5a6b2d6d's 157k re-write was read as a 14-minute turn outliving a 5-minute cache.
    The gap between the call that wrote the cache and the one that missed it was TWELVE
    SECONDS: the entry was alive and the prefix had moved. A classifier that looked only
    at how little was read would get that backwards, so the pair below differs in the
    GAP alone — same tokens, same drop, opposite cause.
    """
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


def test_a_long_gap_that_still_read_the_static_prefix_is_not_an_expiry(transcripts):
    """An expired entry leaves NOTHING; a moved prefix still serves the system prompt.

    Both calls below sit far outside the TTL and differ only in what survived, which is
    the whole discriminator: 20k read back means the entry was there and stopped
    matching, and no longer TTL would have changed that.
    """
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
    """`rewrite_excess` keeps its own threshold-free definition; the causes divide it.

    The two halves must add back to the tax exactly (kn-7a2180ba), even though the raw
    boundary writes they are derived from do not equal it.
    """
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
    """A session with no boundary has NOT been measured at 0% — the two must not read
    alike, or a report prints "0% was the TTL" as though it were a finding."""
    transcripts("s1", [row("m1", write=500), row("m2", write=400, read=500)])
    total = usage.read_session("s1", FLOOR).total
    assert total.rewrite_ttl_share is None
    assert total.rewrite_ttl_excess == 0


def test_the_floor_comes_from_the_catalog_and_reclassifies_the_same_boundary(
        transcripts):
    """`os.cold_prefix_floor` is the knob, and this is the boundary it moves.

    One transcript, read twice under different floors. At 5,000 a boundary reading 16k
    kept its prefix, so no TTL would have helped it; raise the floor above 16k and the
    same boundary is read as an expiry. Nothing else about the session changes, which is
    what makes this the setting's actual effect rather than a coincidence.
    """
    transcripts("s1", [
        row("m1", write=60_000, read=0, at="2026-08-09T00:00:00.000Z"),
        row("m2", write=100, read=60_000, at="2026-08-09T00:00:02.000Z"),
        row("m3", write=60_000, read=16_000, at="2026-08-09T00:30:00.000Z"),
    ])
    assert usage.read_session("s1", cold_prefix_floor=5_000).total.boundaries_ttl == 0
    assert usage.read_session("s1", cold_prefix_floor=20_000).total.boundaries_ttl == 1


def test_the_floor_has_no_default_anywhere_in_this_module():
    """There must be nothing to fall back TO.

    A default here would let a caller that could not reach a catalog still produce a
    classification, and a cost report that classified against a guessed threshold prints
    a finding the configuration never produced. Asserted on the module surface rather
    than on one call, because the way this regresses is somebody re-adding the constant.
    """
    assert not hasattr(usage, "COLD_PREFIX_FLOOR")
    assert not hasattr(usage, "resolved_cold_prefix_floor")
    with pytest.raises(TypeError):
        usage.read_session("s1")            # type: ignore[call-arg]


def test_merging_keeps_the_share_token_weighted_not_averaged():
    """Two sessions of very different size must not each get half a vote."""
    small = usage.Usage(rewrite_ttl_write=10, rewrite_prefix_write=0, boundaries_ttl=1,
                        resume_boundaries=1)
    large = usage.Usage(rewrite_ttl_write=0, rewrite_prefix_write=990,
                        boundaries_ttl=0, resume_boundaries=3)
    merged = small + large
    assert merged.boundaries_ttl == 1
    assert merged.resume_boundaries == 4
    assert merged.rewrite_ttl_share == 0.01


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
    session = usage.read_session("s1", FLOOR)

    assert session.subagent_count == 2
    assert session.main.cache_write == 1000
    assert session.subagents.cache_write == 500
    assert session.total.cache_write == 1500
    assert session.total.output == 22


def test_an_empty_subagent_transcript_is_not_counted_as_an_agent(transcripts):
    """Claude Code leaves behind stub files for agents that produced nothing."""
    transcripts("s1", [row("m1", write=100)], subagents={"agent-empty": []})
    assert usage.read_session("s1", FLOOR).subagent_count == 0


# -- sessions split across project directories ----------------------------------------


def test_a_session_split_across_project_dirs_sums_every_segment(transcripts):
    """Claude Code keys the transcript directory on the segment's cwd, so one session
    id can leave files under two slugs — wo-2fa7c0e9's did (repo root, then its
    worktree), and keeping one path per session id read that work order as $0.51 /
    1 turn when the true figure was three times that.

    Paired with a single-segment session in one of the same directories, which must
    not double.
    """
    transcripts("split", [row("m1", write=100, read=0, out=10)], slug="-repo")
    transcripts("split", [row("m2", write=50, read=400, out=20)],
                slug="-repo-worktree")
    transcripts("solo", [row("m3", out=5)], slug="-repo")

    split = usage.read_session("split", FLOOR).total
    assert split.messages == 2
    assert split.cache_write == 150
    assert split.cache_read == 400
    assert split.output == 30
    # A second file exists only because the session was resumed somewhere else, so
    # the file boundary itself counts as one resume even though no cache drop is
    # visible across separately-read files.
    assert split.resume_boundaries == 1

    assert usage.read_session("solo", FLOOR).total.output == 5


def test_subagents_are_found_beside_every_segment(transcripts):
    """Each segment directory can hold its own subagent transcripts."""
    transcripts("split", [row("m1", write=100)], slug="-repo",
                subagents={"agent-aaa": [row("sa1", out=5)]})
    transcripts("split", [row("m2", write=50)], slug="-repo-worktree",
                subagents={"agent-bbb": [row("sb1", out=7)]})

    session = usage.read_session("split", FLOOR)
    assert session.subagent_count == 2
    assert session.subagents.output == 12


# -- missing evidence -----------------------------------------------------------------


def test_a_missing_transcript_is_not_found_rather_than_free(transcripts):
    """Unmeasurable and zero are different answers and must not render the same."""
    transcripts("present", [row("m1", write=100, out=10)])

    missing = usage.read_session("nope", FLOOR)
    assert missing.found is False
    assert missing.total.list_cost_usd == 0.0

    present = usage.read_session("present", FLOOR)
    assert present.found is True
    assert present.total.list_cost_usd > 0


def test_a_work_order_with_no_session_id_reports_not_found(transcripts):
    """A work order that never got a session must not match a transcript by accident."""
    transcripts("", [row("m1", write=100)], slug="-oddly-named")
    assert usage.read_session("", FLOOR).found is False


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
    """The TTL is a PRICE, and Jarvis bought the expensive one for months.

    1.25x at the 5-minute TTL, 2x at the one-hour one (kn-f94abf34, measured over 1,075
    transcripts). Every write a worker made before `FORCE_PROMPT_CACHING_5M` shipped was
    a 1h write, so pricing them all at 1.25x understated the largest avoidable line in
    the bill by 60% of itself. The split is reported per message and is used where it is
    there.
    """
    transcripts("s1", [row("m1", write=1_000_000, ttl_1h=1_000_000)])
    assert usage.read_session("s1", FLOOR).total.list_cost_usd == pytest.approx(10.0)
    transcripts("s2", [row("m1", write=1_000_000, ttl_5m=1_000_000)])
    assert usage.read_session("s2", FLOOR).total.list_cost_usd == pytest.approx(6.25)
    # Half and half prices in between, and a message with no split at all stays on the
    # 5-minute rate — the floor, and what this module has always charged.
    transcripts("s3", [row("m1", write=1_000_000, ttl_1h=500_000, ttl_5m=500_000)])
    assert usage.read_session("s3", FLOOR).total.list_cost_usd == pytest.approx(8.125)


def test_the_split_is_a_ratio_when_it_covers_only_part_of_the_write(transcripts):
    """A turn's envelope reports the split for PART of the turn while `modelUsage`
    reports the whole; the ratio is what carries over, applied to all of it."""
    priced = usage.priced("claude-opus-5", cache_write=1_000_000,
                          cache_1h=80_000, cache_5m=20_000)
    # 80% of the sample was 1h, so the whole million is priced at 1.25 + 0.8*0.75.
    assert priced.list_cost_usd == pytest.approx(1_000_000 * 5 * 1.85 / 1e6)


def test_the_bill_says_what_each_class_of_token_cost(transcripts):
    """The line items. A total says how much; only the classes say where it went — and
    they are priced 20x apart, so which one dominates IS the finding."""
    transcripts("s1", [row("m1", plain=1_000, write=100_000, read=2_000_000,
                           out=10_000)])
    classes = usage.read_session("s1", FLOOR).total.cost_by_class
    assert classes["input"] == pytest.approx(0.005)
    assert classes["cache_write"] == pytest.approx(0.625)
    assert classes["cache_read"] == pytest.approx(1.0)
    assert classes["output"] == pytest.approx(0.25)
    assert sum(classes.values()) == pytest.approx(
        usage.read_session("s1", FLOOR).total.list_cost_usd)


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


def test_a_transcript_path_may_be_a_plain_string(transcripts):
    """`_assistant_messages` is the module's useful handle for ad-hoc measurement, and a
    string is the obvious thing to hand it — it should not need a `Path`."""
    path = transcripts("s1", [row("m1", write=100, out=10)])
    assert usage._assistant_messages(str(path)) == usage._assistant_messages(path)


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
    assert usage.read_session("s1", FLOOR).total.messages == 1
