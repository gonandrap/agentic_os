"""An absurd OS-side prompt is refused out loud, and the refusal never quotes it.

Spec §4: docs/superpowers/specs/2026-09-26-bounded-model-inputs.md (section
.jarvis/features/fo-ac00376e/sections/wo-0cb6dc6b.md).

Shaped like `tests/test_transport_resilience.py`'s crash-is-not-a-verdict set: a refused
call decides nothing, spends nothing, and leaves no row claiming it happened.
"""

from __future__ import annotations

import pytest

from jarvis import claude_cli
from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore
from tests.test_validation_loop import fleet  # noqa: F401 — the booted-OS fixture

MARKER = "SECRET-PAYLOAD-MARKER-9f3a"


@pytest.fixture()
def ceiling():
    """A small ceiling, restored after the test — the seam is module-level state."""
    before = claude_cli.MAX_OS_PROMPT_CHARS
    claude_cli.set_max_os_prompt_chars(5_000)
    try:
        yield 5_000
    finally:
        claude_cli.set_max_os_prompt_chars(before)


def _oversized(n: int = 6_000) -> str:
    return MARKER + "x" * n


# -- the constants and the seam -----------------------------------------------------


def test_the_shipped_ceiling_sits_above_the_measured_worst_case():
    """285,929 chars is the largest legitimate OS call measured (a feature chair round).
    A default under it would silently disable validation."""
    assert claude_cli.MAX_OS_PROMPT_CHARS_MIN == 300_000 > 285_929
    assert claude_cli.DEFAULT_MAX_OS_PROMPT_CHARS == 400_000
    assert claude_cli.MAX_OS_PROMPT_CHARS >= claude_cli.MAX_OS_PROMPT_CHARS_MIN


def test_the_seam_overrides_the_default_once_at_startup():
    before = claude_cli.MAX_OS_PROMPT_CHARS
    try:
        claude_cli.set_max_os_prompt_chars(500_000)
        assert claude_cli.MAX_OS_PROMPT_CHARS == 500_000
    finally:
        claude_cli.set_max_os_prompt_chars(before)
    assert claude_cli.MAX_OS_PROMPT_CHARS == before


# -- the transport refusal ----------------------------------------------------------


def test_an_oversized_call_is_refused_before_any_subprocess(ceiling, monkeypatch):
    ran: list[object] = []
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: ran.append(a))
    with pytest.raises(claude_cli.PromptTooLargeError):
        claude_cli.run_headless_result(_oversized(), system_prompt="s" * 100)
    assert ran == []


def test_the_refusal_is_a_transport_failure_every_consumer_already_catches(ceiling,
                                                                          monkeypatch):
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: "unreachable")
    try:
        claude_cli.run_headless_result(_oversized())
    except claude_cli.ClaudeCliError as e:          # the handler every seat already has
        assert isinstance(e, claude_cli.PromptTooLargeError)
    else:
        pytest.fail("the oversized call was not refused")


def test_the_refusal_carries_no_int_where_a_usage_limit_is_expected(ceiling,
                                                                    monkeypatch):
    """`seats._run_seat` does `refused=getattr(e, "limit", None)` and puts the result on
    the Opinion as a `UsageLimit`. An int there would poison it."""
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: "unreachable")
    with pytest.raises(claude_cli.PromptTooLargeError) as caught:
        claude_cli.run_headless_result(_oversized(), system_prompt="s" * 100)
    e = caught.value
    assert getattr(e, "limit", None) is None
    assert e.prompt_chars == 6_000 + len(MARKER)
    assert e.system_prompt_chars == 100
    assert e.ceiling == 5_000


def test_the_refusal_names_numbers_and_never_quotes_the_prompt(ceiling, monkeypatch):
    """A refusal that quotes the payload copies it into the inbox, which the user reads
    and the digest then sends to a model."""
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: "unreachable")
    with pytest.raises(claude_cli.PromptTooLargeError) as caught:
        claude_cli.run_headless_result(_oversized(), system_prompt="s" * 100)
    msg = str(caught.value)
    assert MARKER not in msg
    assert "xxx" not in msg                        # no head, no tail, no middle
    assert "os.max_os_prompt_chars" in msg
    for number in (str(6_000 + len(MARKER)), "100", str(6_100 + len(MARKER)), "5000"):
        assert number in msg.replace(",", "")


