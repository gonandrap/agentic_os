"""Neo decides a work order's assumptions, and the record says it was Neo.

docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md.

Four properties are worth more than the rest, and each has its own section:

* **`decide` is a table and is tested as one**, without a network and without a daemon.
  The safety rule lives in that function, and a rule only exercised through the loop that
  calls it is a rule tested once.
* **ACCEPTING IS THE NARROW PATH.** Every shape of reply that is not an explicit,
  routine-stakes approval must leave the assumption with the user — an escalation, a
  denial, a high-stakes acceptance, unparseable output, a failed call. That is the
  direction the whole feature has to fail in, so it is tested from every side rather than
  once.
* **THE RECORD NEVER CREDITS THE USER WITH A MACHINE DECISION.** The work order's own
  brief calls this its single most important post-condition, and it gets its own test at
  the surface a person actually reads.
* **The two mechanisms together take the human out of the last step**, and the negative
  half — auto-review OFF, everything else identical — still waits for a person.
"""

from __future__ import annotations

import json

import pytest

from jarvis import autoreview, neo, ops, stakes
from jarvis.catalog import ValidationConfig, load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

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


# -- `decide`: the condition table, both directions ------------------------------------


def test_a_routine_assumption_on_a_parked_work_order_is_put_to_neo():
    d = decide()
    assert d.armed and d.assumption_id == 3 and d.n == 2


@pytest.mark.parametrize("kw", [{"enabled": False}, {"auto_review": False}])
def test_neither_half_of_the_switch_arms_it_alone(kw):
    """The panel's switch is read BESIDE the authority, never instead of it: what makes
    an accepted assumption safe to land is that the panel judged the work it was part
    of."""
    assert decide(config=cfg(**kw)).code == autoreview.HELD_DISABLED


@pytest.mark.parametrize("status", ["running", "validating", "waiting_pr_merge",
                                    "completed", "waiting_input", "pending"])
def test_only_a_work_order_actually_waiting_on_a_person_is_reviewed(status):
    """`needs_review` is the analogue of auto-merge's `waiting_pr_merge`: the one status
    that means the user owes a decision. An assumption recorded mid-run is a decision
    about work that does not exist yet."""
    assert decide(wo={**WO, "status": status}).code == autoreview.HELD_STATUS


@pytest.mark.parametrize("status", ["accepted", "rejected"])
def test_an_assumption_somebody_already_settled_is_not_reopened(status):
    assert decide(assumption=assumption(status=status)).code == autoreview.HELD_SETTLED


def test_a_panel_that_gave_up_is_not_answered_for_the_user():
    """`ops.land_when_cleared` LANDS an `escalated` round, and the only caller that can
    reach it with one is the user saying ship it anyway. Clearing the assumptions under a
    give-up would have the OS answer a different question from the one it was asked."""
    assert decide(round_outcome="escalated").code == autoreview.HELD_PANEL_GAVE_UP


@pytest.mark.parametrize("outcome", ["", "pending", "passed", "rejected"])
def test_every_other_round_outcome_leaves_the_assumption_reviewable(outcome):
    """The pair to the row above: `escalated` is the ONE outcome that holds, so a guard
    written as "only review a passed work order" would fail here."""
    assert decide(round_outcome=outcome).armed


@pytest.mark.parametrize("outcome", ["", "pending"])
def test_an_open_round_does_not_hold_the_ask_pass(outcome):
    """§5.2 of docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-an-
    assumption-for-ever.md, pinned so a later reader cannot "fix" the asymmetry by
    symmetry: the ASK pass judges the assumption — a sentence the worker wrote — and a
    panel verdict does not change it. Only the CONFIRMATION pass reads the diff."""
    assert decide(round_n=1, round_outcome=outcome).armed
    assert autoreview.decide_early(assumption(), {**WO, "status": "running"}, cfg(),
                                   round_n=1, round_outcome=outcome).armed


def test_the_open_round_hold_is_unreachable_from_either_ask_function():
    """The pair: `round_open` is `decide_confirm`'s alone, so neither ask function can
    produce it whatever the round says."""
    for outcome in ("", "pending", "passed", "rejected", "escalated"):
        for n in (0, 1, 3):
            assert decide(round_n=n,
                          round_outcome=outcome).code != autoreview.HELD_ROUND_OPEN
            assert autoreview.decide_early(
                assumption(), {**WO, "status": "running"}, cfg(), round_n=n,
                round_outcome=outcome).code != autoreview.HELD_ROUND_OPEN


def test_a_refusal_the_worker_has_not_answered_holds_everything_behind_it():
    """A refused assumption is guidance the worker has not answered, and settling its
    siblings would land the very decision the user turned down."""
    assert decide(refusal_answered=False).code == autoreview.HELD_REFUSAL_UNANSWERED


def test_the_hold_says_so_when_the_worker_pushed_without_declaring_it():
    """Spec §2c. Today's sentence — "has not delivered again since" — is FALSE in
    wo-dbea82cf's case: the worker HAS worked since, it just never declared it."""
    d = decide(refusal_answered=False, undeclared_delivery=True)

    assert d.code == autoreview.HELD_REFUSAL_UNANSWERED
    assert d.reason == autoreview.REFUSAL_UNDECLARED_REASON
    assert "pushed commits since without running `jarvis wo finish`" in d.reason
    assert "the OS has asked it to declare them" in d.reason


def test_the_early_hold_carries_the_same_second_sentence():
    """Both sites, per the spec: `decide` and `decide_early` render the same hold."""
    d = autoreview.decide_early(assumption(), {**WO, "status": "running"}, cfg(),
                                refusal_answered=False, undeclared_delivery=True)

    assert d.code == autoreview.HELD_REFUSAL_UNANSWERED
    assert d.reason == autoreview.REFUSAL_UNDECLARED_REASON


def test_the_undeclared_sentence_is_free_of_a_count_a_sha_and_a_clock():
    """It is stored verbatim by `ack_attention` and compared by INV-ATTENTION-REASON, so
    anything in it that moves could never be acknowledged (kn-681db233 point 3)."""
    text = autoreview.REFUSAL_UNDECLARED_REASON

    assert not any(ch.isdigit() for ch in text)


def test_the_undeclared_finding_reaches_the_existing_nudge_remedy(project):
    """Spec §2c's remedy half. The detector does not act: it raises a finding, and the
    supervisor's judge path hands that to `remedies.propose` with the SHIPPED `nudge` —
    a gate request plus a Neo question, and nothing said to the worker until a grant."""
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
    # A proposal is not an act: nothing has reached the session.
    assert store.queued_messages(wo["id"]) == []


def test_an_assumption_is_asked_about_once_and_never_again():
    """One question per assumption for its whole life — and an escalated one is held by
    the user, so re-asking every reconcile tick would be the OS lobbying them."""
    d = decide(assumption=assumption(neo_question_id=41))
    assert d.code == autoreview.HELD_ASKED and "41" in d.reason


# -- the first net: high stakes, before any model call ---------------------------------


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
    """Every entry in `HIGH_STAKES` is the code form of a clause `neo.PERSONA` already
    escalates on, applied one layer earlier — not a second vocabulary for it."""
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
    """The pair, and it is what stops the net being vacuously wide: `monkey` must not
    trip `\\bkey\\b`, and a feature that never fires is not a feature."""
    assert autoreview.high_stakes_marker(text) == ""
    assert decide(assumption=assumption(content=text)).armed


@pytest.mark.parametrize("text", [
    # The three observed in production, issue #713.
    "the lead-edit refusal is on for every project on the next release",
    "Query words under 3 characters are dropped",
    "not verified against the live CLI",
    # The same defect in the rest of the repo's everyday vocabulary.
    "migrate the call sites to the new helper",
    "the schema of the JSON reply is unchanged",
    "dropped from the record once the user acks it",
    "released the lock before returning",
    "ship the fix in this PR",
])
def test_engineering_prose_is_not_an_act(text):
    """A WORD IS NOT AN ACT. `release`, `live`, `drop`, `migrate` and `schema` are this
    repo's daily vocabulary, so bare word patterns held the routine assumptions the
    feature exists to decide — which is indistinguishable from the net being off."""
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
    """The pair to the above, and the expensive direction: narrowing the patterns to the
    ACT must not narrow them past the act."""
    assert decide(assumption=assumption(content=text)).code == \
        autoreview.HELD_HIGH_STAKES


