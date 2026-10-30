"""Neo decides a work order's assumptions, and the record says it was Neo (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md)"""

from __future__ import annotations

import json
import re

import pytest

from jarvis import autoreview, neo, ops, plans, stakes
from jarvis.catalog import ValidationConfig, load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

PR = "https://github.com/acme/proj/pull/7"
JUDGED = "a1b2c3d4e5f6000000000000000000000000aaaa"

ROUTINE = "named the helper `_render_row`, matching the two beside it"


def cfg(**over) -> ValidationConfig:
    return ValidationConfig(**{"enabled": True, "auto_review": True, **over})


WO = {"id": "wo-1", "status": "needs_review", "title": "t", "description": "d"}


def assumption(**over) -> dict:
    return {"id": 3, "n": 2, "status": "pending", "content": ROUTINE,
            "neo_question_id": None, **over}


def decide(**kw):
    return autoreview.decide(kw.pop("assumption", None) or assumption(),
                             kw.pop("wo", None) or WO,
                             kw.pop("config", None) or cfg(), **kw)


# -- `decide`: the condition table, both directions


def test_a_routine_assumption_on_a_parked_work_order_is_put_to_neo():
    d = decide()
    assert d.armed and d.assumption_id == 3 and d.n == 2


@pytest.mark.parametrize("kw", [{"enabled": False}, {"auto_review": False}])
def test_neither_half_of_the_switch_arms_it_alone(kw):
    """The panel's switch is read BESIDE the authority"""
    assert decide(config=cfg(**kw)).code == autoreview.HELD_DISABLED


@pytest.mark.parametrize("status", ["running", "validating", "waiting_pr_merge",
                                    "completed", "waiting_input", "pending"])
def test_only_a_work_order_actually_waiting_on_a_person_is_reviewed(status):
    """`needs_review` is the analogue of auto-merge's `waiting_pr_merge`: the one status that means the..."""
    assert decide(wo={**WO, "status": status}).code == autoreview.HELD_STATUS


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_an_assumption_somebody_already_settled_is_not_reopened(status):
    assert decide(assumption=assumption(status=status)).code == autoreview.HELD_SETTLED


def test_a_panel_that_gave_up_is_not_answered_for_the_user():
    """`ops.land_when_cleared` LANDS an `escalated` round"""
    assert decide(round_outcome="escalated").code == autoreview.HELD_PANEL_GAVE_UP


@pytest.mark.parametrize("outcome", ["", "pending", "passed", "rejected"])
def test_every_other_round_outcome_leaves_the_assumption_reviewable(outcome):
    """The pair to the row above: `escalated` is the ONE outcome that holds"""
    assert decide(round_outcome=outcome).armed


@pytest.mark.parametrize("outcome", ["", "pending"])
def test_an_open_round_does_not_hold_the_ask_pass(outcome):
    """§5.2 of docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-an-..."""
    assert decide(round_n=1, round_outcome=outcome).armed
    assert autoreview.decide_early(assumption(), {**WO, "status": "running"}, cfg(),
                                   round_n=1, round_outcome=outcome).armed


def test_the_open_round_hold_is_unreachable_from_either_ask_function():
    """The pair: `round_open` is `decide_confirm`'s alone"""
    for outcome in ("", "pending", "passed", "rejected", "escalated"):
        for n in (0, 1, 3):
            assert decide(round_n=n,
                          round_outcome=outcome).code != autoreview.HELD_ROUND_OPEN
            assert autoreview.decide_early(
                assumption(), {**WO, "status": "running"}, cfg(), round_n=n,
                round_outcome=outcome).code != autoreview.HELD_ROUND_OPEN


def test_a_refusal_the_worker_has_not_answered_holds_everything_behind_it():
    """A refused assumption is guidance the worker has not answered"""
    assert decide(refusal_answered=False).code == autoreview.HELD_REFUSAL_UNANSWERED


def test_the_hold_says_so_when_the_worker_pushed_without_declaring_it():
    """Spec §2c"""
    d = decide(refusal_answered=False, undeclared_delivery=True)

    assert d.code == autoreview.HELD_REFUSAL_UNANSWERED
    assert d.reason == autoreview.REFUSAL_UNDECLARED_REASON
    assert "pushed commits since without running `jarvis wo finish`" in d.reason
    assert "the OS has asked it to declare them" in d.reason
    # Issue #975: a hold the user reads must end in something they can type
    assert "jarvis validation force" in d.reason
    assert "send it a message asking it to finish" in d.reason


def test_the_early_hold_carries_the_same_second_sentence():
    """Both sites, per the spec: `decide` and `decide_early` render the same hold"""
    d = autoreview.decide_early(assumption(), {**WO, "status": "running"}, cfg(),
                                refusal_answered=False, undeclared_delivery=True)

    assert d.code == autoreview.HELD_REFUSAL_UNANSWERED
    assert d.reason == autoreview.REFUSAL_UNDECLARED_REASON


def test_the_undeclared_sentence_is_free_of_a_count_a_sha_and_a_clock():
    """It is stored verbatim by `ack_attention` and compared by INV-ATTENTION-REASON"""
    text = autoreview.REFUSAL_UNDECLARED_REASON

    assert not any(ch.isdigit() for ch in text)


def test_the_undeclared_finding_reaches_the_existing_nudge_remedy(project):
    """Spec §2c's remedy half"""
    from jarvis import invariants, remedies
    from jarvis.catalog import RemedyConfig

    store = ProjectStore(project)
    neo_store = NeoStore()
    wo = store.create_work_order("the refused one")
    finding = store.add_finding(wo["id"],
                                kind=invariants.UNDECLARED_DELIVERY_KIND,
                                reason=invariants.UNDECLARED_DELIVERY_REASON,
                                source="invariant")

    out = remedies.propose(store, neo_store, "proj_a", store.get_work_order(wo["id"]),
                           finding, "nudge", "declare what you pushed",
                           RemedyConfig(True, ("nudge",)))

    assert out["proposed"]
    assert out["approval"]["kind"] == "self_heal"
    assert out["question"]["kind"] == "approval"
    # A proposal is not an act: nothing has reached the session
    assert store.queued_messages(wo["id"]) == []


def test_an_assumption_is_asked_about_once_and_never_again():
    """One question per assumption for its whole life"""
    d = decide(assumption=assumption(neo_question_id=41))
    assert d.code == autoreview.HELD_ASKED and "41" in d.reason


# -- the first net: high stakes, before any model call


@pytest.mark.parametrize("text", [
    "reused the PRODUCTION api key rather than minting one",
    "deleted the orphaned rows instead of backfilling them",
    "billed the caller for the retry as well",
    "published the package under the MIT licence",
    "this is a breaking change to the CLI's --json shape",
    "wrote the migration as an in-place ALTER",
    "the fixture holds personal data, so I left it in",
])
def test_the_regex_net_holds_an_assumption_before_a_call_is_even_made(text):
    """Every entry in `HIGH_STAKES` is the code form of a clause `neo.PERSONA` already escalates on"""
    d = decide(assumption=assumption(content=text))
    assert d.code == autoreview.HELD_HIGH_STAKES
    assert autoreview.high_stakes_marker(text) in d.reason


@pytest.mark.parametrize("text", [
    ROUTINE,
    "put the new test beside the module's other tests rather than in a new file",
    "the monkeypatched clock is frozen at the dispatch time",
    "one-line docstrings, matching the module",
])
def test_ordinary_mechanical_calls_pass_the_net(text):
    """The pair, and it is what stops the net being vacuously wide: `monkey` must not trip `\\bkey\\b`"""
    assert autoreview.high_stakes_marker(text) == ""
    assert decide(assumption=assumption(content=text)).armed


@pytest.mark.parametrize("text", [
    # The three observed in production, issue #713
    "the lead-edit refusal is on for every project on the next release",
    "Query words under 3 characters are dropped",
    "not verified against the live CLI",
    # The same defect in the rest of the repo's everyday vocabulary
    "migrate the call sites to the new helper",
    "the schema of the JSON reply is unchanged",
    "dropped from the record once the user acks it",
    "released the lock before returning",
    "ship the fix in this PR",
])
def test_engineering_prose_is_not_an_act(text):
    """A WORD IS NOT AN ACT"""
    assert autoreview.high_stakes_marker(text) == ""
    assert decide(assumption=assumption(content=text)).armed


@pytest.mark.parametrize("text", [
    "hard-coded the API credential in the settings file",
    "drop the users table and recreate it from the fixture",
    "deploy to production without waiting for the second review",
    "delete the backups older than 30 days",
    "reads the live credentials from the environment",
    "cut a release once this lands",
    "migrate the database to the new schema in place",
])
def test_the_act_is_still_held(text):
    """The pair to the above, and the expensive direction: narrowing the patterns to the ACT must not..."""
    assert decide(assumption=assumption(content=text)).code == \
        autoreview.HELD_HIGH_STAKES


@pytest.mark.parametrize("text, held", [
    ("the re-write tax re-sends 300k input tokens on every turn", False),
    ("token usage is reported per turn, not per work order", False),
    ("the cache-write price is charged on the prompt tokens", True),  # `price`
    ("I hard-coded the token in settings.json", True),
])
def test_the_token_carve_out_is_a_sense_not_a_word(text, held):
    """`token` stays bare — a hard-coded one is a credential"""
    assert bool(autoreview.high_stakes_marker(text)) is held


# -- the classifier's verdict, passed in

ACT = "cut the tag jarvis-0.6.2 and pushed it, skipping the dry-run preview"


def routine_verdict(**over):
    return stakes.Stakes(**{"high": False, "category": "none",
                            "reason": "a conflict note, not a deletion",
                            "model": "haiku", **over})


def high_verdict(**over):
    return stakes.Stakes(**{"high": True, "category": "publishing",
                            "reason": "cuts and pushes a release tag", "model": "haiku",
                            **over})


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_verdict_of_none_leaves_every_existing_caller_byte_identical(rule):
    """`stakes=None` is today's behaviour"""
    text = "deleted the orphaned rows instead of backfilling them"
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")
    a = assumption(content=text)

    before = rule(a, wo, cfg())
    after = rule(a, wo, cfg(), stakes=None)

    assert before.reason == after.reason
    assert before.code == after.code == autoreview.HELD_HIGH_STAKES
    assert autoreview.high_stakes_marker(text) in before.reason


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_high_verdict_holds_and_the_hold_says_what_the_worker_did(rule):
    """SS3.2: `reason` REPLACES the matched phrase"""
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")

    d = rule(assumption(content=ACT), wo, cfg(), stakes=high_verdict())

    assert d.code == autoreview.HELD_HIGH_STAKES
    assert "cuts and pushes a release tag" in d.reason
    assert "publishing" in d.reason


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_routine_verdict_arms_text_the_regex_would_have_held(rule):
    """THE CASE THAT MATTERS MOST"""
    text = ("this branch's own ops.objection_undeliverable was deleted upstream, so the "
            "conflict resolution keeps the other copy")
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")
    assert autoreview.high_stakes_marker(text)

    assert rule(assumption(content=text), wo, cfg(), stakes=routine_verdict()).armed
    assert rule(assumption(content=text), wo, cfg()).code == autoreview.HELD_HIGH_STAKES


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_high_verdict_holds_text_the_regex_misses(rule):
    """SS1.1, the half no narrowing reaches: a release the regex never saw"""
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")
    assert autoreview.high_stakes_marker(ACT) == ""

    assert rule(assumption(content=ACT), wo, cfg()).armed
    assert rule(assumption(content=ACT), wo, cfg(),
                stakes=high_verdict()).code == autoreview.HELD_HIGH_STAKES


def test_an_unreachable_classifier_holds_and_the_hold_says_so():
    """NEVER FABRICATE A DEFAULT FROM A FAILURE: the hold from a call that never returned must say it..."""
    d = autoreview.decide(assumption(content=ACT), WO, cfg(),
                          stakes=stakes.unreachable("haiku"))

    assert d.code == autoreview.HELD_HIGH_STAKES
    assert stakes.HIGH_UNREACHABLE in d.reason