def test_a_refused_call_writes_no_agent_calls_row(jarvis_home, ceiling, monkeypatch):
    monkeypatch.setattr(claude_cli, "_run", lambda *a, **k: "unreachable")
    central = CentralStore()
    try:
        before = len(central.agent_calls(limit=100))
        with pytest.raises(claude_cli.PromptTooLargeError):
            claude_cli.run_headless_result(_oversized())
        assert len(central.agent_calls(limit=100)) == before
    finally:
        central.close()


def test_a_call_under_the_ceiling_is_untouched(ceiling, monkeypatch):
    seen: list[list[str]] = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return '{"result": "ok"}'

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    assert claude_cli.run_headless_result("short question").text == "ok"
    assert seen


# -- neo_store.ask: refuse, never persist -------------------------------------------


def test_an_oversized_question_is_never_persisted(jarvis_home, ceiling):
    """Neo, q1078 option A: the store is the last place that can refuse a question, and
    a trimmed one would be answered as if it were the question asked."""
    store = NeoStore()
    try:
        with pytest.raises(ValueError) as caught:
            store.ask("proj_a", "wo-1", _oversized(), context="c" * 100)
        msg = str(caught.value)
        assert MARKER not in msg
        assert "os.max_os_prompt_chars" in msg
        assert "5000" in msg.replace(",", "")
        assert store.conn.execute(
            "SELECT COUNT(*) FROM questions").fetchone()[0] == 0
    finally:
        store.close()


def test_a_normal_question_is_still_stored(jarvis_home, ceiling):
    store = NeoStore()
    try:
        q = store.ask("proj_a", "wo-1", "CSV or JSON?")
        assert q["question"] == "CSV or JSON?"
    finally:
        store.close()


def test_the_refusal_is_a_named_error_the_cli_can_render(jarvis_home, ceiling):
    """A worker that asked too big a question must be told the size, the ceiling and the
    setting, so it can shorten and retry — not handed a bare `ValueError`."""
    from jarvis.neo_store import QuestionTooLargeError

    assert issubclass(QuestionTooLargeError, ValueError)
    store = NeoStore()
    try:
        with pytest.raises(QuestionTooLargeError) as caught:
            store.ask("proj_a", "wo-1", _oversized(), context="c" * 100)
    finally:
        store.close()
    msg = str(caught.value)
    assert MARKER not in msg
    assert "os.max_os_prompt_chars" in msg
    assert str(6_000 + len(MARKER) + 100) in msg.replace(",", "")
    assert "5000" in msg.replace(",", "")
    assert "shorten" in msg


def test_jarvis_wo_ask_prints_one_line_and_exits_nonzero(jarvis_home, fake_claude,
                                                         catalog_file, project,
                                                         capsys):
    """The CLI path a worker actually uses. A traceback here is a worker that cannot
    tell a refusal it can act on from a crash it cannot."""
    from jarvis import cli, ops

    before = claude_cli.MAX_OS_PROMPT_CHARS
    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "build the exporter")
    # Under `sections.QUESTION_MAX_CHARS` (4,000), so it is the STORE that refuses this.
    claude_cli.set_max_os_prompt_chars(3_000)
    try:
        rc = cli.main(["wo", "ask", wo["id"], MARKER + "x" * 3_400])
    finally:
        claude_cli.set_max_os_prompt_chars(before)
    err = capsys.readouterr().err.strip()

    assert rc == 1
    assert len(err.splitlines()) == 1, err
    assert err.startswith("error: ")
    assert "os.max_os_prompt_chars" in err
    assert MARKER not in err


# -- the digest clip has a floor ----------------------------------------------------


def test_the_digest_clip_may_not_be_set_below_the_threshold_that_earned_the_call():
    """Under `digest.MIN_CHARS` every digested question is clipped to less than the
    length that made it worth a digest call at all."""
    from jarvis import digest
    from jarvis.catalog import CatalogError, parse_catalog

    assert digest.MIN_CHARS == 800
    with pytest.raises(CatalogError) as caught:
        parse_catalog({"os": {"neo": {"digest_max_question_chars": 799}},
                       "projects": []})
    msg = str(caught.value)
    assert "800" in msg and "digest_max_question_chars" in msg
    assert parse_catalog(
        {"os": {"neo": {"digest_max_question_chars": 800}},
         "projects": []}).os.neo.digest_max_question_chars == 800


# -- the ceiling against the REAL maximal prompt ------------------------------------


