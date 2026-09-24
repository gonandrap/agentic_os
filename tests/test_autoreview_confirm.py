"""The confirmation pass: a provisional verdict only settles once a diff exists.

docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §7.

Three properties carry the section, and each gets its own block:

* **`decide` RUNS UNCHANGED UNDERNEATH.** A provisional verdict is not a ticket past any
  of its seven conditions, so every one of them is exercised again on a row that carries
  one. A confirmation pass that skipped the table would settle assumptions the ask pass
  itself would refuse to put to Neo.
* **CONFIRMING IS THE NARROW PATH**, `test_autoreview.py`'s rule one section along:
  anything that is not a routine-stakes approval of the CONFIRMATION question leaves the
  assumption pending, with both readings on the record.
* **THE OBJECTION GATE IS A RETRY, NOT A REFUSAL.** §6.6 withdraws on the transition into
  `needs_review`; an outstanding objection means "not yet" and costs the assumption
  nothing.
"""

from __future__ import annotations

import os
import subprocess

import pytest

from jarvis import autoreview, ops
from jarvis.project_store import ProjectStore

# The fixtures are `test_autoreview.py`'s, imported rather than re-written: the two
# passes must be driven through the same parked work order or they are not comparable.
from tests.test_autoreview import (  # noqa: F401 — pytest fixtures
    PR,
    ROUTINE,
    SECRET,
    ask,
    assumption,
    cfg,
    drain,
    events,
    park,
    questions,
    started,
)

WO = {"id": "wo-1", "status": "needs_review", "title": "t", "description": "d"}

PROVISIONAL_REASON = "a naming convention, judged while the worker typed"


def judged(**over) -> dict:
    """An assumption carrying an early ACCEPT — the only row §7 has anything to do."""
    return assumption(**{"provisional_verdict": "accept",
                         "provisional_reason": PROVISIONAL_REASON,
                         "provisional_model": "sonnet",
                         "provisional_stakes": "routine",
                         "confirm_question_id": None, **over})


def confirm(**kw):
    return autoreview.decide_confirm(kw.pop("assumption", None) or judged(),
                                     kw.pop("wo", None) or WO,
                                     kw.pop("config", None) or cfg(), **kw)


# -- `decide_confirm`: the four gates of its own ---------------------------------------


def test_an_early_accept_on_a_parked_order_is_put_back_to_neo():
    d = confirm()
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_an_assumption_no_early_pass_judged_has_nothing_to_confirm():
    """The historical row, and the ordinary one on a fleet that never enabled §5."""
    assert confirm(assumption=assumption()).code == autoreview.HELD_UNJUDGED


def test_an_early_objection_is_never_confirmed_because_nothing_was_approved():
    """§7's last paragraph: an `object` approved nothing, so there is nothing to confirm
    and no second call to spend. The user decides it, three ways."""
    d = confirm(assumption=judged(provisional_verdict="object"))
    assert d.code == autoreview.HELD_OBJECTED


def test_a_confirmation_already_filed_is_not_filed_twice():
    """One question per assumption per pass — the invariant `confirm_question_id` exists
    to keep, given `neo_question_id` is deliberately excluded from condition 6 here."""
    d = confirm(assumption=judged(confirm_question_id=77))
    assert d.code == autoreview.HELD_CONFIRMING and "77" in d.reason


def test_an_objection_still_in_flight_holds_the_whole_pass():
    """§7 runs after §6.6, never beside it: settling an assumption while a message about
    it is still on a wire to the worker is the contradiction the ordering removes."""
    d = confirm(objections_outstanding=True)
    assert d.code == autoreview.HELD_OBJECTION_IN_FLIGHT


# -- `decide`'s seven conditions, on a row that carries a provisional verdict -----------


@pytest.mark.parametrize("kw, code", [
    ({"config": cfg(auto_review=False)}, autoreview.HELD_DISABLED),
    ({"config": cfg(enabled=False)}, autoreview.HELD_DISABLED),
    ({"wo": {**WO, "status": "running"}}, autoreview.HELD_STATUS),
    ({"assumption": judged(status="accepted")}, autoreview.HELD_SETTLED),
    ({"round_outcome": "escalated"}, autoreview.HELD_PANEL_GAVE_UP),
    ({"refusal_answered": False}, autoreview.HELD_REFUSAL_UNANSWERED),
    ({"assumption": judged(content=SECRET)}, autoreview.HELD_HIGH_STAKES),
])
def test_a_provisional_verdict_is_not_a_ticket_past_any_condition(kw, code):
    """§7.1: `autoreview.decide` runs first, unchanged, and every one of its seven
    conditions must hold. An early reading is an opinion about an intention — it cannot
    buy the order past a panel that gave up or a permission nobody granted."""
    assert confirm(**kw).code == code