@pytest.mark.parametrize("verdict, armed", [(routine_verdict(), True),
                                            (high_verdict(), False)])
def test_the_confirmation_pass_is_gated_by_the_same_verdict(verdict, armed):
    """`decide_confirm` forwards it unchanged"""
    a = assumption(content=ACT, provisional_verdict="accept", neo_question_id=41)

    assert autoreview.decide_confirm(a, WO, cfg(), stakes=verdict).armed is armed


def test_the_cheap_conditions_still_win_over_the_verdict():
    """Order matters: a row that holds on a cheaper condition must not be reported as a stakes hold"""
    d = autoreview.decide(assumption(content=ACT, status="accepted"), WO, cfg(),
                          stakes=routine_verdict())

    assert d.code == autoreview.HELD_SETTLED


def test_sibling_redaction_stays_on_the_regex_under_every_mode():
    """SS3.6"""
    text = "deleted the orphaned rows instead of backfilling them"
    line = autoreview.sibling_line({"n": 2, "status": "pending", "content": text})

    assert "withheld" in line
    assert text not in line


SECRET = "reused the production api key rather than minting a second one"


def test_a_held_assumption_is_not_quoted_as_some_other_assumptions_context():
    """THE NET IS ABOUT TEXT REACHING A MODEL, NOT ABOUT WHOSE ROW IT IS"""
    line = autoreview.sibling_line({"id": 9, "n": 3, "status": "pending",
                                    "content": SECRET})

    assert "production" not in line and "key" not in line
    assert SECRET not in line
    # Withheld, not dropped: silence would tell the reviewer this work order had only routine assumptions
    assert "#3" in line and "[pending]" in line and "withheld" in line


def test_a_routine_sibling_is_still_quoted_in_full():
    """The control"""
    line = autoreview.sibling_line({"id": 9, "n": 3, "status": "accepted",
                                    "content": ROUTINE})

    assert ROUTINE in line and "withheld" not in line


# -- the second net: what a reply MEANS


def accepted(**over) -> dict:
    return {"escalate": False, "verdict": "approved", "stakes": "routine",
            "reason": "a naming convention", "model": "claude-opus-5", **over}


def test_an_explicit_routine_approval_is_the_one_shape_that_accepts():
    r = autoreview.read_ruling(accepted())
    assert r.accept and not r.overridden and r.model == "claude-opus-5"


def test_neo_marking_its_own_acceptance_high_stakes_overrides_the_acceptance():
    """A backstop a reviewer can wave through is not one"""
    r = autoreview.read_ruling(accepted(stakes="high"))
    assert not r.accept and r.overridden and "high-stakes" in r.reason


@pytest.mark.parametrize("stakes, why", [
    pytest.param(None, "the field is absent — the likeliest malformed reply of all, and "
                       "`neo._validate_verdict` turns it into `''` rather than an error",
                 id="absent"),
    pytest.param("", "the field is there and empty", id="empty"),
    pytest.param("   ", "whitespace only, which `.strip()` makes empty", id="blank"),
    pytest.param("high-stakes", "a plausible misspelling of the word we asked for",
                 id="misspelled"),
    pytest.param("elevated", "a synonym nobody anticipated", id="synonym"),
    pytest.param("HIGH", "the right word in the wrong case — escalates either way, but "
                         "through the allowlist rather than the `== 'high'` compare",
                 id="uppercase"),
    pytest.param("routine, because the helper is intern", "truncated at "
                 "`_validate_verdict`'s 20-char cap, so the value that arrives is not "
                 "the value Neo wrote", id="truncated"),
    pytest.param("high", "the one it was always meant to catch", id="high"),
])
def test_a_stakes_the_os_cannot_read_as_routine_escalates_rather_than_accepting(stakes,
                                                                                why):
    """THE SECOND NET MUST NOT FAIL OPEN, and written as `== "high"` it did"""
    reply = accepted()
    if stakes is None:
        reply.pop("stakes")
    else:
        reply["stakes"] = stakes

    r = autoreview.read_ruling(reply)

    assert not r.accept, why
    assert r.overridden, "the OS overrode an acceptance — the record must say so"
    assert "would have accepted it" in r.reason


def test_an_unclassified_acceptance_is_not_recorded_as_routine():
    """The absence of a warning is not a warning's absence"""
    reply = accepted()
    reply.pop("stakes")

    r = autoreview.read_ruling(reply)

    assert r.stakes == autoreview.STAKES_UNCLASSIFIED != autoreview.STAKES_ROUTINE
    assert "did not classify the stakes at all" in r.reason


def test_the_allowlist_is_exactly_the_word_the_persona_asks_for():
    """Pinned as a set rather than read off the implementation: every entry added here is a value a future..."""
    assert autoreview.ROUTINE_STAKES == frozenset({"routine"})
    assert '"stakes": "routine"' in autoreview.ASSUMPTION_REVIEWER_PERSONA


def test_neo_declining_the_assumption_escalates_and_carries_its_reading():
    """There is no machine rejection (Neo, question 301), so a `deny` reaches the user"""
    r = autoreview.read_ruling(accepted(verdict="denied",
                                        reason="this changes the CLI's output"))
    assert not r.accept and not r.overridden
    assert "would have turned this down" in r.reason
    assert "changes the CLI's output" in r.reason


@pytest.mark.parametrize("verdict", [
    {"escalate": True, "verdict": "denied", "reason": "your call"},
    # Unparseable output, through the real parser rather than a hand-made dict
    neo.parse_verdict("I think you should maybe do the thing?"),
    # A well-formed reply that never says `approve`: absent must never mean yes
    {"escalate": False, "reason": "hmm"},
    # ...nor must a truthy-looking string in the wrong field
    {"escalate": False, "verdict": "approve-ish", "reason": "hmm"},
    # The transport failed; `drain_queue` synthesises this
    {"escalate": True, "answer": "", "reason": "neo call failed: boom", "failed": True},
    {},
])
def test_everything_that_is_not_an_explicit_approval_stays_with_the_user(verdict):
    """THE DIRECTION THE FEATURE MUST FAIL IN"""
    assert not autoreview.read_ruling(verdict).accept


# -- why the OS escalated, as one groupable label


def test_an_override_names_the_stakes_word_that_caused_it():
    high = autoreview.read_ruling(accepted(stakes="high"))
    assert autoreview.escalation_cause(high) == "stakes-high"
    unclassified = accepted()
    unclassified.pop("stakes")
    assert autoreview.escalation_cause(
        autoreview.read_ruling(unclassified)) == "stakes-unclassified"
    odd = autoreview.read_ruling(accepted(stakes="elevated"))
    assert autoreview.escalation_cause(odd) == "stakes-unreadable"


def test_neo_declining_with_no_machine_rejection_is_its_own_cause():
    denied = autoreview.read_ruling(accepted(verdict="denied"))
    assert autoreview.escalation_cause(denied) == "neo-denied"


def test_children_at_or_over_the_cap_is_the_plan_paths_cause():
    """No `Ruling` on that path: the cap outranks whatever Neo said about the plan"""
    assert autoreview.escalation_cause(over_cap=True) == "scope-over-cap"
    # Neo escalated on its own, so the cause is Neo's chosen label, not this one
    assert autoreview.escalation_cause(over_cap=True, escalate=True) == ""


def test_a_fact_pattern_that_is_none_of_the_five_records_no_cause():
    """A NULL is correct and a wrong label is not"""
    assert autoreview.escalation_cause(autoreview.read_ruling(accepted())) == ""
    assert autoreview.escalation_cause() == ""
    # Neo escalated on its own: the cause is the CHOSEN label off its reply
    escalated = accepted(escalate=True, verdict="denied")
    assert autoreview.escalation_cause(autoreview.read_ruling(escalated),
                                       escalate=True) == ""


def test_every_cause_this_helper_can_return_is_in_the_overridden_class():
    """The helper lives in a PURE module and spells the members as literals"""
    from jarvis.neo_store import (ESCALATION_CAUSES, ESCALATION_CAUSES_CHOSEN,
                                  ESCALATION_CAUSES_FAILED,
                                  ESCALATION_CAUSES_OVERRIDDEN)

    unclassified = accepted()
    unclassified.pop("stakes")
    produced = {
        autoreview.escalation_cause(autoreview.read_ruling(accepted(stakes="high"))),
        autoreview.escalation_cause(autoreview.read_ruling(unclassified)),
        autoreview.escalation_cause(autoreview.read_ruling(accepted(stakes="elevated"))),
        autoreview.escalation_cause(autoreview.read_ruling(accepted(verdict="denied"))),
        autoreview.escalation_cause(over_cap=True),
    }
    assert produced == set(ESCALATION_CAUSES_OVERRIDDEN)
    assert not produced & set(ESCALATION_CAUSES_CHOSEN)
    assert not produced & set(ESCALATION_CAUSES_FAILED)
    assert produced <= set(ESCALATION_CAUSES)


def test_the_members_nothing_can_write_are_no_longer_in_the_enum():
    """`stakes.HIGH_UNREACHABLE` / `HIGH_UNPARSEABLE` reach `HELD_HIGH_STAKES`"""
    from jarvis.neo_store import ESCALATION_CAUSES

    assert "classifier-unreachable" not in ESCALATION_CAUSES
    assert "classifier-unparseable" not in ESCALATION_CAUSES


# -- the daemon: asking


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    # The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def park(daemon, *, auto_review: bool, auto_merge: bool = False,
         assumptions: tuple[str, ...] = (ROUTINE,), description: str = "d",
         outcome: str = "passed", pr_url: str = PR):
    """A work order finished behind a pull request, parked on its assumptions"""
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = auto_review
    spec.validation.auto_merge = auto_merge
    # These pin the wide net's hold wording; the tightened default words its holds differently
    spec.validation.stakes_classifier = "regex"
    wo = ops.create_work_order("proj_a", "add feature X", description=description)
    for text in assumptions:
        ops.assume(wo["id"], text)
    ops.finish(wo["id"], "opened a PR", pr_url=pr_url)
    store = ProjectStore(spec.path)
    round_row = store.latest_validation_round(wo_id=wo["id"])
    if round_row is None:
        round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="fp1")
    store.set_validation_head(round_row["id"], JUDGED)
    if outcome:
        store.close_validation_round(round_row["id"], outcome, "")
    return store, store.get_work_order(wo["id"])


def ask(daemon, store):
    daemon.auto_review(daemon.catalog.project("proj_a"), store)


def questions(kind: str = "assumption") -> list[dict]:
    neo_store = NeoStore()
    try:
        return [q for q in neo_store.list_questions() if q["kind"] == kind]
    finally:
        neo_store.close()


def events(store, wo_id: str, kind: str) -> list[dict]:
    return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]


