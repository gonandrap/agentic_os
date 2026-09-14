"""LLM-graded: can a reader actually answer their questions from the bill?

This surface was not rewritten because it was wrong. It was rewritten because it was
UNREADABLE, and no unit test can fail for that. The line it replaced said:

    ~$1.81 · worker ~$1.66 · jarvis ~$0.15 / 2 calls · 1.2M in / 14k out · 2 turns ·
    re-write tax ~$0.28

and the user, who owns the OS, could not tell from it whether the OS spent anything
outside "jarvis", what a re-write tax was, whose tokens the 1.2M were, or how many
tokens each side had spent. Every figure on that line was correct.

So the four questions ARE the eval. A model with no context beyond the rendered bill is
asked each of them and its answer is graded against what the bill actually says. The
subject is `jarvis cost <id>` rendered from a synthetic order whose numbers are known
here, so a wrong answer is checkable rather than a matter of taste.

Opt-in (spends tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_bill_comprehension.py -q
    JARVIS_EVALS_MODEL=opus   # optional, default sonnet
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import agent_usage, bill as bill_mod, claude_cli, cli, ops, usage
from jarvis.project_store import ProjectStore

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")

#: The numbers the synthetic order is built from, so every graded answer has a key.
WORKER_TURNS = 2
NEO_CALLS = 1
PANEL_SEATS = 5
SUBPROC_CALLS = 40


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("bill-comprehension-eval")


#: How many API calls the two turns are spread over in the planted transcript. Enough
#: that the biggest single call is far smaller than the cache-write total, which is what
#: a re-write tax IS: context sent to the cache more often than any one call needed it.
TRANSCRIPT_CALLS = 18


def plant_transcript(tmp_path, monkeypatch, session_id: str) -> None:
    """Write the session transcript the fake CLI does not write, summing to the turns."""
    root = tmp_path / "transcripts" / "-proj"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root.parent))
    totals = {"input": 40 * WORKER_TURNS, "write": 120_000 * WORKER_TURNS,
              "read": 900_000 * WORKER_TURNS, "out": 7_000 * WORKER_TURNS}
    rows = []
    for i in range(TRANSCRIPT_CALLS):
        share = {k: v // TRANSCRIPT_CALLS + (1 if i < v % TRANSCRIPT_CALLS else 0)
                 for k, v in totals.items()}
        rows.append(json.dumps({
            "type": "assistant",
            "message": {"id": f"m{i}", "model": "claude-opus-5",
                        "usage": {"input_tokens": share["input"],
                                  "cache_creation_input_tokens": share["write"],
                                  "cache_read_input_tokens": share["read"],
                                  "output_tokens": share["out"]}},
        }))
    (root / f"{session_id}.jsonl").write_text("\n".join(rows) + "\n")


@pytest.fixture()
def order(jarvis_home, project, fake_claude, catalog_file, tmp_path,
          monkeypatch) -> str:
    """One order that spent in all three ways. Returns its id.

    A session transcript is planted too, matching the recorded turns to the token. The
    re-write tax is derived from the transcript, not from the turn envelopes, so without
    one the bill has no re-write tax on it at all — and two of the scenarios below exist
    to ask what that term means.
    """
    # Every `agent_calls` row below is INVENTED, so it has to land in this test's own
    # home. It does not by default: `testing._bills_real_tokens()` sees a real-model run
    # (`JARVIS_EVALS_LLM`) inside a worker (`JARVIS_WO_ID`) and leaves accounting pointed
    # at the live ledger, so that a worker's eval spending real money is billed to its
    # order. That carve-out is for REAL calls; a fixture's fiction must never reach it —
    # and until this line the OS half of the bill was written to live state and then read
    # back from here, where it was absent, so every scenario graded a bill with no OS
    # spend on it at all. See issue #113.
    monkeypatch.setenv(agent_usage.SPEND_HOME_ENV, str(jarvis_home))
    ops.start_os(str(catalog_file), foreground=True)
    wo = ops.create_work_order("proj_a", "ship the release")
    store = ProjectStore(project)
    try:
        for seq in range(WORKER_TURNS):
            turn = store.create_turn(wo["id"],
                                     kind="dispatch" if not seq else "message",
                                     prompt="p")
            store.finish_turn(turn["id"], "done", result="r", cost_usd=1.20,
                              num_turns=1, usage_json=(
                                  '{"usage_v": 2, "total_cost_usd": 1.20, "input": 40, '
                                  '"cache_write": 120000, "cache_read": 900000, '
                                  '"cache_1h": 120000, "cache_5m": 0, "output": 7000, '
                                  '"api_calls": 12, "context_peak": 140000, '
                                  '"context_window": 1000000}'))
        store.conn.execute("UPDATE work_orders SET session_id=? WHERE id=?",
                           ("sess-comprehension", wo["id"]))
        store.conn.commit()
    finally:
        store.close()
    plant_transcript(tmp_path, monkeypatch, "sess-comprehension")
    for _ in range(NEO_CALLS):
        agent_usage.record("neo_answer", project="proj_a", wo_id=wo["id"],
                           label="question", model="claude-opus-5", question_id=1,
                           usage={"total_cost_usd": 0.10, "input": 10,
                                  "cache_write": 8_000, "cache_read": 25_000,
                                  "output": 700})
    for seat in ("premise", "record", "blast", "taste", "chair")[:PANEL_SEATS]:
        agent_usage.record("panel_seat", project="proj_a", wo_id=wo["id"], label=seat,
                           model="claude-opus-5", question_id=1,
                           usage={"total_cost_usd": 0.04, "input": 10,
                                  "cache_write": 3_000, "cache_read": 9_000,
                                  "output": 400})
    for _ in range(SUBPROC_CALLS):
        agent_usage.record(agent_usage.WORKER_SUBPROCESS, project="proj_a",
                           wo_id=wo["id"], label="pytest", model="claude-opus-5",
                           usage={"total_cost_usd": 0.05, "input": 5,
                                  "cache_write": 2_000, "cache_read": 6_000,
                                  "output": 300})
    # The bill must be internally sound before anyone is asked to read it: grading a
    # broken one would measure the model's charity rather than the surface.
    assert bill_mod.reconcile(ops.bill(wo["id"]))["balanced"]
    return wo["id"]


@pytest.fixture()
def rendered(order, capsys) -> str:
    """`jarvis cost <id>` for that order.

    The CLI rendering rather than the HTML: it is the same payload, and grading prose
    against a model is honest in a way grading it against a wall of markup is not.
    """
    capsys.readouterr()
    assert cli.main(["cost", order]) == 0
    return capsys.readouterr().out


def ask(question: str, rendered: str, cwd: Path) -> str:
    """One reader, one question, nothing but the bill in front of them.

    THE REAL `claude`, not the fake one. Building the order under test needs the OS
    running, which needs the fake CLI installed over `JARVIS_CLAUDE_BIN` — and with it
    still installed here, every question was answered by the stub's canned reply and
    every scenario in this file failed for a reason that had nothing to do with the
    bill. An LLM eval that silently grades a stub is worse than no eval: it reports on a
    surface nobody read.
    """
    prompt = (
        "Here is the output of a command that reports what one unit of work cost in "
        "Claude tokens.\n\n"
        f"```\n{rendered}\n```\n\n"
        f"Question: {question}\n\n"
        "Answer from this output alone, in at most three sentences. If the output "
        "does not say, reply exactly: IT DOES NOT SAY."
    )
    faked = os.environ.pop(claude_cli.CLAUDE_BIN_ENV, None)
    try:
        return claude_cli.run_headless(prompt, model=MODEL, cwd=cwd, tools="",
                                       timeout=180).strip()
    finally:
        if faked is not None:
            os.environ[claude_cli.CLAUDE_BIN_ENV] = faked


# -- the four questions the old surface could not answer -------------------------------


@scenario("bill-llm/comprehension", "says what else the OS spent besides Neo")
def test_the_reader_can_tell_what_the_os_half_covers(rendered, neutral_cwd):
    """Question 1: "why only jarvis? not a single token spent on the OS that wasn't
    jarvis?" — the bill has to name what the OS's half contains AND what it omits."""
    answer = ask("This mentions what Jarvis itself spent. What kinds of work is that "
                 "figure made of, and is there any Claude spending on this order that "
                 "is NOT counted anywhere here?", rendered, neutral_cwd).lower()

    assert "neo" in answer or "panel" in answer or "seat" in answer, answer
    # The floor: a call made outside Jarvis's transport cannot be counted at all, and
    # the bill says so on every rendering.
    assert "floor" in answer or "outside" in answer or "shell" in answer or \
        "cannot be counted" in answer or "not counted" in answer, answer