def _maximal_brief():
    """A knowledge brief the size the measurement was taken against — 592 entries,
    built here rather than read out of a database."""
    from jarvis.central_store import KnowledgeBrief

    return KnowledgeBrief(
        project="jarvis_os", total=592,
        pinned=[{"id": f"kn-{i:08x}", "project": "jarvis_os", "topic": "safety",
                 "content": "a pinned standing instruction, in the user's own words, "
                            "long enough to have been worth pinning " * 2}
                for i in range(6)],
        digest=[{"id": f"kn-{i:08x}", "project": "jarvis_os",
                 "topic": f"topic-{i % 24}",
                 "headline": f"entry {i}: the rule this entry states"}
                for i in range(586)],
        overflow=[(f"topic-{i}", 7) for i in range(12)])


def _maximal_packet():
    """The FEATURE packet behind the 285,929-char measurement."""
    from jarvis.catalog import DEFAULT_VALIDATION_DIFF_CHARS
    from jarvis.evidence import EvidencePacket

    return EvidencePacket(
        unit="feature", subject_id="fo-ac00376e",
        title="Bounded model inputs: no OS-built prompt carries an immense payload",
        description="d" * 8_000, summary="s" * 2_000, declared="e" * 4_000,
        pr_url="https://github.com/x/y/pull/874", source="pull_request",
        base="main", head="feat/bounded",
        stat="\n".join(f" src/jarvis/mod_{i}.py | {i} ++++------" for i in range(120)),
        files=tuple(f"src/jarvis/mod_{i}.py" for i in range(120)),
        diff="D" * DEFAULT_VALIDATION_DIFF_CHARS, diff_truncated=True,
        dropped_files=tuple(f"src/jarvis/dropped_{i}.py" for i in range(30)),
        diff_sha="sha", spec_ref="docs/spec.md § 4", spec_section="S" * 20_000,
        side_effects=tuple(
            {"kind": "release", "id": f"eff-{i}", "summary": "an effect " * 20,
             "detail": "what it changed " * 40, "attested": True} for i in range(6)),
        history=tuple(
            {"round": i + 1, "outcome": "rejected", "head_sha": f"{i:040x}",
             "reason": "the panel's reason " * 30,
             "blockers": [{"title": f"blocker {j} " * 8,
                           "detail": "a blocker the round raised " * 20}
                          for j in range(4)]} for i in range(3)),
        children=tuple(
            {"id": f"wo-{i:08x}", "title": f"child {i}", "status": "completed",
             "summary": "what the child delivered " * 20} for i in range(8)))


def test_the_maximal_shipped_prompt_fits_under_the_default_ceiling(jarvis_home):
    """THE TEST THAT FAILS WHEN THE PACKET GROWS PAST THE BACKSTOP.

    Built through the SHIPPED builders, never against a constant: a constant asserted
    against a constant proves only that two lines of Python agree
    (`tests/test_validation_seats.py`'s module docstring).
    """
    import json

    from jarvis import seats, validation

    system = validation.build_shared_prefix(_maximal_packet(), "jarvis_os",
                                            _maximal_brief())
    user = validation.build_chair_prompt([
        seats.Opinion(seat=seat, status="ok", raw=json.dumps({
            "verdict": "reject", "blocking": True, "reason": "R" * 1_200,
            "asks": ["an ask this seat wants answered " * 9] * 4,
            "findings": [{"severity": "blocker", "title": f"blocker {i} " * 8,
                          "detail": "why it blocks " * 60} for i in range(4)]}))
        for seat in ("tester", "security", "architect", "product")])

    total = len(system) + len(user)
    assert total > 200_000, (
        f"the prompt built here is only {total} chars — it is no longer the maximal "
        f"one, and this test has stopped guarding the backstop")
    assert total < claude_cli.DEFAULT_MAX_OS_PROMPT_CHARS, (
        f"the maximal shipped OS prompt is {total} chars, at or over the "
        f"{claude_cli.DEFAULT_MAX_OS_PROMPT_CHARS}-char default ceiling: raise the "
        f"default or shrink what the packet carries")


# ==================================================================================
# The consumers: one inbox row, one derived blocker, no retry (spec §4)
# ==================================================================================


def _refusal(chars: int = 500_000) -> claude_cli.PromptTooLargeError:
    """The transport's own refusal, as a consumer receives it."""
    return claude_cli.PromptTooLargeError(
        f"refused a validation call: prompt {chars} chars + system prompt 0 chars "
        f"= {chars}, over the 400000-char ceiling (os.max_os_prompt_chars)",
        prompt_chars=chars, system_prompt_chars=0, ceiling=400_000)