def test_the_early_question_does_not_hold_its_own_confirmation():
    """Condition 6 through the documented escape hatch. `neo_question_id` points at the
    EARLY question and always will, so without `asked_question_id` every confirmation
    would hold as `asked` and the pass would never run once."""
    d = confirm(assumption=judged(neo_question_id=41))
    assert d.armed


def test_a_different_question_still_holds_it():
    """The pair: the hatch excludes the assumption's OWN early question and nothing
    else."""
    d = autoreview.decide(judged(neo_question_id=41), WO, cfg(), asked_question_id=9)
    assert d.code == autoreview.HELD_ASKED


# -- the daemon: asking the second question --------------------------------------------


EARLY_QUESTION = 99


def provisional(store, wo, *, verdict: str = "accept",
                reason: str = PROVISIONAL_REASON) -> dict:
    """Stamp §5's verdict by hand. Section 5 has not landed; its COLUMNS have (§4).

    The early QUESTION is linked too, at an id no test ever files, because that link is
    what condition 6 trips on: a fixture without it would leave the escape hatch
    untested at the daemon and every confirmation would still pass.
    """
    (row,) = [a for a in store.all_assumptions(wo["id"])]
    store.record_provisional(row["id"], verdict=verdict, reason=reason,
                             model="sonnet", stakes="routine")
    store.link_assumption_question(row["id"], EARLY_QUESTION)
    return store.all_assumptions(wo["id"])[0]


def _git(cwd, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


DEFAULT_FILES = {"render.py": "def _render_row():\n    return 1\n"}


def with_diff(started, *, files: dict[str, str] | None = None, **kw):
    """A parked order whose worker worktree really has a commit in it.

    `park` alone leaves the repository empty, and `evidence.collect_work_order` answers
    an empty packet for one — which this section ASKS on anyway. So the diff has to be
    real here or the test would pass with the evidence never collected at all.

    `files` is what the commit CONTAINS, because the evidence gate reads the diff itself:
    the secret tests need a diff whose text and whose paths they choose.
    """
    path = started.catalog.project("proj_a").path
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    worktree = path / ".claude" / "worktrees" / "wt"
    _git(path, "worktree", "add", "-q", "-b", "wo-branch", str(worktree))
    for name, body in (files or DEFAULT_FILES).items():
        (worktree / name).write_text(body)
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "the change under review")
    store, wo = park(started, auto_review=True, **kw)
    store.update_work_order(wo["id"], worktree="wt")
    return store, store.get_work_order(wo["id"])


def test_the_confirmation_carries_the_diff_the_early_pass_never_had(started):
    """THE SECOND CALL IS THE FEATURE (Neo, question 549). The cheap design — confirm in
    code, re-ask only if something changed — was refused: a mid-turn verdict never had a
    result summary, so "something changed" is always true."""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert confirmation["kind"] == autoreview.QUESTION_KIND
    assert PROVISIONAL_REASON in confirmation["question"]
    assert "opened a PR" in confirmation["question"]       # the result summary
    assert "render.py" in confirmation["question"]         # the diff
    row = store.all_assumptions(wo["id"])[0]
    assert row["confirm_question_id"] == confirmation["id"]
    assert row["neo_question_id"] == EARLY_QUESTION   # the early link is not overwritten


def test_running_the_confirmation_pass_twice_asks_once(started):
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)
    ask(started, store)

    assert len(questions()) == 1


def test_a_confirmation_in_flight_is_not_a_note_on_the_timeline(started):
    """`confirming` is suppressed for `asked`'s reason: the question is filed, which is
    the pass WORKING. Unsuppressed it would put "held — already with Neo to confirm" on
    every assumption the OS is in the middle of confirming, every reconcile tick."""
    store, wo = with_diff(started)
    provisional(store, wo)

    ask(started, store)
    ask(started, store)

    assert events(store, wo["id"], "autoreview_held") == []


