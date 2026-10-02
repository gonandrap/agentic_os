"""The sealed autopsy: does a rehydrated reading say exactly what the live one said.

The seal exists because every source an autopsy is read from expires — Claude Code prunes
session transcripts and the subagent files beside them — so an order inspected six months
after it settled would otherwise report a clock that got shorter as the evidence aged.

Two claims are tested here and nothing else, because nothing else reads a seal yet (§3
ships dark). First, the FUNCTION-LEVEL ROUND TRIP: `from_seal(to_seal(a))` renders the
same `as_dict` the live `Anatomy` renders, which is what makes a seal a substitute for the
transcript rather than a summary of it. At `full` that holds with NOTHING excluded, tool
`params` included, which is §6; at `normal` the params are the one thing the payload does
not carry at all. Second, THE CAPS: a seal is bounded, and every fold carries numbers that
reconcile against the uncapped reading — announcing a key is not proving a cap.

The round trip is pinned on the committed fixture session (`tests/test_inspection.py`'s
`real_session`) because its three cache writes and two joins were measured from the
unredacted transcript; the caps are pinned on synthetic transcripts under `tmp_path`,
because a committed fixture with 505 spans in one turn would be a file written to agree
with the answer.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from jarvis import autopsy, catalog, cli, db, holds, inspection, usage
from jarvis.daemon import Daemon

from tests.test_cost_report import registered, store  # noqa: F401
from tests.test_inspection import (  # noqa: F401
    FIXTURE_ROOT,
    FIXTURE_SESSION,
    TASK,
    assistant_row,
    parent_rows,
    prompt_row,
    real_session,
    sub_rows,
    tool_rows,
    under_floor_rows,
    write_meta,
    write_transcript,
)

PARAM_KEYS = ("params", "params_truncated", "params_dropped")


def without_params(value):
    """The same payload with every parameter key gone, at any depth.

    §6: tool parameters are sealed at `full` and at NO other level, so a `normal` round
    trip is asserted over everything EXCEPT them — the live reading always has them. The
    `full` round trip strips nothing: that is section 6's acceptance criterion.
    """
    if isinstance(value, dict):
        return {k: without_params(v) for k, v in value.items() if k not in PARAM_KEYS}
    if isinstance(value, list):
        return [without_params(v) for v in value]
    return value


def keys_of(value) -> set[str]:
    """Every dict key anywhere in a payload."""
    if isinstance(value, dict):
        found = set(value)
        for item in value.values():
            found |= keys_of(item)
        return found
    if isinstance(value, list):
        out: set[str] = set()
        for item in value:
            out |= keys_of(item)
        return out
    return set()


def usage_of_fold(fold: dict) -> usage.Usage:
    """The folded calls' spend, rebuilt from the numbers the fold carries."""
    total = usage.Usage()
    for model, row in fold["by_model"].items():
        total = total + usage.priced(
            model, messages=row["messages"], input=row["input"],
            cache_write=row["cache_write"], cache_read=row["cache_read"],
            output=row["output"], cache_1h=row["cache_1h"], cache_5m=row["cache_5m"])
    return total


# -- the round trip --------------------------------------------------------------------


def test_a_sealed_reading_renders_exactly_what_the_live_one_rendered(real_session):
    """The acceptance criterion of §3: `as_dict` stays the SINGLE render contract and is
    computed FROM the rehydrated object, so no surface can tell a seal from a reading.

    With no hold on the order `spans` changes nothing, which is the shape that must hold
    with no exclusions at all beyond the params section 6 owns.
    """
    assert real_session.holds == []

    sealed = autopsy.to_seal(real_session, level="normal")
    back = autopsy.from_seal(sealed, spans=real_session.holds)

    assert without_params(back.as_dict()) == without_params(real_session.as_dict())
    assert back.found is True
    # §6: a `normal` seal carries no parameters, so they are the round trip's exclusion.
    assert "params" not in keys_of(sealed)


def test_a_sealed_reading_at_full_renders_exactly_what_the_live_one_rendered(
        real_session):
    """§6's acceptance criterion: at `full` the round trip strips NOTHING — the tool
    parameters are in the payload and come back byte for byte, so `as_dict` is identical
    with no `without_params` anywhere.
    """
    sealed = autopsy.to_seal(real_session, level="full")

    assert autopsy.from_seal(sealed, spans=[]).as_dict() == real_session.as_dict()
    # Not vacuous: the parameters really are populated in the SEALED payload.
    first = sealed["turns"][0]["spans"][0]
    assert first["detail"] == "List repo structure"
    assert first["params"] == {"description": "List repo structure"}


def test_a_normal_seal_carries_no_parameter_key_at_all(real_session):
    """§6: at `normal` the payload holds NO `params` key rather than an empty one, which
    is what makes the level's own sentence the answer to an empty reading."""
    at_normal = keys_of(autopsy.to_seal(real_session, level="normal"))
    at_full = keys_of(autopsy.to_seal(real_session, level="full"))

    assert not (at_normal & set(PARAM_KEYS))
    assert set(PARAM_KEYS) <= at_full


def test_sealing_the_committed_session_at_full_changes_none_of_its_pinned_numbers(
        real_session):
    """§6: `full` adds retained content and moves no measurement. The ten numbers are
    the committed session's, asserted against the REHYDRATED anatomy."""
    back = autopsy.from_seal(autopsy.to_seal(real_session, level="full"), spans=[])
    part = back.partition()

    assert round(part["wall"]) == 1886
    assert round(part["blocked"]) == 582
    assert round(part["tools"]) == 52
    assert len(back.turns) == 3
    assert [len(t.spans) for t in back.turns] == [46, 26, 9]
    assert [len(t.calls) for t in back.turns] == [32, 26, 10]
    assert len(back.spans) == 81
    assert sum(len(t.calls) for t in back.turns) == 68
    assert len(back.writes) == 3
    assert sum(len(t.subagents) for t in back.turns) == 0


def test_the_holds_are_not_sealed_and_have_to_be_handed_back_in(monkeypatch):
    """Holds come from the OS's own database, which never expires — sealing one would
    freeze an episode that was OPEN at seal time. So `from_seal` takes `spans=` exactly as
    `read_session` does, and the wrong form is FALSE the moment a hold exists.
    """
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(FIXTURE_ROOT))
    plain = inspection.read_session(FIXTURE_SESSION)
    first = plain.turns[0]
    spans = [holds.Hold(cause=holds.NEO_QUESTION, started=first.started + 10,
                        ended=first.started + 70)]
    a = inspection.read_session(FIXTURE_SESSION, spans=spans)
    assert a.held > 0

    sealed = autopsy.to_seal(a, level="normal")

    assert "holds" not in sealed
    assert without_params(autopsy.from_seal(sealed, spans=spans).as_dict()) == \
        without_params(a.as_dict())
    assert without_params(autopsy.from_seal(sealed, spans=[]).as_dict()) != \
        without_params(a.as_dict()), "held, held_by and the partition are hold-derived"


def test_a_rehydration_must_be_handed_the_holds_and_cannot_silently_default_to_none(
        real_session):
    """A forgotten `spans=` used to default to no holds, so a held order rehydrated with
    `held` 0 and the wrong held/active partition and said nothing about it (§3). The
    argument is required, so the omission is a `TypeError` at the call, not a false number.
    """
    with pytest.raises(TypeError):
        autopsy.from_seal(autopsy.to_seal(real_session, level="normal"))