@scenario("bill-llm/comprehension", "explains the re-write tax")
def test_the_reader_can_explain_the_rewrite_tax(rendered, neutral_cwd):
    """Question 2: "what is re-write tax?" — and the answer that matters is that it is
    not an extra charge but a named part of the cache-write line."""
    answer = ask("What is the re-write tax, and is it an extra charge on top of the "
                 "other numbers or part of them?", rendered, neutral_cwd).lower()

    assert "cache" in answer, answer
    assert "not" in answer and ("extra" in answer or "additional" in answer
                                or "on top" in answer), answer


@scenario("bill-llm/comprehension", "attributes the token count correctly")
def test_the_reader_can_tell_whose_tokens_the_total_is(rendered, neutral_cwd):
    """Question 3: "1.2M is worker+jarvis?" — the old line put a whole-order dollar
    figure next to a worker-only token figure, and nothing said so."""
    answer = ask("The headline gives a total number of tokens. Does that number cover "
                 "only the worker's own session, or everything on this bill?",
                 rendered, neutral_cwd).lower()

    assert "everything" in answer or "all" in answer or "total" in answer, answer
    assert "only the worker" not in answer, answer


@scenario("bill-llm/comprehension", "gives tokens per actor")
def test_the_reader_can_read_tokens_per_actor(rendered, neutral_cwd):
    """Question 4: "why don't I see number of tokens per worker and per OS?"."""
    answer = ask("How many tokens did the worker's own session spend, and how many did "
                 "Jarvis spend on this order? Give both.", rendered, neutral_cwd)

    assert "IT DOES NOT SAY" not in answer, answer
    lowered = answer.lower()
    assert "m" in lowered or "k" in lowered, answer  # both figures, at magnitude
    assert "worker" in lowered and "jarvis" in lowered, answer