def inbox(wo_id: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        return [r for r in central.unacked_inbox() if r["wo_id"] == wo_id]
    finally:
        central.close()


def test_a_project_that_has_not_opted_in_is_never_decided_for(started):
    """THE SWITCH IS REAL AND NOT DECORATIVE"""
    store, wo = park(started, auto_review=False)

    ask(started, store)

    assert questions() == []
    assert store.pending_assumptions(wo["id"])
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    # ...and it did not even cost a line on the timeline: an opted-out project's record must not carry...
    assert events(store, wo["id"], "autoreview_held") == []
    assert ops.autoreview_state(store, wo) is None


def test_neo_being_switched_off_files_no_question_nobody_will_drain(started,
                                                                   catalog_file):
    """A question filed against a queue that does not run is worse than no question: the assumption would..."""
    started.catalog.os.neo.enabled = False
    store, wo = park(started, auto_review=True)

    ask(started, store)

    assert questions() == []


def test_each_assumption_gets_its_own_question_never_one_verdict_over_a_list(started):
    """A list invites one judgement over its easiest member"""
    store, wo = park(started, auto_review=True,
                     assumptions=("used tabs in the fixture",
                                  "put the helper at the bottom of the module",
                                  ROUTINE))

    ask(started, store)

    asked = questions()
    assert len(asked) == 3
    assert len({q["id"] for q in asked}) == 3
    rows = store.all_assumptions(wo["id"])
    assert sorted(a["neo_question_id"] for a in rows) == sorted(q["id"] for q in asked)
    # Each question quotes ONE assumption under the heading that is ruled on
    for row in rows:
        q = next(x for x in asked if x["id"] == row["neo_question_id"])
        assert f"# The assumption\n{row['content']}" in q["question"]
        assert "do not rule on these" in q["question"]


def test_no_question_the_os_files_carries_a_high_stakes_assumptions_text(started):
    """THE SAME GUARANTEE THROUGH THE DAEMON"""
    store, wo = park(started, auto_review=True,
                     assumptions=(ROUTINE, SECRET, "used tabs in the fixture"))

    ask(started, store)

    filed = questions()
    # Two routine assumptions are asked about; the high-stakes one is not
    assert len(filed) == 2
    for q in filed:
        assert SECRET not in q["question"]
        assert "production" not in q["question"] and "api key" not in q["question"]
        assert SECRET not in (q.get("context") or "")
    # ...and it is named as withheld rather than silently absent
    assert all("withheld" in q["question"] for q in filed)
    # The held one is on the record with its reason, exactly as before
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES


def test_asking_twice_asks_once(started):
    """`decide`'s condition 6 through the loop: the pass runs every reconcile tick"""
    store, wo = park(started, auto_review=True)

    ask(started, store)
    ask(started, store)

    assert len(questions()) == 1


def test_a_hold_is_recorded_once_per_reason_and_is_not_an_attention_item(started):
    """`_note_autoreview_held`'s dedupe"""
    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key",))

    ask(started, store)
    ask(started, store)
    ask(started, store)

    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES
    assert questions() == []
    assert store.get_work_order(wo["id"])["attention_reason"] != held["reason"]
    assert "held" in ops.autoreview_state(store, wo)["line"]


def test_every_pending_hold_is_named_not_only_the_newest(started):
    """Holds land one assumption at a time; the summary must not show only the last one"""
    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key",
                                  "reused the production db password", ROUTINE,
                                  "pointed the production cron at the new host"))

    ask(started, store)

    held = events(store, wo["id"], "autoreview_held")
    assert len(held) == 3
    line = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert line.startswith("held — the OS would not decide 3 assumptions: ")
    for h in held:
        assert f"#{h['n']}" in line
    assert "#3" not in line

    # A decided assumption stops being owed, and one left reads as it always did
    first, second, *_ = store.pending_assumptions(wo["id"])
    store.review_assumption(first["id"], "accepted")
    store.review_assumption(second["id"], "accepted")
    line = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert "would not decide" not in line and "#4" in line


def settled_round(store, wo_id: str, outcome: str, reason: str = "") -> None:
    """A fresh settled round on the judged commit, `park`'s way"""
    row = store.open_validation_round(wo_id=wo_id, fingerprint="fp1")
    store.set_validation_head(row["id"], JUDGED)
    store.close_validation_round(row["id"], outcome, reason)


def test_a_hold_returning_to_an_earlier_reason_is_recorded_again(started):
    """ISSUE #782, the real trace: held on stakes"""
    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key",))

    ask(started, store)
    settled_round(store, wo["id"], "escalated")
    ask(started, store)
    settled_round(store, wo["id"], "passed")
    ask(started, store)

    assert [h["code"] for h in events(store, wo["id"], "autoreview_held")] == [
        autoreview.HELD_HIGH_STAKES, autoreview.HELD_PANEL_GAVE_UP,
        autoreview.HELD_HIGH_STAKES]
    (line,) = rulings(store, wo["id"])
    assert line.startswith("Held by the OS — mentions 'production'")
    assert "gave up" not in line


# -- the panel's give-up: which round, what it said, and when it stops being true

PANEL_REASON = ("the seats could not agree about whether the retry loop is safe under a\n"
                "partial write, and the chair would not break the tie:   two read the lock\n"
                "as sufficient and one read it as advisory only, which is a question about "
                "the storage engine and not about this diff at all — the chair asked for a "
                "second opinion and none of the three would move, so the submission comes "
                "to you with all three readings attached")


def test_a_panel_hold_names_the_round_and_quotes_what_the_panel_said(started):
    """A reader cannot tell a real disagreement from #778's OAuth outage from the static sentence"""
    store, wo = park(started, auto_review=True, outcome="")
    row = store.latest_validation_round(wo_id=wo["id"])
    store.close_validation_round(row["id"], "escalated", PANEL_REASON)

    ask(started, store)

    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP
    assert held["round"] == 1
    assert "gave up on round 1" in held["reason"]
    # Whitespace-collapsed and clamped
    assert len(PANEL_REASON) > 300
    assert " ".join(PANEL_REASON.split())[:117] + "…" in held["reason"]
    assert "\n" not in held["reason"] and "  " not in held["reason"]


def test_a_panel_hold_stops_being_rendered_once_a_later_round_passes(started):
    """#782's sibling, and the one only the freshness check can satisfy: ONE routine assumption"""
    store, wo = park(started, auto_review=True, outcome="escalated")
    ask(started, store)
    assert "gave up" in rulings(store, wo["id"])[0]

    settled_round(store, wo["id"], "passed")

    # Not-looked-at is an understatement; the stale sentence is a lie
    assert rulings(store, wo["id"]) == [""]
    assert ops.autoreview_state(store, store.get_work_order(wo["id"])) is None
    # The history is KEPT — the hold did happen, and the timeline is append-only
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP


def test_a_second_escalated_round_refreshes_the_hold_rather_than_voiding_it(started):
    """The dedupe key carries the round"""
    store, wo = park(started, auto_review=True, outcome="escalated")
    ask(started, store)
    settled_round(store, wo["id"], "escalated")
    ask(started, store)

    holds = events(store, wo["id"], "autoreview_held")
    assert [h["round"] for h in holds] == [1, 2]
    assert "gave up on round 2" in holds[-1]["reason"]
    assert "gave up" in rulings(store, wo["id"])[0]


def _refusal_hold(store, wo, reason: str) -> autoreview.Decision:
    (row,) = store.all_assumptions(wo["id"])
    return autoreview._held(autoreview.HELD_REFUSAL_UNANSWERED, reason,
                            assumption_id=row["id"], n=1)


def test_the_undeclared_refusal_sentence_is_not_deduped_away_by_the_first(started):
    """ISSUE #975, measured on wo-2933fc24: both refusal sentences share the code..."""
    store, wo = park(started, auto_review=True, outcome="")

    started._note_autoreview_held(
        store, wo["id"], _refusal_hold(store, wo, autoreview.REFUSAL_UNANSWERED_REASON))
    started._note_autoreview_held(
        store, wo["id"], _refusal_hold(store, wo, autoreview.REFUSAL_UNDECLARED_REASON))

    holds = events(store, wo["id"], "autoreview_held")
    assert [h["reason"] for h in holds] == [autoreview.REFUSAL_UNANSWERED_REASON,
                                            autoreview.REFUSAL_UNDECLARED_REASON]
    assert "pushed commits since" in rulings(store, wo["id"])[0]


def test_the_same_refusal_reason_twice_is_still_recorded_once(started):
    """The dedupe the reason key must not weaken: the pass runs every reconcile tick"""
    store, wo = park(started, auto_review=True, outcome="")

    for _ in range(3):
        started._note_autoreview_held(
            store, wo["id"],
            _refusal_hold(store, wo, autoreview.REFUSAL_UNANSWERED_REASON))

    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["reason"] == autoreview.REFUSAL_UNANSWERED_REASON


def test_a_hold_written_before_the_round_was_recorded_is_read_honestly(started):
    """A payload from before this shipped carries no round: KEPT while the newest round is still escalated..."""
    store, wo = park(started, auto_review=True, outcome="escalated")
    (row,) = store.all_assumptions(wo["id"])
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_PANEL_GAVE_UP,
                     "reason": "the validation panel gave up and put this work order in "
                               "front of you",
                     "assumption_id": row["id"], "n": 1})

    assert "gave up" in rulings(store, wo["id"])[0]

    settled_round(store, wo["id"], "passed")

    assert rulings(store, wo["id"]) == [""]


def test_a_confirm_spent_hold_survives_the_panel_freshness_filter(started):
    """§8 row 5 of docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-..."""
    store, wo = park(started, auto_review=True, outcome="passed")
    (row,) = store.all_assumptions(wo["id"])
    payload = {"code": autoreview.HELD_CONFIRM_SPENT,
               "reason": "assumption #1 is yours — the OS asked Neo to confirm its early "
                         "reading (question 920) and that question is no longer open",
               "assumption_id": row["id"], "n": 1}
    store.add_event(wo["id"], "autoreview_held", payload)

    latest = store.latest_validation_round(wo_id=wo["id"])
    assert not ops._panel_hold_is_stale(latest, payload)
    assert not ops._stale_panel_hold(store, wo["id"])("autoreview_held", payload)

    # ...and the hold is what the readers render, on both surfaces
    (line,) = rulings(store, wo["id"])
    assert "question 920" in line and "no longer open" in line
    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))
    assert state["code"] == autoreview.HELD_CONFIRM_SPENT
    assert "question 920" in state["line"]


def test_a_status_hold_stops_being_shown_once_a_review_is_owed_again(started):
    """A `status` hold claims the order is in no state this pass acts in"""
    store, wo = park(started, auto_review=True)
    (row,) = store.all_assumptions(wo["id"])
    store.set_status(wo["id"], "cancelled")
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_STATUS,
                     "reason": "the work order is cancelled, not waiting on a review",
                     "assumption_id": row["id"], "n": 1})

    assert "cancelled" in ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]

    store.set_status(wo["id"], "needs_review")

    assert ops.autoreview_state(store, store.get_work_order(wo["id"])) is None
    assert rulings(store, wo["id"]) == [""]


# docs/superpowers/specs/2026-10-01-a-confirmation-is-not-re-run-on-a-settled- assumption.md §3.3

SETTLED_SPENT = ("assumption #1 is yours — the OS asked Neo to confirm its early reading "
                 "(question 920) and that question is no longer open")


def settled_pair(started):
    """A parked order with one row Neo ACCEPTED and one still pending — §1.1's shape"""
    store, wo = park(started, auto_review=True,
                     assumptions=(ROUTINE, "put the helper at the bottom"))
    first, second = store.all_assumptions(wo["id"])
    store.review_assumption(first["id"], "accepted", decided_by="neo")
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_CONFIRM_SPENT, "reason": SETTLED_SPENT,
                     "assumption_id": first["id"], "n": 1})
    return store, wo, first, second


def test_a_hold_about_a_row_that_has_since_settled_is_not_the_summary_line(started):
    """§3.3: the three observed orders' stored state, which no write-side change reaches"""
    store, wo, first, second = settled_pair(started)

    assert ops.autoreview_state(store, store.get_work_order(wo["id"])) is None
    assert [e["code"] for e in events(store, wo["id"], "autoreview_held")] \
        == [autoreview.HELD_CONFIRM_SPENT]

    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_HIGH_STAKES,
                     "reason": "assumption #2 mentions 'production'",
                     "assumption_id": second["id"], "n": 2})

    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))
    assert state["code"] == autoreview.HELD_HIGH_STAKES
    assert state["assumption_id"] == second["id"]


def test_the_freshness_of_a_hold_is_answered_per_assumption_not_per_order(started):
    """§3.3's cache: one `stale` closure runs over every row of the order"""
    store, wo, first, second = settled_pair(started)
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_HIGH_STAKES,
                     "reason": "assumption #2 mentions 'production'",
                     "assumption_id": second["id"], "n": 2})

    rows = ops.assumptions_with_rulings(store, wo["id"])

    assert rows[0]["os_ruling"] is None
    assert rows[1]["os_ruling"]["code"] == autoreview.HELD_HIGH_STAKES