def test_every_api_call_of_every_turn_survives_the_seal(real_session):
    """`Turn.as_dict` emits `api_calls: len(self.calls)` and a folded `usage`, and carries
    no calls — rehydrate from THAT and `observed` goes False on a turn that made 32 calls
    (issue #227, recreated inside the persistence). The calls themselves are sealed.
    """
    back = autopsy.from_seal(autopsy.to_seal(real_session, level="normal"), spans=[])

    assert [len(t.calls) for t in back.turns] == [len(t.calls)
                                                 for t in real_session.turns]
    assert sum(len(t.calls) for t in back.turns) == 68
    # Per turn, not only the total: a fold that lost a model would still sum right.
    assert [t.usage.as_dict() for t in back.turns] == [t.usage.as_dict()
                                                       for t in real_session.turns]
    assert back.cache_ttl() == real_session.cache_ttl()
    assert back.rewrite_excess() == real_session.rewrite_excess()


def test_the_boundary_census_is_sealed_and_attributed_back_to_its_turns(real_session):
    """The census expires with the transcript, so it is SEALED — the opposite treatment to
    `holds`, which live in the OS's own database and are re-attached live. Unsealed, every
    boundary, context and rewrite figure of a rehydrated reading reads zero.
    """
    assert len(real_session.boundaries) >= 1

    sealed = autopsy.to_seal(real_session, level="normal")
    back = autopsy.from_seal(sealed, spans=[])

    def rows(a):
        return [(b.ts, b.cause, b.cache_write, b.cache_read, b.gap) for b in a.boundaries]

    assert rows(back) == rows(real_session)
    # Attributed to the SAME turns, not merely carried at the session level.
    assert [len(t.boundaries) for t in back.turns] == \
        [len(t.boundaries) for t in real_session.turns]
    assert [[b.cause for b in t.boundaries] for t in back.turns] == \
        [[b.cause for b in t.boundaries] for t in real_session.turns]
    assert back.rewrite() == real_session.rewrite()
    assert [t.usage.as_dict() for t in back.turns] == \
        [t.usage.as_dict() for t in real_session.turns]


def test_a_payload_with_no_census_rehydrates_to_an_empty_one(real_session):
    """The ROUND TRIP is what carries the census: a payload sealed before this key existed
    must read as "not measured" rather than raise. No seal exists in the wild (§3 ships
    dark), which is why `PAYLOAD_VERSION` stays 1."""
    sealed = autopsy.to_seal(real_session, level="normal")
    assert sealed["boundaries"]
    sealed.pop("boundaries")

    back = autopsy.from_seal(sealed, spans=[])

    assert back.boundaries == []
    assert all(t.boundaries == [] for t in back.turns)
    assert back.rewrite()["boundaries"] == 0


def test_a_seal_reads_at_the_same_cold_prefix_floor_as_every_other_reader(
        store, spec, write_transcript, monkeypatch):
    """Read at a lower floor than `jarvis inspect` reads at and a sealed boundary says
    `undecided` where the live reading decided it — the same floor `supervisor` passes."""
    from jarvis import ops

    wo = settled_with_a_session(store, write_transcript, "sess-floor")
    monkeypatch.setattr(ops, "cold_prefix_floor", lambda *a, **k: 4_242)
    seen = {}
    real = inspection.read_session
    monkeypatch.setattr(inspection, "read_session",
                        lambda *a, **k: (seen.update(k), real(*a, **k))[1])

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    assert seen["cold_prefix_floor"] == 4_242
    assert store.get_work_order(wo["id"])["autopsy_json"]


def test_the_three_measured_writes_survive_the_seal_by_cause(real_session):
    """`tests/test_inspection.py:82`'s three, and the causes are the whole point: two of
    them are ~180k tokens each, look identical on a bill and have different fixes."""
    back = autopsy.from_seal(autopsy.to_seal(real_session, level="normal"), spans=[])

    assert [(w.written, w.read, w.cause) for w in back.writes] == [
        (45_169, 0, inspection.COLD_START),
        (157_098, 15_862, inspection.PREFIX_MISS),
        (193_139, 0, inspection.TTL_EXPIRY),
    ]


def test_the_seal_is_not_the_rendered_payload(real_session):
    """§3's trap, pinned so a later refactor cannot collapse the two."""
    sealed = autopsy.to_seal(real_session, level="normal")

    assert sealed != real_session.as_dict()
    assert "calls" in sealed["turns"][0]


def test_the_payload_states_the_floors_and_the_level_it_was_taken_at(real_session):
    """A seal taken at 20000 holds no write below 20000, ever — so it says so. And the
    level is recorded from day one: without it "no params" is ambiguous between "sealed at
    `normal`" and "this order ran no tools"."""
    sealed = autopsy.to_seal(real_session, level="normal")

    assert sealed["write_floor"] == catalog.DEFAULT_INSPECT_REPORT_WRITE_FLOOR == 20_000
    assert sealed["join_floor"] == catalog.DEFAULT_INSPECT_REPORT_JOIN_FLOOR == 30
    assert sealed["autopsy_level"] == "normal"
    assert autopsy.from_seal(sealed, spans=[]).write_floor == 20_000


def test_a_seal_written_before_the_level_existed_reads_as_unknown():
    """A third state, distinct from `normal` and `full`: the ambiguity §3 exists to close
    must not come back as a default."""
    older = {"payload_v": 1, "session_id": "s", "found": True, "turns": []}

    assert autopsy.level_of(older) == autopsy.UNKNOWN
    assert autopsy.UNKNOWN not in ("normal", "full")
    assert autopsy.unseal({"autopsy_json": json.dumps(older),
                           "autopsy_sealed_at": 5.0})["autopsy_level"] == autopsy.UNKNOWN


# -- the caps, proven by reconciling the folded numbers ---------------------------------


def big_span_session(write_transcript, count: int) -> str:
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    for i in range(count):
        name = "TaskOutput" if i % 10 == 0 else "Bash"
        rows += tool_rows(10 + i * 10, 10 + i * 10 + (i + 1), f"t{i}", name,
                          {"description": f"span {i}"})
    rows.append(assistant_row(10 + count * 10 + 20, "m-last", write=1_000))
    return write_transcript(f"spans-{count}", rows)


def test_the_span_cap_keeps_the_dearest_and_the_fold_reconciles(write_transcript):
    """500 per turn, keeping the dearest by seconds — and the folded seconds are carried
    per tool NAME, so they reconcile against BOTH `Turn.tools` and `Turn.blocked`
    (join-ness is derivable from the name through `inspection.JOIN_TOOLS`)."""
    session = big_span_session(write_transcript, autopsy.TURN_SPAN_LIMIT + 5)
    a = inspection.read_session(session)
    (turn,) = a.turns

    sealed = autopsy.to_seal(a, level="normal")["turns"][0]
    fold = sealed["spans_folded"]

    assert autopsy.TURN_SPAN_LIMIT == 500
    assert len(sealed["spans"]) == 500 and fold["count"] == 5
    # The dearest were kept: the five folded are the five shortest.
    assert max(s["ended"] - s["started"] for s in sealed["spans"]) == \
        max(s.seconds for s in turn.spans)
    kept_join = sum(s["ended"] - s["started"] for s in sealed["spans"]
                    if s["name"] in inspection.JOIN_TOOLS)
    kept_tools = sum(s["ended"] - s["started"] for s in sealed["spans"]
                     if s["name"] not in inspection.JOIN_TOOLS)
    folded_join = sum(v for k, v in fold["seconds_by_name"].items()
                      if k in inspection.JOIN_TOOLS)
    folded_tools = sum(v for k, v in fold["seconds_by_name"].items()
                       if k not in inspection.JOIN_TOOLS)

    assert kept_join + folded_join == pytest.approx(turn.blocked)
    assert kept_tools + folded_tools == pytest.approx(turn.tools)
    assert fold["seconds"] == pytest.approx(folded_join + folded_tools)


