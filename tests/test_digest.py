"""Shortening an over-long Neo question for the dashboard.

Neo question #53 was ~7,000 characters of feature-order brief, rendered inline on the
`/neo` page, and the user could not stay across it. This covers the four pieces of the
answer: the vendored output style, the strict-JSON digest itself, the store query that
decides which questions earn one, and the daemon batch that produces them.

The `/neo` rendering — the digest on the page and the disclosure holding the verbatim
text — is in `tests/test_ui.py`, with the browser.
"""

from __future__ import annotations

import json

import pytest

from jarvis import catalog, digest, neo, testing
from jarvis.neo_store import NeoStore


@pytest.fixture()
def store(jarvis_home):
    s = NeoStore()
    try:
        yield s
    finally:
        s.close()


LONG = "Which serialisation format should the exporter emit? " * 40  # ~2,100 chars


def canned(reply: dict | str):
    """A `call` that returns one reply and records what it was asked.

    Returns (call, calls) — `calls` accumulates the kwargs of each invocation, so a test
    can assert the SYSTEM PROMPT and the number of round trips, not merely the result.
    """
    calls: list[dict] = []

    def call(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return reply if isinstance(reply, str) else json.dumps(reply)

    return call, calls


# -- the vendored output style ----------------------------------------------------------


def test_the_output_style_ships_and_is_the_upstream_file():
    """The digest is a prompt plus someone else's style guide; if the style guide is not
    in the wheel the feature is a plain summariser wearing its name."""
    text = digest.load_skill()
    assert "name: i-have-adhd" in text
    # Three rules the digest's own field list is built on. Named individually rather
    # than by a length check, because a truncated or half-copied file has a length too.
    assert "Lead with the next action" in text
    assert "Cap lists at 5 items" in text
    assert "No preamble, no recap, no closing pleasantries" in text
    assert (digest.SKILL_PATH.parent / "i-have-adhd.LICENSE").read_text().startswith(
        "MIT License")


def test_the_output_style_is_not_shipped_into_projects_as_a_skill_or_a_subagent(project):
    """`bootstrap` copytrees `assets/skills/`, `assets/agents/` and
    `assets/project-skills/` WHOLESALE. A prompt dropped in any of them becomes a skill
    every worker loads or a subagent every feature-order planner can invoke — the same
    trap the Neo seats sidestep. This is one prompt for one internal call and belongs to
    none of those populations.

    The negative half is paired with the positive one in the same test: "the file was
    copied nowhere" passes just as well for a file that does not exist at all.
    """
    from jarvis import bootstrap

    bootstrap.install_agent_assets(project, kind="worker")
    bootstrap.install_agent_assets(project, kind="planner")
    bootstrap.install_project_skills(project)
    delivered = [p for p in project.rglob("*") if "adhd" in p.name.lower()]
    assert delivered == []
    # ...and here is where it does live, reachable by the one module that reads it.
    assert digest.SKILL_PATH.is_file()
    assert digest.SKILL_PATH.parent == bootstrap.ASSETS / "digest"


def test_the_system_prompt_is_the_style_verbatim_behind_a_machine_readable_header():
    prompt = digest.build_system_prompt()
    assert prompt.startswith(digest.DIGEST_HEADER)
    assert digest.load_skill() in prompt          # verbatim, not paraphrased
    assert '"headline"' in prompt                  # and the output contract after it
    assert digest.build_system_prompt() is prompt  # byte-stable: cached, one prefix


def test_the_fake_claude_recognises_the_digest_header():
    """The test fake keys on the header string, and it is a separate script that cannot
    import it. Rename `DIGEST_HEADER` without this and every digest test would still
    pass — through the gate-review branch, asserting nothing."""
    assert digest.DIGEST_HEADER in testing.FAKE_CLAUDE


# -- the digest itself --------------------------------------------------------------------


def test_a_digest_is_the_four_fields_the_template_renders():
    call, calls = canned({"headline": "CSV or JSON for the exporter?",
                          "bullets": ["the consumer is a spreadsheet"],
                          "options": ["CSV — what they open", "JSON — nested data"],
                          "recommendation": "CSV"})
    view = digest.summarise(LONG, model="haiku", call=call)

    assert view == {"headline": "CSV or JSON for the exporter?",
                    "bullets": ["the consumer is a spreadsheet"],
                    "options": ["CSV — what they open", "JSON — nested data"],
                    "recommendation": "CSV"}
    assert len(calls) == 1
    assert calls[0]["prompt"] == LONG                     # the question, unabridged
    assert calls[0]["system_prompt"] == digest.build_system_prompt()
    assert calls[0]["model"] == "haiku"


def test_lists_are_capped_at_five_by_the_validator_not_by_the_prompt():
    """The style asks for at most five items. Asking is not enforcing, and twelve
    bullets on the page is the exact problem this feature exists to remove."""
    call, _ = canned({"headline": "h",
                      "bullets": [f"b{i}" for i in range(12)],
                      "options": [f"o{i}" for i in range(12)]})
    view = digest.summarise(LONG, model="haiku", call=call)
    assert len(view["bullets"]) == digest.MAX_BULLETS == 5
    assert len(view["options"]) == digest.MAX_OPTIONS == 5
    assert view["bullets"] == [f"b{i}" for i in range(5)]  # the first five, in order


def test_everything_but_the_headline_may_be_empty():
    """A question that is not a choice has no options, and inventing some would be
    worse than showing none."""
    call, _ = canned({"headline": "Should the exporter keep emitting CSV?"})
    view = digest.summarise(LONG, model="haiku", call=call)
    assert view["headline"]
    assert view["bullets"] == [] and view["options"] == []
    assert view["recommendation"] == ""


def test_a_headline_less_reply_is_retried_once_and_then_refused():
    """A digest with no first line is a blank box where the question used to be, so it
    is refused rather than rendered. Two attempts, then the failure reaches the caller —
    which records it and falls back to the full question."""
    call, calls = canned({"bullets": ["nothing to head this with"]})
    with pytest.raises(Exception) as e:
        digest.summarise(LONG, model="haiku", call=call)
    assert "headline" in str(e.value)
    assert len(calls) == 2
    # The retry appends the complaint to the USER prompt and leaves the system prompt
    # byte-identical — that is what keeps the second call inside the prompt cache.
    assert calls[1]["prompt"].startswith(LONG)
    assert calls[0]["system_prompt"] == calls[1]["system_prompt"]


def test_unparseable_output_is_refused_rather_than_half_rendered():
    call, _ = canned("I'd summarise it, but where would I begin?")
    with pytest.raises(Exception):
        digest.summarise(LONG, model="haiku", call=call)


def test_the_call_strips_the_callees_tools():
    """A tooled callee asked to shorten a question about `src/jarvis/panel.py` goes and
    reads `src/jarvis/panel.py`, and the page then shows a description of the code
    instead of a shortening of the question — a failure that looks like a good answer.

    `attribute=False` rides along for a different reason: the daemon binds this call's
    work order itself through `on_usage`, and leaving the transport's own attribution on
    would write a second `agent_calls` row for the same tokens.
    """
    assert digest.CALL.keywords == {"tools": "", "attribute": False}


def test_the_threshold_is_what_stops_a_one_line_question_costing_a_call():
    assert digest.needs_digest(LONG)
    assert not digest.needs_digest("CSV or JSON?")
    assert not digest.needs_digest("")
    assert digest.needs_digest("x" * digest.MIN_CHARS)      # inclusive at the boundary
    assert not digest.needs_digest("x" * (digest.MIN_CHARS - 1))


# -- storage encoding ---------------------------------------------------------------------


def test_a_recorded_failure_decodes_to_no_digest_but_keeps_its_reason():
    """Both halves matter: the page must fall back to the full question (decode -> None)
    AND the row must stop being NULL, or the daemon would spend a call on it every tick
    for ever."""
    raw = digest.encode_failure("model said nothing useful, twice")
    assert digest.decode(raw) is None
    assert "twice" in digest.failure_reason(raw)
    # ...and a real digest is the other way round.
    good = digest.encode({"headline": "h", "bullets": [], "options": [],
                          "recommendation": ""})
    assert digest.decode(good)["headline"] == "h"
    assert digest.failure_reason(good) == ""


def test_a_missing_or_corrupt_digest_is_simply_no_digest():
    """Every one of these renders the full question, which is what the page did before
    this feature existed. There is no state in which the user loses the question."""
    assert digest.decode(None) is None            # never attempted
    assert digest.decode("") is None              # ditto
    assert digest.decode("not json at all") is None
    assert digest.decode('{"headline": ""}') is None   # empty headline is not a digest
    assert digest.decode('["a", "b"]') is None    # valid JSON, wrong shape


# -- which questions earn one -------------------------------------------------------------


def test_only_long_undigested_questions_the_user_will_read_are_picked_up(store):
    """The status filter is the cost control, so every exclusion is asserted next to the
    one row that must come back — a query returning `[]` would satisfy the exclusions
    perfectly on its own."""
    wanted = store.ask("proj_a", "wo-1", LONG)
    store.mark(wanted["id"], "escalated")

    short = store.ask("proj_a", "wo-1", "CSV or JSON?")
    store.mark(short["id"], "escalated")
    queued = store.ask("proj_a", "wo-1", LONG)                      # still with Neo
    answering = store.ask("proj_a", "wo-1", LONG)
    store.conn.execute("UPDATE questions SET status='answering' WHERE id=?",
                       (answering["id"],))
    reviewed = store.ask("proj_a", "wo-1", LONG)
    store.record_answer(reviewed["id"], "CSV")
    store.review(reviewed["id"], approved=True)
    already = store.ask("proj_a", "wo-1", LONG)
    store.mark(already["id"], "escalated")
    store.set_digest(already["id"], digest.encode({"headline": "h"}))

    picked = [q["id"] for q in store.questions_needing_digest(digest.MIN_CHARS)]
    assert picked == [wanted["id"]]
    for other in (short, queued, answering, reviewed, already):
        assert other["id"] not in picked


def test_an_unreviewed_neo_answer_and_a_failed_call_both_earn_one(store):
    """The other two statuses the user reads, alongside a control in each direction:
    a user-answered question is not awaiting Neo's review, so it is not picked up."""
    unreviewed = store.ask("proj_a", "wo-1", LONG)
    store.record_answer(unreviewed["id"], "CSV", answered_by="neo")
    failed = store.ask("proj_a", "wo-1", LONG)
    store.mark(failed["id"], "failed", reason="the call died")
    by_user = store.ask("proj_a", "wo-1", LONG)
    store.record_answer(by_user["id"], "CSV", answered_by="user")

    picked = [q["id"] for q in store.questions_needing_digest(digest.MIN_CHARS)]
    assert sorted(picked) == sorted([unreviewed["id"], failed["id"]])


def test_the_digest_column_survives_a_reopen_and_starts_null(store, jarvis_home):
    q = store.ask("proj_a", "wo-1", LONG)
    assert store.get(q["id"])["digest"] is None
    store.set_digest(q["id"], digest.encode({"headline": "h"}))
    store.close()
    reopened = NeoStore()
    try:
        assert digest.decode(reopened.get(q["id"])["digest"])["headline"] == "h"
    finally:
        reopened.close()


# -- the daemon batch -----------------------------------------------------------------------


@pytest.fixture()
def daemon(catalog_file, jarvis_home, fake_claude):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    return Daemon(load_catalog(catalog_file))


def escalated_long_question(store: NeoStore, text: str = LONG) -> dict:
    q = store.ask("proj_a", "wo-1", text)
    store.mark(q["id"], "escalated", reason="the user must decide")
    return q


def test_the_daemon_digests_a_long_escalated_question(daemon, store):
    q = escalated_long_question(store)
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)

    view = digest.decode(store.get(q["id"])["digest"])
    assert view["headline"].startswith("digest of:")
    assert len(view["bullets"]) == 5          # the fake offers seven; the cap holds
    assert view["options"] and view["recommendation"]
    # The question itself is untouched: the digest is a display artefact laid beside it,
    # never a replacement for it.
    assert store.get(q["id"])["question"] == LONG