@pytest.mark.parametrize("text, held", [
    ("the re-write tax re-sends 300k input tokens on every turn", False),
    ("token usage is reported per turn, not per work order", False),
    ("the cache-write price is charged on the prompt tokens", True),  # `price`
    ("I hard-coded the token in settings.json", True),
])
def test_the_token_carve_out_is_a_sense_not_a_word(text, held):
    """`token` stays bare — a hard-coded one is a credential — but this repo MEASURES
    ITSELF IN TOKENS, so the economics sense is carved out (Neo, question 562)."""
    assert bool(autoreview.high_stakes_marker(text)) is held


# -- the classifier's verdict, passed in ------------------------------------------------
#
# docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md SS3.7. `decide`
# stays PURE: the caller computes the verdict and passes it, so the whole condition table
# is still unit-testable without a socket.

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
    """`stakes=None` is today's behaviour, and BYTE-IDENTICAL is the claim: the regex
    mode ships as the default, so any drift in this line is a change to what every
    project already running sees on its timeline."""
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
    """SS3.2: `reason` REPLACES the matched phrase. The regex line tells the user a word
    was present; this one tells them what the OS thinks the worker did."""
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")

    d = rule(assumption(content=ACT), wo, cfg(), stakes=high_verdict())

    assert d.code == autoreview.HELD_HIGH_STAKES
    assert "cuts and pushes a release tag" in d.reason
    assert "publishing" in d.reason


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_routine_verdict_arms_text_the_regex_would_have_held(rule):
    """THE CASE THAT MATTERS MOST. `delet` is the regex's top marker at 21 of 91 hits and
    almost none of them is a deletion; under a routine verdict the row arms."""
    text = ("this branch's own ops.objection_undeliverable was deleted upstream, so the "
            "conflict resolution keeps the other copy")
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")
    assert autoreview.high_stakes_marker(text)

    assert rule(assumption(content=text), wo, cfg(), stakes=routine_verdict()).armed
    assert rule(assumption(content=text), wo, cfg()).code == autoreview.HELD_HIGH_STAKES


@pytest.mark.parametrize("rule", [autoreview.decide, autoreview.decide_early])
def test_a_high_verdict_holds_text_the_regex_misses(rule):
    """SS1.1, the half no narrowing reaches: a release the regex never saw."""
    wo = dict(WO, status="running" if rule is autoreview.decide_early else "needs_review")
    assert autoreview.high_stakes_marker(ACT) == ""

    assert rule(assumption(content=ACT), wo, cfg()).armed
    assert rule(assumption(content=ACT), wo, cfg(),
                stakes=high_verdict()).code == autoreview.HELD_HIGH_STAKES


def test_an_unreachable_classifier_holds_and_the_hold_says_so():
    """NEVER FABRICATE A DEFAULT FROM A FAILURE: the hold from a call that never
    returned must say it never returned, not borrow a category."""
    d = autoreview.decide(assumption(content=ACT), WO, cfg(),
                          stakes=stakes.unreachable("haiku"))

    assert d.code == autoreview.HELD_HIGH_STAKES
    assert stakes.HIGH_UNREACHABLE in d.reason


@pytest.mark.parametrize("verdict, armed", [(routine_verdict(), True),
                                            (high_verdict(), False)])
def test_the_confirmation_pass_is_gated_by_the_same_verdict(verdict, armed):
    """`decide_confirm` forwards it unchanged, so a provisional accept is no ticket past
    the net the ask pass was gated by."""
    a = assumption(content=ACT, provisional_verdict="accept", neo_question_id=41)

    assert autoreview.decide_confirm(a, WO, cfg(), stakes=verdict).armed is armed


def test_the_cheap_conditions_still_win_over_the_verdict():
    """Order matters: a row that holds on a cheaper condition must not be reported as a
    stakes hold, whatever the classifier said about it."""
    d = autoreview.decide(assumption(content=ACT, status="accepted"), WO, cfg(),
                          stakes=routine_verdict())

    assert d.code == autoreview.HELD_SETTLED


def test_sibling_redaction_stays_on_the_regex_under_every_mode():
    """SS3.6. `sibling_line` runs over a LIST — N model calls to render ONE prompt — and
    being wrong there costs one sentence of context, not a decision in the user's name.
    So the consequence is explicit: a row the classifier arms can still be withheld."""
    text = "deleted the orphaned rows instead of backfilling them"
    line = autoreview.sibling_line({"n": 2, "status": "pending", "content": text})

    assert "withheld" in line
    assert text not in line


SECRET = "reused the production api key rather than minting a second one"


def test_a_held_assumption_is_not_quoted_as_some_other_assumptions_context():
    """THE NET IS ABOUT TEXT REACHING A MODEL, NOT ABOUT WHOSE ROW IT IS.

    Condition 7 stops a high-stakes assumption being RULED on. The question about the
    routine assumption beside it lists the siblings for context, so without the same
    filter there the feature ships the exact sentence it exists to withhold — and one
    high-stakes row beside one routine row is the ORDINARY shape of a work order, so this
    was the common path rather than a corner.
    """
    line = autoreview.sibling_line({"id": 9, "n": 3, "status": "pending",
                                    "content": SECRET})

    assert "production" not in line and "key" not in line
    assert SECRET not in line
    # Withheld, not dropped: silence would tell the reviewer this work order had only
    # routine assumptions. The number, the status and the withholding are a
    # classification; the content is the secret.
    assert "#3" in line and "[pending]" in line and "withheld" in line


def test_a_routine_sibling_is_still_quoted_in_full():
    """The control. A filter that withheld everything would pass the test above and make
    the sibling list — the reason an assumption is sometimes defensible at all — empty."""
    line = autoreview.sibling_line({"id": 9, "n": 3, "status": "accepted",
                                    "content": ROUTINE})

    assert ROUTINE in line and "withheld" not in line


# -- the second net: what a reply MEANS ------------------------------------------------


def accepted(**over) -> dict:
    return {"escalate": False, "verdict": "approved", "stakes": "routine",
            "reason": "a naming convention", "model": "claude-opus-5", **over}


def test_an_explicit_routine_approval_is_the_one_shape_that_accepts():
    r = autoreview.read_ruling(accepted())
    assert r.accept and not r.overridden and r.model == "claude-opus-5"


def test_neo_marking_its_own_acceptance_high_stakes_overrides_the_acceptance():
    """A backstop a reviewer can wave through is not one — the argument that makes the
    child cap outrank Neo on a plan."""
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
    """THE SECOND NET MUST NOT FAIL OPEN, and written as `== "high"` it did.

    An allowlist rather than a blocklist (kn-32434cef's shape): every row here is a reply
    that parses cleanly, says `approve`, and carries a `stakes` the OS has no reason to
    trust. Under the old compare each one read as ROUTINE and was accepted with the
    backstop silently off — and the first row, the field simply missing, is the likeliest
    malformed reply a model produces.

    The cost of getting this wrong in this direction is one review action the user was
    going to make anyway. The cost of the other direction is a decision made in their
    name with no backstop at all.
    """
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
    """The absence of a warning is not a warning's absence. Writing `routine` into the
    row would put a judgement in Neo's mouth that it never made, and that row is what
    `jarvis wo show` and the escalation event carry."""
    reply = accepted()
    reply.pop("stakes")

    r = autoreview.read_ruling(reply)

    assert r.stakes == autoreview.STAKES_UNCLASSIFIED != autoreview.STAKES_ROUTINE
    assert "did not classify the stakes at all" in r.reason


def test_the_allowlist_is_exactly_the_word_the_persona_asks_for():
    """Pinned as a set rather than read off the implementation: every entry added here is
    a value a future reader has to trust, and the persona names one."""
    assert autoreview.ROUTINE_STAKES == frozenset({"routine"})
    assert '"stakes": "routine"' in autoreview.ASSUMPTION_REVIEWER_PERSONA


def test_neo_declining_the_assumption_escalates_and_carries_its_reading():
    """There is no machine rejection (Neo, question 301), so a `deny` reaches the user —
    WITH Neo's reason, which is strictly more than they get today."""
    r = autoreview.read_ruling(accepted(verdict="denied",
                                        reason="this changes the CLI's output"))
    assert not r.accept and not r.overridden
    assert "would have turned this down" in r.reason
    assert "changes the CLI's output" in r.reason


