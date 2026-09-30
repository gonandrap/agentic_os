"""The sealed autopsy: does a rehydrated reading say exactly what the live one said.

The seal exists because every source an autopsy is read from expires — Claude Code prunes
session transcripts and the subagent files beside them — so an order inspected six months
after it settled would otherwise report a clock that got shorter as the evidence aged.

Two claims are tested here and nothing else, because nothing else reads a seal yet (§3
ships dark). First, the FUNCTION-LEVEL ROUND TRIP: `from_seal(to_seal(a))` renders the
same `as_dict` the live `Anatomy` renders, which is what makes a seal a substitute for the
transcript rather than a summary of it. Second, THE CAPS: a seal is bounded, and every
fold carries numbers that reconcile against the uncapped reading — announcing a key is not
proving a cap.

The round trip is pinned on the committed fixture session (`tests/test_inspection.py`'s
`real_session`) because its three cache writes and two joins were measured from the
unredacted transcript; the caps are pinned on synthetic transcripts under `tmp_path`,
because a committed fixture with 505 spans in one turn would be a file written to agree
with the answer.
"""

from __future__ import annotations

import json

import pytest

from jarvis import autopsy, catalog, holds, inspection, usage
from jarvis.daemon import Daemon

from tests.test_cost_report import registered, store  # noqa: F401
from tests.test_inspection import (  # noqa: F401
    FIXTURE_ROOT,
    FIXTURE_SESSION,
    assistant_row,
    prompt_row,
    real_session,
    tool_rows,
    write_transcript,
)

PARAM_KEYS = ("params", "params_truncated", "params_dropped")