def test_the_daemon_leaves_a_short_question_alone(daemon, store):
    """No digest AND no call: the threshold is only a cost control if the call is the
    thing it stops."""
    q = store.ask("proj_a", "wo-1", "CSV or JSON?")
    store.mark(q["id"], "escalated")
    before = len(list(_calls_dir().glob("*.json")))
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    assert store.get(q["id"])["digest"] is None
    assert len(list(_calls_dir().glob("*.json"))) == before


def test_a_failed_digest_is_recorded_once_and_never_retried(daemon, store):
    """Two ticks, one attempt. Without the recorded failure the column stays NULL, the
    row stays in the query, and the daemon spends a call on it every five seconds."""
    q = escalated_long_question(store, LONG + " FORCE_DIGEST_FAIL")
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    assert digest.decode(store.get(q["id"])["digest"]) is None
    assert digest.failure_reason(store.get(q["id"])["digest"])
    assert store.questions_needing_digest(digest.MIN_CHARS) == []


def test_a_garbled_digest_is_recorded_as_a_failure_not_rendered(daemon, store):
    q = escalated_long_question(store, LONG + " FORCE_DIGEST_GARBAGE")
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    assert digest.decode(store.get(q["id"])["digest"]) is None
    assert "headline" in digest.failure_reason(store.get(q["id"])["digest"])