@pytest.mark.parametrize("verdict", [
    {"escalate": True, "verdict": "denied", "reason": "your call"},
    # Unparseable output, through the real parser rather than a hand-made dict.
    neo.parse_verdict("I think you should maybe do the thing?"),
    # A well-formed reply that never says `approve`: absent must never mean yes.
    {"escalate": False, "reason": "hmm"},
    # ...nor must a truthy-looking string in the wrong field.
    {"escalate": False, "verdict": "approve-ish", "reason": "hmm"},
    # The transport failed; `drain_queue` synthesises this.
    {"escalate": True, "answer": "", "reason": "neo call failed: boom", "failed": True},
    {},
])
def test_everything_that_is_not_an_explicit_approval_stays_with_the_user(verdict):
    """THE DIRECTION THE FEATURE MUST FAIL IN, asserted over the whole shape space rather
    than on the one reply somebody thought of."""
    assert not autoreview.read_ruling(verdict).accept


# -- why the OS escalated, as one groupable label --------------------------------------
#
# docs/specs/2026-10-01-neo-observability.md §1 plus Neo's ruling on question 1170: an
# override is a THIRD answer to "who decided", so these five members are their own class
# and every one is derived from facts the code already holds — never read off a reply.


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
    """No `Ruling` on that path: the cap outranks whatever Neo said about the plan."""
    assert autoreview.escalation_cause(over_cap=True) == "scope-over-cap"
    # Neo escalated on its own, so the cause is Neo's chosen label, not this one.
    assert autoreview.escalation_cause(over_cap=True, escalate=True) == ""


def test_a_fact_pattern_that_is_none_of_the_five_records_no_cause():
    """A NULL is correct and a wrong label is not. An acceptance the OS dropped at settle
    time for its own reasons is not one of these five."""
    assert autoreview.escalation_cause(autoreview.read_ruling(accepted())) == ""
    assert autoreview.escalation_cause() == ""
    # Neo escalated on its own: the cause is the CHOSEN label off its reply, which
    # `drain_queue` already wrote, and this must not overwrite it.
    escalated = accepted(escalate=True, verdict="denied")
    assert autoreview.escalation_cause(autoreview.read_ruling(escalated),
                                       escalate=True) == ""


def test_every_cause_this_helper_can_return_is_in_the_overridden_class():
    """The helper lives in a PURE module and spells the members as literals, so the enum
    is pinned from the test rather than by an import that would invert the layering."""
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
    """`stakes.HIGH_UNREACHABLE` / `HIGH_UNPARSEABLE` reach `HELD_HIGH_STAKES`, which
    holds the review BEFORE a question row exists — so nothing could ever write these.
    Documented as an invisible class in the report instead (Neo, question 1170)."""
    from jarvis.neo_store import ESCALATION_CAUSES

    assert "classifier-unreachable" not in ESCALATION_CAUSES
    assert "classifier-unparseable" not in ESCALATION_CAUSES


# -- the daemon: asking ----------------------------------------------------------------


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    # The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`. Several
    # tests here stand in for the USER at the console, and `ops._refuse_worker_write`
    # refuses a worker on purpose — the same thing `tests/test_config_console.py` drops,
    # for the same reason.
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def park(daemon, *, auto_review: bool, auto_merge: bool = False,
         assumptions: tuple[str, ...] = (ROUTINE,), description: str = "d",
         outcome: str = "passed", pr_url: str = PR):
    """A work order finished behind a pull request, parked on its assumptions.

    Everything the seven conditions need, so each test moves exactly one of them.

    The round is opened and settled BY HAND, `tests/test_automerge.py`'s `arm`'s way and
    for its reason: the panel does not run in a unit test, and a settled `passed` round
    on the judged commit is what a green one leaves behind. Doing it here also keeps the
    catalog FILE's `validation.enabled` false, so `ops.finish` does not try to collect an
    evidence packet from a repository these tests never built.
    """
    spec = daemon.catalog.project("proj_a")
    spec.validation.enabled = True
    spec.validation.auto_review = auto_review
    spec.validation.auto_merge = auto_merge
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
    """THE SWITCH IS REAL AND NOT DECORATIVE. Everything else lines up and the only
    missing fact is the project's own permission: nothing is asked, nothing is held, and
    the work order waits for the user exactly as it does today."""
    store, wo = park(started, auto_review=False)

    ask(started, store)

    assert questions() == []
    assert store.pending_assumptions(wo["id"])
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    # ...and it did not even cost a line on the timeline: an opted-out project's record
    # must not carry notes about a mechanism it does not have.
    assert events(store, wo["id"], "autoreview_held") == []
    assert ops.autoreview_state(store, wo) is None


def test_neo_being_switched_off_files_no_question_nobody_will_drain(started,
                                                                   catalog_file):
    """A question filed against a queue that does not run is worse than no question: the
    assumption would be parked behind it for ever."""
    started.catalog.os.neo.enabled = False
    store, wo = park(started, auto_review=True)

    ask(started, store)

    assert questions() == []


def test_each_assumption_gets_its_own_question_never_one_verdict_over_a_list(started):
    """A list invites one judgement over its easiest member. Three assumptions, three
    questions, three back-links."""
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
    # Each question quotes ONE assumption under the heading that is ruled on, and names
    # the others only as context.
    for row in rows:
        q = next(x for x in asked if x["id"] == row["neo_question_id"])
        assert f"# The assumption\n{row['content']}" in q["question"]
        assert "do not rule on these" in q["question"]


def test_no_question_the_os_files_carries_a_high_stakes_assumptions_text(started):
    """THE SAME GUARANTEE THROUGH THE DAEMON, asserted over EVERY question filed rather
    than over the one the test happens to look at. The unit test pins `sibling_line`; this
    pins that nothing routes around it — the question body, the `context` field the
    dashboard renders, and any question of any kind this pass produces."""
    store, wo = park(started, auto_review=True,
                     assumptions=(ROUTINE, SECRET, "used tabs in the fixture"))

    ask(started, store)

    filed = questions()
    # Two routine assumptions are asked about; the high-stakes one is not.
    assert len(filed) == 2
    for q in filed:
        assert SECRET not in q["question"]
        assert "production" not in q["question"] and "api key" not in q["question"]
        assert SECRET not in (q.get("context") or "")
    # ...and it is named as withheld rather than silently absent, so the reviewer knows
    # the order has an assumption it is not being shown.
    assert all("withheld" in q["question"] for q in filed)
    # The held one is on the record with its reason, exactly as before.
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_HIGH_STAKES


def test_asking_twice_asks_once(started):
    """`decide`'s condition 6 through the loop: the pass runs every reconcile tick."""
    store, wo = park(started, auto_review=True)

    ask(started, store)
    ask(started, store)

    assert len(questions()) == 1


def test_a_hold_is_recorded_once_per_reason_and_is_not_an_attention_item(started):
    """`_note_autoreview_held`'s dedupe. A held assumption is one the user decides
    themselves, which is what they did for every assumption before this existed — and the
    work order is already on their list carrying `assumptions pending review`."""
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


def settled_round(store, wo_id: str, outcome: str, reason: str = "") -> None:
    """A fresh settled round on the judged commit, `park`'s way."""
    row = store.open_validation_round(wo_id=wo_id, fingerprint="fp1")
    store.set_validation_head(row["id"], JUDGED)
    store.close_validation_round(row["id"], outcome, reason)


def test_a_hold_returning_to_an_earlier_reason_is_recorded_again(started):
    """ISSUE #782, the real trace: held on stakes, then a transient panel give-up, then a
    forced round that PASSED — and the third pass held on the stakes again and wrote
    nothing, because the dedupe looked at EVERY past hold. `assumptions_with_rulings`
    renders the newest, so every surface went on saying the panel gave up on an order
    whose panel had passed."""
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


# -- the panel's give-up: which round, what it said, and when it stops being true -------
#
# docs/superpowers/specs/2026-09-26-a-panel-gave-up-hold-says-which-round-and-stops-when-
# it-passes.md. The hold is an append-only event read as a statement about NOW, so this
# one — whose subject is a queryable row — is resolved against that row at the read side.

PANEL_REASON = ("the seats could not agree about whether the retry loop is safe under a\n"
                "partial write, and the chair would not break the tie:   two read the lock\n"
                "as sufficient and one read it as advisory only, which is a question about "
                "the storage engine and not about this diff at all — the chair asked for a "
                "second opinion and none of the three would move, so the submission comes "
                "to you with all three readings attached")


