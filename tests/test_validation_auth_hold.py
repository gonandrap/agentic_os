"""A validation round whose seats could not AUTHENTICATE waits. GitHub issue #778.

The live failure is wo-ea5b9856, wo-3a7d9bda, wo-15f5d969 and wo-8d964c56, 2026-09-25/26:
all four seats died in ~1.5s each with `failed (rc=1): Failed to authenticate: OAuth
session expired and could not be refreshed` — the usage window had just reopened and many
`claude` processes raced one OAuth refresh — and every one of those rounds was closed
`escalated` on the FIRST failure, spent a round number and was never retried. The same
accounts' worker turns recovered by themselves through `worker_session.PAUSE_AUTH`.

These drive the REAL `Daemon._validate_work_order` and the real `validation.decide`: the
defect spans the transport's classification, the seat, the panel's verdict and the round
machine, and a test of any one of them alone passed throughout.
"""

from __future__ import annotations

import time

import pytest

from jarvis import claude_cli, ops, seats, validation
from jarvis.catalog import ValidationConfig
from jarvis.claude_cli import AuthFailure, AuthFailureError, UsageLimit, UsageLimitError
from jarvis.daemon import AUTH_HOLD_BACKOFF, Daemon
from jarvis.evidence import EvidencePacket
from jarvis.invariants import status_label, true_blockers
from jarvis.project_store import (VALIDATION_AUTH_CAUSE, VALIDATION_HELD_CAUSE,
                                  ProjectStore, validation_hold,
                                  validation_hold_until, validation_standing)
from jarvis.timeline import _describe
from tests.test_validation_loop import (  # noqa: F401
    Validator, fleet, finish, passed)

#: The refusal exactly as the four live rounds carried it — never reworded: the anchor in
#: `claude_cli._AUTH_RE` is what this string has to keep matching.
AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"

REFUSAL = "You've hit your session limit · resets 11:50pm (America/Los_Angeles)"


def auth_error(message: str = AUTH) -> AuthFailureError:
    return AuthFailureError(AuthFailure(message=message))


def refused(in_seconds: float = 3600) -> UsageLimitError:
    return UsageLimitError(UsageLimit(message=REFUSAL,
                                      reset_at=time.time() + in_seconds))


def _rounds(store, wo_id):
    return [(r["round"], r["outcome"]) for r in store.validation_rounds(wo_id=wo_id)]


def _auth_events(store, wo_id):
    from jarvis import db

    return [db.from_json(e["payload"], {}) | {"ts": e["ts"]}
            for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("cause") == VALIDATION_AUTH_CAUSE]


# -- 1. the transport: where the classification has to happen --------------------------


def test_an_auth_failure_in_the_result_envelope_is_classified_at_the_transport():
    """Spec §2. The seat sees an exception, so `_cli_failure` is the last place the text
    is still the CLI's own — and `.auth.message` must keep the live string."""
    envelope = '{"type":"result","subtype":"error","is_error":true,"result":"%s"}' % AUTH
    error = claude_cli._cli_failure(["-p", "judge"], 1, envelope, "")

    assert isinstance(error, AuthFailureError)
    assert AUTH in error.auth.message
    assert isinstance(error, claude_cli.ClaudeCliError), "every `except` must keep working"


def test_the_transcript_shaped_stderr_carrier_is_classified_too():
    """Spec §2. `claude -p` wrote neither result JSON nor stderr on 2026-08-27 and the
    reason came back off the transcript, so the other carrier must classify as well."""
    error = claude_cli._cli_failure(["-p", "judge"], 1, "", AUTH)

    assert isinstance(error, AuthFailureError)
    assert AUTH in error.auth.message


def test_a_usage_refusal_in_the_same_envelope_still_wins():
    """Spec §2: the auth branch sits AFTER the usage one. A spent window is the failure
    that states a deadline, and reading one as an auth hold would drop that moment."""
    envelope = '{"result":"%s\\n%s"}' % (REFUSAL, AUTH)
    error = claude_cli._cli_failure(["-p", "judge"], 1, envelope, "")

    assert isinstance(error, UsageLimitError)
    assert not isinstance(error, AuthFailureError)


def test_the_prefix_is_why_the_classification_cannot_live_on_op_raw():
    """Spec §2, the correction this spec makes: `_AUTH_RE` is `^`-anchored under
    MULTILINE — deliberately, so a worker writing PROSE about an expired login is not
    read as one — and the seat's `raw` is the PREFIXED message. A classifier reading it
    would silently keep escalating, which is the defect."""
    prefixed = str(claude_cli._cli_failure(["-p", "judge"], 1, "", "boom: " + AUTH))

    assert "failed (rc=1)" in prefixed
    assert claude_cli.auth_failure(prefixed) is None