def test_an_order_level_hold_names_no_row_and_is_never_dropped_by_one(started):
    """§3.3's first bullet: a payload with no `assumption_id` makes no claim about a row"""
    store, wo = park(started, auto_review=True)
    (row,) = store.all_assumptions(wo["id"])
    store.review_assumption(row["id"], "accepted", decided_by="neo")
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_DISABLED,
                     "reason": "this project has not given the OS permission to decide "
                               "its assumptions (`validation.auto_review`)"})

    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))

    assert state["code"] == autoreview.HELD_DISABLED


# -- the daemon: ruling


def drain(daemon):
    daemon._neo_drain()


def test_an_accepted_assumption_settles_and_the_record_says_the_os_did_it(
        started, catalog_file):
    """THE POST-CONDITION THE WHOLE FEATURE RESTS ON"""
    ops.set_config("validation.auto_review", True, project="proj_a",
                   reason="the panel has been right for a month",
                   catalog_path=str(catalog_file))
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (q,) = questions()

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted"
    assert row["decided_by"] == "neo" and row["decided_by"] != "user"
    assert row["decided_reason"] and "test-forced acceptance" in row["decided_reason"]
    assert row["decided_model"]
    assert row["decided_config_version"] == ops.current_config_version() is not None
    assert row["neo_question_id"] == q["id"]
    (event,) = events(store, wo["id"], "autoreview_accepted")
    assert event["decided_by"] == "neo" and event["neo_question_id"] == q["id"]
    # The worker finished long ago: nothing may start a turn on it
    assert store.list_messages(wo["id"]) == []
    assert events(store, wo["id"], "neo_answered") == []


def test_jarvis_wo_show_never_presents_a_machine_decision_as_the_users(
        started, capsys):
    """THE SURFACE TEST, and it is deliberately at the CLI rather than in `ops`: what matters is what a..."""
    from jarvis import cli

    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",
                                  "reused the production api key"))
    ask(started, store)
    drain(started)
    # ...and the user rules on the one the OS would not touch
    ops.review_work_order(wo["id"], accept=True, feedback="fine, I minted a new one")

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"]])
    out = capsys.readouterr().out

    assert "accepted by the OS (neo" in out
    assert "accepted by you" in out
    # The strong form: the machine's line must not be sayable as the user's
    machine = [l for l in out.splitlines() if "FORCE_ACCEPT" in l]
    assert machine and all("by you" not in l for l in machine)

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"], "--json"])
    rows = json.loads(capsys.readouterr().out)["assumptions"]
    assert {r["decided_by"] for r in rows} == {"neo", "user"}


@pytest.mark.parametrize("token, overridden", [
    ("", False),                    # the fake's default: escalate
    ("FORCE_DENY", False),          # Neo thinks it is wrong — still the user's call
    ("FORCE_ACCEPT_HIGH", True),    # Neo accepted; the code would not take it
])
def test_a_ruling_that_is_not_an_acceptance_leaves_the_assumption_exactly_where_it_was(
        started, token, overridden):
    """No settling, no landing, and NO INBOX ROW: nothing changed for the user"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"{token} — {ROUTINE}".strip(" —"),))
    ask(started, store)
    (q,) = questions()

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and row["decided_by"] == ""
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert inbox(wo["id"]) == []
    (event,) = events(store, wo["id"], "autoreview_escalated")
    assert event["overridden"] is overridden
    # However Neo phrased it, the question is the user's now
    neo_store = NeoStore()
    try:
        assert neo_store.get(q["id"])["status"] == "escalated"
    finally:
        neo_store.close()


def test_the_user_is_told_once_when_the_last_assumption_leaves_their_list(started):
    """One row, per work order, at the moment their review list changes"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",
                                  "FORCE_ACCEPT — put the helper at the bottom"))

    ask(started, store)
    drain(started)

    (row,) = inbox(wo["id"])
    assert row["level"] == "info"
    assert "decided 2 assumption(s) for you" in row["title"]
    assert f"jarvis wo show {wo['id']}" in row["body"]
    assert "jarvis neo review" in row["body"]
    assert not store.pending_assumptions(wo["id"])


def test_the_users_own_verdict_wins_a_race_with_neos(started):
    """They got there first through `jarvis wo review`"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    ops.review_work_order(wo["id"], accept=False, feedback="no, use the long name")

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "rejected" and row["decided_by"] == "user"
    assert events(store, wo["id"], "autoreview_accepted") == []


# -- the settle site: everything that can change while Neo is thinking


def test_a_panel_that_gives_up_while_neo_is_thinking_stops_the_settle(started):
    """THE ONE THAT MATTERS MOST, and it is reachable rather than theoretical: arming on a `pending` round..."""
    store, wo = park(started, auto_review=True, outcome="pending",
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (q,) = questions()
    # The panel reaches its verdict while the question is in flight, and gives up
    store.close_validation_round(
        store.latest_validation_round(wo_id=wo["id"])["id"], "escalated",
        "the seats could not agree")

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_accepted") == []
    # The order did NOT land behind it — the thing the panel's give-up was protecting
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    # ...and the OS said why, in both places: the work order's record and Neo's own list
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP
    # WHICH round and WHAT IT SAID, not merely that one gave up: this round closed with a real reason
    assert held["round"] == 1
    assert "gave up on round 1" in held["reason"]
    assert "the seats could not agree" in held["reason"]
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_PANEL_GAVE_UP
    assert questions()[0]["status"] == "escalated"
    assert q["id"] == questions()[0]["id"]
    # The one line the user reads on `jarvis wo show` says it is theirs again
    line = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert "left with you" in line
    # Still pending: no resolution clause (the spec's §Tests 3)
    assert "since " not in line


# -- WHO decided, on the row the report groups by


def _cause(question_id: int) -> str | None:
    neo_store = NeoStore()
    try:
        return neo_store.get(question_id)["escalation_cause"]
    finally:
        neo_store.close()


@pytest.mark.parametrize("token, cause", [
    ("FORCE_DENY", "neo-denied"),           # no machine rejection, so the user decides
    ("FORCE_ACCEPT_HIGH", "stakes-high"),   # Neo accepted and flagged it; the OS obeyed
])
def test_an_override_at_the_settle_records_which_fact_caused_it(started, token, cause):
    store, _wo = park(started, auto_review=True,
                      assumptions=(f"{token} — {ROUTINE}",))
    ask(started, store)
    (q,) = questions()

    drain(started)

    assert _cause(q["id"]) == cause


def test_a_ruling_the_os_dropped_at_the_settle_records_no_cause(started):
    """THE HONEST NULL"""
    store, wo = park(started, auto_review=True, outcome="pending",
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (q,) = questions()
    store.close_validation_round(
        store.latest_validation_round(wo_id=wo["id"])["id"], "escalated",
        "the seats could not agree")

    drain(started)

    assert questions()[0]["status"] == "escalated"
    assert _cause(q["id"]) is None


# -- the banner against the assumption's CURRENT row


def _escalated(daemon):
    """A work order parked on one assumption the OS left with the user"""
    store, wo = park(daemon, auto_review=True)
    ask(daemon, store)
    drain(daemon)
    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending"
    return store, wo, row["id"]


def _line(store, wo_id: str) -> str:
    return ops.autoreview_state(store, store.get_work_order(wo_id))["line"]


def test_an_accepted_assumption_is_no_longer_left_with_you(started):
    """The escalation stays on the record"""
    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "accepted", reason="fine")

    line = _line(store, wo["id"])
    assert "left with you" in line
    assert "since accepted by you" in line


def test_a_rejected_assumption_says_rejected_and_not_accepted(started):
    """The status column is interpolated, not hard-coded"""
    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "rejected", reason="no")

    line = _line(store, wo["id"])
    assert "since rejected by you" in line
    assert "accepted" not in line


def test_a_still_pending_escalation_reads_exactly_as_before(started):
    """The common case: nothing read, nothing appended"""
    store, wo, _aid = _escalated(started)

    assert "since " not in _line(store, wo["id"])


def test_a_decision_the_os_took_is_not_credited_to_the_user(started):
    """One attribution renderer, asserted as one: `ops.assumption_decider`"""
    from jarvis.project_store import ASSUMPTION_DECIDER_OS

    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="no new surface", model="claude-opus-5")

    row = store.get_assumption(aid)
    assert _line(store, wo["id"]).endswith(
        f"; since accepted by {ops.assumption_decider(row)}")
    assert ops.assumption_decider(row) == "the OS (neo, claude-opus-5)"


def test_an_event_written_before_the_payload_carried_the_id_is_unchanged(started):
    """Unresolvable, and a banner is not the place to guess"""
    wo = ops.create_work_order("proj_a", "older event")
    store = ProjectStore(started.catalog.project("proj_a").path)
    store.add_event(wo["id"], "autoreview_escalated",
                    {"n": 1, "reason": "this is a surface others call"})

    assert _line(store, wo["id"]) == (
        "assumption #1 left with you — this is a surface others call")


def test_a_work_order_cancelled_while_neo_is_thinking_is_not_settled_and_re_landed(
        started):
    """The same gap by another door"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    ops.cancel(wo["id"])

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and not row["decided_by"]
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert events(store, wo["id"], "autoreview_accepted") == []
    # `_note_autoreview_held` drops `status` at the ASK site, where it is unreachable
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_STATUS and "cancelled" in held["reason"]


def test_permission_revoked_while_neo_is_thinking_stops_the_settle(started):
    """The switch is read at the settle as well as at the ask"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    started.catalog.project("proj_a").validation.auto_review = False

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and not row["decided_by"]
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_DISABLED


def test_nothing_having_changed_the_second_check_lets_the_ruling_through(started):
    """The control"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted" and row["decided_by"] == "neo"
    assert events(store, wo["id"], "autoreview_held") == []


def test_the_question_being_delivered_does_not_block_its_own_settle():
    """`asked_question_id` at the unit level"""
    linked = assumption(neo_question_id=41)

    assert decide(assumption=linked).code == autoreview.HELD_ASKED
    assert decide(assumption=linked, asked_question_id=41).armed
    assert decide(assumption=linked, asked_question_id=42).code == autoreview.HELD_ASKED


# -- the refusals that were already there, left honest


def test_ack_and_done_refuse_while_an_assumption_is_pending_and_stop_once_it_is_not(
        started):
    """CHECKED RATHER THAN ASSUMED, as the work order asked"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))

    with pytest.raises(ops.OpsError, match="assumption"):
        ops.ack_attention(wo["id"])
    with pytest.raises(ops.OpsError, match="assumption"):
        ops.mark_done(wo["id"])

    ask(started, store)
    drain(started)

    # Neither refusal was widened, narrowed or given a branch for this: the fact they guard is gone
    assert ops.mark_done(wo["id"])["status"] == "completed"


# -- the two mechanisms together


def merge_gate(store, wo_id: str) -> None:
    """Approve the `auto_merge` gate the poll files, the way Neo's drain would"""
    from jarvis import gates

    (approval,) = [a for a in store.list_approvals(wo_id)
                   if a["kind"] == "auto_merge"]
    gates.apply_decision(store, approval["id"], "approved", "the panel read it", "neo")