def test_a_panel_hold_names_the_round_and_quotes_what_the_panel_said(started):
    """A reader cannot tell a real disagreement from #778's OAuth outage from the static
    sentence, and the round reason — the one text that distinguishes them — was read at
    both daemon call sites and thrown away."""
    store, wo = park(started, auto_review=True, outcome="")
    row = store.latest_validation_round(wo_id=wo["id"])
    store.close_validation_round(row["id"], "escalated", PANEL_REASON)

    ask(started, store)

    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP
    assert held["round"] == 1
    assert "gave up on round 1" in held["reason"]
    # Whitespace-collapsed and clamped, `ops.objection_response_line`'s rule: one line
    # inside a list of assumptions, with the full text one pointer away.
    assert len(PANEL_REASON) > 300
    assert " ".join(PANEL_REASON.split())[:117] + "…" in held["reason"]
    assert "\n" not in held["reason"] and "  " not in held["reason"]


def test_a_panel_hold_stops_being_rendered_once_a_later_round_passes(started):
    """#782's sibling, and the one only the freshness check can satisfy: ONE routine
    assumption, so no second hold reason masks the stale line the way
    `test_a_hold_returning_to_an_earlier_reason_is_recorded_again`'s stakes hold does.

    Measured on wo-15f5d969: round 3 was forced and PASSED at the same commit, and both
    the pending assumptions and the `auto_review:` line still said the panel gave up."""
    store, wo = park(started, auto_review=True, outcome="escalated")
    ask(started, store)
    assert "gave up" in rulings(store, wo["id"])[0]

    settled_round(store, wo["id"], "passed")

    # Not-looked-at is an understatement; the stale sentence is a lie.
    assert rulings(store, wo["id"]) == [""]
    assert ops.autoreview_state(store, store.get_work_order(wo["id"])) is None
    # The history is KEPT — the hold did happen, and the timeline is append-only.
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP


def test_a_second_escalated_round_refreshes_the_hold_rather_than_voiding_it(started):
    """The dedupe key carries the round, or round 2's give-up writes nothing, the payload
    still says round 1, and the freshness check drops a hold that is TRUE."""
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
    """ISSUE #975, measured on wo-2933fc24: both refusal sentences share the code
    `HELD_REFUSAL_UNANSWERED`, and `undeclared_delivery` can only become true AFTER a hold
    was written with the other one. Keyed on the code alone, the second write was dropped
    and `assumptions_with_rulings` — newest hold wins — kept rendering 'has not delivered
    again since' about a worker that had pushed commits."""
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
    """The dedupe the reason key must not weaken: the pass runs every reconcile tick."""
    store, wo = park(started, auto_review=True, outcome="")

    for _ in range(3):
        started._note_autoreview_held(
            store, wo["id"],
            _refusal_hold(store, wo, autoreview.REFUSAL_UNANSWERED_REASON))

    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["reason"] == autoreview.REFUSAL_UNANSWERED_REASON


def test_a_hold_written_before_the_round_was_recorded_is_read_honestly(started):
    """A payload from before this shipped carries no round: KEPT while the newest round is
    still escalated (the panel did give up), dropped once a later one passed."""
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
    """§8 row 5 of docs/superpowers/specs/2026-09-28-a-dropped-confirmation-must-not-hold-
    an-assumption-for-ever.md: the freshness filter is `panel_gave_up`'s alone. A
    `HELD_CONFIRM_SPENT` hold is about a spent NEO QUESTION, not about a round, so a
    later passed round says nothing about it — dropping it would put the stale settle-site
    sentence back on every surface, which is the defect this spec fixes."""
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

    # ...and the hold is what the readers render, on both surfaces.
    (line,) = rulings(store, wo["id"])
    assert "question 920" in line and "no longer open" in line
    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))
    assert state["code"] == autoreview.HELD_CONFIRM_SPENT
    assert "question 920" in state["line"]


def test_a_status_hold_stops_being_shown_once_a_review_is_owed_again(started):
    """A `status` hold claims the order is in no state this pass acts in, so it is stale
    exactly when the order now IS in one (2026-09-27-a-stale-merge-hold-is-not-the-reason-
    a-pr-is-not-merging.md §6). The live shape: an order cancelled mid-ruling, then
    reopened — every surface went on saying the OS declined to look at the assumption.
    """
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


# docs/superpowers/specs/2026-10-01-a-confirmation-is-not-re-run-on-a-settled-
# assumption.md §3.3. Every per-assumption hold claims something about a row, and the
# claim is untrue once that row is decided — whatever the code says.

SETTLED_SPENT = ("assumption #1 is yours — the OS asked Neo to confirm its early reading "
                 "(question 920) and that question is no longer open")


def settled_pair(started):
    """A parked order with one row Neo ACCEPTED and one still pending — §1.1's shape."""
    store, wo = park(started, auto_review=True,
                     assumptions=(ROUTINE, "put the helper at the bottom"))
    first, second = store.all_assumptions(wo["id"])
    store.review_assumption(first["id"], "accepted", decided_by="neo")
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_CONFIRM_SPENT, "reason": SETTLED_SPENT,
                     "assumption_id": first["id"], "n": 1})
    return store, wo, first, second


def test_a_hold_about_a_row_that_has_since_settled_is_not_the_summary_line(started):
    """§3.3: the three observed orders' stored state, which no write-side change reaches.
    The claim is withdrawn; the event stays on the timeline."""
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
    """§3.3's cache: one `stale` closure runs over every row of the order, so a single
    status slot would answer the pending row with the accepted row's status."""
    store, wo, first, second = settled_pair(started)
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_HIGH_STAKES,
                     "reason": "assumption #2 mentions 'production'",
                     "assumption_id": second["id"], "n": 2})

    rows = ops.assumptions_with_rulings(store, wo["id"])

    assert rows[0]["os_ruling"] is None
    assert rows[1]["os_ruling"]["code"] == autoreview.HELD_HIGH_STAKES


def test_an_order_level_hold_names_no_row_and_is_never_dropped_by_one(started):
    """§3.3's first bullet: a payload with no `assumption_id` makes no claim about a row,
    so the clause cannot answer about it and must leave it alone."""
    store, wo = park(started, auto_review=True)
    (row,) = store.all_assumptions(wo["id"])
    store.review_assumption(row["id"], "accepted", decided_by="neo")
    store.add_event(wo["id"], "autoreview_held",
                    {"code": autoreview.HELD_DISABLED,
                     "reason": "this project has not given the OS permission to decide "
                               "its assumptions (`validation.auto_review`)"})

    state = ops.autoreview_state(store, store.get_work_order(wo["id"]))

    assert state["code"] == autoreview.HELD_DISABLED


# -- the daemon: ruling ----------------------------------------------------------------


def drain(daemon):
    daemon._neo_drain()


def test_an_accepted_assumption_settles_and_the_record_says_the_os_did_it(
        started, catalog_file):
    """THE POST-CONDITION THE WHOLE FEATURE RESTS ON, at the row level: who, why, which
    model, under which configuration, and the question that ruled.

    The config write is what puts a version in the ledger, so the stamp has something
    real to be — asserting a truthy column against an empty ledger would pass on None
    for ever (`ops.current_config_version` answers None honestly on a fleet that has
    never written one)."""
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
    # The worker finished long ago: nothing may start a turn on it.
    assert store.list_messages(wo["id"]) == []
    assert events(store, wo["id"], "neo_answered") == []


def test_jarvis_wo_show_never_presents_a_machine_decision_as_the_users(
        started, capsys):
    """THE SURFACE TEST, and it is deliberately at the CLI rather than in `ops`: what
    matters is what a person reads. Both routes are exercised in one work order, so the
    test would fail just as loudly if the OS's verdict were rendered like the user's."""
    from jarvis import cli

    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",
                                  "reused the production api key"))
    ask(started, store)
    drain(started)
    # ...and the user rules on the one the OS would not touch.
    ops.review_work_order(wo["id"], accept=True, feedback="fine, I minted a new one")

    capsys.readouterr()
    cli.main(["wo", "show", wo["id"]])
    out = capsys.readouterr().out

    assert "accepted by the OS (neo" in out
    assert "accepted by you" in out
    # The strong form: the machine's line must not be sayable as the user's.
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
    """No settling, no landing, and NO INBOX ROW: nothing changed for the user, and a
    second announcement of an unchanged fact is the attention cost this feature exists to
    reduce."""
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
    # However Neo phrased it, the question is the user's now — which is what
    # `jarvis neo list` and `invariants.check_neo_escalations_are_live` both read.
    neo_store = NeoStore()
    try:
        assert neo_store.get(q["id"])["status"] == "escalated"
    finally:
        neo_store.close()