def test_the_call_cap_carries_the_folded_remainder_as_numbers(write_transcript):
    """Folding calls changes `usage`, `context_peak`, `cache_ttl()` and
    `rewrite_excess()`, so the remainder is carried as numbers or all four become lies.
    No synthetic call stands in for a folded one — a fabricated row would be read as an
    API call that happened."""
    count = autopsy.TURN_CALL_LIMIT + 5
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    rows += [assistant_row(10 + i, f"m{i}", write=100 + i, read=50 * i,
                           ttl_5m=100 + i)
             for i in range(count)]
    session = write_transcript("calls", rows)
    a = inspection.read_session(session)
    (turn,) = a.turns
    assert len(turn.calls) == count

    sealed = autopsy.to_seal(a, level="normal")["turns"][0]
    fold = sealed["calls_folded"]
    back = autopsy.from_seal(autopsy.to_seal(a, level="normal"), spans=[])

    assert autopsy.TURN_CALL_LIMIT == 200
    assert len(sealed["calls"]) == 200 and fold["count"] == 5
    assert len(back.turns[0].calls) == 200, "nothing invented to stand in for a fold"
    # All four of the figures a fold would otherwise falsify, reconciled. The ADDITIVE
    # token keys by sum; `rewrite_excess` is max(0, sum(cache_write) - context_peak) and
    # is NOT additive, so it reconciles by its own definition from the fold's numbers.
    combined = back.turns[0].usage + usage_of_fold(fold)
    live = turn.usage.as_dict()
    additive = ("messages", "input", "cache_write", "cache_read", "output",
                "cache_1h", "cache_5m", "billed_input", "cached_input", "total_tokens")
    assert {k: combined.as_dict()[k] for k in additive} == {k: live[k] for k in additive}
    assert max(back.turns[0].context_peak, fold["context_peak"]) == turn.context_peak
    ttl = back.cache_ttl()
    assert {k: ttl[k] + fold["cache_ttl"][k] for k in ttl} == a.cache_ttl()
    written = sum(c.cache_write for c in back.turns[0].calls) + fold["cache_write"]
    assert max(0, written - turn.context_peak) == a.rewrite_excess()
    assert written - turn.context_peak == live["rewrite_excess"] > 0
    # And therefore the dollar figure derived from it, which the fold's numbers restate.
    restated = usage.Usage(
        messages=combined.messages, input=combined.input,
        cache_write=combined.cache_write, cache_read=combined.cache_read,
        output=combined.output, context_peak=turn.context_peak,
        rewrite_excess=max(0, written - turn.context_peak),
        cache_1h=combined.cache_1h, cache_5m=combined.cache_5m,
        cost_by_model=dict(combined.cost_by_model))
    assert round(restated.rewrite_cost_usd, 2) == live["rewrite_cost_usd"]


def test_the_subagent_cap_says_how_many_were_not_sealed(write_transcript):
    """Depth was already bounded by `SUBAGENT_DEPTH_READ`; breadth was not."""
    count = autopsy.SUBAGENT_LIMIT + 1
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    for i in range(count):
        rows += tool_rows(10 + i * 10, 15 + i * 10, f"tool{i}", "Agent",
                          {"task_id": f"sub{i}"})
    rows.append(assistant_row(10 + count * 10, "m-last", write=500))
    session = write_transcript(
        "many-subagents", rows,
        subagents={f"agent-sub{i}": [prompt_row(11 + i * 10, "go"),
                                     assistant_row(13 + i * 10, f"s{i}", write=200)]
                   for i in range(count)})
    a = inspection.read_session(session)
    (turn,) = a.turns
    assert len(turn.subagents) == count

    sealed = autopsy.to_seal(a, level="normal")["turns"][0]

    assert autopsy.SUBAGENT_LIMIT == 20
    assert len(sealed["subagents"]) == 20
    assert sealed["subagents_folded"] == {"count": 1}


def test_the_payload_ceiling_drops_in_the_order_the_spec_states(write_transcript,
                                                                monkeypatch):
    """params, then subagent params, then subagent spans, then spans past the per-turn
    limit — each drop ANNOUNCED. The ceiling is lowered for the test rather than a
    megabyte of synthetic transcript written: the rungs are what is under test, and the
    shipped ceiling is asserted separately."""
    assert autopsy.PAYLOAD_CEILING == 1_000_000

    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    rows += tool_rows(10, 20, "tool0", "Agent", {"task_id": "sub0"})
    rows += [r for i in range(30)
             for r in tool_rows(30 + i * 5, 32 + i * 5, f"b{i}", "Bash",
                                {"description": f"reading file number {i}"})]
    rows.append(assistant_row(400, "m-last", write=900))
    session = write_transcript("fat", rows, subagents={
        "agent-sub0": [prompt_row(11, "go")]
        + [r for i in range(20)
           for r in tool_rows(12 + i, 12.5 + i, f"s{i}", "Bash",
                              {"description": f"subagent read {i}"})]
        + [assistant_row(40, "s-last", write=300)]})
    a = inspection.read_session(session)
    sub_seconds = sum(s.seconds for t in a.turns[0].subagents for tt in t.turns
                      for s in tt.spans)

    whole = len(json.dumps(autopsy.to_seal(a, level="normal")))
    assert "dropped_for_size" not in autopsy.to_seal(a, level="normal")

    # One byte under: the FIRST rung with anything to give up runs, and only it — params
    # are never sealed at `normal`, so rungs 1 and 2 have nothing to drop.
    monkeypatch.setattr(autopsy, "PAYLOAD_CEILING", whole - 1)
    once = autopsy.to_seal(a, level="normal")

    assert once["dropped_for_size"] == ["subagent_spans"]
    assert once["turns"][0]["spans"], "the lead agent's own spans were not touched yet"
    # Announced with NUMBERS, so the seconds still reconcile after the drop.
    sub_fold = once["turns"][0]["subagents"][0]["turns"][0]["spans_folded"]
    assert sub_fold["count"] == 20
    assert sub_fold["seconds"] == pytest.approx(sub_seconds)

    monkeypatch.setattr(autopsy, "PAYLOAD_CEILING", 1)
    every = autopsy.to_seal(a, level="normal")

    assert every["dropped_for_size"] == ["subagent_spans", "spans_over_limit"]
    assert len(json.dumps(every)) < len(json.dumps(once)) < whole
    top = every["turns"][0]
    assert top["spans"] == []
    assert top["spans_folded"]["count"] == 31
    assert top["spans_folded"]["seconds"] == pytest.approx(a.turns[0].tools
                                                           + a.turns[0].blocked)


# -- the redaction battery, over a seal at EITHER level (§3, §6's cases) ---------------
#
# Triggers (`Prompt.quote`) and span detail are user-authored text and both ARE sealed at
# either level, so §6's battery runs over both. `params` are sealed at `full`, so the pair
# shape `_credential_pair` catches — a credential-NAMED dict key holding a credential-
# SHAPED value — is reachable from a seal now and IS asserted, below, along with the
# nested-leaf case. Its ASSIGNMENT form, the same named key inside a sealed string, is
# what the `normal` half of the battery can reach.


def leaky_session(write_transcript, name: str, secret: str, *,
                  command: str, prompt: str) -> str:
    return write_transcript(name, [
        prompt_row(0, prompt),
        *tool_rows(1, 2, "t1", "Bash", {"command": command}),
        assistant_row(10, "m1", write=1_000),
    ])