def test_an_early_objection_costs_no_second_call_at_the_daemon_either(started):
    """The pure hold, asserted where the money is spent."""
    store, wo = with_diff(started)
    provisional(store, wo, verdict="object")

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTED


def test_a_historical_row_behaves_exactly_as_it_did_before_this_section(started):
    """Every new column empty: the ordinary `decide`/`propose` path, one question, and
    nothing linked to a confirmation."""
    store, wo = park(started, auto_review=True)

    ask(started, store)

    (q,) = questions()
    row = store.all_assumptions(wo["id"])[0]
    assert row["neo_question_id"] == q["id"] and row["confirm_question_id"] is None
    assert "CONFIRM" not in q["question"]


# -- the daemon: the outstanding-objection gate ----------------------------------------


def test_an_objection_in_flight_holds_the_pass_and_says_so(started):
    """kn-22ba6087: a guard that returns early must still record why. `objection_in_
    flight` is therefore NOT on `_note_autoreview_held`'s suppression list — unlike the
    four holds that mean "never a candidate", this one is a state the user can see."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_OBJECTION_IN_FLIGHT


def test_an_objection_the_worker_actually_received_does_not_hold_anything(started):
    """DELIVERED IS NOT OUTSTANDING. The worker was told, the record can explain how and
    when, and the assumption the OS was going to confirm is confirmed."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    store.mark_objection_delivered(row["id"])

    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


def test_the_gate_is_a_retry_and_not_a_refusal(started):
    """"Not yet" costs the assumption nothing: the withdrawal runs, the next tick asks."""
    store, wo = with_diff(started)
    row = provisional(store, wo)
    store.record_objection(row["id"], envelope_id=1, transport="queue", sent_ts=1.0)
    ask(started, store)

    store.withdraw_objection(row["id"])
    ask(started, store)

    assert len(questions()) == 1
    assert store.all_assumptions(wo["id"])[0]["confirm_question_id"] is not None


# -- the daemon: what comes back -------------------------------------------------------