def test_the_user_is_told_once_when_the_last_assumption_leaves_their_list(started):
    """One row, per work order, at the moment their review list changes — and it names
    the correction path, which is the other half of making this reversible."""
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
    """They got there first through `jarvis wo review`. Their decision stands, and the
    machine's is dropped rather than written over it."""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    ops.review_work_order(wo["id"], accept=False, feedback="no, use the long name")

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "rejected" and row["decided_by"] == "user"
    assert events(store, wo["id"], "autoreview_accepted") == []


# -- the settle site: everything that can change while Neo is thinking -----------------
#
# The ask FREEZES NOTHING. A model call is seconds to minutes wide, and `decide` runs
# before it — so every condition it checked is a fact that can be false by the time the
# ruling comes back. The settle is the step that cannot be taken back: it clears the
# assumption and `ops.land_when_cleared` lands the order behind it. So the table is run
# again against state read at that moment, and these are the rows that reach it.


def test_a_panel_that_gives_up_while_neo_is_thinking_stops_the_settle(started):
    """THE ONE THAT MATTERS MOST, and it is reachable rather than theoretical: arming on
    a `pending` round is explicitly allowed (`test_every_other_round_outcome_leaves_the
    _assumption_reviewable`), so pending -> escalated is the ordinary way a panel gives
    up on an order the OS has already asked about.

    Without the re-check the OS clears the assumptions and lands — with auto-merge on,
    merges — a work order the panel deliberately put in front of the user, silently and
    with nothing on the record saying so (kn-fcbfd42b).
    """
    store, wo = park(started, auto_review=True, outcome="pending",
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    (q,) = questions()
    # The panel reaches its verdict while the question is in flight, and gives up.
    store.close_validation_round(
        store.latest_validation_round(wo_id=wo["id"])["id"], "escalated",
        "the seats could not agree")

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_accepted") == []
    # The order did NOT land behind it — the thing the panel's give-up was protecting.
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    # ...and the OS said why, in both places: the work order's record and Neo's own list.
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_PANEL_GAVE_UP
    # WHICH round and WHAT IT SAID, not merely that one gave up: this round closed with a
    # real reason, and the hold is where the user meets it beside the assumption.
    assert held["round"] == 1
    assert "gave up on round 1" in held["reason"]
    assert "the seats could not agree" in held["reason"]
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_PANEL_GAVE_UP
    assert questions()[0]["status"] == "escalated"
    assert q["id"] == questions()[0]["id"]
    # The one line the user reads on `jarvis wo show` says it is theirs again.
    line = ops.autoreview_state(store, store.get_work_order(wo["id"]))["line"]
    assert "left with you" in line
    # Still pending: no resolution clause (the spec's §Tests 3).
    assert "since " not in line


# -- WHO decided, on the row the report groups by --------------------------------------
#
# docs/specs/2026-10-01-neo-observability.md §1 and Neo's ruling on question 1170. These
# re-marks happen AFTER Neo answered, so without a cause every one of them renders "not
# recorded" for ever — on `kind='assumption'`, the population the user complained about.


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
    """THE HONEST NULL. This escalation is caused by the condition table re-running, not
    by anything in Neo's reply — the acceptance itself was routine. A label from the five
    would say the OS overrode a stakes word or Neo denied it, and neither happened."""
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


# -- the banner against the assumption's CURRENT row -----------------------------------
#
# docs/superpowers/specs/2026-09-25-a-decided-assumption-is-not-left-with-you.md.


def _escalated(daemon):
    """A work order parked on one assumption the OS left with the user."""
    store, wo = park(daemon, auto_review=True)
    ask(daemon, store)
    drain(daemon)
    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending"
    return store, wo, row["id"]


def _line(store, wo_id: str) -> str:
    return ops.autoreview_state(store, store.get_work_order(wo_id))["line"]


def test_an_accepted_assumption_is_no_longer_left_with_you(started):
    """The escalation stays on the record — it happened — and the banner stops claiming
    the user still owes the decision they already took."""
    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "accepted", reason="fine")

    line = _line(store, wo["id"])
    assert "left with you" in line
    assert "since accepted by you" in line


def test_a_rejected_assumption_says_rejected_and_not_accepted(started):
    """The status column is interpolated, not hard-coded."""
    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "rejected", reason="no")

    line = _line(store, wo["id"])
    assert "since rejected by you" in line
    assert "accepted" not in line


def test_a_still_pending_escalation_reads_exactly_as_before(started):
    """The common case: nothing read, nothing appended."""
    store, wo, _aid = _escalated(started)

    assert "since " not in _line(store, wo["id"])


def test_a_decision_the_os_took_is_not_credited_to_the_user(started):
    """One attribution renderer, asserted as one: `ops.assumption_decider`."""
    from jarvis.project_store import ASSUMPTION_DECIDER_OS

    store, wo, aid = _escalated(started)

    store.review_assumption(aid, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="no new surface", model="claude-opus-5")

    row = store.get_assumption(aid)
    assert _line(store, wo["id"]).endswith(
        f"; since accepted by {ops.assumption_decider(row)}")
    assert ops.assumption_decider(row) == "the OS (neo, claude-opus-5)"


def test_an_event_written_before_the_payload_carried_the_id_is_unchanged(started):
    """Unresolvable, and a banner is not the place to guess."""
    wo = ops.create_work_order("proj_a", "older event")
    store = ProjectStore(started.catalog.project("proj_a").path)
    store.add_event(wo["id"], "autoreview_escalated",
                    {"n": 1, "reason": "this is a surface others call"})

    assert _line(store, wo["id"]) == (
        "assumption #1 left with you — this is a surface others call")


def test_a_work_order_cancelled_while_neo_is_thinking_is_not_settled_and_re_landed(
        started):
    """The same gap by another door. `decide` condition 2 is checked against the work
    order's status at ASK time; the order can be cancelled before the ruling lands, and
    settling it would clear the assumptions of an order nobody is waiting on and push it
    back through `ops.land_when_cleared`."""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)
    ops.cancel(wo["id"])

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "pending" and not row["decided_by"]
    assert store.get_work_order(wo["id"])["status"] == "cancelled"
    assert events(store, wo["id"], "autoreview_accepted") == []
    # `_note_autoreview_held` drops `status` at the ASK site, where it is unreachable.
    # Here it is the whole reason, and suppressing it would leave the OS's decision not
    # to act as the one thing it never wrote down.
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_STATUS and "cancelled" in held["reason"]


def test_permission_revoked_while_neo_is_thinking_stops_the_settle(started):
    """The switch is read at the settle as well as at the ask. A user who turns
    `validation.auto_review` off has withdrawn the authority the ruling was spending, and
    a ruling already paid for is not a reason to spend it anyway."""
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
    """The control. A guard that held everything would pass all four tests above and
    break the feature, so the arming path is asserted from the same fixture — one
    `park`, one `ask`, one `drain`, nothing touched in between."""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)

    drain(started)

    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted" and row["decided_by"] == "neo"
    assert events(store, wo["id"], "autoreview_held") == []


def test_the_question_being_delivered_does_not_block_its_own_settle():
    """`asked_question_id` at the unit level, and it is the reason the second `decide`
    call says anything at all. Condition 6 exists to stop a SECOND question being filed;
    at the settle site the assumption is linked to the very question being delivered, so
    that link must not read as "already with Neo" — while a link to a DIFFERENT question
    still holds, because two rulings on one assumption is a state nobody designed."""
    linked = assumption(neo_question_id=41)

    assert decide(assumption=linked).code == autoreview.HELD_ASKED
    assert decide(assumption=linked, asked_question_id=41).armed
    assert decide(assumption=linked, asked_question_id=42).code == autoreview.HELD_ASKED


# -- the refusals that were already there, left honest ---------------------------------