LEVELS = ("normal", "full")


@pytest.mark.parametrize("level", LEVELS)
def test_a_secret_in_a_bash_command_never_reaches_a_seal_at_either_level(
        write_transcript, level):
    """§6's case over the seal: an undescribed `Bash` call puts the command line in
    `ToolSpan.detail`, which IS sealed, so a token there is sealed too unless redaction
    ran before the value was stored."""
    secret = "sk-live-0ff1ce9a7b3c2d"
    a = inspection.read_session(leaky_session(
        write_transcript, "seal-leaky-bash", secret,
        command=f'curl -H "Authorization: Bearer {secret}" https://x',
        prompt="You are the worker agent for wo-1"))

    sealed = autopsy.to_seal(a, level=level)
    span = sealed["turns"][0]["spans"][0]

    assert secret not in json.dumps(sealed)
    assert "<redacted: an Authorization header value>" in span["detail"]
    if level == "full":
        assert "<redacted: an Authorization header value>" in span["params"]["command"]


@pytest.mark.parametrize("level", LEVELS)
def test_a_credential_named_key_in_a_command_never_reaches_a_seal_at_either_level(
        write_transcript, level):
    """The named-key case in both forms a seal can carry it: an assignment inside a sealed
    string at either level, and the `params` pair itself once `full` seals them."""
    secret = "hunter2000abc"
    a = inspection.read_session(leaky_session(
        write_transcript, "seal-leaky-key", secret,
        command=f"DB_PASSWORD={secret} ./deploy.sh",
        prompt="You are the worker agent for wo-1"))

    sealed = autopsy.to_seal(a, level=level)
    span = sealed["turns"][0]["spans"][0]

    assert secret not in json.dumps(sealed)
    assert inspection.CREDENTIAL_VALUE_MARKER in span["detail"]
    if level == "full":
        assert inspection.CREDENTIAL_VALUE_MARKER in span["params"]["command"]
    else:
        assert "params" not in keys_of(sealed)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("secret, prompt", [
    ("ghp_A1b2C3d4E5f6G7h8I9j0",
     "push it with GH_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0 when the tests pass"),
    ("hunter2000abc", "the staging box takes DB_PASSWORD=hunter2000abc"),
])
def test_a_secret_a_user_typed_never_reaches_a_seal_at_either_level(
        write_transcript, secret, prompt, level):
    """A trigger quote is whatever the user or Jarvis typed, so it carries both of §6's
    cases — and a seal outlives the transcript it was read from, so an unredacted quote is
    a credential the OS keeps for ever in its own database."""
    a = inspection.read_session(leaky_session(
        write_transcript, f"seal-leaky-trigger-{secret[:4]}", secret,
        command="gh pr list", prompt=prompt))
    assert a.turns[0].triggers[0].quote

    sealed = autopsy.to_seal(a, level=level)

    assert secret not in json.dumps(sealed)
    assert inspection.CREDENTIAL_VALUE_MARKER in \
        sealed["turns"][0]["triggers"][0]["quote"]


#: A value the credential test accepts whole and REJECTS once it is cut to five
#: characters: the shape is "six or more with a digit and a letter". Cutting first
#: therefore seals the head of a credential the redaction would have replaced entirely —
#: which is exactly what the ORDER claim is about, and a longer cut proves nothing because
#: the pattern still matches what survives it.
CUTTABLE_SECRET = "a1b2c3d4e5f6g7h8i9j0"
CUT_HEAD = CUTTABLE_SECRET[:5]


@pytest.mark.parametrize("level", LEVELS)
def test_a_sealed_detail_is_redacted_before_it_is_truncated_at_either_level(
        write_transcript, level):
    """Order, in the seal: truncating first would cut the assignment short and seal the
    HEAD of the credential. A cut marker is a cosmetic loss; half a secret is not."""
    command = f"DB_PASSWORD={CUTTABLE_SECRET} ./deploy.sh"
    a = inspection.read_session(
        leaky_session(write_transcript, "seal-cut-detail", CUTTABLE_SECRET,
                      command=command, prompt="You are the worker agent for wo-1"),
        catalog.InspectConfig(quote_chars=len("DB_PASSWORD=") + len(CUT_HEAD)))

    sealed = autopsy.to_seal(a, level=level)
    span = sealed["turns"][0]["spans"][0]

    assert CUT_HEAD not in json.dumps(sealed)
    # The marker's own head is what the cut lands in, which is the cosmetic loss.
    assert span["detail"].startswith("DB_PASSWORD=<red")
    if level == "full":
        assert inspection.CREDENTIAL_VALUE_MARKER in span["params"]["command"]


@pytest.mark.parametrize("level", LEVELS)
def test_a_sealed_trigger_quote_is_redacted_before_it_is_capped_at_either_level(
        write_transcript, level):
    """The same order on the other sealed string: the quote cap must never leave the head
    of a credential standing where the whole value would have been replaced."""
    prompt = f"DB_PASSWORD={CUTTABLE_SECRET} is what staging takes"
    a = inspection.read_session(
        leaky_session(write_transcript, "seal-cut-trigger", CUTTABLE_SECRET,
                      command="gh pr list", prompt=prompt),
        catalog.InspectConfig(quote_chars=len("DB_PASSWORD=") + len(CUT_HEAD)))

    sealed = autopsy.to_seal(a, level=level)

    assert CUT_HEAD not in json.dumps(sealed)
    assert sealed["turns"][0]["triggers"][0]["quote"].startswith("DB_PASSWORD=<red")


# -- the two cases only `params` can carry, reachable from a seal at `full` (§6) --------


def test_a_credential_named_param_key_is_replaced_whole_in_a_full_seal(write_transcript):
    """The pair shape `inspection._credential_pair` catches: a credential-NAMED dict key
    holding a credential-SHAPED value. No sealed string carries it — only `params` do — so
    this case was unreachable from a seal until `full` sealed them."""
    session = write_transcript("seal-pair", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash",
                   {"command": "./deploy.sh", "password": "hunter2000abc"}),
    ])
    a = inspection.read_session(session)

    sealed = autopsy.to_seal(a, level="full")

    assert "hunter2000abc" not in json.dumps(sealed)
    assert sealed["turns"][0]["spans"][0]["params"]["password"] == \
        inspection.CREDENTIAL_VALUE_MARKER


def test_a_secret_in_a_nested_edit_never_reaches_a_full_seal(write_transcript):
    """`test_a_secret_in_a_nested_edit_never_reaches_the_payload`'s case over the seal: a
    `MultiEdit` input is a list of dicts, so the credential is a LEAF that neither
    assignment regex sees after `json.dumps`."""
    token = "ghp_A1b2C3d4E5f6G7h8I9j0"
    session = write_transcript("seal-nested-edit", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "MultiEdit",
                   {"file_path": "x",
                    "edits": [{"old_string": "a", "new_string": f"GH_TOKEN={token}"}]}),
    ])
    a = inspection.read_session(session)

    sealed = autopsy.to_seal(a, level="full")

    assert token not in json.dumps(sealed)
    assert inspection.CREDENTIAL_VALUE_MARKER in \
        sealed["turns"][0]["spans"][0]["params"]["edits"]