def test_a_seat_that_could_not_authenticate_abstains_and_carries_it(monkeypatch,
                                                                    tmp_path):
    """Spec §2: carried, never raised — one seat that cannot authenticate must not take
    down a panel the other three answered. That call belongs to `decide`."""
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **k: (_ for _ in ()).throw(auth_error()))
    op = seats._run_seat("tester", "p", "s", "sonnet", 10, tmp_path)

    assert (op.status, op.replied) == ("abstained", False)
    assert op.auth is not None and AUTH in op.auth.message


# -- 2. the panel's classifier ---------------------------------------------------------


def _packet() -> EvidencePacket:
    return EvidencePacket(
        unit="work_order", subject_id="wo-1", title="t", description="", summary="",
        declared="", pr_url="", base="aaa", head="bbb", stat=" a.py | 1 +",
        files=("a.py",), diff="+x = 1\n", diff_truncated=False, dropped_files=(),
        diff_sha="sha")


@pytest.fixture()
def panel(tmp_path, jarvis_home, monkeypatch):
    """`decide` over opinions the test writes, with no seat ever called.

    The classifier is the unit under test and the seats are scenery: what matters is
    which exception one all-down round raises, and per-seat evidence exists nowhere else.
    """
    store = ProjectStore(tmp_path / "proj")
    wo = store.create_work_order("t")
    row = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
    monkeypatch.setattr(seats, "prime_cache", lambda *a, **k: None)

    def run(opinions):
        monkeypatch.setattr(seats, "run_blind", lambda *a, **k: list(opinions))
        return validation.decide(store, dict(row), _packet(),
                                 ValidationConfig(enabled=True))

    yield run
    store.close()


def _down(seat: str, **kw) -> seats.Opinion:
    return seats.Opinion(seat=seat, raw=f"claude -p ... failed (rc=1): {AUTH}",
                         status="abstained", replied=False, **kw)


def test_a_panel_that_could_not_authenticate_raises_rather_than_escalating(panel):
    """Spec §1, step 2, and the defect in one line: `escalated` is COUNTED and terminal,
    so the first auth failure spent the round and a user's attention."""
    with pytest.raises(AuthFailureError) as caught:
        panel([_down(s, auth=AuthFailure(message=AUTH))
               for s in ("tester", "security", "architect", "product")])

    assert AUTH in caught.value.auth.message


def test_one_seat_failing_on_auth_is_enough_when_no_seat_replied(panel):
    """Spec §1: ANY seat for the auth branch, unlike the transient one below. Four seats
    that each died differently with one of them refused on auth is still an account that
    cannot answer, and retrying costs ~1.5s and zero tokens."""
    with pytest.raises(AuthFailureError):
        panel([_down("tester", auth=AuthFailure(message=AUTH)),
               seats.Opinion(seat="security", raw="boom", status="abstained",
                             replied=False)])


def test_a_seat_carrying_both_a_refusal_and_an_auth_failure_takes_the_usage_branch(panel):
    """Spec §1, the ordering pin: the usage-window branch is first and is today's
    behaviour byte for byte."""
    with pytest.raises(UsageLimitError) as caught:
        panel([_down("tester", refused=UsageLimit(message=REFUSAL, reset_at=1.0),
                     auth=AuthFailure(message=AUTH))])

    assert not isinstance(caught.value, AuthFailureError)


def test_a_panel_that_is_genuinely_down_still_reaches_the_transport_budget(panel):
    """Spec §1, step 3: ALL seats transient raises a PLAIN `ClaudeCliError`, so the
    existing `_validation_outage` budget applies unchanged. Not a subclass — the branch
    that must own this is the generic one."""
    with pytest.raises(claude_cli.ClaudeCliError) as caught:
        panel([seats.Opinion(seat=s, raw="API Error: 503 upstream connect error",
                             status="abstained", replied=False)
               for s in ("tester", "security")])

    assert type(caught.value) is claude_cli.ClaudeCliError


def test_a_seat_that_replied_garbage_still_escalates(panel, monkeypatch):
    """Spec §1: the `replied` gate above the branch is untouched. A seat that replied,
    even with something nobody can parse, counts as reached — and silence is not a pass,
    so this is still a human's problem, raised by nothing.

    The chair reaches no verdict, so `escalated` comes from where it always did — the
    fall-through that fails toward the user — and never from the auth branch.
    """
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **k: claude_cli.HeadlessResult(
                            text="the chair said nothing parseable", usage={}))
    verdict = panel([seats.Opinion(seat="tester", raw="not json", status="failed"),
                     *(_down(s, auth=AuthFailure(message=AUTH))
                       for s in ("security", "architect", "product"))])

    assert verdict["outcome"] == "escalated"


