"""The daemon half of the stakes classifier: the call, the row, the shadow event.

docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md SS3.5, SS3.8.

Four modes and the whole feature is in what each one does NOT do:

* `regex` — THE SHIPPED DEFAULT, and it must be indistinguishable from today. No model
  call, no `agent_calls` row, no event.
* `shadow` — both run and THE REGEX STILL DECIDES. The work order's outcome is
  byte-identical to `regex`; only an event nobody renders is added.
* `classifier` — the verdict decides, in both directions.
* `regex-tightened` — the A/B's recommendation (§7). The TIGHTENED net decides and, like
  `regex`, it makes no call and writes no row.

`tests/test_stakes.py` holds the pure module; this file holds the wiring.
"""

from __future__ import annotations

import json

import pytest

from jarvis import autoreview, claude_cli, ops, stakes
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
JUDGED = "a1b2c3d4e5f6000000000000000000000000aaaa"

#: The regex fires on `delet` and this is not a deletion — the top marker on the fleet,
#: 21 of its 91 hits, and almost none of them an act.
MENTION = ("this branch's own ops.objection_undeliverable was deleted upstream, so the "
           "conflict resolution keeps the other copy")
#: SS1.1: a release the regex never saw, because `release` appears only as a noun.
ACT = "cut the tag jarvis-0.6.2 and pushed it, skipping the dry-run preview"

ROUTINE_REPLY = json.dumps({"high": False, "category": "none",
                            "reason": "a conflict note, not a deletion"})
HIGH_REPLY = json.dumps({"high": True, "category": "publishing",
                         "reason": "cuts and pushes a release tag"})


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


class FakeCall:
    """The classifier's transport, and ONLY the classifier's.

    A call carrying some other system prompt — Neo answering the question this pass
    files — is handed to the real transport (the suite's fake `claude` binary), so a
    test can drive the ask and the settle in one go.
    """

    def __init__(self, *replies, raises: bool = False):
        self.replies = list(replies)
        self.raises = raises
        self.prompts: list[str] = []
        self.kwargs: list[dict] = []
        self.real = claude_cli.run_headless_result

    def __call__(self, prompt, **kw):
        if kw.get("system_prompt") != stakes.PERSONA:
            return self.real(prompt, **kw)
        self.prompts.append(prompt)
        self.kwargs.append(dict(kw))
        if self.raises:
            raise claude_cli.ClaudeCliError("no transport")
        reply = self.replies[min(len(self.prompts), len(self.replies)) - 1]
        return claude_cli.HeadlessResult(
            text=reply, usage={"input": 900, "output": 40, "total_cost_usd": 0.0011},
            session_id="sess-1", model="claude-haiku-4-5-20251001")


def park(daemon, *, mode: str, content: str, auto_review: bool = True):
    """A work order parked on one assumption, with the fleet in one classifier mode."""
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = auto_review
    spec.validation.stakes_classifier = mode
    wo = ops.create_work_order("proj_a", "add feature X", description="d")
    ops.assume(wo["id"], content)
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(spec.path)
    row = store.latest_validation_round(wo_id=wo["id"])
    if row is None:
        row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp1")
    store.set_validation_head(row["id"], JUDGED)
    store.close_validation_round(row["id"], "passed", "")
    return store, store.get_work_order(wo["id"])


def tick(daemon, store, call=None, monkeypatch=None):
    if call is not None:
        monkeypatch.setattr(claude_cli, "run_headless_result", call)
    daemon.auto_review(daemon.catalog.project("proj_a"), store)


def questions() -> list[dict]:
    neo_store = NeoStore()
    try:
        return [q for q in neo_store.list_questions() if q["kind"] == "assumption"]
    finally:
        neo_store.close()


def calls(kind: str = "stakes_classifier") -> list[dict]:
    central = CentralStore()
    try:
        return [r for r in central.agent_calls() if r["kind"] == kind]
    finally:
        central.close()


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


# -- `regex`: nothing at all ------------------------------------------------------------