def _inbox(level: str | None = None) -> list[dict]:
    central = CentralStore()
    try:
        return [r for r in central.unacked_inbox(level)
                if "too large" in r["title"] or "too large" in (r["body"] or "")]
    finally:
        central.close()


# -- the neo drain -------------------------------------------------------------------


@pytest.fixture()
def neo_refused(jarvis_home, fake_claude, catalog_file, project):
    """A dispatched work order whose queued Neo question is refused by the transport."""
    from jarvis import ops
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    ops.start_os(str(catalog_file), foreground=True)
    daemon = Daemon(load_catalog(catalog_file))
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    # The refusal is about the WHOLE prompt — Neo's persona and the learnings index are
    # most of it — so the ceiling, not the question, is what makes this call absurd.
    before = claude_cli.MAX_OS_PROMPT_CHARS
    claude_cli.set_max_os_prompt_chars(50)
    try:
        yield daemon, wo
    finally:
        claude_cli.set_max_os_prompt_chars(before)


def test_a_refused_question_decides_nothing_and_spends_no_attempt(neo_refused):
    """`test_transport_resilience.py`'s six assertions, for the one failure that must
    NOT be retried: the same call fails the same way for ever."""
    from jarvis import neo as neo_mod
    from jarvis import testing as T

    deliver, unreachable, refused = T.Recorder(), T.Recorder(), T.Recorder()
    store = NeoStore()
    try:
        results = neo_mod.drain_queue(store, model="sonnet", deliver=deliver,
                                      unreachable=unreachable, refused=refused)
        q = store.get(1)
    finally:
        store.close()

    assert not deliver.calls, "a call that never happened was delivered as a verdict"
    assert not unreachable.calls, "the deterministic refusal took the retry path"
    assert len(refused.calls) == 1
    assert results[0]["verdict"] is None
    assert q["attempts"] == 0, "a refusal that can never succeed spent an attempt"
    assert q["answer"] is None and q["status"] != "escalated"


def test_the_neo_refusal_reaches_the_inbox_exactly_once_and_quotes_nothing(neo_refused):
    from jarvis import invariants
    from jarvis.project_store import ProjectStore

    daemon, wo = neo_refused
    daemon._neo_drain()
    daemon._neo_drain()          # nothing left to refuse: the row is settled

    rows = _inbox()
    assert len(rows) == 1, [r["title"] for r in rows]
    assert rows[0]["level"] == "critical"
    assert rows[0]["wo_id"] == wo["id"]
    assert "os.max_os_prompt_chars" in rows[0]["body"]
    for text in (rows[0]["title"], rows[0]["body"]):
        assert MARKER not in text
        assert "Should the export default" not in text, "the row quoted the prompt"

    store = ProjectStore(daemon.catalog.projects[0].path)
    try:
        fresh = store.get_work_order(wo["id"])
        said = store.os_prompt_refusal_open(wo["id"])
        assert said is not None and said["ceiling"] == 50
        assert invariants.true_blockers(store, fresh)[0] == \
            invariants.os_prompt_refused_blocker(said)
    finally:
        store.close()


def test_the_flag_survives_a_reconcile_tick_and_goes_down_after_the_clearing_event(
        neo_refused):
    """A raw `flag_attention` would fail this: INV-ATTENTION-REASON rewrites any reason
    `true_blockers` cannot re-derive."""
    from jarvis import invariants
    from jarvis.project_store import ProjectStore

    daemon, wo = neo_refused
    daemon._neo_drain()
    daemon.tick()                                   # a full reconcile tick

    store = ProjectStore(daemon.catalog.projects[0].path)
    try:
        fresh = store.get_work_order(wo["id"])
        said = store.os_prompt_refusal_open(wo["id"])
        assert fresh["needs_attention"] == 1
        assert fresh["attention_reason"] == invariants.os_prompt_refused_blocker(said)
        assert MARKER not in fresh["attention_reason"]

        store.add_event(wo["id"], "neo_answered", {"neo_question_id": 1})
        assert store.os_prompt_refusal_open(wo["id"]) is None
        after = invariants.true_blockers(store, store.get_work_order(wo["id"]))
        assert invariants.os_prompt_refused_blocker(said) not in after
    finally:
        store.close()
    assert len(_inbox()) == 1, "the reconcile tick wrote a second inbox row"