# -- and the thing a bill is for -------------------------------------------------------


@scenario("bill-llm/comprehension", "names the dearest class of token")
def test_the_reader_can_find_where_the_money_went(rendered, neutral_cwd):
    """The question a bill exists to answer, which the old surface never could: which
    KIND of token cost the most, given they are priced 20x apart."""
    answer = ask("Which class of token accounts for the largest share of this bill?",
                 rendered, neutral_cwd).lower()

    assert "cache" in answer, answer
    assert "write" in answer or "read" in answer, answer


@scenario("bill-llm/comprehension", "the panel's seats are individually visible")
def test_the_reader_can_price_one_panel_seat(rendered, neutral_cwd):
    """The standing ruling rendered: one row per seat, because "is the panel worth it"
    is answered by what each seat cost and never by their aggregate."""
    answer = ask("Did a panel of agents deliberate on this order, and can you tell "
                 "what any individual seat of it cost?", rendered, neutral_cwd).lower()

    assert "yes" in answer or "panel" in answer, answer
    assert any(seat in answer for seat in ("premise", "record", "blast", "taste",
                                           "chair")), answer


# -- the glossary ----------------------------------------------------------------------


@pytest.fixture()
def explained(order, capsys) -> str:
    """The same bill with `--explain`: the "?" bubbles the page shows, in the terminal."""
    capsys.readouterr()
    assert cli.main(["cost", order, "--explain"]) == 0
    return capsys.readouterr().out


@scenario("bill-llm/comprehension", "the glossary teaches an unfamiliar word")
def test_a_reader_who_has_never_seen_the_word_can_learn_it(explained, neutral_cwd):
    """The help hints exist because "re-write tax" was unguessable from the line alone.

    So the test is not "is the sentence present" — a unit test does that — but whether a
    reader with nothing else can come away with the two facts that matter: what it is,
    and that it is already inside the other numbers rather than added to them.
    """
    answer = ask("I have never seen the term \"re-write tax\" before. What is it, and "
                 "should I add it to the other figures to get my total?",
                 explained, neutral_cwd).lower()

    assert "cache" in answer, answer
    assert "not" in answer and ("add" in answer or "extra" in answer
                                or "on top" in answer or "part of" in answer), answer


@scenario("bill-llm/comprehension", "the glossary distinguishes the two cache classes")
def test_the_glossary_makes_the_cache_classes_distinguishable(explained, neutral_cwd):
    """Cache write and cache read differ by 12x in price and by one word in name. If a
    glossary cannot separate those two, it is decoration."""
    answer = ask("What is the difference between a cache write and a cache read here, "
                 "and which one is more expensive per token?",
                 explained, neutral_cwd).lower()

    assert "write" in answer and "read" in answer, answer
    assert "1.25" in answer or "2x" in answer or "tenth" in answer or "0.1" in answer, \
        answer