def test_the_regex_mode_makes_no_call_and_writes_no_row(started, monkeypatch):
    """THE SHIPPED DEFAULT IS INDISTINGUISHABLE FROM TODAY. A fleet that has not opted in
    must not pay one token or grow one row, or the default is not free."""
    store, wo = park(started, mode="regex", content=MENTION)
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)

    assert call.prompts == []
    assert calls() == []
    assert events(store, wo["id"], "autoreview_stakes_disagreed") == []
    held = events(store, wo["id"], "autoreview_held")
    assert [h["code"] for h in held] == [autoreview.HELD_HIGH_STAKES]


def test_the_default_mode_is_the_regex(started):
    assert started.catalog.project("proj_a").validation.stakes_classifier == "regex"


# -- `shadow`: the regex still decides --------------------------------------------------


def test_shadow_records_the_disagreement_and_changes_nothing_else(started, monkeypatch):
    """SS3.5. The regex held; the classifier said routine; the assumption is still held,
    and the only new thing on the record is an event nobody renders."""
    store, wo = park(started, mode="shadow", content=MENTION)
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)

    assert len(call.prompts) == 1
    assert MENTION in call.prompts[0]
    [event] = events(store, wo["id"], "autoreview_stakes_disagreed")
    assert event["regex"] == autoreview.high_stakes_marker(MENTION)
    assert event["classifier_high"] is False
    assert event["category"] == "none"
    assert event["parsed"] is True
    assert event["model"] == "claude-haiku-4-5-20251001"
    assert questions() == []
    assert [h["code"] for h in events(store, wo["id"], "autoreview_held")] == [
        autoreview.HELD_HIGH_STAKES]


def test_shadow_leaves_the_work_orders_outcome_byte_identical_to_the_regex(
        started, monkeypatch):
    """The claim `shadow` is for: a measurement, not a behaviour change. Same question
    filed or not filed, same hold, same reason — the event is the only difference."""
    store_a, wo_a = park(started, mode="regex", content=MENTION)
    tick(started, store_a)
    regex_held = events(store_a, wo_a["id"], "autoreview_held")

    store_b, wo_b = park(started, mode="shadow", content=MENTION)
    tick(started, store_b, FakeCall(ROUTINE_REPLY), monkeypatch)
    shadow_held = events(store_b, wo_b["id"], "autoreview_held")

    assert [h["reason"] for h in shadow_held] == [h["reason"] for h in regex_held]
    assert store_b.get_work_order(wo_b["id"])["status"] == \
        store_a.get_work_order(wo_a["id"])["status"]


def test_shadow_records_an_agreement_as_no_event(started, monkeypatch):
    """One kind for one fact. An event per row per tick would bury the disagreements,
    which are the only interesting shadow result."""
    store, wo = park(started, mode="shadow", content=MENTION)
    tick(started, store, FakeCall(HIGH_REPLY), monkeypatch)

    assert events(store, wo["id"], "autoreview_stakes_disagreed") == []
    assert len(calls()) == 1


def test_the_disagreement_event_is_not_rendered_to_the_user(started, monkeypatch):
    """SS3.5, taken knowingly: a work order whose behaviour did not change must not grow
    a timeline line saying a mechanism disagreed with itself."""
    from jarvis import ops as ops_mod

    store, wo = park(started, mode="shadow", content=MENTION)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    assert "autoreview_stakes_disagreed" not in ops_mod.AUTOREVIEW_EVENTS


# -- the dedupe -------------------------------------------------------------------------


def test_a_second_tick_over_the_same_state_writes_no_second_event(started, monkeypatch):
    """`_note_autoreview_held`'s discipline: the pass runs every reconcile tick for as
    long as the order sits there."""
    store, wo = park(started, mode="shadow", content=MENTION)
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)
    tick(started, store, call, monkeypatch)

    assert len(events(store, wo["id"], "autoreview_stakes_disagreed")) == 1