def test_ack_and_done_refuse_while_an_assumption_is_pending_and_stop_once_it_is_not(
        started):
    """CHECKED RATHER THAN ASSUMED, as the work order asked. Both refuse on PENDING
    assumptions, so an auto-accepted one simply stops being pending and neither path
    needs a special case — this is the test that says so, from both sides."""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))

    with pytest.raises(ops.OpsError, match="assumption"):
        ops.ack_attention(wo["id"])
    with pytest.raises(ops.OpsError, match="assumption"):
        ops.mark_done(wo["id"])

    ask(started, store)
    drain(started)

    # Neither refusal was widened, narrowed or given a branch for this: the fact they
    # guard is gone, so they let it through.
    assert ops.mark_done(wo["id"])["status"] == "completed"


# -- the two mechanisms together -------------------------------------------------------


def merge_gate(store, wo_id: str) -> None:
    """Approve the `auto_merge` gate the poll files, the way Neo's drain would.

    Done directly rather than through the fake's reviewer so that this test is about the
    HANDOFF between the two mechanisms and not about the merge gate, which
    `tests/test_automerge.py` covers in full.
    """
    from jarvis import gates

    (approval,) = [a for a in store.list_approvals(wo_id)
                   if a["kind"] == "auto_merge"]
    gates.apply_decision(store, approval["id"], "approved", "the panel read it", "neo")


def test_an_order_with_an_assumption_reaches_merged_with_nobody_typing_anything(
        started, fake_gh, local_base):
    """THE WHOLE POINT, end to end: assumption -> Neo -> settled -> parked -> gate ->
    merged, and no `automerge` condition was weakened to get there. Condition 3 passes
    because the assumption is genuinely no longer pending."""
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
    # The assumption did not go quiet — it was decided, by the OS, on the record.
    (row,) = store.all_assumptions(wo["id"])
    assert row["status"] == "accepted" and row["decided_by"] == autoreview.DECIDER
    # And the merge decision still HOLDS on a pending assumption: nothing was
    # special-cased away. Re-asserted here rather than only in the sibling file, because
    # this is the test that would tempt somebody to weaken it.
    held = automerge.decide({"id": 1, "round": 1, "outcome": "passed",
                             "head_sha": JUDGED},
                            {**store.get_work_order(wo["id"]),
                             "status": "waiting_pr_merge"},
                            type("P", (), {"head_oid": JUDGED})(), spec.validation,
                            validated_head=JUDGED, pending_assumptions=True)
    assert held.code == automerge.HELD_ASSUMPTIONS


def test_with_auto_review_off_the_same_order_still_waits_for_the_user(started, fake_gh):
    """THE NEGATIVE HALF, identical in every other respect. Without this the test above
    is green on a work order that never had a pending assumption at all."""
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


# -- the switch ------------------------------------------------------------------------


def test_the_fleet_default_ships_off_and_a_project_inherits_it(catalog_file):
    catalog = load_catalog(catalog_file)
    assert catalog.os.validation.auto_review is False
    assert catalog.project("proj_a").validation.auto_review is False


def test_a_project_keeps_its_own_answer_over_the_fleets(tmp_path):
    """Authority is granted one project at a time: a project that never opted in is
    never decided for, however the fleet is configured."""
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
    """Not a constant and not an environment variable: the ordinary config console, and
    — because `*.validation.*` already covers it — with the mandatory `--reason` and a
    recorded version that every safety key carries."""
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
    """THE CORRECTION PATH, which is what makes a machine decision reversible — and it is
    the command the OS's own inbox row tells the user to run, so its failure mode is not
    a corner. `neo_review` forwards a correction to the worker whenever the work order is
    not terminal, and an auto-reviewed one is typically `waiting_pr_merge`: without the
    guard, teaching Neo would start a turn on an order nobody asked to reopen."""
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
    """The mirror of the alarm refusal. Nobody asked this question and the worker
    finished long before it was filed, so `jarvis neo answer` would start a turn on a
    work order nobody asked to reopen — and the verdict the user means has its own
    command. The pair is the empty message queue: an error message alone is green on a
    path that never delivered anything anyway."""
    store, wo = park(started, auto_review=True)
    ask(started, store)
    drain(started)                       # the fake escalates by default
    (q,) = questions()

    with pytest.raises(ops.OpsError, match="jarvis wo review"):
        ops.neo_answer_escalated(q["id"], "yes, that is fine")

    assert store.list_messages(wo["id"]) == []
    assert store.pending_assumptions(wo["id"])


# -- the stale-escalation sweep --------------------------------------------------------


def test_a_question_the_user_answered_another_way_stops_asking_for_a_ruling(started):
    """Adding a Neo question kind is seven edits (kn-4edb0eb7), and this is the fourth
    (`invariants.check_neo_escalations_are_live`). The user answers an escalated
    assumption question with `jarvis wo review`, which never touches the question — so
    without this it goes on asking for a ruling that has already been given."""
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
    """THE HALF THAT MATTERS MORE, and the one a sweep gets wrong in the dangerous
    direction. `_stale_assumption_question` closes a question whose assumption has been
    settled; a version that closed one whose assumption is STILL PENDING would silently
    retire a live escalation — the user would never see the decision they are owed, and
    nothing would say it had gone."""
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
    """The rule all four siblings share, stated in `_stale_assumption_question`: the
    checks run per project against an OS-WIDE `neo.db`, so "no such assumption here" is
    how another project's rows are skipped — and it cannot be told apart from a subject
    that has gone. A missing row must therefore be left alone, not swept."""
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


# -- what the OS did with EACH assumption, on both surfaces (GitHub issue #712) --------


def rulings(store, wo_id: str) -> list[str]:
    """Every assumption's ruling line, in order, as a person reads it."""
    return [ops.assumption_ruling_line(a)
            for a in ops.assumptions_with_rulings(store, wo_id)]


def test_a_held_assumption_says_what_held_it_and_an_asked_one_says_it_is_out(started):
    """THE DEFECT: a pending assumption the OS had already ruled on read exactly like one
    nobody had looked at. The hold reason is a timeline event and not a column, so this
    is also the assertion that the derivation reaches the row."""
    store, wo = park(started, auto_review=True,
                     assumptions=("reused the production api key", ROUTINE))

    ask(started, store)

    held, asked = rulings(store, wo["id"])
    assert held.startswith("Held by the OS — mentions 'production'")
    # The number is on the row already; repeating it inside the reason is not a ruling.
    assert "assumption #1" not in held
    (q,) = questions()
    assert asked == f"Asked Neo (question {q['id']}), awaiting ruling"


def test_an_escalation_carries_neos_reason_and_its_question(started):
    """What the user has to decide, beside the assumption they have to decide it on —
    instead of buried in the timeline."""
    store, wo = park(started, auto_review=True)
    ask(started, store)
    (q,) = questions()

    drain(started)

    (line,) = rulings(store, wo["id"])
    assert line.startswith(f"Neo escalated (question {q['id']}): ")
    assert "assumption reviews escalate unless forced" in line


def test_an_assumption_nobody_has_looked_at_says_nothing_at_all(started):
    """`''` IS A FACT, and a different one from a hold: an opted-out project's
    assumptions must not grow a line about a mechanism it does not have."""
    store, wo = park(started, auto_review=False)

    ask(started, store)

    assert rulings(store, wo["id"]) == [""]


