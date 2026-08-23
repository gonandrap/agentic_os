"""What the user is shown of a validation: rounds everywhere, seats only on demand.

THE ONE RULE THIS FILE EXISTS TO PIN. A round — its number, its outcome, the reason the
submitter was sent back — is a fact about the work and belongs on every surface anyone
reads by default. What the SEATS said is deliberation: it is stored, it is inspectable,
and it is never pushed. `jarvis validation show` is the only place in the CLI it appears.

So most tests here are PAIRS, and the pairing is not decoration. "The seat's reply is
absent from `wo show`" is indistinguishable from "no opinion was ever recorded" unless
the same test also finds that reply somewhere — so every absence assertion is made
against a fixture whose presence assertion runs beside it.
"""

from __future__ import annotations

import json

from jarvis import cli, ops
from jarvis.project_store import ProjectStore


# -- fixtures: rounds and opinions, written straight into the store -------------------
#
# Validation ships DISABLED and its producers are other work orders' code. These are
# rendering tests: they stage the rows the readers read, which is also the only way to
# stage a round that has already been rejected without running a panel.


def _rounds(store: ProjectStore, wo_id=None, fo_id=None) -> list[dict]:
    """Two settled rounds on one unit: a rejection, then a pass. Oldest first."""
    subject = {"wo_id": wo_id} if wo_id else {"fo_id": fo_id}
    first = store.open_validation_round(**subject, fingerprint="aaaa1111",
                                        summary="added the exporter",
                                        evidence="ran pytest -q")
    store.close_validation_round(first["id"], "rejected",
                                 "no test touches the new branch")
    store.record_validation_opinion(first["id"], "tester", reply=TESTER_REPLY,
                                    verdict="reject", model="sonnet", latency_ms=1200)
    store.record_validation_opinion(first["id"], "security", reply="nothing exposed",
                                    verdict="pass", model="sonnet", latency_ms=900)
    second = store.open_validation_round(**subject, fingerprint="bbbb2222",
                                         summary="added the exporter",
                                         evidence="ran pytest -q, plus a new case")
    store.close_validation_round(second["id"], "passed", "the new case covers it")
    store.record_validation_opinion(second["id"], "tester", reply="covered now",
                                    verdict="pass", model="sonnet", latency_ms=1100)
    return [first, second]


TESTER_REPLY = "you claim tests were added and no file under tests/ appears in the diff"


def _validated_wo(project) -> str:
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        _rounds(store, wo_id=wo["id"])
        return wo["id"]
    finally:
        store.close()


def _validated_fo(project) -> str:
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("CSV export", description="the whole ask")
        _rounds(store, fo_id=fo["id"])
        return fo["id"]
    finally:
        store.close()


def _out(capsys, argv: list[str]) -> str:
    assert cli.main(argv) == 0, argv
    return capsys.readouterr().out


# -- rounds on the default documents ---------------------------------------------------


def test_wo_show_lists_its_rounds_oldest_first(jarvis_home, catalog_file, project,
                                               capsys):
    ops.start_os(str(catalog_file), foreground=True)
    wo_id = _validated_wo(project)

    out = _out(capsys, ["wo", "show", wo_id])

    assert "round 1" in out and "round 2" in out
    assert out.index("round 1") < out.index("round 2")
    assert "aaaa1111" in out and "bbbb2222" in out
    assert "rejected" in out and "passed" in out
    assert "no test touches the new branch" in out


def test_fo_show_lists_its_rounds_oldest_first_and_names_its_manager(
        jarvis_home, catalog_file, project, capsys):
    ops.start_os(str(catalog_file), foreground=True)
    fo_id = _validated_fo(project)
    store = ProjectStore(project)
    try:
        mgr = store.create_work_order("own the follow-through", kind="manager",
                                      parent_id=fo_id)
    finally:
        store.close()

    out = _out(capsys, ["fo", "show", fo_id])

    assert out.index("round 1") < out.index("round 2")
    assert "aaaa1111" in out and "bbbb2222" in out
    assert "the new case covers it" in out
    assert f"manager: {mgr['id']}" in out