def test_a_second_disagreement_of_a_different_shape_is_not_lost(started):
    """Why the key is `(assumption, regex marker, classifier verdict)` and not the
    assumption alone: keyed on the row, a second disagreement of a DIFFERENT shape on the
    same assumption is swallowed by the first, and that is the shadow result worth having.

    Driven through `_note_stakes_disagreement` directly, because the two shapes cannot
    both be reached through the pass on one row: a disagreement where the regex did not
    fire ARMS the row under `shadow` and puts it with Neo, so the next tick holds on a
    cheaper condition and never calls.
    """
    store, wo = park(started, mode="shadow", content=MENTION)
    row = {"id": 77, "n": 1, "content": MENTION}
    routine = stakes.Stakes(high=False, category="none", reason="a mention",
                            model="haiku")

    started._note_stakes_disagreement(store, wo["id"], row, routine)
    started._note_stakes_disagreement(store, wo["id"], row, routine)
    assert len(events(store, wo["id"], "autoreview_stakes_disagreed")) == 1

    started._note_stakes_disagreement(
        store, wo["id"], dict(row, content=ACT),
        stakes.Stakes(high=True, category="publishing", reason="cuts a release tag",
                      model="haiku"))

    first, second = events(store, wo["id"], "autoreview_stakes_disagreed")
    assert (first["regex"], first["classifier_high"]) == ("delet", False)
    assert (second["regex"], second["classifier_high"]) == ("", True)


# -- `classifier`: the verdict decides --------------------------------------------------


def test_the_classifier_arms_a_mention_the_regex_held(started, monkeypatch):
    """The precision win, end to end: the row the regex parked on `delet` is put to Neo."""
    store, wo = park(started, mode="classifier", content=MENTION)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    assert len(questions()) == 1
    assert [h["code"] for h in events(store, wo["id"], "autoreview_held")] == []


def test_the_classifier_holds_an_act_the_regex_missed(started, monkeypatch):
    """The recall half, which no narrowing of the regex reaches (SS1.1)."""
    assert autoreview.high_stakes_marker(ACT) == ""
    store, wo = park(started, mode="classifier", content=ACT)
    tick(started, store, FakeCall(HIGH_REPLY), monkeypatch)

    assert questions() == []
    [held] = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES
    assert "cuts and pushes a release tag" in held["reason"]


def test_the_classifier_mode_writes_no_disagreement_event(started, monkeypatch):
    """Under `classifier` there is nothing to disagree about — the regex is off."""
    store, wo = park(started, mode="classifier", content=MENTION)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    assert events(store, wo["id"], "autoreview_stakes_disagreed") == []


def test_a_transport_failure_holds_and_says_it_was_unreachable(started, monkeypatch):
    """SS3.3. Call failed, timed out, returned nothing: HELD — and the hold says it was a
    failure rather than fabricating a category."""
    store, wo = park(started, mode="classifier", content=MENTION)
    tick(started, store, FakeCall(raises=True), monkeypatch)

    assert questions() == []
    [held] = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES
    assert stakes.HIGH_UNREACHABLE in held["reason"]


def test_the_classifier_is_called_with_no_tools_at_all(started, monkeypatch):
    """Spec SS3.10. `tools=""` strips every built-in AND sends `--strict-mcp-config`
    (claude_cli.py:1522): a tooled classifier goes and reads the real repository and
    answers about THAT, which is a different classifier from the one the A/B measured."""
    store, _wo = park(started, mode="classifier", content=ACT)
    call = FakeCall(HIGH_REPLY)
    tick(started, store, call, monkeypatch)

    assert call.kwargs, "the classifier was never called"
    assert call.kwargs[0]["tools"] == "", (
        f"the daemon left the classifier its tools: {call.kwargs[0].get('tools')!r}")


def test_an_unparseable_reply_holds_and_says_so(started, monkeypatch):
    store, wo = park(started, mode="classifier", content=MENTION)
    tick(started, store, FakeCall("I think it's fine"), monkeypatch)

    [held] = events(store, wo["id"], "autoreview_held")
    assert stakes.HIGH_UNPARSEABLE in held["reason"]


def test_the_settle_site_re_checks_the_same_way_the_ask_did(started, monkeypatch,
                                                            catalog_file):
    """THE DEFECT A CACHED `stakes=None` WOULD HAVE SHIPPED. The settle re-runs the whole
    condition table against freshly read state; with the regex back in charge there, every
    row the classifier armed would be asked, paid for, and then dropped as high-stakes."""
    from jarvis import ops as ops_mod

    ops_mod.set_config("validation.auto_review", True, project="proj_a",
                       reason="measuring the classifier",
                       catalog_path=str(catalog_file))
    store, wo = park(started, mode="classifier", content=f"FORCE_ACCEPT — {MENTION}")
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)
    assert len(questions()) == 1

    started._neo_drain()

    [row] = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted"
    assert row["decided_by"] == "neo"
    # Two calls: one to arm the ask, one to re-check the settle against state read now.
    assert len(call.prompts) == 2
    assert len(calls()) == 2