def test_a_settled_assumption_carries_the_reason_it_was_settled(started):
    """The acceptance reason is part of the ruling too — `jarvis wo show` had it and the
    page hid it in a `title=`."""
    store, wo = park(started, auto_review=True,
                     assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    ask(started, store)

    drain(started)

    assert rulings(store, wo["id"]) == ["test-forced acceptance: a naming convention"]


def test_wo_show_carries_every_ruling_and_not_just_the_last_one(started, capsys):
    """THE SURFACE, and the half the summary line cannot do: `auto_review:` says what the
    mechanism did LAST, so on a three-assumption order two rulings were invisible."""
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
    # `--json` keeps the payload, not the prose: whoever reads it reads the event.
    assert rows[0]["os_ruling"]["code"] == autoreview.HELD_HIGH_STAKES


def test_the_cli_points_at_the_round_and_only_for_a_panel_hold(started, capsys):
    """WHERE TO READ THE FULL PANEL REASON, on the surface that cannot carry a URL. The
    pointer is per-surface by construction: `ops.assumption_ruling_line` is pure and
    shared, so a `jarvis …` command in the reason would be printed inside an HTML page.

    TWO ORDERS, because one cannot render both holds as its newest: `decide` checks the
    panel before the stakes net, so under an escalated round every row holds on the panel.
    """
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

    # ...and NOT on a hold whose subject is not a round.
    capsys.readouterr()
    cli.main(["wo", "show", stakes_hold["id"]])
    other = capsys.readouterr().out
    assert "mentions 'production'" in other and "jarvis validation show" not in other

    # `--json` keeps the rows untouched, which is what that function promises.
    capsys.readouterr()
    cli.main(["wo", "show", gave_up["id"], "--json"])
    assert "jarvis validation show" not in capsys.readouterr().out


# -- a question Neo never answered is not a question in flight -------------------------
#
# docs/superpowers/specs/2026-09-26-an-unreachable-neo-question-is-not-a-question-in-
# flight.md. GitHub issue #788.


LAST_ERROR = "connection reset by peer"


def _kill(question_id: int) -> None:
    """Spend the ladder on one question: `failed`, stamped `UNREACHABLE_PREFIX`."""
    neo_store = NeoStore()
    try:
        assert neo_store.release_claim(question_id, LAST_ERROR,
                                       max_attempts=0) == "unreachable"
    finally:
        neo_store.close()


def _unreachable_ask(daemon):
    """A parked order whose assumption is linked to a question nobody will ever answer."""
    store, wo = park(daemon, auto_review=True)
    ask(daemon, store)
    (q,) = questions()
    _kill(q["id"])
    return store, wo, q


def _unreachable_confirmation(daemon):
    """The same on the `confirm: True` link, written by its real writer."""
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
    """THE SYMPTOM: `autoreview_asked` is append-only, so the row claimed a ruling was on
    its way for the life of the work order. The question is dead and the line says so."""
    store, wo, q = _unreachable_ask(started)

    (line,) = rulings(store, wo["id"])

    assert "awaiting ruling" not in line
    assert line.startswith(f"Neo could not be reached (question {q['id']}) — nobody "
                           f"judged this; you decide it: ")
    assert f"jarvis neo answer {q['id']}" in line
    assert f"jarvis wo review {wo['id']}" in line
    assert line.endswith(f"— last error: {LAST_ERROR} (after 0 retries — nobody has "
                         f"judged this)")
    # A crash is not a decision: neither word may appear.
    assert "escalated" not in line and "Neo decided" not in line


def test_an_unreachable_confirmation_says_what_was_never_confirmed(started):
    """Both links, one branch — `propose_confirmation` writes `autoreview_asked` too."""
    store, wo, q = _unreachable_confirmation(started)

    (line,) = rulings(store, wo["id"])

    assert "awaiting ruling" not in line
    assert (f"Neo could not be reached (question {q['id']}) — nobody judged this to "
            f"confirm its early reading; you decide it: ") in line


def test_the_summary_line_puts_an_unreachable_ask_back_with_the_user(started):
    """The same derivation on `jarvis wo show`'s one line, in `left with you`'s words so
    it sorts with the other two claims of that shape."""
    store, wo, q = _unreachable_ask(started)

    line = _line(store, wo["id"])

    assert line == (f"assumption #1 left with you — Neo could not be reached "
                    f"(question {q['id']}); nobody judged it")
    assert "is with Neo" not in line
    # The row is still pending, so there is nothing to resolve it against.
    assert "since " not in line


def test_a_dead_question_re_arms_the_assumption_it_was_asked_on():
    """Condition 6's mandate is unchanged — one question per assumption — because a
    `failed` question is not a question any more."""
    a = assumption(neo_question_id=41)

    assert decide(assumption=a, unreachable_question_ids=(41,)).armed
    assert decide(assumption=a).code == autoreview.HELD_ASKED
    assert decide(assumption=a,
                  unreachable_question_ids=(9,)).code == autoreview.HELD_ASKED


def test_the_early_pass_re_arms_on_a_dead_link_too():
    """`decide_early` reaches one on an order that is still `running`."""
    a = assumption(neo_question_id=41)
    wo = {**WO, "status": "running"}

    assert autoreview.decide_early(a, wo, cfg(),
                                   unreachable_question_ids=(41,)).armed
    assert autoreview.decide_early(a, wo, cfg()).code == autoreview.HELD_ASKED


@pytest.mark.parametrize("status", ["queued", "answering", "answered", "escalated"])
def test_a_question_that_is_genuinely_in_flight_is_never_in_the_set(started, status):
    """Only `failed` means nobody judged it: `escalated` is Neo handing the question back
    WITH a decision, and the other three are out or already delivered."""
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
    """What the daemon supplies, read off both columns."""
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
    """The whole point: the daemon asks again once the question is dead, on the pass that
    would otherwise hold it for the life of the order."""
    store, wo, dead = _unreachable_ask(started)

    ask(started, store)

    asks = [q["id"] for q in questions()]
    assert len(asks) == 2, "the outage held the assumption for the life of the order"
    (row,) = store.all_assumptions(wo["id"])
    assert row["neo_question_id"] == max(asks) != dead["id"]


def test_a_second_outage_on_the_re_asked_question_re_arms_it_again(started):
    """THE LOOP #788 asked to be made safe: re-arming is not a one-shot. A dead question
    is replaced by one that ALSO dies, and the assumption is asked about a third time
    rather than being stuck behind the replacement for the life of the order — while the
    ask still costs at most one question per tick, so an outage cannot become spam.

    docs/superpowers/specs/2026-09-26-an-unreachable-neo-question-is-not-a-question-in-
    flight.md §4.
    """
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

    # THE SAFETY: the live question holds the next pass, so the ask does not multiply.
    ask(started, store)
    assert sorted(q["id"] for q in questions()) == ids
    assert len(events(store, wo["id"], "autoreview_asked")) == 3
    # `HELD_ASKED` is suppressed (`_holds_not_recorded`): the question IS filed.
    assert events(store, wo["id"], "autoreview_held") == []


# -- the decision record: the order's own rulings reach the packet ----------------------
#
# docs/superpowers/specs/2026-09-28-an-assumption-review-reads-the-orders-own-rulings.md.
#
# THE DEFECT: the reviewer escalated asking the user to re-decide something already
# decided on the same work order, because no answered question, no user message and no
# user ruling on a sibling was ever in the packet.


def qrow(**over) -> dict:
    return {"id": 887, "wo_id": "wo-1", "kind": "question", "status": "answered",
            "question": "should the kill remedy exist?", "answer": "neither A nor B",
            "answered_by": "neo", "ts": 100.0, "review_status": "unreviewed",
            "review_feedback": None, **over}


class FakeNeo:
    """`answered_questions` and `get`, the two reads the record makes. Rows are given
    newest-first, as the query returns them; `others` exist but are not answered here.

    `window` is a HARD bound on the query independent of the `limit` the caller asks for:
    whatever the caller's row bound, a cited ruling can fall outside the rows it got back,
    and that case is the one that used to read as `not answered`."""

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
    """Test 1. Q900 escalated citing "a user correction I can't see" — the user's own
    answer to a question on the SAME work order. Both facts have to be there: the id, so
    the reviewer can cite it, and WHO answered, because the user and Neo are different
    authority and Q879 turned on not knowing which."""
    out = record(qrow(answered_by="user", answer="build NO kill remedy"))

    assert "Q887" in out
    assert "answered by user" in out
    assert "build NO kill remedy" in out


def test_a_corrected_neo_answer_carries_the_correction_that_overrode_it():
    """A corrected Neo answer is not authority on its own: `neo_store.review` leaves the
    user's feedback as the only ruling that survived."""
    out = record(qrow(review_status="corrected",
                      review_feedback="no — always ask first"))

    assert "the user corrected this: no — always ask first" in out


def test_an_empty_record_says_so_rather_than_going_silent():
    """"No prior decisions" and "prior decisions I was not shown" must not read alike: a
    reviewer that cannot tell them apart is right to escalate."""
    assert autoreview.decision_record(FakeStore(), FakeNeo(), "wo-1", []) == ""
    assert "(no prior decisions recorded)" in autoreview._ruling_question(   # noqa: SLF001
        "p", WO, assumption(), [])


def test_the_record_is_capped_newest_first_and_says_what_it_dropped():
    """Test 2. The omission is stated, and LAST (kn-1485b845). Going silent is the
    failure this whole spec is about."""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i} " + "x" * 800)
            for i in range(12)]
    out = record(*rows)

    assert "ruling 0" in out, "the newest ruling was evicted"
    assert "ruling 11" not in out
    last = out.strip().splitlines()[-1]
    assert "older items omitted" in last and "6000 characters" in last


