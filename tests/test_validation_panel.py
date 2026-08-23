"""`validation.decide` end to end, against the fake `claude` and the stored rows.

TWO THINGS THIS FILE IS BUILT AROUND.

**The fake's validator branch must be the one that answers.** `chair` is a legal seat name
in Neo's roster and in the validator's, so a validation chair answered by the Neo branch
comes back a perfectly well-formed Neo verdict carrying no outcome at all — and a lenient
`decide` would read that as a pass, giving a green suite that exercised nothing. The first
test asserts the two branches answer in different shapes, in one test, on the same fake.

**Assertions are on STORED ROWS AND CALL COUNTS**, never on "a verdict came back". The whole
feature is a claim about what was actually judged: how many seats ran, what each said, and
which of them the outcome came from.
"""

from __future__ import annotations

import json

import pytest

from jarvis import claude_cli, validation
from jarvis.catalog import ValidationConfig
from jarvis.daemon import Daemon
from jarvis.evidence import EvidencePacket
from jarvis.project_store import VALIDATOR_SEATS, ProjectStore


def packet(**kw) -> EvidencePacket:
    base = dict(
        unit="work_order", subject_id="wo-1", title="Add the thing",
        description="the brief", summary="I added the thing",
        declared="I ran `uv run pytest`", pr_url="", base="aaa", head="bbb",
        stat=" src/thing.py | 2 +-", files=("src/thing.py",),
        diff="--- a/src/thing.py\n+++ b/src/thing.py\n+x = 1\n",
        diff_truncated=False, dropped_files=(), diff_sha="sha", children=())
    return EvidencePacket(**{**base, **kw})


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


@pytest.fixture()
def round_row(store):
    wo = store.create_work_order("t")
    return store.open_validation_round(wo_id=wo["id"], fingerprint="f")


def cfg(**kw) -> ValidationConfig:
    return ValidationConfig(**{"enabled": True, **kw})


def headless(fake) -> list[dict]:
    return [c for c in fake.calls if "-p" in c["argv"]]


def seat_of(call: dict) -> str | None:
    argv = call["argv"]
    system = argv[argv.index("--append-system-prompt") + 1]
    return next((s for s in VALIDATOR_SEATS
                 if f"# Jarvis validation seat: {s}" in system), None)


# -- the fake answers the right roster --------------------------------------------------


def test_a_validation_chair_and_a_neo_chair_get_different_replies(jarvis_home,
                                                                  fake_claude):
    """THE COLLISION, asserted in one test. Both calls are `chair`; only the header
    differs. If the Neo branch answered the validation call, the reply below would carry
    `escalate` and no `outcome`, and `decide` would have nothing to read a verdict from.
    """
    validator = claude_cli.run_headless_result(
        "the packet", system_prompt="# Jarvis validation seat: chair")
    neo = claude_cli.run_headless_result(
        "the question", system_prompt="# Neo panel seat: chair")

    assert set(json.loads(validator.text)) == {"outcome", "reason"}
    assert set(json.loads(neo.text)) == {"escalate", "answer", "reason"}
    assert json.loads(validator.text)["outcome"] == "passed"
    assert "outcome" not in json.loads(neo.text)


def test_fail_seat_takes_down_exactly_one_validator_seat(store, round_row, jarvis_home,
                                                         fake_claude):
    """Degradation is per seat: one abstains and the rest proceed. Keyed on its own
    variable so that failing a validation `chair` cannot also fail Neo's."""
    fake_claude.fail_seat("security", roster="validator")

    validation.decide(store, round_row, packet(), cfg())

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["security"]["status"] == "abstained"
    assert [rows[s]["status"] for s in ("tester", "architect", "maintainer", "chair")] \
        == ["ok"] * 4


def test_failing_a_neo_seat_does_not_touch_the_validation_panel(store, round_row,
                                                                jarvis_home, fake_claude):
    """The other half of the pairing: one shared variable would have taken the validation
    chair down here, and the test above would still have passed."""
    fake_claude.fail_seat("chair")   # Neo's roster, by default

    validation.decide(store, round_row, packet(), cfg())

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["chair"]["status"] == "ok"


# -- what one round records and returns ---------------------------------------------------


def test_decide_records_one_opinion_per_seat_and_returns_the_contract_keys(
        store, round_row, jarvis_home, fake_claude):
    result = validation.decide(store, round_row, packet(), cfg())

    rows = store.validation_opinions(round_row["id"])
    assert {r["seat"] for r in rows} == set(VALIDATOR_SEATS)
    assert len(rows) == 5, "one row per seat, and never two for one"
    assert all(r["reply"] for r in rows), "the raw reply is stored verbatim"
    assert all(r["latency_ms"] >= 0 for r in rows)

    assert set(result) == {"outcome", "reason", "seats"}
    assert result["outcome"] == "passed"
    assert result["reason"] == "", "a pass carries no feedback"
    assert {s["seat"] for s in result["seats"]} == set(VALIDATOR_SEATS)
    assert all(s["reply"] for s in result["seats"]), (
        "the round machine re-records these rows and asserts the reply is not empty")