def test_digesting_is_off_when_no_digest_model_is_configured(daemon, store):
    """The one knob that turns the extra calls off. Empty model, no call, page unchanged."""
    daemon.catalog.os.neo.digest_model = ""
    q = escalated_long_question(store)
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    assert store.get(q["id"])["digest"] is None


def test_digesting_is_off_when_neo_is_off(daemon, store):
    daemon.catalog.os.neo.enabled = False
    q = escalated_long_question(store)
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    assert store.get(q["id"])["digest"] is None


def test_one_batch_is_bounded_and_the_rest_wait_for_the_next_tick(daemon, store):
    """An instance upgrading into this feature can have a backlog of long questions
    already in `neo.db`. The first batch must not turn into an unbounded spend."""
    from jarvis.daemon import DIGEST_BATCH

    ids = [escalated_long_question(store)["id"] for _ in range(DIGEST_BATCH + 2)]
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    done = [i for i in ids if store.get(i)["digest"] is not None]
    assert len(done) == DIGEST_BATCH
    assert len(store.questions_needing_digest(digest.MIN_CHARS)) == 2


def test_a_digest_never_reaches_neo_or_the_worker(daemon, store, project):
    """THE LINE THIS FEATURE MUST NOT CROSS. The digest is display; Neo answers from the
    full text. So: digest a question, then let Neo answer a fresh copy of it, and assert
    the prompt Neo received is the question in full and carries no digest — with the
    control, in the same test, that a digest demonstrably existed at that moment.
    """
    digested = escalated_long_question(store)
    daemon.digest_tick()
    daemon.digest_pool.shutdown(wait=True)
    view = digest.decode(store.get(digested["id"])["digest"])
    assert view is not None                     # the control: there WAS one to leak

    store.ask("proj_a", "wo-1", LONG)
    daemon._neo_drain()

    asked = [json.loads(p.read_text()) for p in _calls_dir().glob("*.json")]
    neo_prompts = [c["argv"][c["argv"].index("-p") + 1] for c in asked
                   if "-p" in c["argv"]
                   and digest.DIGEST_HEADER not in " ".join(c["argv"])]
    assert neo_prompts, "Neo was never called"
    assert any(LONG in p for p in neo_prompts)              # it read the whole thing
    assert not any(view["headline"] in p for p in neo_prompts)


def _calls_dir():
    """Where the fake `claude` records one file per invocation."""
    import os
    from pathlib import Path

    return Path(os.environ["FAKE_CLAUDE_DIR"]) / "calls"


# -- configuration ---------------------------------------------------------------------------


def test_the_digest_model_is_catalog_configurable_and_cheap_by_default(tmp_path, project):
    assert catalog.NeoConfig().digest_model == "haiku"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({
        "os": {"neo": {"digest_model": "sonnet"}},
        "projects": [{"name": "proj_a", "path": str(project), "description": "d"}],
    }))
    assert catalog.load_catalog(path).os.neo.digest_model == "sonnet"


def test_neo_reads_the_question_in_full_and_the_disclosure_shows_that_prompt():
    """The disclosure's contract: what it displays is what `answer_question` sends, built
    by the same function, so the two cannot drift."""
    q = {"project": "proj_a", "wo_id": "wo-1", "question": LONG,
         "context": "the exporter work order"}
    prompt = neo.build_question_prompt(q)
    assert LONG in prompt
    assert "the exporter work order" in prompt
    assert "wo-1" in prompt
