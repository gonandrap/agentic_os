"""The seat primitive, extracted from Neo's panel so two panels can share it.

THE HIGHEST-VALUE TEST IN THIS FILE IS THE CACHE ONE. `chair.md` ships in
`assets/neo-seats/` AND in `assets/validator-seats/`, and `definition` is cached. A
name-only key would hand whichever roster asked second the OTHER panel's mandate —
silently, with every other test in the suite green. So the test loads the Neo chair, the
validator chair, and THEN THE NEO CHAIR AGAIN: a name-only key passes the first two
assertions and fails only the third.

The rest of the file pins the two properties that made the extraction worth doing at all:
`run_blind` cannot reach a store (it is not in its signature, and it runs happily on a
thread that has none), and it genuinely fans out — proved with a barrier, because a
wall-clock comparison passes on a fast machine that ran the seats one after another.
"""

from __future__ import annotations

import inspect
import json
import threading
import time
from pathlib import Path

import pytest

from jarvis import claude_cli, panel, seats, validation
from jarvis.bootstrap import ASSETS

NEO = seats.Roster(assets=ASSETS / "neo-seats", vocabulary=("premise", "chair"),
                   header="# Neo panel seat: {seat}")
VALIDATOR = seats.Roster(assets=ASSETS / "validator-seats",
                         vocabulary=("tester", "chair"),
                         header="# Jarvis validation seat: {seat}")


def reply(text: str = '{"verdict": "pass"}', usage=None):
    return claude_cli.HeadlessResult(text=text, usage=usage)


# -- the cache trap -------------------------------------------------------------------


def test_two_rosters_sharing_a_seat_name_do_not_share_a_cache_entry():
    """The whole reason `definition` takes a roster. Load Neo's chair, load the
    validator's, then LOAD NEO'S AGAIN — the third read is the one a name-only key gets
    wrong, and it is the one that would ship a Neo mandate into a validation verdict."""
    seats.definition.cache_clear()

    neo_first = seats.definition(NEO, "chair")[1]
    validator = seats.definition(VALIDATOR, "chair")[1]
    neo_again = seats.definition(NEO, "chair")[1]

    assert neo_first != validator, "the two chairs are different seats entirely"
    assert "Neo" in neo_first and "validation panel" in validator
    assert neo_again == neo_first, (
        "a name-only cache key poisons the first roster with the second's mandate")


def test_the_two_shipped_rosters_disagree_about_every_shared_seat():
    """`chair` is the only name the two vocabularies share today, and this is what keeps
    the test above honest if that ever changes."""
    shared = set(panel.neo_roster().vocabulary) & set(validation.roster().vocabulary)
    assert shared == {"chair"}
    for seat in shared:
        assert (seats.definition(panel.neo_roster(), seat)[1]
                != seats.definition(validation.roster(), seat)[1])


def test_a_roster_is_hashable_and_compares_by_value():
    """Both are load-bearing: hashable so it can be a cache key at all, by value so a
    caller that rebuilds its roster per call still hits the cache."""
    assert hash(NEO) == hash(seats.Roster(assets=NEO.assets, vocabulary=NEO.vocabulary,
                                          header=NEO.header))
    assert NEO != VALIDATOR


# -- reading a definition ---------------------------------------------------------------


def test_parse_definition_splits_frontmatter_from_the_mandate():
    meta, body = seats.parse_definition("---\nname: x\nmodel: haiku\n---\nthe mandate\n")

    assert meta == {"name": "x", "model": "haiku"}
    assert body == "the mandate"


@pytest.mark.parametrize("text", ["no frontmatter at all", "---\nname: x\nnever closed"])
def test_a_malformed_definition_is_a_seat_error(text):
    with pytest.raises(seats.SeatError):
        seats.parse_definition(text)