def test_shadow_never_calls_at_the_settle_site(started, monkeypatch, catalog_file):
    """Under `shadow` the regex decided the ask, so the regex decides the settle — a
    second net at one end and not the other would be a behaviour change `shadow` promises
    not to make."""
    from jarvis import ops as ops_mod

    ops_mod.set_config("validation.auto_review", True, project="proj_a",
                       reason="measuring the classifier",
                       catalog_path=str(catalog_file))
    store, _wo = park(started, mode="shadow",
                      content="FORCE_ACCEPT — named the helper `_render_row`")
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)
    started._neo_drain()

    assert len(call.prompts) == 1


# -- the spend --------------------------------------------------------------------------


def test_one_agent_calls_row_per_call_charged_to_the_work_order(started, monkeypatch):
    """SS3.8. A kind of its own and not a label on `neo_answer`: whether this feature
    earns its price is what the classifier costs against the escalations it saves, and a
    row folded into another kind cannot answer it."""
    store, wo = park(started, mode="shadow", content=MENTION)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    [row] = calls()
    assert row["kind"] == "stakes_classifier"
    assert row["label"] == "assumption"
    assert row["wo_id"] == wo["id"]
    assert row["project"] == "proj_a"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["ok"] == 1
    assert row["output"] == 40


def test_a_failed_call_still_writes_a_row_marked_not_ok(started, monkeypatch):
    """`add_agent_call`'s own rule: a None-usage row says a call was made and cost
    something unknown, which is a different fact from no call at all."""
    store, _wo = park(started, mode="classifier", content=MENTION)
    tick(started, store, FakeCall(raises=True), monkeypatch)

    [row] = calls()
    assert row["ok"] == 0
    assert row["output"] == 0


def test_the_kind_has_a_label_a_reader_can_put_a_word_to():
    from jarvis import agent_usage

    assert agent_usage.KIND_LABELS["stakes_classifier"] == \
        "classifying an assumption's stakes"


def test_a_row_held_on_a_cheaper_condition_never_reaches_a_model(started, monkeypatch):
    """Order matters: the cheap conditions run first, so an assumption already settled,
    already asked, or on a panel that gave up never costs a call."""
    store, _wo = park(started, mode="classifier", content=MENTION, auto_review=False)
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)

    assert call.prompts == []
    assert calls() == []


# -- `regex-tightened`: the A/B's recommendation, and it calls nothing -------------------


def test_the_tightened_mode_makes_no_call_and_writes_no_row(started, monkeypatch):
    """§7. It is a regex, so it costs what `regex` costs: no call, no `agent_calls` row."""
    store, _wo = park(started, mode="regex-tightened", content=MENTION)
    call = FakeCall(ROUTINE_REPLY)
    tick(started, store, call, monkeypatch)

    assert call.prompts == []
    assert calls() == []


def test_the_tightened_mode_arms_a_mention_the_shipped_net_held(started, monkeypatch):
    """The precision half without a model: `delet` on a conflict note is put to Neo."""
    store, wo = park(started, mode="regex-tightened", content=MENTION)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    assert len(questions()) == 1
    assert [h["code"] for h in events(store, wo["id"], "autoreview_held")] == []


def test_the_tightened_mode_holds_an_act_the_shipped_net_missed(started, monkeypatch):
    """The recall half. The hold names the PHRASE it matched and says which net ruled, so
    it is never read as a model's ruling."""
    assert autoreview.high_stakes_marker(ACT) == ""
    store, wo = park(started, mode="regex-tightened", content=ACT)
    tick(started, store, FakeCall(ROUTINE_REPLY), monkeypatch)

    assert questions() == []
    [held] = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES
    assert "tag jarvis-0.6.2" in held["reason"]
    assert autoreview.tightened_verdict(ACT).model == "regex-tightened"