def test_a_sealed_param_value_is_redacted_before_it_is_truncated(write_transcript):
    """The order claim on a sealed PARAM, where `ParamCaps.per_value` is 500 and is not
    configurable: the cut is built to land FIVE characters into the secret, so cutting
    first would seal `CUT_HEAD` — which the credential SHAPE no longer matches, making a
    later redaction incapable of removing it."""
    caps = inspection.PARAM_CAPS
    head, assign, tail = "echo ", " DB_PASSWORD=", " ./deploy.sh"
    filler = "x" * (caps.per_value - len(head) - len(assign) - len(CUT_HEAD))
    command = head + filler + assign + CUTTABLE_SECRET + tail
    assert len(command) > caps.per_value, "the cut must really happen"
    assert command[:caps.per_value] == head + filler + assign + CUT_HEAD
    a = inspection.read_session(write_transcript("seal-cut-param", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 2, "t1", "Bash", {"description": "deploy", "command": command}),
    ]))

    sealed = autopsy.to_seal(a, level="full")
    span = sealed["turns"][0]["spans"][0]

    assert CUT_HEAD not in json.dumps(sealed)
    assert "DB_PASSWORD=<red" in span["params"]["command"]
    assert "command" in span["params_truncated"]


def test_a_sealed_param_dropped_for_the_turn_budget_is_announced_and_leaks_nothing(
        write_transcript):
    """The other order claim: redact BEFORE cap. Once a turn has spent `per_turn` the
    later span's keys are DROPPED, and the seal must say which — a payload that drops
    silently is not reproducible, and the value must never have carried the secret."""
    caps = inspection.PARAM_CAPS
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    for i in range(caps.per_turn // caps.per_span):
        rows += tool_rows(1 + i, 2 + i, f"t{i}", "Bash",
                          {f"k{j}": "c" * (caps.per_span // 4) for j in range(4)})
    rows += tool_rows(500, 501, "t-last", "Bash",
                      {"description": "deploy",
                       "command": "DB_PASSWORD=hunter2000abc ./deploy.sh"})
    a = inspection.read_session(write_transcript("seal-burn", rows))

    sealed = autopsy.to_seal(a, level="full")
    last = sealed["turns"][0]["spans"][-1]

    assert "hunter2000abc" not in json.dumps(sealed)
    assert last["params"] == {}
    assert last["params_dropped"] == ["description", "command"]


# -- nested subagents at `full`, and the field a rehydration must not lose (§6) ---------


def test_a_nested_subagent_s_params_survive_the_round_trip_at_full(write_transcript,
                                                                  tmp_path):
    """§6's other half: nested subagent anatomies WITH their params. The nesting is
    recursive, so the level has to reach a subagent's own spans and not only the lead's."""
    session = write_transcript("nested-full", parent_rows(),
                              subagents={f"agent-{TASK}": sub_rows(1100, write=25_000)})
    write_meta(tmp_path, session, TASK, "explorer")
    a = inspection.read_session(session)

    sealed = autopsy.to_seal(a, level="full")

    assert sealed["turns"][0]["subagents"][0]["turns"][0]["spans"][0]["params"] == \
        {"pattern": "needle"}
    assert autopsy.from_seal(sealed, spans=[]).as_dict() == a.as_dict()


def test_a_subagent_s_threshold_free_figures_survive_the_seal(write_transcript):
    """Spec 2026-10-02 §1.3: a sealed order is the only reading left once the transcript
    expires, so a field the seal drops becomes a silent zero on every settled order —
    the exact failure mode this spec fixes."""
    rows = under_floor_rows(1100) + [
        # One cache read going BACKWARDS, so there is a boundary to round-trip too.
        assistant_row(1200, "s-back", write=5_000, read=1_000)]
    session = write_transcript("sealed-floor", parent_rows(),
                               subagents={f"agent-{TASK}": rows})
    a = inspection.read_session(session, cold_prefix_floor=50_000)
    live = a.turns[0].subagents[0]

    back = autopsy.from_seal(autopsy.to_seal(a, level="full"), spans=[])
    sealed = back.turns[0].subagents[0]

    assert sealed.total_written == live.total_written == 35_000
    assert sealed.max_write == live.max_write == 5_000
    assert sealed.write_floor == live.write_floor == 20_000
    assert sealed.api_call_count == live.api_call_count == 7
    assert [b.cause for b in sealed.boundaries] == [b.cause for b in live.boundaries]
    assert len(sealed.boundaries) == 1
    assert sealed.as_dict() == live.as_dict()
    assert back.as_dict() == a.as_dict()


def test_a_secret_in_a_nested_subagent_s_params_never_reaches_a_full_seal(
        write_transcript, tmp_path):
    """The nesting is REDACTED and not copied: the secret lives only in the subagent's own
    span params, which is a place the lead's sealed strings can never speak for."""
    secret = "sk-live-0ff1ce9a7b3c2d"
    session = write_transcript("nested-leak", parent_rows(), subagents={
        f"agent-{TASK}": [
            prompt_row(1100, "do the thing", sdk=False),
            *tool_rows(1101, 1102, "s-t1", "Bash",
                       {"description": "call the API",
                        "command": f'curl -H "Authorization: Bearer {secret}" https://x'}),
        ]})
    write_meta(tmp_path, session, TASK, "explorer")
    a = inspection.read_session(session)
    sub_span = a.turns[0].subagents[0].turns[0].spans[0]
    assert sub_span.detail == "call the API", "the secret is only in the params"

    sealed = autopsy.to_seal(a, level="full")

    assert secret not in json.dumps(sealed)
    assert "<redacted: an Authorization header value>" in \
        sealed["turns"][0]["subagents"][0]["turns"][0]["spans"][0]["params"]["command"]


def test_a_backgrounded_delegation_span_is_still_backgrounded_after_the_seal(
        write_transcript):
    """Neo 1124: `backgrounded` is sealed at EVERY level because `ToolSpan.is_join`
    derives from it. Without the field a backgrounded `Agent` rehydrates as a join and
    `Turn.blocked` counts seconds the live reading never did."""
    a = inspection.read_session(write_transcript("backgrounded", [
        prompt_row(0, "You are the worker agent for wo-1"),
        *tool_rows(1, 100, "bg", "Agent",
                   {"description": "d", "run_in_background": True}),
        *tool_rows(101, 150, "fg", "Agent", {"description": "d"}),
    ]))
    live = a.turns[0]
    bg, fg = live.spans
    assert (bg.backgrounded, bg.is_join) == (True, False)
    assert (fg.backgrounded, fg.is_join) == (False, True)

    back = autopsy.from_seal(autopsy.to_seal(a, level="normal"), spans=[]).turns[0]

    sealed_bg, sealed_fg = back.spans
    assert (sealed_bg.backgrounded, sealed_bg.is_join) == (True, False)
    assert (sealed_fg.backgrounded, sealed_fg.is_join) == (False, True)
    assert back.blocked == live.blocked


def fat_full_session(write_transcript, name: str):
    """A session with params on the lead's spans AND a nested subagent's, fat enough that
    the ceiling bites — the two params rungs and the sentence they force share it."""
    rows = [prompt_row(0, "You are the worker agent for wo-1")]
    rows += tool_rows(10, 20, "tool0", "Agent", {"task_id": "sub0"})
    rows += [r for i in range(30)
             for r in tool_rows(30 + i * 5, 32 + i * 5, f"b{i}", "Bash",
                                {"description": f"reading file number {i}"})]
    rows.append(assistant_row(400, "m-last", write=900))
    session = write_transcript(name, rows, subagents={
        "agent-sub0": [prompt_row(11, "go")]
        + [r for i in range(20)
           for r in tool_rows(12 + i, 12.5 + i, f"s{i}", "Bash",
                              {"description": f"subagent read {i}"})]
        + [assistant_row(40, "s-last", write=300)]})
    return inspection.read_session(session)


def params_dropped_seal(a, monkeypatch, *, rungs: int) -> dict:
    """The same `full` seal after `rungs` of the ceiling's params rungs have bitten.

    `_fit` measures with `db.to_json`, so the ceiling is set in THOSE bytes, and the
    second step measures the payload WITHOUT its own announcement.
    """
    whole = len(db.to_json(autopsy.to_seal(a, level="full")))
    monkeypatch.setattr(autopsy, "PAYLOAD_CEILING", whole - 1)
    payload = autopsy.to_seal(a, level="full")
    if rungs > 1:
        unannounced = {k: v for k, v in payload.items() if k != "dropped_for_size"}
        monkeypatch.setattr(autopsy, "PAYLOAD_CEILING",
                            len(db.to_json(unannounced)) - 1)
        payload = autopsy.to_seal(a, level="full")
    return payload


def test_the_payload_ceiling_drops_the_two_params_rungs_first_at_full(write_transcript,
                                                                     monkeypatch):
    """§6 activates rungs 1 and 2 of section 3's ceiling: at `full` the parameters are the
    FIRST thing given up, the lead's before the subagents'."""
    a = fat_full_session(write_transcript, "fat-full")

    # `_fit` measures with `db.to_json`, so the ceiling is set in THOSE bytes.
    whole = len(db.to_json(autopsy.to_seal(a, level="full")))
    monkeypatch.setattr(autopsy, "PAYLOAD_CEILING", whole - 1)
    once = autopsy.to_seal(a, level="full")

    assert once["dropped_for_size"] == ["params"]
    assert not (keys_of(once["turns"][0]["spans"][0]) & set(PARAM_KEYS))
    assert once["turns"][0]["subagents"][0]["turns"][0]["spans"][0]["params"]

    # The ceiling is checked BEFORE `dropped_for_size` is added, so the announcement's own
    # bytes are not part of what the next rung is measured against.
    unannounced = {k: v for k, v in once.items() if k != "dropped_for_size"}
    monkeypatch.setattr(autopsy, "PAYLOAD_CEILING", len(db.to_json(unannounced)) - 1)
    twice = autopsy.to_seal(a, level="full")

    assert twice["dropped_for_size"][:2] == ["params", "subagent_params"]
    assert not (keys_of(twice["turns"][0]["subagents"][0]) & set(PARAM_KEYS))


# -- the empty-params sentence, three states and one renderer (§6) ----------------------


def test_an_empty_params_at_normal_says_the_level_did_not_record_them():
    note = autopsy.params_note(autopsy.NORMAL, False)

    assert "not recorded at this level" in note


def test_an_empty_params_at_full_says_the_order_ran_no_tools():
    note = autopsy.params_note(autopsy.FULL, False)

    assert "ran no tools" in note


def test_an_empty_params_on_a_seal_predating_the_level_is_a_third_state():
    """The ambiguity §6 exists to close must not come back as a default: three levels,
    three DIFFERENT claims, and nothing to say when there ARE parameters."""
    notes = [autopsy.params_note(level, False)
             for level in (autopsy.NORMAL, autopsy.FULL, autopsy.UNKNOWN)]

    assert len(set(notes)) == 3
    assert all(notes)
    assert "not recorded at this level" not in notes[2]
    assert "ran no tools" not in notes[2]
    assert autopsy.params_note(autopsy.FULL, True) == ""


def test_a_full_seal_whose_params_the_ceiling_dropped_does_not_claim_it_ran_no_tools(
        write_transcript, monkeypatch):
    """The sentence would LIE: both params rungs went to fit the ceiling, so the payload
    holds no `params` key anywhere about an order that ran tools by the dozen."""
    a = fat_full_session(write_transcript, "fat-note")
    twice = params_dropped_seal(a, monkeypatch, rungs=2)
    assert twice["dropped_for_size"][:2] == ["params", "subagent_params"]
    assert "params" not in keys_of(twice)

    provenance = autopsy._provenance(autopsy.SEALED, anatomy=a, sealed=twice)

    assert "ran no tools" not in provenance["params_note"]
    assert "dropped to fit the payload ceiling" in provenance["params_note"]
    assert "this SEAL" in provenance["params_note"]


def test_only_the_lead_s_params_dropped_is_still_explained_though_some_remain(
        write_transcript, monkeypatch):
    """Rung 1 alone: the nested subagents keep theirs, so `any_params` is True and the
    short-circuit would leave the lead's absence unexplained. The dropped case is decided
    FIRST for exactly this."""
    a = fat_full_session(write_transcript, "fat-note-one")
    once = params_dropped_seal(a, monkeypatch, rungs=1)
    assert once["dropped_for_size"] == ["params"]
    assert "params" in keys_of(once), "the subagents still carry theirs"

    provenance = autopsy._provenance(autopsy.SEALED, anatomy=a, sealed=once)

    assert provenance["params"] is True
    assert "dropped to fit the payload ceiling" in provenance["params_note"]
    assert provenance["params_note"] == autopsy.params_note(autopsy.FULL, True,
                                                            ["params"])


def test_the_renderer_prints_the_dropped_for_size_sentence_too(write_transcript,
                                                               monkeypatch, capsys):
    """The case a wrong sentence damages most: the renderer composes none of this one
    either."""
    a = fat_full_session(write_transcript, "fat-render")
    twice = params_dropped_seal(a, monkeypatch, rungs=2)
    provenance = autopsy._provenance(autopsy.SEALED, anatomy=a, sealed=twice)

    cli._print_autopsy_provenance(provenance)

    out = capsys.readouterr().out
    assert provenance["params_note"] in out
    assert "ran no tools" not in out


def test_the_provenance_carries_the_sentence_and_the_renderer_prints_it(real_session,
                                                                       capsys):
    """The renderer composes NOTHING: the sentence it prints is the payload's own."""
    sealed = autopsy.to_seal(real_session, level="normal")
    sealed["sealed_at"] = 5.0
    provenance = autopsy._provenance(autopsy.SEALED, anatomy=real_session, sealed=sealed)

    assert provenance["params_note"] == autopsy.params_note(autopsy.NORMAL, False)

    cli._print_autopsy_provenance(provenance)

    assert provenance["params_note"] in capsys.readouterr().out


# -- the daemon step, and the level that gates it ---------------------------------------


@pytest.fixture()
def spec(registered):
    return catalog.ProjectSpec(name="proj_a", path=registered)


#: The lead's verbatim parameters on the daemon's session — §6's pass-through is only
#: observable if there IS a parameter to find in `autopsy_json`.
DAEMON_PARAMS = {"description": "list the tree", "command": "ls -la /tmp/proj"}


def at_level(spec, level: str):
    """The same project at one observability level — §5's gate is a REAL level, never a
    patched predicate."""
    return dataclasses.replace(
        spec, observability=catalog.ObservabilityConfig(level=level))


def settled_with_a_session(store, write_transcript, session: str,
                           tools: bool = False) -> dict:
    """One settled order with a transcript. `tools=True` adds a span carrying
    `DAEMON_PARAMS`: the call counts other daemon tests pin stay at one without it."""
    wo = store.create_work_order("an order that finished", "")
    store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                       (session, wo["id"]))
    store.conn.commit()
    write_transcript(session, [prompt_row(0, "You are the worker agent for wo-1"),
                               *(tool_rows(2, 4, "t1", "Bash", DAEMON_PARAMS)
                                 if tools else []),
                               assistant_row(10, "m1", write=30_000)])
    store.set_status(wo["id"], "completed")
    return store.get_work_order(wo["id"])


def test_the_daemon_seals_every_settled_order_and_only_once(store, spec,
                                                            write_transcript,
                                                            monkeypatch):
    """One place to get it right, and it catches the orders that settled before this
    existed while their evidence is still on disk. The project names no level, so §5's
    fleet default `normal` seals it."""
    wo = settled_with_a_session(store, write_transcript, "sess-tick")
    open_wo = store.create_work_order("still running", "")
    store.set_status(open_wo["id"], "running")
    walks = []
    real_index = usage.index_sessions
    monkeypatch.setattr(usage, "index_sessions",
                        lambda *a, **k: (walks.append(1), real_index(*a, **k))[1])

    daemon = Daemon.__new__(Daemon)
    daemon.seal_autopsies(spec, store)

    sealed = store.get_work_order(wo["id"])
    assert sealed["autopsy_json"] and sealed["autopsy_sealed_at"]
    assert len(walks) == 1, "one index of the transcript tree for the whole batch"
    # An open order is not sealed: it has not finished spending.
    assert store.get_work_order(open_wo["id"])["autopsy_json"] is None
    assert store.unsealed_autopsy_orders() == []
    was = sealed["autopsy_sealed_at"]
    daemon.seal_autopsies(spec, store)
    assert store.get_work_order(wo["id"])["autopsy_sealed_at"] == was


def test_an_order_whose_autopsy_raises_leaves_the_queue_with_an_error_payload(
        store, spec, write_transcript, monkeypatch):
    """Sealed EMPTY rather than left pending: one order that cannot be read must not park
    itself at the head of the queue and block every order behind it for ever."""
    monkeypatch.setattr(autopsy, "seal", lambda *a, **k: 1 / 0)
    wo = settled_with_a_session(store, write_transcript, "sess-broken")

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    row = store.get_work_order(wo["id"])
    assert "error" in json.loads(row["autopsy_json"])
    assert store.unsealed_autopsy_orders() == []


def test_a_project_at_level_off_has_no_autopsy_sealed_at_all(store, spec,
                                                             write_transcript):
    """§5: the gate is `level_for(...) != OFF`, so `off` is how a project declines and
    nothing is frozen onto its orders."""
    off = at_level(spec, "off")
    wo = settled_with_a_session(store, write_transcript, "sess-off")

    assert autopsy.records_autopsy(wo, off.observability) is False

    Daemon.seal_autopsies(Daemon.__new__(Daemon), off, store)

    assert store.get_work_order(wo["id"])["autopsy_json"] is None
    assert [o["id"] for o in store.unsealed_autopsy_orders()] == [wo["id"]]


def test_a_project_at_level_normal_seals_and_the_payload_says_normal(store, spec,
                                                                    write_transcript):
    """§5: `normal` covers the autopsy, and the level the gate resolved is recorded as
    `autopsy_level`."""
    wo = settled_with_a_session(store, write_transcript, "sess-normal")

    Daemon.seal_autopsies(Daemon.__new__(Daemon), at_level(spec, "normal"), store)

    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert payload["autopsy_level"] == "normal"


def test_a_project_at_level_full_seals_with_full_through_the_daemon(store, spec,
                                                                   write_transcript):
    """§5's pass-through, end to end: the resolved level is used TWICE, as the gate and as
    `seal(level=...)`. Without the second use `full` ships dark while every test passes."""
    wo = settled_with_a_session(store, write_transcript, "sess-full")

    Daemon.seal_autopsies(Daemon.__new__(Daemon), at_level(spec, "full"), store)

    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert payload["autopsy_level"] == "full"
    assert autopsy.level_of(payload) == "full"


def test_the_daemon_at_level_full_writes_the_verbatim_params_into_autopsy_json(
        store, spec, write_transcript):
    """§6 through §5's pass-through: the STORED payload is what a reader gets, so the
    parameters have to survive the daemon step and not only `to_seal`."""
    wo = settled_with_a_session(store, write_transcript, "sess-full-params",
                                tools=True)

    Daemon.seal_autopsies(Daemon.__new__(Daemon), at_level(spec, "full"), store)

    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert "params" in keys_of(payload)
    assert payload["turns"][0]["spans"][0]["params"] == DAEMON_PARAMS


def test_the_daemon_at_level_normal_writes_no_params_key_into_autopsy_json_at_all(
        store, spec, write_transcript):
    """The other half of the same seam, over the same session with the same tool: at
    `normal` there is no `params` key ANYWHERE, not an empty one."""
    wo = settled_with_a_session(store, write_transcript, "sess-normal-params",
                                tools=True)

    Daemon.seal_autopsies(Daemon.__new__(Daemon), at_level(spec, "normal"), store)

    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert not (keys_of(payload) & set(PARAM_KEYS))


def test_a_project_with_no_observability_block_is_still_sealed(store, spec,
                                                               write_transcript):
    """§5: where "opt in per project" and "the default is `normal`" collide, the DEFAULT
    wins — a project never has to name a level before its orders are sealed."""
    parsed = catalog.parse_catalog({"projects": [{"name": "proj_a", "path": "/tmp/a"}]})
    silent = dataclasses.replace(spec, observability=parsed.projects[0].observability)
    wo = settled_with_a_session(store, write_transcript, "sess-no-block")

    assert silent.observability.level == "normal"

    Daemon.seal_autopsies(Daemon.__new__(Daemon), silent, store)

    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert payload["autopsy_level"] == "normal"


def test_an_autopsy_still_answers_once_the_transcript_is_gone(store, spec,
                                                             write_transcript,
                                                             tmp_path):
    """The whole reason to seal: Claude Code prunes transcripts on its own schedule, and
    an order read afterwards must not report a shorter clock than it ran."""
    wo = settled_with_a_session(store, write_transcript, "sess-pruned")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    before = autopsy.unseal(store.get_work_order(wo["id"]))

    (tmp_path / "projects" / "-proj" / "sess-pruned.jsonl").unlink()
    assert inspection.read_session("sess-pruned").found is False

    after = autopsy.unseal(store.get_work_order(wo["id"]))

    assert after == before
    back = autopsy.from_seal(after, spans=[])
    assert back.found is True and [len(t.calls) for t in back.turns] == [1]
    assert [w.written for w in back.writes] == [30_000]


# -- the versioned re-seal -------------------------------------------------------------


def test_an_old_payload_is_upgraded_from_itself_and_never_from_the_transcript(
        store, spec, write_transcript, monkeypatch, tmp_path):
    """The one place a naive copy of the bill is wrong. Re-reading an EXPIRED transcript
    would overwrite good data with nothing, so an upgrade reads the stored payload ONLY.
    `autopsy_sealed_at` is preserved — when the order settled is not changed by
    re-deriving its payload — and `resealed_at` says the re-derivation happened."""
    wo = settled_with_a_session(store, write_transcript, "sess-old")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    row = store.get_work_order(wo["id"])
    was = row["autopsy_sealed_at"]
    (tmp_path / "projects" / "-proj" / "sess-old.jsonl").unlink()

    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)
    fresh = autopsy._upgrade_seal("proj_a", store.project_path, row,
                                  autopsy.unseal(row))

    assert fresh["payload_v"] == autopsy.PAYLOAD_VERSION
    assert fresh["resealed_at"] > 0
    assert [len(t["calls"]) for t in fresh["turns"]] == [1], "upgraded from itself"
    again = store.get_work_order(wo["id"])
    assert again["autopsy_sealed_at"] == was
    assert json.loads(again["autopsy_json"])["payload_v"] == autopsy.PAYLOAD_VERSION


def test_nothing_to_upgrade_leaves_the_seal_alone(store, spec, write_transcript):
    wo = settled_with_a_session(store, write_transcript, "sess-current")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    row = store.get_work_order(wo["id"])

    assert autopsy._upgrade_seal("proj_a", store.project_path, row,
                                 autopsy.unseal(row)) is None


def test_a_fresh_reading_at_a_higher_floor_showing_fewer_writes_is_refused(monkeypatch):
    """An autopsy has NO monotone quantity: a parser fix can yield fewer spans and a
    raised floor fewer writes. So the counts are compared AT THE SAME FLOORS, and a floor
    artefact is never adopted as a correction."""
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(FIXTURE_ROOT))
    a = inspection.read_session(FIXTURE_SESSION)
    sealed = autopsy.to_seal(a, level="normal")
    higher = inspection.read_session(
        FIXTURE_SESSION, catalog.InspectConfig(report_write_floor=180_000))
    assert len(higher.writes) < len(sealed["writes"])

    assert autopsy.adopts_fresh_reading(sealed, higher) is False
    assert autopsy.adopts_fresh_reading(sealed, a) is True


def test_a_reading_that_lost_the_transcript_is_refused(monkeypatch, tmp_path):
    """`found` is false, so there is nothing to adopt and the old seal stands."""
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(FIXTURE_ROOT))
    sealed = autopsy.to_seal(inspection.read_session(FIXTURE_SESSION), level="normal")
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(tmp_path / "gone"))

    gone = inspection.read_session(FIXTURE_SESSION)

    assert gone.found is False
    assert autopsy.adopts_fresh_reading(sealed, gone) is False