def test_an_order_with_an_assumption_reaches_merged_with_nobody_typing_anything(
        started, fake_gh, local_base):
    """THE WHOLE POINT, end to end: assumption -> Neo -> settled -> parked -> gate -> merged"""
    from jarvis import automerge

    store, wo = park(started, auto_review=True, auto_merge=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    fake_gh.set_pr(PR, "OPEN", checks=[{"__typename": "CheckRun", "name": "unit",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS"}],
                   merge_state="CLEAN", head_oid=JUDGED)
    spec = started.catalog.project("proj_a")

    ask(started, store)
    drain(started)
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    started.poll_pull_requests(spec, store)          # proposes the merge gate
    merge_gate(store, wo["id"])
    started.poll_pull_requests(spec, store)          # ...and merges under it

    assert [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert events(store, wo["id"], "automerge_merged")
    # The assumption did not go quiet — it was decided, by the OS, on the record
    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted" and row["decided_by"] == autoreview.DECIDER
    # And the merge decision still HOLDS on a pending assumption: nothing was special-cased away
    held = automerge.decide({"id": 1, "round": 1, "outcome": "passed",
                             "head_sha": JUDGED},
                            {**store.get_work_order(wo["id"]),
                             "status": "waiting_pr_merge"},
                            type("P", (), {"head_oid": JUDGED})(), spec.validation,
                            validated_head=JUDGED, pending_assumptions=True)
    assert held.code == automerge.HELD_ASSUMPTIONS


def test_with_auto_review_off_the_same_order_still_waits_for_the_user(started, fake_gh):
    """THE NEGATIVE HALF, identical in every other respect"""
    store, wo = park(started, auto_review=False, auto_merge=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    fake_gh.set_pr(PR, "OPEN", checks=[{"__typename": "CheckRun", "name": "unit",
                                        "status": "COMPLETED",
                                        "conclusion": "SUCCESS"}],
                   merge_state="CLEAN", head_oid=JUDGED)
    spec = started.catalog.project("proj_a")

    ask(started, store)
    drain(started)
    started.poll_pull_requests(spec, store)
    started.poll_pull_requests(spec, store)

    assert not [c for c in fake_gh.calls if c["argv"][:2] == ["pr", "merge"]]
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert store.pending_assumptions(wo["id"])
    assert store.get_work_order(wo["id"])["needs_attention"]


# -- the switch


def test_the_fleet_default_ships_off_and_a_project_inherits_it(catalog_file):
    catalog = load_catalog(catalog_file)
    assert catalog.os.validation.auto_review is False
    assert catalog.project("proj_a").validation.auto_review is False


def test_a_project_keeps_its_own_answer_over_the_fleets(tmp_path):
    """Authority is granted one project at a time: a project that never opted in is never decided for"""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "os": {"validation": {"enabled": True, "auto_review": True}},
        "projects": [
            {"name": "opted_out", "path": str(tmp_path),
             "validation": {"auto_review": False}},
            {"name": "inherits", "path": str(tmp_path)},
        ]}))
    catalog = load_catalog(path)
    assert catalog.project("opted_out").validation.auto_review is False
    assert catalog.project("inherits").validation.auto_review is True


def test_a_project_opts_in_through_the_same_cli_as_every_other_safety_setting(
        started, catalog_file):
    """Not a constant and not an environment variable: the ordinary config console, and"""
    assert ops.safety_key("projects.proj_a.validation.auto_review")
    with pytest.raises(ops.OpsError):
        ops.set_config("validation.auto_review", True, project="proj_a",
                       catalog_path=str(catalog_file))          # no reason given

    out = ops.set_config("validation.auto_review", True, project="proj_a",
                         reason="the panel has been right for a month",
                         catalog_path=str(catalog_file))

    assert out["safety"] and out["path"] == "projects.proj_a.validation.auto_review"
    assert load_catalog(catalog_file).project("proj_a").validation.auto_review is True


def test_correcting_the_ruling_teaches_neo_without_reopening_the_work_order(started):
    """THE CORRECTION PATH, which is what makes a machine decision reversible"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    drain(started)
    (q,) = questions()

    out = ops.neo_review(q["id"], approved=False,
                         feedback="helper names are mine to pick — always ask")

    assert out["learning_recorded"] and out["forwarded_to_worker"] is False
    assert store.list_messages(wo["id"]) == []
    neo_store = NeoStore()
    try:
        assert any("always ask" in row["content"]
                   for row in neo_store.learnings("proj_a", limit=20))
    finally:
        neo_store.close()


def test_the_user_cannot_answer_an_escalated_assumption_into_a_finished_worker(started):
    """The mirror of the alarm refusal"""
    store, wo = park(started, auto_review=True)
    ask(started, store)
    drain(started)                       # the fake escalates by default
    (q,) = questions()

    with pytest.raises(ops.OpsError, match="jarvis wo review"):
        ops.neo_answer_escalated(q["id"], "yes, that is fine")

    assert store.list_messages(wo["id"]) == []
    assert store.pending_assumptions(wo["id"])


# -- the stale-escalation sweep


def test_a_question_the_user_answered_another_way_stops_asking_for_a_ruling(started):
    """Adding a Neo question kind is seven edits (kn-4edb0eb7)"""
    from jarvis import invariants

    store, wo = park(started, auto_review=True)
    ask(started, store)
    drain(started)                       # the fake escalates by default
    ops.review_work_order(wo["id"], accept=True, feedback="fine")

    violations = list(invariants.check_neo_escalations_are_live(store))

    assert [v.invariant for v in violations] == ["INV-NEO-ESCALATION-STALE"]
    neo_store = NeoStore()
    try:
        (q,) = questions()
        assert neo_store.get(q["id"])["status"] not in ("escalated", "failed")
    finally:
        neo_store.close()


def test_an_assumption_still_waiting_on_the_user_is_not_swept(started):
    """THE HALF THAT MATTERS MORE, and the one a sweep gets wrong in the dangerous direction"""
    from jarvis import invariants

    store, wo = park(started, auto_review=True)
    ask(started, store)
    drain(started)                       # the fake escalates by default
    (q,) = questions()

    violations = list(invariants.check_neo_escalations_are_live(store))

    assert violations == []
    assert store.pending_assumptions(wo["id"])
    neo_store = NeoStore()
    try:
        assert neo_store.get(q["id"])["status"] == "escalated"
    finally:
        neo_store.close()


def test_another_projects_assumption_question_is_left_alone(started):
    """The rule all four siblings share"""
    from jarvis import invariants

    store, _wo = park(started, auto_review=True)
    neo_store = NeoStore()
    try:
        stranger = neo_store.ask("proj_b", "wo-elsewhere",
                                 "ASSUMPTION REVIEW — someone else's order",
                                 kind="assumption")
        neo_store.mark(stranger["id"], "escalated", reason="theirs to decide")
    finally:
        neo_store.close()

    assert list(invariants.check_neo_escalations_are_live(store)) == []

    neo_store = NeoStore()
    try:
        assert neo_store.get(stranger["id"])["status"] == "escalated"
    finally:
        neo_store.close()


# -- what the OS did with EACH assumption, on both surfaces (GitHub issue #712)


def rulings(store, wo_id: str) -> list[str]:
    """Every assumption's ruling line, in order, as a person reads it"""
    return [ops.assumption_ruling_line(a)
            for a in ops.assumptions_with_rulings(store, wo_id)]


def test_a_held_assumption_says_what_held_it_and_an_asked_one_says_it_is_out(started):
    """THE DEFECT: a pending assumption the OS had already ruled on read exactly like one nobody had..."""
    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key", ROUTINE))

    ask(started, store)

    held, asked = rulings(store, wo["id"])
    assert held.startswith("Held by the OS — mentions 'production'")
    # The number is on the row already; repeating it inside the reason is not a ruling
    assert "assumption #1" not in held
    (q,) = questions()
    assert asked == f"Asked Neo (question {q['id']}), awaiting ruling"


def test_an_escalation_carries_neos_reason_and_its_question(started):
    """What the user has to decide, beside the assumption they have to decide it on"""
    store, wo = park(started, auto_review=True)
    ask(started, store)
    (q,) = questions()

    drain(started)

    (line,) = rulings(store, wo["id"])
    assert line.startswith(f"Neo escalated (question {q['id']}): ")
    assert "assumption reviews escalate unless forced" in line


def test_an_assumption_nobody_has_looked_at_says_nothing_at_all(started):
    """`''` IS A FACT, and a different one from a hold: an opted-out project's assumptions must not grow a..."""
    store, wo = park(started, auto_review=False)

    ask(started, store)

    assert rulings(store, wo["id"]) == [""]


def test_a_settled_assumption_carries_the_reason_it_was_settled(started):
    """The acceptance reason is part of the ruling too"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)

    drain(started)

    assert rulings(store, wo["id"]) == ["test-forced acceptance: a naming convention"]


def test_wo_show_carries_every_ruling_and_not_just_the_last_one(started, capsys):
    """THE SURFACE, and the half the summary line cannot do: `auto_review:` says what the mechanism did..."""
    from jarvis import cli

    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key",
                                  f"FORCE_ACCEPT — {ROUTINE}"))
    ask(started, store)
    drain(started)

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"]])
    out = capsys.readouterr().out

    held = [l for l in out.splitlines() if "production api key" in l]
    assert held and "Held by the OS — mentions 'production'" in held[0]
    assert "test-forced acceptance" in out

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"], "--json"])
    rows = json.loads(capsys.readouterr().out)["assumptions"]
    # `--json` keeps the payload, not the prose: whoever reads it reads the event
    assert rows[0]["os_ruling"]["code"] == autoreview.HELD_HIGH_STAKES


def test_the_cli_points_at_the_round_and_only_for_a_panel_hold(started, capsys):
    """WHERE TO READ THE FULL PANEL REASON, on the surface that cannot carry a URL"""
    from jarvis import cli

    store, gave_up = park(started, auto_review=True, outcome="escalated")
    _, stakes_hold = park(started, auto_review=True,
                          assumptions=("reused the production api key",))

    ask(started, store)

    pointer = f"jarvis validation show {gave_up['id']}"
    capsys.readouterr()
    cli.main(["wo", "show", gave_up["id"]])
    out = capsys.readouterr().out
    (assumption_line,) = [l for l in out.splitlines() if "_render_row" in l]
    assert assumption_line.rstrip().endswith(pointer)
    (summary,) = [l for l in out.splitlines() if "auto_review" in l]
    assert summary.rstrip().endswith(pointer)

    # ...and NOT on a hold whose subject is not a round
    capsys.readouterr()
    cli.main(["wo", "show", stakes_hold["id"]])
    other = capsys.readouterr().out
    assert "mentions 'production'" in other and "jarvis validation show" not in other

    # `--json` keeps the rows untouched, which is what that function promises
    capsys.readouterr()
    cli.main(["wo", "show", gave_up["id"], "--json"])
    assert "jarvis validation show" not in capsys.readouterr().out


# -- a question Neo never answered is not a question in flight


LAST_ERROR = "connection reset by peer"


def _kill(question_id: int) -> None:
    """Spend the ladder on one question: `failed`, stamped `UNREACHABLE_PREFIX`"""
    neo_store = NeoStore()
    try:
        assert neo_store.release_claim(question_id, LAST_ERROR,
                                       max_attempts=0) == "unreachable"
    finally:
        neo_store.close()


def _unreachable_ask(daemon):
    """A parked order whose assumption is linked to a question nobody will ever answer"""
    store, wo = park(daemon, auto_review=True)
    ask(daemon, store)
    (q,) = questions()
    _kill(q["id"])
    return store, wo, q


def _unreachable_confirmation(daemon):
    """The same on the `confirm: True` link, written by its real writer"""
    store, wo = park(daemon, auto_review=True)
    rows = store.all_assumptions(wo["id"])
    neo_store = NeoStore()
    try:
        q = autoreview.propose_confirmation(store, neo_store, "proj_a", wo, rows[0], rows)
    finally:
        neo_store.close()
    _kill(q["id"])
    return store, wo, q


def test_an_unreachable_ask_reads_as_the_users_and_not_as_awaiting_ruling(started):
    """THE SYMPTOM: `autoreview_asked` is append-only"""
    store, wo, q = _unreachable_ask(started)

    (line,) = rulings(store, wo["id"])

    assert "awaiting ruling" not in line
    assert line.startswith(f"Neo could not be reached (question {q['id']}) — nobody "
                           f"judged this; you decide it: ")
    assert f"jarvis neo answer {q['id']}" in line
    assert f"jarvis wo review {wo['id']}" in line
    assert line.endswith(f"— last error: {LAST_ERROR} (after 0 retries — nobody has "
                         f"judged this)")
    # A crash is not a decision: neither word may appear
    assert "escalated" not in line and "Neo decided" not in line


def test_an_unreachable_confirmation_says_what_was_never_confirmed(started):
    """Both links, one branch — `propose_confirmation` writes `autoreview_asked` too"""
    store, wo, q = _unreachable_confirmation(started)

    (line,) = rulings(store, wo["id"])

    assert "awaiting ruling" not in line
    assert (f"Neo could not be reached (question {q['id']}) — nobody judged this to "
            f"confirm its early reading; you decide it: ") in line


def test_the_summary_line_puts_an_unreachable_ask_back_with_the_user(started):
    """The same derivation on `jarvis wo show`'s one line"""
    store, wo, q = _unreachable_ask(started)

    line = _line(store, wo["id"])

    assert line == (f"assumption #1 left with you — Neo could not be reached "
                    f"(question {q['id']}); nobody judged it")
    assert "is with Neo" not in line
    # The row is still pending, so there is nothing to resolve it against
    assert "since " not in line


def test_a_dead_question_re_arms_the_assumption_it_was_asked_on():
    """Condition 6's mandate is unchanged"""
    a = assumption(neo_question_id=41)

    assert decide(assumption=a, unreachable_question_ids=(41,)).armed
    assert decide(assumption=a).code == autoreview.HELD_ASKED
    assert decide(assumption=a,
                  unreachable_question_ids=(9,)).code == autoreview.HELD_ASKED


def test_the_early_pass_re_arms_on_a_dead_link_too():
    """`decide_early` reaches one on an order that is still `running`"""
    a = assumption(neo_question_id=41)
    wo = {**WO, "status": "running"}

    assert autoreview.decide_early(a, wo, cfg(),
                                   unreachable_question_ids=(41,)).armed
    assert autoreview.decide_early(a, wo, cfg()).code == autoreview.HELD_ASKED


@pytest.mark.parametrize("status", ["queued", "answering", "answered", "escalated"])
def test_a_question_that_is_genuinely_in_flight_is_never_in_the_set(started, status):
    """Only `failed` means nobody judged it: `escalated` is Neo handing the question back WITH a decision"""
    store, wo = park(started, auto_review=True)
    ask(started, store)
    (q,) = questions()
    neo_store = NeoStore()
    try:
        neo_store.conn.execute("UPDATE questions SET status=? WHERE id=?",
                               (status, q["id"]))
        ids = started._unreachable_question_ids(   # noqa: SLF001
            neo_store, store.all_assumptions(wo["id"]))
    finally:
        neo_store.close()

    assert ids == set()


def test_the_set_names_a_dead_link_of_either_kind(started):
    """What the daemon supplies, read off both columns"""
    store, wo, q = _unreachable_ask(started)
    (row,) = store.all_assumptions(wo["id"])
    store.link_assumption_confirmation(row["id"], q["id"])
    neo_store = NeoStore()
    try:
        ids = started._unreachable_question_ids(   # noqa: SLF001
            neo_store, store.all_assumptions(wo["id"]))
    finally:
        neo_store.close()

    assert ids == {q["id"]}


def test_an_outage_that_ends_lets_the_assumption_be_asked_about_again(started):
    """The whole point: the daemon asks again once the question is dead"""
    store, wo, dead = _unreachable_ask(started)

    ask(started, store)

    asks = [q["id"] for q in questions()]
    assert len(asks) == 2, "the outage held the assumption for the life of the order"
    (row,) = store.all_assumptions(wo["id"])
    assert row["neo_question_id"] == max(asks) != dead["id"]


def test_a_second_outage_on_the_re_asked_question_re_arms_it_again(started):
    """THE LOOP #788 asked to be made safe: re-arming is not a one-shot"""
    store, wo, first = _unreachable_ask(started)

    ask(started, store)
    second = max(q["id"] for q in questions())
    _kill(second)
    ask(started, store)

    ids = sorted(q["id"] for q in questions())
    assert len(ids) == 3, "the second outage stuck the assumption behind a dead question"
    third = ids[-1]
    assert third not in (first["id"], second)
    (row,) = store.all_assumptions(wo["id"])
    assert row["neo_question_id"] == third, "the link still points at a dead question"

    # THE SAFETY: the live question holds the next pass, so the ask does not multiply
    ask(started, store)
    assert sorted(q["id"] for q in questions()) == ids
    assert len(events(store, wo["id"], "autoreview_asked")) == 3
    # `HELD_ASKED` is suppressed (`_holds_not_recorded`): the question IS filed
    assert events(store, wo["id"], "autoreview_held") == []


# -- the decision record: the order's own rulings reach the packet


def qrow(**over) -> dict:
    return {"id": 887, "wo_id": "wo-1", "kind": "question", "status": "answered",
            "question": "should the kill remedy exist?", "answer": "neither A nor B",
            "answered_by": "neo", "ts": 100.0, "review_status": "unreviewed",
            "review_feedback": None, **over}


class FakeNeo:
    """`answered_questions` and `get`, the two reads the record makes"""

    def __init__(self, *rows: dict, others: tuple[dict, ...] = (),
                 window: int | None = None):
        self.rows = list(rows)
        self.all = {r["id"]: r for r in (*rows, *others)}
        self.asked: list[str] = []
        self.window = window
        self.limits: list[int] = []

    def answered_questions(self, wo_id: str, limit: int = 20) -> list[dict]:
        self.limits.append(limit)
        if self.window is not None:
            limit = min(limit, self.window)
        return [r for r in self.rows
                if r["wo_id"] == wo_id and r["status"] == "answered"][:limit]

    def get(self, question_id: int) -> dict | None:
        return self.all.get(int(question_id))

    def ask(self, project, wo_id, question, context="", kind="") -> dict:
        self.asked.append(question)
        return {"id": 1, "question": question}


class FakeStore:
    def __init__(self, *messages: dict):
        self.messages = list(messages)

    def user_messages(self, wo_id: str, limit: int = 100) -> list[dict]:
        return self.messages

    def link_assumption_question(self, *a) -> None:
        pass

    link_assumption_confirmation = link_assumption_question

    def add_event(self, *a) -> None:
        pass


def record(*rows: dict, messages: tuple[dict, ...] = (), siblings=None,
           others: tuple[dict, ...] = (), ruling_on=None, window: int | None = None,
           **kw) -> str:
    return autoreview.decision_record(
        FakeStore(*messages), FakeNeo(*rows, others=others, window=window), "wo-1",
        [assumption()] if siblings is None else siblings, assumption=ruling_on, **kw)


def test_an_answered_question_reaches_the_packet_with_its_id_and_who_answered():
    """Test 1"""
    out = record(qrow(answered_by="user", answer="build NO kill remedy"))

    assert "Q887" in out
    assert "answered by user" in out
    assert "build NO kill remedy" in out


def test_a_corrected_neo_answer_carries_the_correction_that_overrode_it():
    """A corrected Neo answer is not authority on its own: `neo_store.review` leaves the user's feedback..."""
    out = record(qrow(review_status="corrected",
                      review_feedback="no — always ask first"))

    assert "the user corrected this: no — always ask first" in out


def test_an_empty_record_says_so_rather_than_going_silent():
    """"No prior decisions" and "prior decisions I was not shown" must not read alike: a reviewer that..."""
    assert autoreview.decision_record(FakeStore(), FakeNeo(), "wo-1", []) == ""
    assert "(no prior decisions recorded)" in autoreview._ruling_question(   # noqa: SLF001
        "p", WO, assumption(), [])


def test_the_record_is_capped_newest_first_and_says_what_it_dropped():
    """Test 2"""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i} " + "x" * 800)
            for i in range(12)]
    out = record(*rows)

    assert "ruling 0" in out, "the newest ruling was evicted"
    assert "ruling 11" not in out
    last = out.strip().splitlines()[-1]
    assert "older items omitted" in last and "6000 characters" in last


