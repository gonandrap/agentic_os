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

from jarvis import autoreview, neo, ops
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


def test_a_refusal_the_worker_has_not_answered_holds_everything_behind_it():
    """A refused assumption is guidance the worker has not answered, and settling its
    siblings would land the very decision the user turned down."""
    assert decide(refusal_answered=False).code == autoreview.HELD_REFUSAL_UNANSWERED


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
    assert "gave up" in held["reason"]
    (escalated,) = events(store, wo["id"], "autoreview_escalated")
    assert escalated["dropped"] == autoreview.HELD_PANEL_GAVE_UP
    assert questions()[0]["status"] == "escalated"
    assert q["id"] == questions()[0]["id"]
    # The one line the user reads on `jarvis wo show` says it is theirs again.
    assert "left with you" in ops.autoreview_state(
        store, store.get_work_order(wo["id"]))["line"]


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
        started, fake_gh):
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