def test_a_tick_re_seals_a_settled_order_whose_payload_predates_the_current_version(
        store, spec, write_transcript, monkeypatch, tmp_path):
    """The WRITER owns the re-seal end to end, on a second bounded queue in the same tick
    step, so a stale seal repairs itself instead of waiting for a reader. The transcript
    is DELETED before the tick: the upgrade reads the stored payload only."""
    wo = settled_with_a_session(store, write_transcript, "sess-stale-tick")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    was = store.get_work_order(wo["id"])["autopsy_sealed_at"]
    (tmp_path / "projects" / "-proj" / "sess-stale-tick.jsonl").unlink()

    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)
    assert [o["id"] for o in
            store.stale_autopsy_orders(autopsy.PAYLOAD_VERSION)] == [wo["id"]]

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    row = store.get_work_order(wo["id"])
    payload = json.loads(row["autopsy_json"])
    assert payload["payload_v"] == autopsy.PAYLOAD_VERSION
    assert payload["resealed_at"] > 0
    assert row["autopsy_sealed_at"] == was, "when it settled did not change"
    assert [len(t["calls"]) for t in payload["turns"]] == [1], "from itself"
    assert store.stale_autopsy_orders(autopsy.PAYLOAD_VERSION) == []


def test_a_tick_re_seals_a_payload_written_before_the_version_field_existed(
        store, spec, write_transcript, monkeypatch):
    """The reason the query COALESCEs: a payload frozen before `payload_v` was sealed has
    no such key, `json_extract` gives NULL, and NULL compares to nothing — so without it
    the oldest seals in the fleet are the ones that never repair."""
    wo = settled_with_a_session(store, write_transcript, "sess-no-version")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    payload = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    payload.pop("payload_v")
    store.seal_autopsy(wo["id"], json.dumps(payload))
    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)

    assert [o["id"] for o in
            store.stale_autopsy_orders(autopsy.PAYLOAD_VERSION)] == [wo["id"]]

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    again = json.loads(store.get_work_order(wo["id"])["autopsy_json"])
    assert again["payload_v"] == autopsy.PAYLOAD_VERSION