def test_a_definition_whose_name_disagrees_with_its_file_is_refused(tmp_path):
    (tmp_path / "tester.md").write_text("---\nname: testre\n---\nthe mandate")
    roster = seats.Roster(assets=tmp_path, vocabulary=("tester",), header="# {seat}")

    with pytest.raises(seats.SeatError, match="testre"):
        seats.definition(roster, "tester")


def test_shipped_reports_what_this_build_has_in_the_rosters_own_order(tmp_path):
    (tmp_path / "b.md").write_text("---\nname: b\n---\nb")
    roster = seats.Roster(assets=tmp_path, vocabulary=("a", "b", "c"), header="# {seat}")

    assert seats.shipped(roster) == ("b",)

    with pytest.raises(seats.SeatError, match="no definition ships"):
        seats.definition(roster, "a")


# -- run_blind: no store, and no way to reach one ----------------------------------------


def test_run_blind_takes_no_store_and_runs_off_the_main_thread(monkeypatch, tmp_path):
    """The thread-locality rule, expressed as a type. A sqlite connection belongs to the
    thread that opened it, so a seat on a pool thread must not query one — and it cannot,
    because there is no store in this signature to query.

    Run from a thread with no store in scope as well as asserted on the signature: the
    signature is the guarantee, the thread is the proof it is a real one.
    """
    params = inspect.signature(seats.run_blind).parameters
    assert "store" not in params
    assert not any("Store" in str(p.annotation) for p in params.values())

    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **kw: reply('{"verdict": "pass"}'))
    out: dict[str, list] = {}

    def worker():
        out["opinions"] = seats.run_blind(
            {"tester": ("system", "user"), "security": ("system", "user")},
            models={"tester": "haiku", "security": ""}, timeout=5, cwd=tmp_path)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=10)

    assert [o.seat for o in out["opinions"]] == ["tester", "security"]
    assert [o.verdict for o in out["opinions"]] == ["pass", "pass"]


def test_run_blind_fans_out_concurrently(monkeypatch, tmp_path):
    """A BARRIER, not a wall-clock comparison. Timing assertions pass on a machine that
    ran the seats one after another and simply ran them fast; a barrier of three can only
    be released if all three calls are in flight at once."""
    barrier = threading.Barrier(3, timeout=10)

    def call(*a, **kw):
        barrier.wait()
        return reply()

    monkeypatch.setattr(claude_cli, "run_headless_result", call)

    opinions = seats.run_blind(
        {s: ("system", "user") for s in ("tester", "security", "architect")},
        models={}, timeout=5, cwd=tmp_path)

    assert len(opinions) == 3
    assert all(o.status == "ok" for o in opinions)


def test_the_prompts_and_model_each_seat_was_given_reach_the_call(monkeypatch, tmp_path):
    seen: list[dict] = []

    def call(prompt, system_prompt=None, model=None, timeout=300, cwd=None, tools=None,
             **kw):
        seen.append({"prompt": prompt, "system": system_prompt, "model": model,
                     "timeout": timeout, "cwd": cwd, "tools": tools,
                     "attribute": kw.get("attribute")})
        return reply()

    monkeypatch.setattr(claude_cli, "run_headless_result", call)

    seats.run_blind({"tester": ("the tester mandate", "the packet")},
                    models={"tester": "haiku"}, timeout=17, cwd=tmp_path, tools="")

    assert seen == [{"prompt": "the packet", "system": "the tester mandate",
                     "model": "haiku", "timeout": 17, "cwd": tmp_path, "tools": "",
                     "attribute": False}]


def test_no_prompts_makes_no_calls(monkeypatch, tmp_path):
    def explode(*a, **kw):  # pragma: no cover - the point is that it never runs
        raise AssertionError("an empty round must not reach the transport")

    monkeypatch.setattr(claude_cli, "run_headless_result", explode)

    assert seats.run_blind({}, models={}, timeout=5, cwd=tmp_path) == []


# -- silence is not the same as unusable output --------------------------------------------