# -- 3. the round machine: held, not spent --------------------------------------------


def test_a_four_seat_auth_failure_holds_rather_than_escalating(fleet):
    """Test (a). Five drains is far past the old behaviour, which closed the round
    `escalated` on the first failure, flagged the user and never went again."""
    validator = Validator(auth_error())
    fleet.daemon.validator = validator
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    for _ in range(5):
        fleet.drain()

    store = fleet.store()
    try:
        fresh = store.get_work_order(wo["id"])
        assert len(validator.calls) == 1, "the panel went again inside the backoff"
        assert _rounds(store, wo["id"]) == [(1, "failed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert fresh["status"] == "validating", "an auth failure settled the work order"
        assert true_blockers(store, fresh) == [], (
            "nobody judged the work and nobody was asked to")
        assert store.envelopes(subject_wo_id=wo["id"]) == [], "a hold sent feedback"
        row = store.validation_rounds(wo_id=wo["id"])[0]
        assert row["hold_cause"] == VALIDATION_AUTH_CAUSE
        assert validation_hold_until(
            store.events_of_kind(wo["id"], "validation_failed"), 1) > time.time()
    finally:
        store.close()


def test_the_same_round_is_judged_by_itself_after_the_backoff(fleet, monkeypatch):
    """Test (a), second half: no human anywhere in it, and the submitter spends round ONE.

    The BACKOFF is shortened rather than the process clock moved: `validation_hold_until`
    takes the latest moment for a round, so the hold still has to lapse on its own terms
    (tests/test_validation_ci_hold.py's idiom, for its reason).
    """
    monkeypatch.setattr("jarvis.daemon.AUTH_HOLD_BACKOFF", (-1.0,))
    fleet.daemon.validator = Validator(auth_error(), passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()   # every seat failed on auth
    fleet.drain()   # so this tick judges the SAME round, unprompted

    store = fleet.store()
    try:
        assert _rounds(store, wo["id"]) == [(1, "passed")]
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    finally:
        store.close()


@pytest.mark.parametrize("attempt,delay", [(1, 60.0), (2, 300.0), (3, 900.0),
                                          (4, 3600.0), (5, 3600.0), (6, 3600.0)])
def test_the_backoff_is_one_minute_to_an_hour_and_never_gives_up(fleet, attempt, delay):
    """Test (b). The last entry is a CAP, not a final attempt: what clears an auth failure
    is a human running `/login`, which may be thirty seconds or next week away, and a
    count that gave up would turn that into the spent round this spec is removing."""
    store = ProjectStore(fleet.project)
    try:
        wo = store.create_work_order("t")
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="f")
        for _ in range(attempt):
            Daemon._validation_auth_held(store, dict(wo), int(rnd["id"]), 1,
                                         AuthFailure(message=AUTH))

        events = _auth_events(store, wo["id"])
        assert len(events) == attempt
        assert events[-1]["attempt"] == attempt
        assert events[-1]["reopens_at"] - events[-1]["ts"] == pytest.approx(delay, abs=2)
        assert AUTH in events[-1]["error"]

        row = store.get_validation_round(int(rnd["id"]))
        assert row["outcome"] == "failed", "an auth streak escalated on a count"
        assert row["hold_cause"] == VALIDATION_AUTH_CAUSE
        assert store.counted_validation_rounds(wo_id=wo["id"]) == 0
        assert AUTH_HOLD_BACKOFF == (60.0, 300.0, 900.0, 3600.0)
    finally:
        store.close()


def test_an_auth_hold_spends_none_of_the_transport_budget(fleet, monkeypatch):
    """Test (c) — Neo's hard condition, question 723. Three auth holds then three genuine
    outages: if a hold counted, the third outage would arrive with the budget gone and
    this would escalate one drain early."""
    monkeypatch.setattr("jarvis.daemon.AUTH_HOLD_BACKOFF", (-1.0,))
    fleet.daemon.validator = Validator(
        auth_error(), auth_error(), auth_error(),
        claude_cli.ClaudeCliError("connection reset"),
        claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(5):
            fleet.drain()
            assert store.get_work_order(wo["id"])["status"] == "validating"
        fleet.drain()  # the THIRD transport outage — now it may give up
        assert store.latest_validation_round(wo_id=wo["id"])["outcome"] == "escalated"
    finally:
        store.close()


def test_an_all_seats_transient_round_still_escalates_after_three(fleet):
    """Test (d), the other half: this spec routes the auth case AWAY from the outage
    budget and changes nothing about the budget itself (#235's remaining half, #235)."""
    fleet.daemon.validator = Validator(claude_cli.ClaudeCliError("connection reset"))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    store = fleet.store()
    try:
        for _ in range(3):
            fleet.drain()
        assert store.get_work_order(wo["id"])["status"] == "needs_review"
    finally:
        store.close()


def test_a_genuine_usage_limit_behaves_exactly_as_today(fleet):
    """Test (e). The window hold is the mechanism this one reuses, so a regression here
    is the likeliest cost of adding a third cause."""
    fleet.daemon.validator = Validator(refused(3600))
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        row = store.validation_rounds(wo_id=wo["id"])[0]
        assert row["hold_cause"] == VALIDATION_HELD_CAUSE
        assert validation_standing(row) == ("held for the usage window", "active", "◑")
    finally:
        store.close()


def test_the_hold_lifts_on_the_backoff_alone_and_not_on_the_credentials_file(
        fleet, monkeypatch, signin):
    """Test (10). Gating the re-run on `claude_cli.signin_changed_at` is Neo's REJECTED
    design (question 723): it returns None for a keychain or `ANTHROPIC_API_KEY` sign-in,
    and "cannot tell" there would strand the round for ever. Pinned so nobody reinstates
    it quietly — the timing is identical with and without a sign-in."""
    monkeypatch.setattr("jarvis.daemon.AUTH_HOLD_BACKOFF", (-1.0,))
    fleet.daemon.validator = Validator(auth_error(), passed())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()   # no credentials file exists: the gate points at one that does not
    fleet.drain()

    store = fleet.store()
    try:
        assert _rounds(store, wo["id"]) == [(1, "passed")]
    finally:
        store.close()

    signin()  # the user running `/login` changes nothing about the schedule
    other = fleet.dispatch()
    fleet.change(other["id"], "print('two')\n")
    finish(fleet, other["id"])
    fleet.daemon.validator = Validator(auth_error(), passed())

    fleet.drain()
    fleet.drain()

    store = fleet.store()
    try:
        assert _rounds(store, other["id"]) == [(1, "passed")]
    finally:
        store.close()


# -- 4. it must not be silent ---------------------------------------------------------


def test_the_hold_reads_as_an_active_hold_on_every_surface(fleet):
    """Test (h). `VALIDATION_CI_CAUSE`'s precedent exactly: `reopens_at` here is a RECHECK
    INTERVAL, not a moment anything promised, so NO surface prints it — printing one as a
    promise is the specific mistake (`invariants.CI_HOLD_NOTE`)."""
    fleet.daemon.validator = Validator(auth_error())
    wo = fleet.dispatch()
    fleet.change(wo["id"], "print('one')\n")
    finish(fleet, wo["id"])

    fleet.drain()

    store = fleet.store()
    try:
        row = store.validation_rounds(wo_id=wo["id"])[0]
        assert row["outcome"] == "failed", "the mechanism layer must not change"
        assert validation_standing(row) == ("held for authentication", "active", "◑")

        line = ops.round_line(ops.validation_rounds(store, wo_id=wo["id"])[0])
        assert "held for authentication" in line and "· failed ·" not in line

        label = status_label(store, store.get_work_order(wo["id"]))
        assert "Claude Code authentication" in label
        assert "resumes by itself at" not in label, "a recheck interval is not a promise"
        assert "review round 1" in label, "the round is still the subject"
    finally:
        store.close()


def test_the_hold_is_readable_on_the_timeline():
    """A new CAUSE is as invisible as a new kind until `_describe` names it (kn-3f133363),
    and this one must read as neither of the three it sits beside."""
    title, detail = _describe("validation_failed", {
        "round": 1, "cause": VALIDATION_AUTH_CAUSE, "reopens_at": 1_789_714_200.0,
        "attempt": 2, "error": AUTH})

    assert "held" in title.lower() and "authenticate" in title
    assert "attempt 2" in detail and AUTH in detail
    assert "11:50" not in detail and "resuming" not in detail, "no moment is promised"
    assert "unreachable" not in title, "an auth hold is not a reviewer outage"


def test_the_new_cause_holds_the_round_like_the_two_before_it():
    """Membership in `VALIDATION_HOLDING_CAUSES` is the whole scheduler change: a cause
    missing from it holds once and then spins every tick for ever."""
    event = {"payload": '{"round": 1, "cause": "%s", "reopens_at": 70}'
                        % VALIDATION_AUTH_CAUSE}

    assert validation_hold([event], 1) == (70, VALIDATION_AUTH_CAUSE)
    assert VALIDATION_AUTH_CAUSE != VALIDATION_HELD_CAUSE