def test_a_confirmed_assumption_settles_exactly_as_an_ordinary_acceptance_does(started):
    store, wo = with_diff(started, assumptions=(f"FORCE_ACCEPT — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "accepted"
    assert row["decided_by"] == "neo" and row["decided_by"] != "user"
    assert store.pending_assumptions(wo["id"]) == []
    (event,) = events(store, wo["id"], "autoreview_confirmed")
    assert event["neo_question_id"] == confirmation["id"]
    assert event["provisional_reason"] == PROVISIONAL_REASON
    assert event["provisional_verdict"] == "accept"


def test_a_verdict_that_does_not_confirm_leaves_the_assumption_with_the_user(started):
    """BOTH READINGS, side by side: what Neo thought while the work ran, and what it
    thought once it saw the result. That is strictly more than the user gets today."""
    store, wo = with_diff(started, assumptions=(f"FORCE_DENY — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []
    (event,) = events(store, wo["id"], "autoreview_unconfirmed")
    assert event["provisional_verdict"] == "accept"
    assert event["provisional_reason"] == PROVISIONAL_REASON
    assert event["provisional_model"] == "sonnet"
    assert "test-forced" in event["reason"]
    assert event["neo_question_id"] == confirmation["id"]
    assert next(q for q in questions()
                if q["id"] == confirmation["id"])["status"] == "escalated"


@pytest.mark.parametrize("marker", ["FORCE_FAIL", "FORCE_GARBAGE"])
def test_a_failure_is_never_a_confirmation(started, marker):
    """The transport failing and the model answering something else are the same fact
    here: nothing was confirmed, so nothing settles.

    The FORCE_FAIL half has two claims of its own, one test each below: the question
    stays RETRYABLE, and no `autoreview_unconfirmed` event is written.
    """
    store, wo = with_diff(started, assumptions=(f"{marker} — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    row = store.all_assumptions(wo["id"])[0]
    assert row["status"] == "pending" and not row["decided_by"]
    assert events(store, wo["id"], "autoreview_confirmed") == []


def test_a_failed_call_leaves_the_confirmation_queued_for_retry(started):
    """A MODEL THAT WAS NEVER REACHED HAS MADE NO JUDGEMENT (pinned fleet learning).

    `neo.drain_queue` hands a crashed call back through `NeoStore.release_claim`, which
    requeues it with `attempts` incremented and only writes `failed` once the retries are
    spent. Pinned here because the failure mode it replaces — `failed` at `attempts=0`
    with an escalation synthesised from the crash — reached the user as a ruling Neo
    never made (question 388), and this pass files the questions it would happen to.
    """
    store, wo = with_diff(started, assumptions=(f"FORCE_FAIL — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)
    (confirmation,) = questions()

    drain(started)

    row = next(q for q in questions() if q["id"] == confirmation["id"])
    assert row["status"] == "queued"
    assert row["status"] not in ("failed", "answered", "escalated")
    assert not row["answer"]


def test_a_failed_call_writes_no_unconfirmed_event(started):
    """A DIFFERENT CLAIM from "nothing settled": `autoreview_unconfirmed` records that
    Neo looked at the delivered result and would not confirm its own earlier reading. A
    call that never happened produced no such reading, and writing the event would put
    that judgement on the record in Neo's name."""
    store, wo = with_diff(started, assumptions=(f"FORCE_FAIL — {ROUTINE}",))
    provisional(store, wo)
    ask(started, store)

    drain(started)

    assert events(store, wo["id"], "autoreview_unconfirmed") == []


# -- the second gate: a diff the OS will not copy into a stored question ---------------
#
# `decide_confirm` is pure over a ROW and cannot see a diff. This is the gate over the
# EVIDENCE, and it exists because `neo.ask` PERSISTS the question text: a diff that adds
# a credential would land in the question store and on `/neo` and `jarvis neo list`,
# surfaces the ask pass never put diff content on (kn-deef42ea — a redaction decision is
# also a filing decision). Neo, question 593, chose this narrow net over running
# `high_stakes_marker` on the diff.

SECRET_VALUE = "sk-live-9f2b7c41d8e3a6"
SECRET_BODY = f'api_key = "{SECRET_VALUE}"\n'

#: The negative control's body, and it is the test that stops this becoming the wide
#: net: every word here is this repo's daily vocabulary.
PROSE_BODY = (
    "# the credential check runs before the production deploy path\n"
    "def _render_row(row):\n"
    '    """Delete the row from the render list, never from the database."""\n'
    "    return row\n"
)


def held_codes(store, wo_id: str) -> list[str]:
    return [e["code"] for e in events(store, wo_id, "autoreview_held")]


def test_a_secret_shaped_added_line_is_never_copied_into_a_stored_question(started):
    """THE BLOCKING FINDING. No question is filed at all — the assumption stays pending
    and is the user's, exactly as it is without this feature. Filing the question with
    the diff withheld is the cheap design Neo refused in question 549."""
    store, wo = with_diff(started, files={"settings.py": SECRET_BODY})
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    assert all(SECRET_VALUE not in q["question"] for q in questions())
    assert store.all_assumptions(wo["id"])[0]["status"] == "pending"
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    assert SECRET_VALUE not in held["reason"]


def test_a_secret_shaped_path_holds_it_whatever_the_body_says(started):
    """The path is evidence on its own: a work order that adds a `.env` is one whose
    diff the OS will not store, and reading the file to find out would be the same
    mistake one layer down."""
    store, wo = with_diff(started, files={".env": "GREETING=hello\n"})
    provisional(store, wo)

    ask(started, store)

    assert questions() == []
    (held,) = events(store, wo["id"], "autoreview_held")
    assert held["code"] == autoreview.HELD_EVIDENCE_SECRET
    assert ".env" in held["reason"]


def test_this_repos_own_everyday_diff_still_arms_and_still_asks(started):
    """THE NEGATIVE CONTROL, and without it a net that holds everything passes. Running
    `high_stakes_marker` over a diff was REJECTED (Neo, question 593) for exactly this:
    "credential", "production" and "delete" appear in nearly every change this repo
    makes, so that net would hold almost every confirmation and switch the pass off
    silently."""
    store, wo = with_diff(started, files={"render.py": PROSE_BODY})
    provisional(store, wo)

    ask(started, store)

    (confirmation,) = questions()
    assert "render.py" in confirmation["question"]
    assert autoreview.HELD_EVIDENCE_SECRET not in held_codes(store, wo["id"])


# -- `secret_marker`: pure, and it never repeats the secret ----------------------------


@pytest.mark.parametrize("line", [
    '+api_key = "sk-live-9f2b7c41d8e3a6"',
    "+AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI7K9bPxRfiCY1EXAMPLEKEY2",
    '+    self.client_secret = "7f3a9b2c5d8e1f4a6b0c"',
    "+export GITHUB_TOKEN=ghp_9aF3kLm2Qr7Xz1Bv6Nt0",
    "+-----BEGIN RSA PRIVATE KEY-----",
    "+ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQC9x2 user@host",
    "+Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.aGk",
    "+Authorization: Basic dXNlcjpwYXNzd29yZDEyMw==",
])
def test_secret_shaped_added_lines_fire(line):
    assert autoreview.secret_marker("", line)


@pytest.mark.parametrize("line", [
    '+api_key = ""',
    "+api_key = ''",
    "+api_key = None",
    "+api_key: str",
    '+password = "changeme"',
    '+client_secret = "<your-secret-here>"',
    '+api_key = "${API_KEY}"',
    '+api_key = os.environ["API_KEY"]',
    '+token = os.getenv("TOKEN")',
    '+api_key = "REDACTED"',
    '+api_key = "example"',
    # This module's own code, which names secrets for a living.
    '+HELD_EVIDENCE_SECRET = "evidence_secret"',
    '+    r"credential|secret|password|\\bapi[ -]?key\\b"',
    "+    marker = high_stakes_marker(text)",
    "+# the api key is read from the environment, never committed",
])
def test_placeholders_and_prose_do_not_fire(line):
    assert autoreview.secret_marker("", line) == ""


def test_a_removed_secret_line_does_not_fire():
    """A removed secret is the change doing the right thing, and holding the pass on it
    would punish the only diff that fixes one."""
    assert autoreview.secret_marker("", f'-api_key = "{SECRET_VALUE}"') == ""


def test_a_diff_header_is_not_read_as_an_added_line():
    """`+++ b/path` starts with `+` and is not a line the diff adds — so the assignment
    net must not read one, whatever the rest of the header says."""
    assert autoreview.secret_marker("", f'+++ b/api_token = "{SECRET_VALUE}"') == ""


def test_the_marker_never_carries_the_secret_itself():
    """IT IS RENDERED on the timeline and on `jarvis wo show`. A marker quoting the
    value would move the secret from the question store to the event store."""
    marker = autoreview.secret_marker("", f'+api_key = "{SECRET_VALUE}"')
    assert marker and SECRET_VALUE not in marker
    assert not any(c in marker for c in ("9f2b", "sk-live"))


@pytest.mark.parametrize("path", [
    " .env | 2 +-",
    " config/.env.production | 2 +-",
    " certs/server.pem | 2 +-",
    " certs/server.key | 2 +-",
    " keystore.p12 | 2 +-",
    " keystore.pfx | 2 +-",
    " deploy/id_rsa | 2 +-",
    " deploy/id_ed25519 | 2 +-",
    " infra/.aws/credentials | 2 +-",
    " home/.netrc | 2 +-",
    " home/.npmrc | 2 +-",
    " app/secrets.yaml | 2 +-",
    " gcp-service-account-prod.json | 2 +-",
])
def test_secret_shaped_paths_fire_from_the_stat(path):
    assert autoreview.secret_marker(path, "")


@pytest.mark.parametrize("path", [
    " src/jarvis/autoreview.py | 40 ++++",
    " tests/test_autoreview_confirm.py | 12 +-",
    " docs/keyboard-shortcuts.md | 3 +-",
    " src/jarvis/ui/templates/_question.html | 2 +-",
])
def test_this_repos_own_paths_do_not_fire(path):
    assert autoreview.secret_marker(path, "") == ""


def test_a_secret_shaped_path_in_a_diff_header_fires_too():
    assert ".env" in autoreview.secret_marker("", "diff --git a/.env b/.env\n")


# -- `decide_evidence`: the hold it returns --------------------------------------------


def test_decide_evidence_arms_on_an_ordinary_diff():
    d = autoreview.decide_evidence(judged(), " render.py | 2 +-", "+    return 1\n")
    assert d.armed and d.assumption_id == 3 and d.n == 2


def test_decide_evidence_holds_and_carries_the_row_it_is_about():
    """`assumption_id` and `n` so `_note_autoreview_held` dedupes per assumption, as
    every other hold does — otherwise one work order records this every tick."""
    d = autoreview.decide_evidence(judged(), "", f'+api_key = "{SECRET_VALUE}"')
    assert d.code == autoreview.HELD_EVIDENCE_SECRET
    assert d.assumption_id == 3 and d.n == 2
    assert SECRET_VALUE not in d.reason