def test_a_smaller_per_project_cap_evicts_more():
    """The cap is a per-project catalog key (`validation.decision_record_chars`)"""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i} " + "x" * 800)
            for i in range(12)]
    tight = autoreview.decision_record(FakeStore(), FakeNeo(*rows), "wo-1", [],
                                       chars=2000)

    assert "ruling 0" in tight
    assert "ruling 2" not in tight
    assert "capped at 2000 characters" in tight.strip().splitlines()[-1]


def test_a_cited_question_is_placed_first_in_full_and_never_evicted():
    """Test 3"""
    long_answer = "the user said: " + "y" * 3000
    cited = qrow(id=887, ts=1.0, answer=long_answer)
    fillers = [qrow(id=900 - i, ts=500.0 - i, answer="z" * 900) for i in range(10)]
    cites = assumption(content="per Neo question 887, kept the shim")
    out = record(*fillers, cited, siblings=[cites], ruling_on=cites)

    assert long_answer in out, "the cited answer was truncated or evicted"
    assert out.index("Q887") < out.index("Q900")


def test_a_cited_question_answered_outside_the_query_window_is_shown_in_full():
    """§4 (#832)"""
    cited = qrow(id=887, ts=1.0, answer="the user said: keep the shim")
    newer = [qrow(id=900 - i, ts=500.0 - i, answer=f"ruling {i}") for i in range(3)]
    cites = assumption(content="per Neo question 887, kept the shim")
    out = record(*newer, cited, siblings=[cites], ruling_on=cites, window=3)

    assert "not answered" not in out
    assert "  Q887 (cited by the assumption) [answered by neo]" in out
    assert "the user said: keep the shim" in out


def test_short_items_the_cap_evicts_are_counted_exactly():
    """The omission line names the count the CAP evicted"""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i}") for i in range(5)]
    out = record(*rows, siblings=[], chars=200)

    last = out.strip().splitlines()[-1]
    assert "at least" not in last
    assert "older items omitted" in last and "capped at 200 characters" in last
    kept = [ln for ln in out.splitlines() if "answered by" in ln]
    assert f"{5 - len(kept)} older items omitted" in last


def test_rows_the_row_bound_cut_are_counted_in_the_same_omission_line(monkeypatch):
    """§3"""
    monkeypatch.setattr(autoreview, "_ANSWERED_ROWS", 3)
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i}") for i in range(6)]
    out = record(*rows, siblings=[])

    lines = out.strip().splitlines()
    assert "ruling 3" not in out
    assert len([ln for ln in lines if "omitted" in ln]) == 1
    assert "at least 1 older item" in lines[-1], lines[-1]


@pytest.mark.parametrize("cite", ["Neo question 887", "question 887", "Q887", "Neo 887"])
def test_every_field_spelling_of_a_question_id_is_recognised(cite):
    cites = assumption(content=f"decided this because {cite} said so")
    out = record(siblings=[cites], ruling_on=cites,
                 others=(qrow(id=887, status="queued", answer=None),))

    assert "Q887 (cited by the assumption; asked on this order, not answered)" in out


def test_only_the_assumption_being_ruled_on_contributes_citations():
    """The user's ruling on the recorded assumption: each order cites its OWN questions, not its siblings'"""
    ruling_on = assumption(id=20, n=6, content="per Q906, kept the shim")
    siblings = [assumption(id=i, n=i, content=f"decided per Q{900 + i}")
                for i in range(1, 6)] + [ruling_on]
    out = record(qrow(id=906, answer="the user said: keep the shim"),
                 siblings=siblings, ruling_on=ruling_on,
                 others=tuple(qrow(id=900 + i, status="queued", answer=None)
                              for i in range(1, 6)))

    cited = [line for line in out.splitlines() if "cited by the assumption" in line]
    assert cited[0].startswith("  Q906 (cited by the assumption)")
    assert "the user said: keep the shim" in cited[0]
    assert len(cited) == 1
    assert "Q905" not in out


#: Spec §4a: a sibling reference is keyword-anchored, and a bare `#n` matches nothing
def test_a_cited_sibling_assumption_renders_with_its_ruling():
    """Test 5"""
    sibling = assumption(id=9, n=3, status="approved", decided_by="user",
                         decided_reason="CSV is what the exporter already writes",
                         content="wrote the header row")
    cites = assumption(id=10, n=4, content="Per assumption #3, quoted every field")
    out = record(siblings=[sibling, cites], ruling_on=cites)

    assert "#3" in out
    assert "wrote the header row" in out
    assert "approved" in out
    assert "by the user" in out
    assert "CSV is what the exporter already writes" in out