def test_wo_show_json_adds_the_rounds_and_changes_nothing_else(
        jarvis_home, catalog_file, project, capsys):
    """Additive means additive: other tooling reads this document.

    The baseline is the same work order BEFORE it has any rounds, so the comparison is
    against the real shape rather than a list of keys copied out of the source — a list
    that would keep passing after the key it forgot to mention disappeared.
    """
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
    finally:
        store.close()

    before = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))
    assert before["validation_rounds"] == []   # present even when empty

    store = ProjectStore(project)
    try:
        _rounds(store, wo_id=wo["id"])
    finally:
        store.close()
    after = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))

    assert set(after) == set(before)
    for key in before:
        if key == "validation_rounds":
            continue
        assert type(after[key]) is type(before[key]), key
        assert after[key] == before[key], key

    assert [r["round"] for r in after["validation_rounds"]] == [1, 2]
    assert after["validation_rounds"][0]["fingerprint"] == "aaaa1111"
    assert after["validation_rounds"][0]["outcome"] == "rejected"
    assert after["validation_rounds"][1]["outcome"] == "passed"


def test_a_work_order_with_no_rounds_reads_exactly_as_it_did(
        jarvis_home, catalog_file, project, capsys):
    """The control for every test above: validation ships disabled, so this is what
    every unit in every fleet looks like until someone turns it on. A human reading
    `wo show` must not be shown an empty structure for a thing that never happened."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
    finally:
        store.close()

    out = _out(capsys, ["wo", "show", wo["id"]])

    assert "validation_rounds" not in out
    assert "round 1" not in out


# -- the separation: deliberation is on demand and nowhere else -----------------------


def test_the_seats_are_absent_by_default_and_present_on_demand(
        jarvis_home, catalog_file, project, capsys):
    """THE PAIRING THAT MAKES THE ABSENCE MEAN ANYTHING.

    Without the second half, "the reply is not in `wo show`" passes just as well against
    a store where no seat ever opined — which is to say against nothing at all.
    """
    ops.start_os(str(catalog_file), foreground=True)
    wo_id = _validated_wo(project)
    fo_id = _validated_fo(project)

    default_wo = _out(capsys, ["wo", "show", wo_id])
    default_fo = _out(capsys, ["fo", "show", fo_id])
    for doc in (default_wo, default_fo):
        assert TESTER_REPLY not in doc
        assert "tester" not in doc
        assert "security" not in doc
    # ...and the rounds themselves ARE there, which is what tells this apart from a
    # surface that simply lost the whole validation.
    assert "rejected" in default_wo and "rejected" in default_fo

    for unit in (wo_id, fo_id):
        deep = _out(capsys, ["validation", "show", unit])
        assert TESTER_REPLY in deep
        assert "tester" in deep and "security" in deep
        assert "1200ms" in deep
        assert "abstained" not in deep      # nothing here abstained; the column is real


def test_the_timeline_of_a_rejection_carries_no_seat(
        jarvis_home, catalog_file, project, capsys):
    """A rejection reaches the timeline as an outcome and a reason — the ask the worker
    has to answer. Which seat raised it is deliberation, and the timeline is read by
    default."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        rounds = _rounds(store, wo_id=wo["id"])
        store.add_event(wo["id"], "validation_rejected",
                        {"round": 1, "round_id": rounds[0]["id"],
                         "reason": "no test touches the new branch"})
    finally:
        store.close()

    timeline = _out(capsys, ["wo", "show", wo["id"]])
    assert "Validation rejected" in timeline
    assert "no test touches the new branch" in timeline
    assert "tester" not in timeline and "security" not in timeline

    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "tester" in deep and "security" in deep