def test_a_seat_that_never_replied_is_distinguished_from_one_that_replied_unusably(
        monkeypatch, tmp_path):
    """BOTH IN ONE TEST, because the distinction is the whole of what `replied` carries
    and it is invisible in `status` alone: one is a call that never produced a reply, the
    other is a reply a caller can still route on.

    Neo's panel turns on exactly this — a premise seat that said nothing falls back to the
    single agent, while one that said something unparseable has routed toward the full
    panel — so a copy that dropped the field would change behaviour and pass every test
    about statuses.
    """
    def call(prompt, system_prompt=None, **kw):
        if "silent" in system_prompt:
            raise claude_cli.ClaudeCliError("the call timed out")
        return reply("this is, on reflection, hard to say")

    monkeypatch.setattr(claude_cli, "run_headless_result", call)

    silent, unusable = seats.run_blind(
        {"tester": ("silent", "p"), "security": ("talkative", "p")},
        models={}, timeout=5, cwd=tmp_path)

    assert (silent.status, silent.replied) == ("abstained", False)
    assert (unusable.status, unusable.replied) == ("failed", True)
    assert unusable.raw == "this is, on reflection, hard to say"
    assert silent.status != unusable.status, "and the stored rows differ too"


def test_a_seat_that_fails_does_not_take_the_round_down(monkeypatch, tmp_path):
    def call(prompt, system_prompt=None, **kw):
        if system_prompt == "boom":
            raise claude_cli.ClaudeCliError("no")
        return reply()

    monkeypatch.setattr(claude_cli, "run_headless_result", call)

    opinions = seats.run_blind({"tester": ("boom", "p"), "security": ("fine", "p")},
                               models={}, timeout=5, cwd=tmp_path)

    assert [o.status for o in opinions] == ["abstained", "ok"]


def test_the_opinion_carries_the_latency_and_the_usage_it_cost(monkeypatch, tmp_path):
    """Recorded per seat rather than per round: whether a panel earns its price is exactly
    the question of which seat is the expensive one, and an aggregate cannot answer it."""
    monkeypatch.setattr(claude_cli, "run_headless_result",
                        lambda *a, **kw: (time.sleep(0.01), reply(usage={"in": 1}))[1])

    op, = seats.run_blind({"tester": ("s", "p")}, models={"tester": "haiku"},
                          timeout=5, cwd=tmp_path)

    assert op.usage == {"in": 1}
    assert op.latency_ms >= 1
    assert op.model == "haiku"


def test_the_summary_never_carries_the_raw_reply():
    """Deliberation is stored and inspectable on demand; it is never pushed. A caller that
    must persist the reply reads `raw` by name, which is a decision it makes rather than
    one it inherits."""
    op = seats.Opinion(seat="tester", raw=json.dumps({"reason": "a secret reading"}),
                       verdict="pass", model="haiku", latency_ms=3)

    assert "a secret reading" not in json.dumps(op.summary())
    assert set(op.summary()) == {"seat", "status", "verdict", "route", "model",
                                 "latency_ms"}


def test_data_is_none_for_a_seat_that_is_not_ok():
    assert seats.Opinion(seat="t", raw='{"a": 1}').data == {"a": 1}
    assert seats.Opinion(seat="t", raw='{"a": 1}', status="abstained").data is None


# -- the layer this module sits at -----------------------------------------------------------


def test_seats_imports_only_the_two_modules_it_is_allowed_to():
    """A near-leaf: an adapter dragged under it (`bootstrap`, a store, `neo`) is how the
    asset directory would stop being a parameter and start being a global again."""
    import ast

    found: set[str] = set()
    for node in ast.walk(ast.parse(Path(seats.__file__).read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.update([base] if node.module else [base + a.name for a in node.names])

    assert found == {"__future__", "logging", "time", "concurrent.futures",
                     "dataclasses", "functools", "pathlib", "typing",
                     ".claude_cli", ".structured"}
    for forbidden in (".bootstrap", ".neo", ".neo_store", ".panel", ".validation",
                      ".project_store", ".central_store", ".paths", ".catalog"):
        assert forbidden not in found