def test_several_siblings_cited_in_one_phrase_all_resolve():
    first = assumption(id=9, n=2, content="wrote the header row")
    second = assumption(id=11, n=3, content="quoted every field")
    cites = assumption(id=10, n=4, content="Per assumptions 2 and 3, kept the shim")
    out = record(siblings=[first, second, cites], ruling_on=cites)

    assert "wrote the header row" in out and "quoted every field" in out


def test_a_cited_high_stakes_sibling_is_still_withheld():
    """Test 5's second half: citing a sibling does not lift the withholding net"""
    sibling = assumption(id=9, n=3, status="pending",
                         content="deleted the production database dump")
    cites = assumption(id=10, n=4, content="Per assumption #3, kept the shim")
    out = record(siblings=[sibling, cites], ruling_on=cites)

    assert "production database dump" not in out
    assert "withheld" in out


def test_a_bare_number_in_the_text_cites_nothing():
    """Test 7: `issue #832` and `PR #1090` are not references"""
    sibling = assumption(id=9, n=2, content="wrote the header row")
    cites = assumption(id=10, n=4, content="Followed issue #832 and PR #2, per #2")
    out = record(siblings=[sibling, cites], ruling_on=cites)

    assert "wrote the header row" not in out


def test_the_scanner_is_keyword_anchored_for_both_kinds():
    assert autoreview.cited_question_ids("per Q887 and neo question 888") == [887, 888]
    assert autoreview.cited_question_ids("issue #832 and PR #1090") == []
    assert autoreview.cited_sibling_ns("per assumption #2") == [2]
    assert autoreview.cited_sibling_ns("per assumptions 2 and 3") == [2, 3]
    assert autoreview.cited_sibling_ns("issue #832") == []


def test_the_resolved_refs_on_the_ask_path_are_bounded_by_the_record_cap():
    """Test 4's bound: `validation.decision_record_chars`, one resolver for both verbs"""
    short = autoreview.cited_context(FakeNeo(qrow(id=887, answer="keep the shim")),
                                     "wo-1", "Per Q887, and the footer?", [])
    assert "Q887" in short and "keep the shim" in short

    tight = autoreview.cited_context(
        FakeNeo(qrow(id=887, answer="y" * 9000)), "wo-1", "Per Q887, and the footer?",
        [], cfg=ValidationConfig(decision_record_chars=500))
    assert "y" * 9000 not in tight
    assert len(tight) < 1200


def test_an_assumption_already_stored_over_the_cap_still_reviews():
    """Test 9: the caps are write-time only — no length check in `autoreview`"""
    stored = assumption(content="z" * 4000)
    out = autoreview._ruling_question("p", WO, stored, [stored])   # noqa: SLF001

    assert "z" * 4000 in out


def test_a_cited_question_on_another_work_order_is_named_and_not_quoted():
    """Content from a different order has not been through this order's evidence gates"""
    cites = assumption(content="as Q887 decided")
    out = record(siblings=[cites], ruling_on=cites,
                 others=(qrow(id=887, wo_id="wo-9", answer="quoted elsewhere"),))

    assert ("Q887 (cited by the assumption; belongs to another work order — not shown)"
            in out)
    assert "quoted elsewhere" not in out


def test_a_cited_question_that_never_existed_is_stated_not_swallowed():
    """A worker citing a question id that was never asked is a fact about the assumption the reviewer is..."""
    cites = assumption(content="as Q887 decided")
    out = record(siblings=[cites], ruling_on=cites)

    assert "Q887 (cited by the assumption; no such question)" in out


def test_an_assumption_kind_row_is_a_one_liner_and_its_packet_is_never_requoted():
    """Test 4"""
    packet = "ASSUMPTION REVIEW — rule on assumption #4 " + "p" * 5000
    out = record(qrow(id=879, kind=autoreview.QUESTION_KIND, question=packet,
                      answer="approve — routine naming"),
                 siblings=[assumption(), assumption(id=8, n=4, neo_question_id=879)])

    assert "p" * 200 not in out and packet not in out
    assert "Q879" in out and "assumption #4" in out
    assert "approve — routine naming" in out


def test_an_assumption_kind_row_whose_sibling_is_gone_still_reads_as_a_ruling():
    """A ruling whose subject cannot be located is still a ruling"""
    out = record(qrow(id=879, kind=autoreview.QUESTION_KIND, question="x" * 4000,
                      answer="approve"))

    assert "Q879" in out and "assumption (row not found)" in out


def test_a_record_item_carrying_a_credential_is_withheld_not_dropped():
    """Test 5"""
    out = record(qrow(answer='API_KEY = "sk-live-4a9f8c2b7d1e"'))

    assert "sk-live-4a9f8c2b7d1e" not in out
    assert "Q887 (withheld — carries a line assigning API_KEY)" in out


def test_an_answer_whose_credential_is_on_a_later_line_is_still_withheld():
    """The net sees the field as the user typed it"""
    out = record(qrow(answer="Use this config:\npassword = hunter2-not-a-shape"))

    assert "hunter2-not-a-shape" not in out
    assert "Q887 (withheld — carries a line assigning password)" in out


def test_a_user_message_whose_credential_is_on_a_later_line_is_still_withheld():
    out = record(messages=({"ts": 1727500000.0, "body": "see below\nAPI_KEY=abc123xyz"},))

    assert "abc123xyz" not in out
    assert "(withheld — carries a line assigning API_KEY)" in out


def test_a_user_ruling_whose_credential_is_on_a_later_line_is_still_withheld():
    out = record(siblings=[
        assumption(),
        assumption(id=8, n=4, status="rejected", decided_by="",
                   decided_reason="as agreed:\nDB_PASSWORD = s3cr3t-not-a-shape"),
    ])

    assert "s3cr3t-not-a-shape" not in out
    assert "#4 rejected (withheld — carries a line assigning DB_PASSWORD)" in out


def test_a_high_stakes_wording_in_a_settled_ruling_is_shown_in_full():
    """§5, on the user's own ruling on Neo question 943: the high-stakes net is NOT applied here (#832)"""
    out = record(qrow(answered_by="user", answer="yes, delete the production rows"))

    assert "delete the production rows" in out
    assert "withheld" not in out


def test_a_user_message_on_the_order_reaches_the_record():
    out = record(messages=({"ts": 1727500000.0, "body": "no kill remedy, ever"},))

    assert "user message" in out and "no kill remedy, ever" in out


def test_a_sibling_the_user_settled_carries_their_reason():
    """Test 6"""
    out = record(siblings=[
        assumption(),
        assumption(id=8, n=4, status="rejected", decided_by="",
                   decided_reason="I want the flag spelled out"),
    ])

    assert "#4 rejected by the user — I want the flag spelled out" in out


def test_a_sibling_neo_settled_is_rendered_as_neos_not_the_users():
    """Test 6's other half, and Q879's own case: Neo's verdict on a sibling reaches the packet through its..."""
    out = record(qrow(id=877, kind=autoreview.QUESTION_KIND, question="packet",
                      answer="approve — mechanical"),
                 siblings=[
                     assumption(),
                     assumption(id=8, n=4, status="accepted",
                                decided_by=autoreview.DECIDER,
                                decided_reason="mechanical naming",
                                neo_question_id=877),
                 ])

    assert "by the user" not in out
    assert "Q877" in out and "answered by neo" in out and "assumption #4" in out


RECORD_HEADER = "# What has already been decided on this work order"


def test_propose_builds_the_section_with_no_daemon_and_no_model_call():
    """Test 7"""
    neo_ = FakeNeo(qrow(answered_by="user"))

    autoreview.propose(FakeStore(), neo_, "p", WO, assumption(), [assumption()])

    (packet,) = neo_.asked
    assert RECORD_HEADER in packet and "Q887" in packet
    # After the sibling list, before the closing answer instructions
    assert packet.index("do not rule on these") < packet.index(RECORD_HEADER)
    assert packet.index(RECORD_HEADER) < packet.index("Answer with `escalate`")


def test_the_projects_own_cap_reaches_the_packet_both_passes_build():
    """§3/§7"""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i}") for i in range(5)]
    tight = ValidationConfig(decision_record_chars=200)

    for build in (
        lambda neo_: autoreview.propose(FakeStore(), neo_, "p", WO, assumption(),
                                        [assumption()], cfg=tight),
        lambda neo_: autoreview.propose_confirmation(FakeStore(), neo_, "p", WO,
                                                     assumption(), [assumption()],
                                                     cfg=tight),
    ):
        neo_ = FakeNeo(*rows)
        build(neo_)
        (packet,) = neo_.asked
        assert "capped at 200 characters" in packet
        assert str(autoreview.DEFAULT_VALIDATION_DECISION_RECORD_CHARS) not in packet
        assert "ruling 4" not in packet


# -- the spec section the assumption cites (GitHub issue #1035)


SPEC_BODY = ("## 2. The remedy\n\nA worker that cannot reach the bus retries three "
             "times and then parks.")
SPEC_DOC = f"# Design\n\n## 1. Why\n\nBecause.\n\n{SPEC_BODY}\n\n## 3. Later\n\nNo.\n"
CHILD = {"id": "wo-1", "status": "needs_review", "title": "t", "description": "d",
         "parent_id": "fo-1", "kind": "child", "spec_section": "2. The remedy"}


class SpecStore(FakeStore):
    """`specs.spec_of`'s one read — the parent feature's stored plan"""

    def __init__(self, *messages: dict, doc: str = SPEC_DOC,
                 path: str = "docs/superpowers/specs/2026-10-13-bus.md"):
        super().__init__(*messages)
        self.plan = json.dumps({"design_doc": path, "design_doc_content": doc})

    def get_feature_order(self, fo_id: str) -> dict:
        return {"id": fo_id, "plan": self.plan}


SPEC_HEADER = "# THE SPEC SECTION THIS WORK ORDER WAS BUILT TO"


def builds(store, cfg=None):
    """Both passes, each as a one-argument builder over a fresh `FakeNeo`"""
    return (
        lambda neo_: autoreview.propose(store, neo_, "p", CHILD, assumption(),
                                        [assumption()], cfg=cfg),
        lambda neo_: autoreview.propose_confirmation(store, neo_, "p", CHILD,
                                                     assumption(), [assumption()],
                                                     cfg=cfg),
    )


def test_the_spec_section_reaches_both_packets_with_no_daemon_and_no_model_call():
    """#1035: a reviewer asked to rule on an assumption that cites the spec was shown no spec"""
    for build in builds(SpecStore()):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert SPEC_HEADER in packet
        assert "docs/superpowers/specs/2026-10-13-bus.md § 2. The remedy" in packet
        assert "retries three times and then parks" in packet
        # The authority behind the brief: after it, before what was delivered
        assert packet.index("# The work order it was recorded against") \
            < packet.index(SPEC_HEADER) < packet.index("# What the worker says it")


def test_the_projects_own_spec_cap_binds_in_both_packets():
    """`validation.assumption_spec_chars` is per project"""
    tight = ValidationConfig(assumption_spec_chars=40)

    for build in builds(SpecStore(), cfg=tight):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert "retries three times" not in packet
        assert "spec section truncated" in packet
        assert str(autoreview.DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS) not in packet
        assert "docs/superpowers/specs/2026-10-13-bus.md § 2. The remedy" in packet


def test_a_section_over_the_budget_is_trimmed_with_a_marker_and_the_reference():
    """Bounded-model-inputs §2: the reference rides whether or not anything was cut"""
    long_doc = f"# D\n\n## 2. The remedy\n\n{'x' * 9000}\n"
    neo_ = FakeNeo(qrow())

    autoreview.propose(SpecStore(doc=long_doc), neo_, "p", CHILD, assumption(),
                       [assumption()])

    (packet,) = neo_.asked
    assert "x" * 9000 not in packet and "x" * 500 in packet
    assert ("[spec section truncated — 6,000 of 9,018 chars; full section: "
            "docs/superpowers/specs/2026-10-13-bus.md § 2. The remedy]") in packet