def test_validation_show_says_so_plainly_when_nothing_ever_ran(
        jarvis_home, catalog_file, project, capsys):
    """`neo show --panel`'s behaviour, and for the same reason: a reader who asks this
    of a fleet with validation switched off is owed a sentence, not an empty list."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        fo = store.create_feature_order("CSV export", description="the whole ask")
    finally:
        store.close()

    for unit, word in ((wo["id"], "work order"), (fo["id"], "feature order")):
        out = _out(capsys, ["validation", "show", unit])
        assert f"no validation has run on this {word}" in out
        assert "round 1" not in out


def test_validation_show_finds_a_feature_order_by_its_id_alone(
        jarvis_home, catalog_file, project, capsys):
    """One command, both units: the id says which, so the caller never has to."""
    ops.start_os(str(catalog_file), foreground=True)
    fo_id = _validated_fo(project)

    out = _out(capsys, ["validation", "show", fo_id])
    assert "feature order" in out
    assert "aaaa1111" in out


# -- envelopes -------------------------------------------------------------------------


def test_doctor_reports_a_lost_envelope_and_ignores_a_delivered_one(
        jarvis_home, catalog_file, project, capsys):
    """An undeliverable envelope is feedback that reached NOBODY, and from the outside
    it looks exactly like feedback that landed. Paired with a delivered envelope on the
    same project, because "doctor mentions one envelope" is only a finding if doctor
    stays quiet about the other."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        lost = store.create_work_order("the cancelled one")
        fine = store.create_work_order("the delivered one")
        lost_env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                       kind="review_feedback", subject_wo_id=lost["id"],
                                       payload={"reason": "no tests"})
        good_env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                       kind="review_feedback", subject_wo_id=fine["id"],
                                       payload={"reason": "no tests"})
        store.mark_envelope(lost_env, "undeliverable",
                            note="no work order fills role implementor")
        store.mark_envelope(good_env, "delivered", delivered_wo_id=fine["id"])
    finally:
        store.close()

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out

    assert "INV-ENVELOPE-LOST" in out
    assert lost["id"] in out
    assert "reached nobody" in out
    assert fine["id"] not in out


def test_a_lost_envelope_goes_quiet_once_its_subject_is_settled(
        jarvis_home, catalog_file, project, capsys):
    """The bound that keeps a clean bill of health meaningful.

    `undeliverable` is terminal — nothing ever clears it — so reporting every one for
    ever would mean this project's `jarvis doctor` never came back clean again, and a
    check that can only ever say "something is wrong" is one an operator learns to skip.
    Once the subject is settled the lost message can no longer cost anything; it stays
    on the unit's page and in `jarvis validation show` for the record.
    """
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("the cancelled one")
        env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                  kind="review_feedback", subject_wo_id=wo["id"])
        store.mark_envelope(env, "undeliverable", note="nobody fills role implementor")
    finally:
        store.close()

    assert cli.main(["doctor"]) == 1        # open: it is reported
    assert "INV-ENVELOPE-LOST" in capsys.readouterr().out

    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "cancelled")
    finally:
        store.close()

    assert cli.main(["doctor"]) == 0        # settled: it is not
    out = capsys.readouterr().out
    assert "INV-ENVELOPE-LOST" not in out
    assert "all OS invariants hold" in out

    # ...and it is still on the record, which is what makes going quiet acceptable.
    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "undeliverable" in deep
    assert "nobody fills role implementor" in deep


def test_validation_show_carries_the_delivered_envelopes_too(
        jarvis_home, catalog_file, project, capsys):
    """Delivered envelopes are routine: they belong in the on-demand view and in no
    default listing. This is the presence half of that rule — the absence half is
    `wo show`, which never mentions an envelope at all."""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                  kind="review_feedback", subject_wo_id=wo["id"])
        store.mark_envelope(env, "delivered", delivered_wo_id=wo["id"])
    finally:
        store.close()

    assert "review_feedback" not in _out(capsys, ["wo", "show", wo["id"]])

    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "review_feedback" in deep
    assert "delivered" in deep