def test_the_four_seats_run_blind_and_the_chair_sees_them_all(store, round_row,
                                                              jarvis_home, fake_claude):
    """Blind means none of the four was given another's reply; the chair, and only the
    chair, is shown all four."""
    validation.decide(store, round_row, packet(), cfg())

    calls = {seat_of(c): c["argv"][c["argv"].index("-p") + 1] for c in headless(fake_claude)}
    for seat in ("tester", "security", "architect", "maintainer"):
        assert "The panel's opinions" not in calls[seat]
    assert "The panel's opinions" in calls["chair"]
    for seat in ("tester", "security", "architect", "maintainer"):
        assert f"## Seat: {seat}" in calls["chair"]


def test_the_roster_decides_which_seats_run(store, round_row, jarvis_home, fake_claude):
    validation.decide(store, round_row, packet(),
                      cfg(roster=("tester", "security", "chair")))

    assert {seat_of(c) for c in headless(fake_claude)} == {"tester", "security", "chair"}
    assert {r["seat"] for r in store.validation_opinions(round_row["id"])} == {
        "tester", "security", "chair"}


def test_the_per_seat_model_and_timeout_reach_the_calls(store, round_row, jarvis_home,
                                                         fake_claude):
    validation.decide(store, round_row, packet(),
                      cfg(roster=("tester", "chair"), seat_models={"tester": "haiku"},
                          chair_model="opus", timeout=11))

    models = {seat_of(c): (c["argv"][c["argv"].index("--model") + 1]
                           if "--model" in c["argv"] else None)
              for c in headless(fake_claude)}
    assert models == {"tester": "haiku", "chair": "opus"}
    assert validation.seat_model("architect", cfg()) == "", (
        "no model configured sends no --model flag, rather than guessing one")


def test_a_seat_whose_markdown_does_not_ship_records_failed_without_stalling(
        store, round_row, jarvis_home, fake_claude, monkeypatch, tmp_path):
    """`VALIDATOR_SEATS` is the vocabulary, not this build's shipped set: a catalog may
    name a seat whose definition arrives in a later release. Recorded `failed` rather than
    `abstained` — a seat that CANNOT run is not one that timed out."""
    seat_dir = tmp_path / "seats"
    seat_dir.mkdir()
    for s in ("security", "chair"):
        (seat_dir / f"{s}.md").write_text(
            (validation.SEAT_ASSETS / f"{s}.md").read_text())
    monkeypatch.setattr(validation, "SEAT_ASSETS", seat_dir)

    result = validation.decide(store, round_row, packet(),
                               cfg(roster=("security", "tester", "chair")))

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["tester"]["status"] == "failed"
    assert rows["security"]["status"] == "ok"
    assert result["outcome"] == "passed", "the round still reached a verdict"


# -- who outranks whom ----------------------------------------------------------------------


def test_a_security_veto_outranks_a_chair_that_would_have_passed(store, round_row,
                                                                  jarvis_home, fake_claude):
    """PAIRED with the control below. The chair's default reply in the fake is `passed`,
    so this asserts the veto beat a pass that was genuinely on the table — and it asserts
    the chair was never even asked, on the stored rows and the call count."""
    result = validation.decide(
        store, round_row, packet(declared="FORCE_BLOCK_SECURITY"), cfg())

    assert result["outcome"] == "rejected"
    assert "test-forced security objection" in result["reason"]
    assert "answer the security objection" in result["reason"], "the asks travel too"

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert "chair" not in rows, "the chair does not get a vote on the safety rule"
    assert rows["security"]["verdict"] == "reject"
    assert {seat_of(c) for c in headless(fake_claude)} == {
        "tester", "security", "architect", "maintainer"}


def test_the_same_packet_without_the_veto_passes_through_the_chair(store, round_row,
                                                                    jarvis_home,
                                                                    fake_claude):
    """The control for the test above: without the veto the chair runs, is recorded, and
    its outcome is what comes back."""
    result = validation.decide(store, round_row, packet(), cfg())

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert result["outcome"] == "passed"
    assert rows["chair"]["verdict"] == "pass"
    assert len(headless(fake_claude)) == 5


def test_a_tester_veto_rejects_too(store, round_row, jarvis_home, fake_claude):
    result = validation.decide(
        store, round_row, packet(declared="FORCE_BLOCK_TESTER"), cfg())

    assert result["outcome"] == "rejected"
    assert "chair" not in {r["seat"] for r in store.validation_opinions(round_row["id"])}