def test_a_smaller_per_project_cap_evicts_more():
    """The cap is a per-project catalog key (`validation.decision_record_chars`), so the
    bound one project's reviewer reads is the bound that project set."""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i} " + "x" * 800)
            for i in range(12)]
    tight = autoreview.decision_record(FakeStore(), FakeNeo(*rows), "wo-1", [],
                                       chars=2000)

    assert "ruling 0" in tight
    assert "ruling 2" not in tight
    assert "capped at 2000 characters" in tight.strip().splitlines()[-1]


def test_a_cited_question_is_placed_first_in_full_and_never_evicted():
    """Test 3. Q900 escalated naming a question id it could not see. That row is the one
    item most likely to be why the packet exists, so the cap does not reach it."""
    long_answer = "the user said: " + "y" * 3000
    cited = qrow(id=887, ts=1.0, answer=long_answer)
    fillers = [qrow(id=900 - i, ts=500.0 - i, answer="z" * 900) for i in range(10)]
    cites = assumption(content="per Neo question 887, kept the shim")
    out = record(*fillers, cited, siblings=[cites], ruling_on=cites)

    assert long_answer in out, "the cited answer was truncated or evicted"
    assert out.index("Q887") < out.index("Q900")


def test_a_cited_question_answered_outside_the_query_window_is_shown_in_full():
    """§4. An ANSWERED ruling the row bound cut out of `answered_questions` used to be
    rendered `not answered`, and the persona escalates on that line — #832 reproduced on
    the exact path this feature exists for. The row's own `status` decides."""
    cited = qrow(id=887, ts=1.0, answer="the user said: keep the shim")
    newer = [qrow(id=900 - i, ts=500.0 - i, answer=f"ruling {i}") for i in range(3)]
    cites = assumption(content="per Neo question 887, kept the shim")
    out = record(*newer, cited, siblings=[cites], ruling_on=cites, window=3)

    assert "not answered" not in out
    assert "  Q887 (cited by the assumption) [answered by neo]" in out
    assert "the user said: keep the shim" in out


def test_short_items_the_cap_evicts_are_counted_exactly():
    """The omission line names the count the CAP evicted, with no hedge: the cap is a
    number this code knows."""
    rows = [qrow(id=900 - i, ts=100.0 - i, answer=f"ruling {i}") for i in range(5)]
    out = record(*rows, siblings=[], chars=200)

    last = out.strip().splitlines()[-1]
    assert "at least" not in last
    assert "older items omitted" in last and "capped at 200 characters" in last
    kept = [ln for ln in out.splitlines() if "answered by" in ln]
    assert f"{5 - len(kept)} older items omitted" in last


def test_rows_the_row_bound_cut_are_counted_in_the_same_omission_line(monkeypatch):
    """§3. Rows the query's LIMIT cut never enter `items`, so a record that FITS under the
    character cap used to print no omission line at all — the "no prior decisions" versus
    "decisions I was not shown" silence kn-1485b845 forbids. One line, every cause."""
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
    """The user's ruling on the recorded assumption: each order cites its OWN questions,
    not its siblings'. A sibling naming an id is that sibling's business."""
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


def test_a_cited_question_on_another_work_order_is_named_and_not_quoted():
    """Content from a different order has not been through this order's evidence gates,
    and a worker citing it does not make it this order's record."""
    cites = assumption(content="as Q887 decided")
    out = record(siblings=[cites], ruling_on=cites,
                 others=(qrow(id=887, wo_id="wo-9", answer="quoted elsewhere"),))

    assert ("Q887 (cited by the assumption; belongs to another work order — not shown)"
            in out)
    assert "quoted elsewhere" not in out


def test_a_cited_question_that_never_existed_is_stated_not_swallowed():
    """A worker citing a question id that was never asked is a fact about the assumption
    the reviewer is ruling on."""
    cites = assumption(content="as Q887 decided")
    out = record(siblings=[cites], ruling_on=cites)

    assert "Q887 (cited by the assumption; no such question)" in out


def test_an_assumption_kind_row_is_a_one_liner_and_its_packet_is_never_requoted():
    """Test 4. An `assumption` question row IS a review packet — description, diff and
    sibling list. Quoting it is recursive and would spend the whole cap on one item. The
    RULING is the payload (Q879's missing fact), so it is rendered, naming the sibling."""
    packet = "ASSUMPTION REVIEW — rule on assumption #4 " + "p" * 5000
    out = record(qrow(id=879, kind=autoreview.QUESTION_KIND, question=packet,
                      answer="approve — routine naming"),
                 siblings=[assumption(), assumption(id=8, n=4, neo_question_id=879)])

    assert "p" * 200 not in out and packet not in out
    assert "Q879" in out and "assumption #4" in out
    assert "approve — routine naming" in out


def test_an_assumption_kind_row_whose_sibling_is_gone_still_reads_as_a_ruling():
    """A ruling whose subject cannot be located is still a ruling."""
    out = record(qrow(id=879, kind=autoreview.QUESTION_KIND, question="x" * 4000,
                      answer="approve"))

    assert "Q879" in out and "assumption (row not found)" in out


def test_a_record_item_carrying_a_credential_is_withheld_not_dropped():
    """Test 5. The secret net is the ONE net that runs here (§5). It names the SHAPE and
    never the value, and withholds rather than dropping: silence is the bug."""
    out = record(qrow(answer='API_KEY = "sk-live-4a9f8c2b7d1e"'))

    assert "sk-live-4a9f8c2b7d1e" not in out
    assert "Q887 (withheld — carries a line assigning API_KEY)" in out


def test_an_answer_whose_credential_is_on_a_later_line_is_still_withheld():
    """The net sees the field as the user typed it. `_ASSIGNMENT_RE` anchors at the start
    of a LINE, so normalising the whitespace before the net runs makes an assignment on
    every line but the first invisible — the payload-vs-rendered-line defect one step
    earlier."""
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
    """§5, on the user's own ruling on Neo question 943: the high-stakes net is NOT
    applied here. Every item is settled by construction, so there is no decision left to
    withhold — and hiding a ruling the user already gave is exactly the #832 bug."""
    out = record(qrow(answered_by="user", answer="yes, delete the production rows"))

    assert "delete the production rows" in out
    assert "withheld" not in out


def test_a_user_message_on_the_order_reaches_the_record():
    out = record(messages=({"ts": 1727500000.0, "body": "no kill remedy, ever"},))

    assert "user message" in out and "no kill remedy, ever" in out


def test_a_sibling_the_user_settled_carries_their_reason():
    """Test 6. `jarvis wo review --feedback` writes the user's reasoning to
    `decided_reason` — the `assumptions` table has no `review_feedback` column — with
    `decided_by` naming who decided."""
    out = record(siblings=[
        assumption(),
        assumption(id=8, n=4, status="rejected", decided_by="",
                   decided_reason="I want the flag spelled out"),
    ])

    assert "#4 rejected by the user — I want the flag spelled out" in out


def test_a_sibling_neo_settled_is_rendered_as_neos_not_the_users():
    """Test 6's other half, and Q879's own case: Neo's verdict on a sibling reaches the
    packet through its ANSWERED QUESTION row, never as a ruling of the user's."""
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
    """Test 7. The record is built in `autoreview`, so the daemon changes not at all."""
    neo_ = FakeNeo(qrow(answered_by="user"))

    autoreview.propose(FakeStore(), neo_, "p", WO, assumption(), [assumption()])

    (packet,) = neo_.asked
    assert RECORD_HEADER in packet and "Q887" in packet
    # After the sibling list, before the closing answer instructions.
    assert packet.index("do not rule on these") < packet.index(RECORD_HEADER)
    assert packet.index(RECORD_HEADER) < packet.index("Answer with `escalate`")


def test_the_projects_own_cap_reaches_the_packet_both_passes_build():
    """§3/§7. `cfg=cfg` is threaded from the daemon so the bound the reviewer reads is the
    bound the PROJECT set; with only the `cfg=None` path covered, a per-project value could
    stop arriving and every test still pass."""
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


def test_answered_questions_is_newest_first_and_keeps_every_kind(started):
    """The query, §1. Newest first — unlike `open_questions` — because the caller caps and
    the newest ruling is the one that supersedes. NO `kind` filter: an `approval` the user
    answered is exactly the kind of ruling #832 is about."""
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