# -- the validation round --------------------------------------------------------------


def test_a_refused_round_does_not_burn_three_outage_retries(fleet):
    """`_validation_outage` retries three times. A deterministic refusal must be
    refused ONCE and escalated: nobody has judged the work, and retrying only delays
    saying so."""
    from jarvis import db, invariants
    from tests.test_validation_loop import Validator, finish

    validator = Validator(_refusal())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    for _ in range(4):
        fleet.drain()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert len(validator.calls) == 1, "a deterministic refusal was retried"
        transport = [e for e in store.events_of_kind(wo["id"], "validation_failed")
                     if db.from_json(e["payload"], {}).get("cause") == "transport"]
        assert transport == [], "a refusal spent a retryable outage attempt"
        assert [(r["round"], r["outcome"])
                for r in store.validation_rounds(wo_id=wo["id"])] == [(1, "escalated")]
        said = store.os_prompt_refusal_open(wo["id"])
        assert said is not None
        assert invariants.true_blockers(store, fresh)[0] == \
            invariants.os_prompt_refused_blocker(said)
        assert fresh["attention_reason"] == invariants.os_prompt_refused_blocker(said)
    finally:
        store.close()

    rows = _inbox()
    assert len(rows) == 1 and rows[0]["level"] == "critical"
    assert "os.max_os_prompt_chars" in rows[0]["body"]
    assert MARKER not in rows[0]["body"] + rows[0]["title"]


def test_the_refusal_is_handled_before_the_generic_clause_at_every_site():
    """GETTING THIS ORDER WRONG IS SILENT. `PromptTooLargeError` is a
    `ClaudeCliError` subclass, so a clause placed after the generic one is never
    reached and the refusal is retried three times as an outage — with every test
    about the generic path still green (daemon.py's precedent comment).

    TWO SHAPES, so the table below names the right pair of markers per site rather
    than hunting for whichever comes first. `neo.drain_queue` and
    `Daemon._validate_feature` DISPATCH IN THE `try`: a chain of `except` clauses
    whose generic end is `except claude_cli.ClaudeCliError`.
    `Daemon._validate_work_order` CAPTURES and then dispatches: its
    `except claude_cli.ClaudeCliError as e: failure = e` only names the error and
    decides nothing, so it is not a handler and must be excluded; the handler is the
    later `isinstance` ladder, whose generic end is `if failure is not None`.

    Source-level, like `tests/test_remedies.py`'s AST pin, because the feature half has
    no cheap runtime harness and the property is about the order of two lines.
    """
    import inspect

    from jarvis import neo as neo_mod
    from jarvis.daemon import Daemon

    sites = (
        # captures, then dispatches by `isinstance`
        (Daemon._validate_work_order,
         "isinstance(failure, claude_cli.PromptTooLargeError)",
         "if failure is not None"),
        # dispatch by `except` chain
        (Daemon._validate_feature,
         "except claude_cli.PromptTooLargeError",
         "except claude_cli.ClaudeCliError"),
        (neo_mod.drain_queue,
         "except claude_cli.PromptTooLargeError",
         "except claude_cli.ClaudeCliError"),
    )

    for fn, specific_marker, generic_marker in sites:
        src = inspect.getsource(fn)
        # `index` raises when a site changes shape, which is the other way this pin fails
        specific = src.index(specific_marker)
        generic = src.index(generic_marker)
        assert specific < generic, (
            f"{fn.__qualname__} handles the generic ClaudeCliError before the "
            f"PromptTooLargeError subclass, so the refusal is never seen")


def test_a_later_passing_round_clears_the_blocker(fleet):
    from jarvis import invariants
    from tests.test_validation_loop import Validator, finish, passed

    fleet.daemon.validator = Validator(_refusal())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = fleet.store()
    try:
        assert store.os_prompt_refusal_open(wo["id"]) is not None
    finally:
        store.close()

    fleet.daemon.validator = Validator(passed())
    fleet.change(wo["id"], "print('two')\n")
    finish(fleet, wo["id"])
    fleet.drain()

    store = fleet.store()
    try:
        assert store.os_prompt_refusal_open(wo["id"]) is None
        fresh = store.get_work_order(wo["id"])
        assert not [b for b in invariants.true_blockers(store, fresh)
                    if "os.max_os_prompt_chars" in b]
    finally:
        store.close()