def test_an_architect_objection_does_not_reject_and_the_chair_still_rules(
        store, round_row, jarvis_home, fake_claude):
    """The seat holds no veto: it blocks in its reply and the panel proceeds to the chair,
    which passes. Paired with the security test above, which is the same reply from a seat
    that does hold one."""
    result = validation.decide(
        store, round_row, packet(declared="FORCE_BLOCK_ARCHITECT"), cfg())

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["architect"]["verdict"] == "reject"
    assert rows["chair"]["status"] == "ok"
    assert result["outcome"] == "passed"


def test_a_chair_that_rejects_is_the_outcome(store, round_row, jarvis_home, fake_claude):
    result = validation.decide(
        store, round_row, packet(declared="FORCE_VALIDATION_REJECT"), cfg())

    assert result["outcome"] == "rejected"
    assert "not covered by the evidence" in result["reason"]
    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["chair"]["verdict"] == "reject", (
        "the chair's own words, narrowed to the vocabulary the store accepts")


# -- failing toward the user, never toward a pass --------------------------------------------


def test_a_chair_reply_with_no_outcome_escalates(store, round_row, jarvis_home,
                                                  fake_claude):
    result = validation.decide(
        store, round_row, packet(declared="FORCE_VALIDATION_NO_OUTCOME"), cfg())

    assert result["outcome"] == "escalated"
    assert result["reason"]


def test_a_chair_reply_that_will_not_parse_escalates(store, round_row, jarvis_home,
                                                      fake_claude):
    result = validation.decide(
        store, round_row, packet(declared="FORCE_VALIDATION_GARBAGE_CHAIR"), cfg())

    assert result["outcome"] == "escalated"
    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["chair"]["status"] == "failed", "it replied; it just said nothing usable"


def test_every_seat_going_down_escalates_rather_than_passing(store, round_row,
                                                              jarvis_home, fake_claude):
    """The chair would otherwise synthesise from four abstentions and could pass work
    nothing judged — the one outcome this feature cannot produce."""
    fake_claude.fail_seat("tester", "security", "architect", "maintainer",
                          roster="validator")

    result = validation.decide(store, round_row, packet(), cfg())

    assert result["outcome"] == "escalated"
    assert "chair" not in {r["seat"] for r in store.validation_opinions(round_row["id"])}


def test_a_chair_that_cannot_be_reached_is_total_failure(store, round_row, jarvis_home,
                                                          fake_claude):
    """Not a seat abstaining: there is no verdict without the chair. The round machine
    catches this, marks the round `failed` and retries without consuming a round — so the
    abstention has to be RECORDED before the exception leaves."""
    fake_claude.fail_seat("chair", roster="validator")

    with pytest.raises(claude_cli.ClaudeCliError):
        validation.decide(store, round_row, packet(), cfg())

    rows = {r["seat"]: r for r in store.validation_opinions(round_row["id"])}
    assert rows["chair"]["status"] == "abstained"


def test_a_panel_with_no_chair_escalates(store, round_row, jarvis_home, fake_claude):
    result = validation.decide(store, round_row, packet(),
                               cfg(roster=("tester", "security")))

    assert result["outcome"] == "escalated"


def test_the_reason_is_capped(store, round_row, jarvis_home, fake_claude):
    """It is quoted inside `daemon.REVIEW_FEEDBACK`, which the bus frames again."""
    result = validation.decide(
        store, round_row, packet(declared="FORCE_BLOCK_SECURITY"), cfg())

    assert len(result["reason"]) <= validation.REASON_LIMIT


# -- the seam ---------------------------------------------------------------------------------


def test_the_validator_is_none_when_validation_is_disabled():
    """The feature ships disabled, and at that default not one seat is ever called."""
    assert Daemon._validator(ValidationConfig()) is None
    assert Daemon._validator(ValidationConfig(enabled=False)) is None


def test_the_validator_is_the_real_panel_when_enabled(store, round_row, jarvis_home,
                                                       fake_claude):
    """PAIRED with the row above, and asserted by DRIVING it rather than by its type: a
    callable that returned a canned pass would satisfy `is not None` perfectly."""
    validator = Daemon._validator(cfg())

    assert validator is not None
    result = validator(store, dict(round_row), packet())

    assert result["outcome"] == "passed"
    assert {r["seat"] for r in store.validation_opinions(round_row["id"])} == set(
        VALIDATOR_SEATS)
    assert len(headless(fake_claude)) == 5


def test_the_validator_carries_the_catalogs_settings(store, round_row, jarvis_home,
                                                      fake_claude):
    """The seam hands `_validator` the config once; every round it judges must use it."""
    validator = Daemon._validator(cfg(roster=("tester", "chair"),
                                      seat_models={"tester": "haiku"}))

    validator(store, dict(round_row), packet())

    assert {seat_of(c) for c in headless(fake_claude)} == {"tester", "chair"}