def test_no_spec_section_says_so_explicitly_in_both_packets():
    """An absent section must read as absent"""
    # `WO` has no `parent_id`, so `spec_of` returns None without touching the store
    for build in (
        lambda neo_: autoreview.propose(FakeStore(), neo_, "p", WO, assumption(),
                                        [assumption()]),
        lambda neo_: autoreview.propose_confirmation(FakeStore(), neo_, "p", WO,
                                                     assumption(), [assumption()]),
    ):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert SPEC_HEADER in packet and "no spec section" in packet


def test_a_spec_whose_named_section_does_not_resolve_is_not_the_same_as_none():
    """Two different facts: this order was planned against a spec and its section could not be found"""
    neo_ = FakeNeo(qrow())

    autoreview.propose(SpecStore(doc="# D\n\n## 9. Elsewhere\n\nno.\n"), neo_, "p",
                       CHILD, assumption(), [assumption()])

    (packet,) = neo_.asked
    assert "did not resolve" in packet and "docs/superpowers/specs/2026-10-13-bus.md" in packet


def test_the_instruction_tells_the_reviewer_to_cite_the_section_not_escalate():
    """The whole point of #1035: a spec section that settles the assumption is a ruling"""
    for build in builds(SpecStore()):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert "cite the section" in packet


# -- the section the ASSUMPTION cites (user decision 2026-10-14, Neo learning #102)


CITED_HEADER = "# THE SPEC SECTION THIS ASSUMPTION CITES"
CITED_TEXT = "## 4. Retries\n\nThree attempts, then park and say so."


def citing(**over) -> dict:
    """An assumption whose own cited section was resolved and stored at assume time"""
    return assumption(**{"spec_path": "docs/superpowers/specs/2026-10-14-retries.md",
                         "spec_which": "4. Retries", "spec_text": CITED_TEXT, **over})


def test_the_section_the_assumption_cites_rides_in_both_packets():
    """It SUPPLEMENTS the work order's own spec block"""
    for build in (
        lambda neo_: autoreview.propose(SpecStore(), neo_, "p", CHILD, citing(),
                                        [assumption()]),
        lambda neo_: autoreview.propose_confirmation(SpecStore(), neo_, "p", CHILD,
                                                     citing(), [assumption()]),
    ):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert CITED_HEADER in packet
        assert ("docs/superpowers/specs/2026-10-14-retries.md § 4. Retries") in packet
        assert "Three attempts, then park and say so." in packet
        # The work order's own section is still there, and first
        assert SPEC_HEADER in packet and "retries three times and then parks" in packet
        assert packet.index(SPEC_HEADER) < packet.index(CITED_HEADER)


def test_an_assumption_that_cites_nothing_grows_no_second_block():
    for build in builds(SpecStore()):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert CITED_HEADER not in packet


TWO_CITED = ("docs/a.md § 3:\n## 3. Errors\n\nThey raise.\n\n"
             "docs/b.md § Data model:\n## Data model\n\nDicts.")


def test_the_reference_line_names_every_section_that_was_cited():
    """User decision 2026-10-15: the pointer columns are parallel newline lists"""
    neo_ = FakeNeo(qrow())

    autoreview.propose(SpecStore(), neo_, "p", CHILD,
                       citing(spec_path="docs/a.md\ndocs/b.md",
                              spec_which="3\nData model", spec_text=TWO_CITED),
                       [assumption()])

    (packet,) = neo_.asked
    assert "docs/a.md § 3; docs/b.md § Data model" in packet
    assert "docs/a.md § 3:" in packet and "docs/b.md § Data model:" in packet
    assert "They raise." in packet and "Dicts." in packet


def test_lists_of_different_length_say_so_instead_of_dropping_a_reference():
    neo_ = FakeNeo(qrow())

    autoreview.propose(SpecStore(), neo_, "p", CHILD,
                       citing(spec_path="docs/a.md\ndocs/b.md", spec_which="3",
                              spec_text=TWO_CITED),
                       [assumption()])

    (packet,) = neo_.asked
    assert "docs/a.md § 3; docs/b.md § (no section named)" in packet
    assert "2 paths recorded against 1 section names" in packet


def test_the_truncation_note_repeats_every_cited_reference():
    neo_ = FakeNeo(qrow())

    autoreview.propose(SpecStore(), neo_, "p", CHILD,
                       citing(spec_path="docs/a.md\ndocs/b.md",
                              spec_which="3\nData model",
                              spec_text="y" * 9000),
                       [assumption()], cfg=ValidationConfig(assumption_spec_chars=100))

    (packet,) = neo_.asked
    assert ("of 9,000 chars; full section: "
            "docs/a.md § 3; docs/b.md § Data model]") in packet


# -- one budget, need-based, half as a FLOOR (Neo ruling Q1884)


def doc_with(remedy_chars: int) -> str:
    """The fixture document with `2"""
    head = "## 2. The remedy\n\n"
    return (f"# Design\n\n## 1. Why\n\nBecause.\n\n{head}"
            f"{'r' * (remedy_chars - len(head))}\n\n## 3. Later\n\nNo.\n")


def cite_of(chars: int) -> str:
    head = "## 4. Retries\n\n"
    return head + "c" * (chars - len(head))


def one_packet(doc: str, cited: str, chars: int) -> str:
    neo_ = FakeNeo(qrow())
    autoreview.propose(SpecStore(doc=doc), neo_, "p", CHILD,
                       citing(spec_text=cited), [assumption()],
                       cfg=ValidationConfig(assumption_spec_chars=chars))
    (packet,) = neo_.asked
    return packet


def test_neither_block_is_trimmed_when_both_fit():
    """Q1884: each block takes only what it NEEDS"""
    packet = one_packet(doc_with(200), cite_of(3000), 4000)

    assert "spec section truncated" not in packet
    assert "r" * 182 in packet and "c" * 2985 in packet


def test_half_is_a_floor_the_under_floor_block_donates_its_remainder_from():
    """200 + 9000 over a 4000 budget: the 200 stays whole and only the oversized block is trimmed"""
    packet = one_packet(doc_with(200), cite_of(9000), 4000)

    assert packet.count("spec section truncated") == 1
    assert "r" * 182 in packet
    assert "[spec section truncated — 3,800 of 9,000 chars" in packet


def test_two_oversized_blocks_each_get_the_floor_and_both_announce_the_cut():
    packet = one_packet(doc_with(3000), cite_of(9000), 4000)

    assert packet.count("spec section truncated") == 2
    assert "[spec section truncated — 2,000 of 3,000 chars" in packet
    assert "[spec section truncated — 2,000 of 9,000 chars" in packet


def test_one_block_alone_gets_the_whole_budget():
    neo_ = FakeNeo(qrow())
    autoreview.propose(SpecStore(doc=doc_with(9000)), neo_, "p", CHILD, assumption(),
                       [assumption()], cfg=ValidationConfig(assumption_spec_chars=4000))

    (packet,) = neo_.asked
    assert "[spec section truncated — 4,000 of 9,000 chars" in packet


UNCITED = "THE-UNCITED-SENTENCE, which nobody asked about."


def test_no_question_builder_can_emit_a_whole_spec_file():
    """The regression the decision exists to prevent: a payload carrying a DOCUMENT"""
    big = (f"# Design\n\n## 1. Why\n\n{UNCITED}\n" + "w" * 9000
           + "\n\n## 2. The remedy\n\n" + "r" * 9000 + "\n\n## 3. Later\n\n"
           + "l" * 9000 + "\n")
    tight = ValidationConfig(assumption_spec_chars=200)

    for build in (
        lambda neo_: autoreview.propose(SpecStore(doc=big), neo_, "p", CHILD, citing(),
                                        [assumption()], cfg=tight),
        lambda neo_: autoreview.propose_confirmation(SpecStore(doc=big), neo_, "p",
                                                     CHILD, citing(), [assumption()],
                                                     cfg=tight),
    ):
        neo_ = FakeNeo(qrow())
        build(neo_)
        (packet,) = neo_.asked
        assert big not in packet
        assert UNCITED not in packet
        assert "w" * 100 not in packet      # §1 is not this child's section
        assert "l" * 100 not in packet      # nor is §3
        # §2 IS this child's section, and arrives bounded by the budget
        longest = max(len(run) for run in re.findall(r"r+", packet))
        assert 0 < longest < 200

    outline = plans.build_plan_context(
        {"design_doc": "docs/superpowers/specs/x.md", "design_doc_content": big},
        "main @ abc1234")
    assert big not in outline and UNCITED not in outline
    for run in ("w" * 100, "r" * 100, "l" * 100):
        assert run not in outline


def test_answered_questions_is_newest_first_and_keeps_every_kind(started):
    """The query, §1 (#832)"""
    neo_store = NeoStore()
    try:
        first = neo_store.ask("proj_a", "wo-rec", "one")
        second = neo_store.ask("proj_a", "wo-rec", "two", kind="approval")
        still_open = neo_store.ask("proj_a", "wo-rec", "three")
        other = neo_store.ask("proj_a", "wo-elsewhere", "four")
        for q in (first, second, other):
            neo_store.record_answer(q["id"], "yes", answered_by="user")

        got = neo_store.answered_questions("wo-rec")

        assert [r["id"] for r in got] == [second["id"], first["id"]]
        assert still_open["id"] not in [r["id"] for r in got]
        assert neo_store.answered_questions("wo-rec", limit=1) == got[:1]
    finally:
        neo_store.close()


# -- a transient drop on the ASK path is re-asked (io-51469add, finding `dropped-rulings`)


def test_an_ask_dropped_because_the_status_flipped_is_asked_again(started):
    """The confirmation path's #833 retry, one path along: wo-764454c5, wo-8fd40f1a"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (first,) = questions()
    store.set_status(wo["id"], "running", trigger="test")

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and row["neo_question_id"] is None
    spent = next(q for q in questions() if q["id"] == first["id"])
    # Never `escalated`: after the link is cleared nobody could answer it
    assert spent["status"] == "answered"
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_STATUS
    assert escalated["re_asked"] is True
    line = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert "goes back to Neo" in line and "left with you" not in line

    store.set_status(wo["id"], "needs_review", trigger="test")
    ask(started, store)

    (second,) = [q for q in questions() if q["id"] != first["id"]]
    assert store.all_assumptions(wo["id"])[0]["neo_question_id"] == second["id"]

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "accepted" and row["decided_by"] == "neo"


def test_a_high_stakes_drop_on_the_ask_path_keeps_its_link_and_is_the_users(started):
    """Non-transient codes are NEVER re-asked: the row really is the user's"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (first,) = questions()
    row = store.all_assumptions(wo["id"])[0]
    store.conn.execute("UPDATE assumptions SET content=? WHERE id=?", (SECRET, row["id"]))

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["neo_question_id"] == first["id"]
    assert questions()[0]["status"] == "escalated"
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_HIGH_STAKES
    assert escalated["re_asked"] is False

    ask(started, store)
    assert [q["id"] for q in questions()] == [first["id"]]


def test_an_ask_dropped_on_a_cancelled_order_is_not_re_armed(started):
    """`status` is transient only while the order can come back to `needs_review`"""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (first,) = questions()
    ops.cancel(wo["id"])

    drain(started)

    assert store.all_assumptions(wo["id"])[0]["neo_question_id"] == first["id"]
    assert questions()[0]["status"] == "escalated"
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["re_asked"] is False


def test_an_unseen_reference_is_asked_for_not_ruled_on():
    """Neo Q2290: assumption #2 argued from a withheld #1; Neo must ask for the data, not guess"""
    persona = autoreview.ASSUMPTION_REVIEWER_PERSONA
    assert "NEVER RULE ON PARTIAL DATA" in persona
    # Running order: object so the worker restates it; finished: escalate as evidence-insufficient
    rule = persona[persona.index("NEVER RULE ON PARTIAL DATA"):]
    rule = rule[:rule.index("\n\n")]
    assert "OBJECTION" in rule and "self-contained" in rule
    assert "evidence-insufficient" in rule
    assert "never\naccept" in rule or "never accept" in " ".join(rule.split())


def test_neo_answers_an_unseen_reference_by_asking_for_it():
    """The question-side twin of the rule above"""
    assert "NEVER DECIDE ON PARTIAL DATA" in neo.PERSONA