def test_a_tick_writes_nothing_over_a_seal_that_is_already_current(store, spec,
                                                                  write_transcript):
    """An upgrade that returns None leaves the seal alone — not retried into a loop and
    not logged as a failure."""
    wo = settled_with_a_session(store, write_transcript, "sess-current-tick")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    before = store.get_work_order(wo["id"])

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    after = store.get_work_order(wo["id"])
    assert after["autopsy_json"] == before["autopsy_json"]
    assert after["autopsy_sealed_at"] == before["autopsy_sealed_at"]


def test_a_project_at_level_off_leaves_a_stale_seal_unupgraded_too(store, spec,
                                                                   write_transcript,
                                                                   monkeypatch):
    """§5's gate covers BOTH queues: at `off` a stale seal is not upgraded either."""
    wo = settled_with_a_session(store, write_transcript, "sess-off-stale")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    before = store.get_work_order(wo["id"])["autopsy_json"]

    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)
    Daemon.seal_autopsies(Daemon.__new__(Daemon), at_level(spec, "off"), store)

    assert store.get_work_order(wo["id"])["autopsy_json"] == before


def test_an_order_whose_sealed_payload_is_not_json_does_not_stop_the_stale_queue(
        store, spec, write_transcript, monkeypatch):
    """`json_extract` RAISES on malformed JSON, so one unparseable payload made the query
    throw and stalled the whole project's autopsy tick. It is simply not on the queue: the
    other stale order still comes back and the tick still upgrades it."""
    bad = settled_with_a_session(store, write_transcript, "sess-bad-json")
    good = settled_with_a_session(store, write_transcript, "sess-good-json")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    store.seal_autopsy(bad["id"], "not json")

    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)

    assert [o["id"] for o in
            store.stale_autopsy_orders(autopsy.PAYLOAD_VERSION)] == [good["id"]]

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    assert json.loads(store.get_work_order(good["id"])["autopsy_json"])[
        "payload_v"] == autopsy.PAYLOAD_VERSION
    assert store.get_work_order(bad["id"])["autopsy_json"] == "not json"


