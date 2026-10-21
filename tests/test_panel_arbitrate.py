"""The veto table, one test per row"""

from __future__ import annotations

import json

from jarvis.neo import _VERDICT_ALIASES
from jarvis.neo_store import SEATS
from jarvis.panel import arbitrate

# Every verdict word the OS accepts anywhere, plus "no verdict at all"
VERDICTS = ("", *sorted(_VERDICT_ALIASES))

#: `ok | abstained | failed` are what a seat is actually recorded as
STATUSES = ("ok", "escalate", "abstained", "failed")


def op(seat: str, status: str = "ok", **reply) -> dict:
    """One seat's opinion, in the shape `panel_opinions` stores it"""
    return {"seat": seat, "status": status, "reply": json.dumps(reply)}


def escalated(result) -> bool:
    return (result is not None and result["escalate"] is True
            and result["approve"] is False and result["verdict"] == "denied")


# -- blast: the only veto in the room


def test_blast_returning_escalate_forces_escalate():
    assert escalated(arbitrate([op("blast", escalate=True, reason="unbounded")]))


def test_blast_vetoing_a_proposed_dismiss_demotes_it_to_escalate():
    """Demotes, never promotes"""
    result = arbitrate([
        op("premise", escalate=False, verdict="dismiss", route="fast"),
        op("blast", escalate=False, veto=True, reason="that command does run the script"),
    ])

    assert escalated(result)
    assert result is not None and result["verdict"] != "dismissed"


def test_blast_vetoing_a_proposed_approve_demotes_it_to_escalate_not_to_approve():
    result = arbitrate([
        op("premise", escalate=False, verdict="approve", route="panel"),
        op("blast", escalate=False, veto=True, reason="the checks were never run"),
    ])

    assert escalated(result)


def test_a_bare_blast_veto_with_nothing_proposed_still_escalates():
    """The design words this row as vetoing a PROPOSED dismiss or approve"""
    assert escalated(arbitrate([op("blast", escalate=False, veto=True, reason="no")]))


def test_an_abstaining_blast_neither_vetoes_nor_counts_as_consent():
    """A seat that errors or times out is recorded `abstained` and the panel PROCEEDS"""
    proposed = op("premise", escalate=False, verdict="approve", route="panel")

    assert arbitrate([proposed, op("blast", status="abstained")]) is None
    assert arbitrate([proposed, op("blast", status="failed", veto=True)]) is None
    assert escalated(arbitrate([proposed, op("blast", veto=True, reason="no evidence")]))


# -- record: the standing rulings


def test_record_reporting_an_unresolvable_contradiction_forces_escalate():
    assert escalated(arbitrate([
        op("record", escalate=False, contradiction="unresolvable",
           reason="two standing rulings cannot both hold here")]))


def test_a_resolvable_contradiction_forces_nothing():
    """The control that makes the test above mean something"""
    assert arbitrate([op("record", contradiction="resolvable", reason="superseded")]) is None
    assert arbitrate([op("record", contradiction="", reason="nothing on file")]) is None


def test_record_returning_escalate_forces_escalate():
    """The record seat's second way of saying the same thing"""
    assert escalated(arbitrate([op("record", escalate=True, reason="unsquarable")]))


# -- premise: a proposal is not a decision


def test_premise_proposing_dismiss_is_a_proposal_and_forces_nothing():
    """`dismiss` is the premise seat's to PROPOSE and never its to impose"""
    assert arbitrate([op("premise", escalate=False, verdict="dismiss",
                         route="fast", reason="this greps for a name")]) is None


def test_premise_returning_escalate_forces_escalate():
    """The design's veto table gives this seat no forcing power"""
    assert escalated(arbitrate([op("premise", escalate=True, route="panel",
                                   reason="the frame is wrong")]))


# -- taste: forces nothing, vetoes nothing, and that is the point


def test_taste_forces_nothing_however_it_objects():
    """THE NEGATIVE CONTROL THE TABLE EXISTS FOR"""
    objections = (
        {"escalate": True, "reason": "I would rather the user decided this"},
        {"veto": True, "reason": "this is not what they meant"},
        {"escalate": True, "veto": True, "contradiction": "unresolvable"},
        {"verdict": "deny", "escalate": True},
    )
    for objection in objections:
        assert arbitrate([op("taste", **objection)]) is None, objection
        # ...and the same words, from the seat that owns the veto, do force
        assert escalated(arbitrate([op("blast", **objection)])), objection

    # The one reply that is inert from BOTH seats
    asking = {"verdict": "approve", "approve": True}
    assert arbitrate([op("taste", **asking)]) is None
    assert arbitrate([op("blast", **asking)]) is None


def test_taste_cannot_veto_a_proposed_dismiss_even_alongside_a_silent_blast():
    """The composition that would slip past a per-seat check: nobody who may object is objecting"""
    assert arbitrate([
        op("premise", verdict="dismiss", route="fast"),
        op("blast", status="abstained"),
        op("taste", escalate=True, veto=True, reason="I dislike this"),
    ]) is None


# -- the chair, and seats the table has never heard of