def without_params(value):
    """The same payload with every parameter key gone, at any depth.

    §3: tool parameters belong to the `full` level, which is section 6's — so the round
    trip is asserted over everything EXCEPT them rather than over a fixture whose params
    have been cleared, which would pass against a `to_seal` that dropped nothing.
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
    # The seal must NOT carry the parameters — at EITHER level, this child.
    assert "params" not in keys_of(sealed)


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
    # All four of the figures a fold would otherwise falsify, reconciled.
    assert (back.turns[0].usage + usage_of_fold(fold)).as_dict() == turn.usage.as_dict()
    assert max(back.turns[0].context_peak, fold["context_peak"]) == turn.context_peak
    ttl = back.cache_ttl()
    assert {k: ttl[k] + fold["cache_ttl"][k] for k in ttl} == a.cache_ttl()
    written = sum(c.cache_write for c in back.turns[0].calls) + fold["cache_write"]
    assert max(0, written - turn.context_peak) == a.rewrite_excess()


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


# -- the redaction battery, over a `normal` seal (§3, §6's cases) ----------------------
#
# Triggers (`Prompt.quote`) and span detail are user-authored text and both ARE sealed
# here, so §6's battery runs over the seal too. `params` are NOT sealed at either level in
# this child, so the pair shape `_credential_pair` catches — a credential-NAMED dict key
# holding a credential-SHAPED value — is unreachable from a seal and is not asserted here:
# it would pass against a payload that carried nothing. Its ASSIGNMENT form, the same
# named key inside a sealed string, is.


def leaky_session(write_transcript, name: str, secret: str, *,
                  command: str, prompt: str) -> str:
    return write_transcript(name, [
        prompt_row(0, prompt),
        *tool_rows(1, 2, "t1", "Bash", {"command": command}),
        assistant_row(10, "m1", write=1_000),
    ])


def test_a_secret_in_a_bash_command_never_reaches_a_normal_seal(write_transcript):
    """§6's case over the seal: an undescribed `Bash` call puts the command line in
    `ToolSpan.detail`, which IS sealed, so a token there is sealed too unless redaction
    ran before the value was stored."""
    secret = "sk-live-0ff1ce9a7b3c2d"
    a = inspection.read_session(leaky_session(
        write_transcript, "seal-leaky-bash", secret,
        command=f'curl -H "Authorization: Bearer {secret}" https://x',
        prompt="You are the worker agent for wo-1"))

    sealed = autopsy.to_seal(a, level="normal")

    assert secret not in json.dumps(sealed)
    assert "<redacted: an Authorization header value>" in \
        sealed["turns"][0]["spans"][0]["detail"]


def test_a_credential_named_key_in_a_command_never_reaches_a_normal_seal(
        write_transcript):
    """The named-key case in the only form a seal can carry it: an assignment inside a
    string, not a `params` pair. `params` are section 6's and are sealed at neither level
    here."""
    secret = "hunter2000abc"
    a = inspection.read_session(leaky_session(
        write_transcript, "seal-leaky-key", secret,
        command=f"DB_PASSWORD={secret} ./deploy.sh",
        prompt="You are the worker agent for wo-1"))

    sealed = autopsy.to_seal(a, level="normal")

    assert secret not in json.dumps(sealed)
    assert inspection.CREDENTIAL_VALUE_MARKER in sealed["turns"][0]["spans"][0]["detail"]
    assert "params" not in keys_of(sealed)


@pytest.mark.parametrize("secret, prompt", [
    ("ghp_A1b2C3d4E5f6G7h8I9j0",
     "push it with GH_TOKEN=ghp_A1b2C3d4E5f6G7h8I9j0 when the tests pass"),
    ("hunter2000abc", "the staging box takes DB_PASSWORD=hunter2000abc"),
])
def test_a_secret_a_user_typed_never_reaches_a_normal_seal(write_transcript, secret,
                                                           prompt):
    """A trigger quote is whatever the user or Jarvis typed, so it carries both of §6's
    cases — and a seal outlives the transcript it was read from, so an unredacted quote is
    a credential the OS keeps for ever in its own database."""
    a = inspection.read_session(leaky_session(
        write_transcript, f"seal-leaky-trigger-{secret[:4]}", secret,
        command="gh pr list", prompt=prompt))
    assert a.turns[0].triggers[0].quote

    sealed = autopsy.to_seal(a, level="normal")

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


def test_a_sealed_detail_is_redacted_before_it_is_truncated(write_transcript):
    """Order, in the seal: truncating first would cut the assignment short and seal the
    HEAD of the credential. A cut marker is a cosmetic loss; half a secret is not."""
    command = f"DB_PASSWORD={CUTTABLE_SECRET} ./deploy.sh"
    a = inspection.read_session(
        leaky_session(write_transcript, "seal-cut-detail", CUTTABLE_SECRET,
                      command=command, prompt="You are the worker agent for wo-1"),
        catalog.InspectConfig(quote_chars=len("DB_PASSWORD=") + len(CUT_HEAD)))

    sealed = autopsy.to_seal(a, level="normal")

    assert CUT_HEAD not in json.dumps(sealed)
    # The marker's own head is what the cut lands in, which is the cosmetic loss.
    assert sealed["turns"][0]["spans"][0]["detail"].startswith("DB_PASSWORD=<red")


def test_a_sealed_trigger_quote_is_redacted_before_it_is_capped(write_transcript):
    """The same order on the other sealed string: the quote cap must never leave the head
    of a credential standing where the whole value would have been replaced."""
    prompt = f"DB_PASSWORD={CUTTABLE_SECRET} is what staging takes"
    a = inspection.read_session(
        leaky_session(write_transcript, "seal-cut-trigger", CUTTABLE_SECRET,
                      command="gh pr list", prompt=prompt),
        catalog.InspectConfig(quote_chars=len("DB_PASSWORD=") + len(CUT_HEAD)))

    sealed = autopsy.to_seal(a, level="normal")

    assert CUT_HEAD not in json.dumps(sealed)
    assert sealed["turns"][0]["triggers"][0]["quote"].startswith("DB_PASSWORD=<red")


# -- the daemon step, and the dark state it ships in -----------------------------------


@pytest.fixture()
def spec(registered):
    return catalog.ProjectSpec(name="proj_a", path=registered)


def settled_with_a_session(store, write_transcript, session: str) -> dict:
    wo = store.create_work_order("an order that finished", "")
    store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                       (session, wo["id"]))
    store.conn.commit()
    write_transcript(session, [prompt_row(0, "You are the worker agent for wo-1"),
                               assistant_row(10, "m1", write=30_000)])
    store.set_status(wo["id"], "completed")
    return store.get_work_order(wo["id"])


def test_the_daemon_seals_every_settled_order_and_only_once(store, spec,
                                                            write_transcript,
                                                            monkeypatch):
    """One place to get it right, and it catches the orders that settled before this
    existed while their evidence is still on disk. The predicate is FORCED TRUE here: the
    shipped one is False until the gate of section 5 lands."""
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
    monkeypatch.setattr(autopsy, "seal", lambda *a, **k: 1 / 0)
    wo = settled_with_a_session(store, write_transcript, "sess-broken")

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    row = store.get_work_order(wo["id"])
    assert "error" in json.loads(row["autopsy_json"])
    assert store.unsealed_autopsy_orders() == []


def test_the_shipped_predicate_seals_nothing_for_anybody(store, spec, write_transcript):
    """§3's ruling, and a test so the dark state cannot be undone by accident: no autopsy
    is sealed fleet-wide ahead of the gate that governs it."""
    wo = settled_with_a_session(store, write_transcript, "sess-dark")

    assert autopsy.records_autopsy(wo, spec.observability) is False

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    assert store.get_work_order(wo["id"])["autopsy_json"] is None


def test_an_autopsy_still_answers_once_the_transcript_is_gone(store, spec,
                                                             write_transcript,
                                                             monkeypatch, tmp_path):
    """The whole reason to seal: Claude Code prunes transcripts on its own schedule, and
    an order read afterwards must not report a shorter clock than it ran."""
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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


def test_nothing_to_upgrade_leaves_the_seal_alone(store, spec, write_transcript,
                                                  monkeypatch):
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
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
                                                                  write_transcript,
                                                                  monkeypatch):
    """An upgrade that returns None leaves the seal alone — not retried into a loop and
    not logged as a failure."""
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
    wo = settled_with_a_session(store, write_transcript, "sess-current-tick")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    before = store.get_work_order(wo["id"])

    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    after = store.get_work_order(wo["id"])
    assert after["autopsy_json"] == before["autopsy_json"]
    assert after["autopsy_sealed_at"] == before["autopsy_sealed_at"]


def test_the_shipped_predicate_leaves_a_stale_seal_alone_too(store, spec,
                                                             write_transcript,
                                                             monkeypatch):
    """The dark state covers BOTH queues: with the shipped predicate returning False no
    stale seal is upgraded either."""
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: True)
    wo = settled_with_a_session(store, write_transcript, "sess-dark-stale")
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)
    before = store.get_work_order(wo["id"])["autopsy_json"]

    monkeypatch.setattr(autopsy, "PAYLOAD_VERSION", autopsy.PAYLOAD_VERSION + 1)
    monkeypatch.setattr(autopsy, "records_autopsy", lambda wo, cfg: False)
    Daemon.seal_autopsies(Daemon.__new__(Daemon), spec, store)

    assert store.get_work_order(wo["id"])["autopsy_json"] == before