def test_a_seal_predating_the_threshold_free_keys_rehydrates_absent_and_not_zero(
        write_transcript):
    """Spec 2026-10-02 §1.3 applied to seals written BEFORE the fields existed: every
    settled order on this box is one. `row.get("total_written", 0)` turned an ABSENT
    figure into a measured zero, and the renderer then printed `wrote nothing to the
    cache` over a subagent that wrote 334,427 tokens (wo-fb7c0fc2, a8e11a7e)."""
    session = write_transcript("old-seal", parent_rows(),
                               subagents={f"agent-{TASK}": under_floor_rows(1100)})
    sealed = autopsy.to_seal(inspection.read_session(session), level="normal")
    row = sealed["turns"][0]["subagents"][0]
    for key in ("total_written", "max_write", "write_floor", "api_call_count",
                "boundaries"):
        del row[key]

    sub = autopsy.from_seal(sealed, spans=[]).turns[0].subagents[0]

    assert sub.total_written is None and sub.max_write is None
    assert sub.write_floor is None and sub.api_call_count is None
    assert sub.boundaries == []
    assert sub.rewrite() is None
    assert sub.as_dict()["rewrite"] is None
    assert sub.as_dict()["total_written"] is None


def test_a_seal_from_the_current_code_round_trips_a_real_measured_zero(
        write_transcript):
    """The other half: a genuine `total_written == 0` is a MEASUREMENT and must survive
    the seal as 0, never as `None`."""
    session = write_transcript("zero-seal", parent_rows(),
                               subagents={f"agent-{TASK}": sub_rows(1100)})
    a = inspection.read_session(session)
    live = a.turns[0].subagents[0]
    sealed = autopsy.from_seal(autopsy.to_seal(a, level="normal"),
                               spans=[]).turns[0].subagents[0]

    assert live.total_written == 0 and sealed.total_written == 0
    assert sealed.max_write == 0 and sealed.api_call_count == 3
    assert sealed.write_floor == 20_000
    assert sealed.rewrite()["cache_write"] == 0