def test_the_chairs_own_reply_is_never_arbitrated():
    """The chair is what arbitration runs INSTEAD of"""
    assert arbitrate([op("chair", escalate=True, veto=True, reason="x")]) is None


def test_a_seat_the_table_does_not_know_forces_nothing():
    assert arbitrate([op("auditor", escalate=True, veto=True)]) is None
    assert arbitrate([op("", escalate=True)]) is None


def test_no_opinions_at_all_forces_nothing():
    assert arbitrate([]) is None


# -- silence, in all the shapes it arrives in


def test_a_reply_that_will_not_parse_is_silence_not_a_signal():
    """Unparseable output is a seat that said nothing usable"""
    assert arbitrate([{"seat": "blast", "status": "ok",
                       "reply": "the blast radius here is, well, hard to say"}]) is None
    assert arbitrate([{"seat": "blast", "status": "ok", "reply": ""}]) is None
    assert arbitrate([{"seat": "blast", "status": "ok", "reply": "{}"}]) is None


def test_an_unrecognised_status_is_read_as_silence():
    """Only `ok` is a seat that spoke"""
    for status in ("abstained", "failed", "escalate", "", "OK"):
        assert arbitrate([op("blast", status=status, escalate=True)]) is None, status
    assert escalated(arbitrate([op("blast", status="ok", escalate=True)]))


def test_a_flag_that_is_not_a_boolean_is_read_permissively():
    """`bool()`, not `is True`"""
    assert escalated(arbitrate([op("blast", escalate="false", reason="r")]))
    assert escalated(arbitrate([op("blast", veto="no", reason="r")]))
    assert arbitrate([op("blast", escalate=None, veto=0, reason="r")]) is None
    assert arbitrate([op("blast", escalate=False, veto=False, reason="r")]) is None


# -- the reason that reaches the user


def test_the_forced_reason_is_the_forcing_seats_own_line_and_names_no_seat():
    """The chair does not run, so this line is what the user reads"""
    result = arbitrate([op("blast", escalate=True,
                           reason="this really does restart the fleet")])

    assert result is not None
    assert result["reason"] == "this really does restart the fleet"
    assert not [s for s in SEATS if s in result["reason"].lower()]
    assert result["answer"] == "", "an escalation carries no answer"


def test_the_seat_that_owns_escalate_is_quoted_when_several_seats_force():
    result = arbitrate([
        op("premise", escalate=True, reason="premise line"),
        op("record", escalate=True, reason="record line"),
        op("blast", escalate=True, reason="blast line"),
    ])

    assert result is not None and result["reason"] == "blast line"


def test_a_forcing_seat_with_no_reason_does_not_silence_one_that_wrote_something():
    result = arbitrate([op("blast", escalate=True, reason="   "),
                        op("record", escalate=True, reason="the record cannot be squared")])

    assert result is not None and result["reason"] == "the record cannot be squared"


def test_a_forced_escalation_always_carries_a_reason():
    result = arbitrate([op("blast", escalate=True)])

    assert escalated(result) and result is not None and result["reason"].strip()


def test_a_forced_outcome_is_the_whole_verdict_shape():
    """`decide` returns `{**forced, "panel": …}`, and every consumer of a verdict reads it by key"""
    result = arbitrate([op("blast", escalate=True, reason="r")])

    assert result is not None
    assert set(result) == {"escalate", "answer", "reason", "verdict", "approve", "dispatch"}
    assert result["dispatch"] is None, (
        "the chair is what turns a seat's proposal into a dispatch, and it did not run"
    )


# -- inertness, and the one property that admits no exception


def test_arbitrate_is_inert_on_the_default_roster():
    """The shipped roster is `premise` + chair"""
    default_roster = [op("premise", escalate=False, verdict="dismiss", route="fast",
                         reason="this greps for a name")]

    assert arbitrate(default_roster) is None
    assert escalated(arbitrate([*default_roster, op("blast", veto=True, reason="it runs")]))


def test_nothing_can_force_an_approval():
    """Exhaustive: every seat the OS knows plus one it does not"""
    seats = (*SEATS, "auditor")
    contradictions = ("", "resolvable", "unresolvable")
    checked = 0
    for seat in seats:
        for status in STATUSES:
            for verdict in VERDICTS:
                for contradiction in contradictions:
                    for escalate in (True, False):
                        for veto in (True, False):
                            for approve in (True, False):
                                result = arbitrate([op(
                                    seat, status=status, verdict=verdict,
                                    contradiction=contradiction, escalate=escalate,
                                    veto=veto, approve=approve, reason="r")])
                                checked += 1
                                if result is None:
                                    continue
                                assert result["escalate"] is True
                                assert result["approve"] is False
                                assert result["verdict"] == "denied"
                                assert result["verdict"] != "approved"

    assert checked == len(seats) * len(STATUSES) * len(VERDICTS) * 3 * 8

    # And the same, with the WHOLE roster in the room at once
    together = arbitrate([op(s, verdict="approve", approve=True, escalate=False,
                             veto=False, reason="ship it") for s in SEATS])
    assert together is None, "no combination of seats can approve; only the chair rules"
