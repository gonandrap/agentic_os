"""The character budget on Neo's learnings block.

Neo's system prompt is persona + every learning IN FULL, and until now only the ROW
COUNT was bounded. Unlike the project knowledge base — which ships workers an index and
lets them fetch what they need — Neo cannot look anything up: its calls are headless with
the question as their only input. So the only available bound is a character budget, and
what these tests pin is the three properties that make such a budget safe rather than
merely small: it evicts the OLDEST (so the block stays append-only and the cached prefix
survives an ordinary new learning), it SAYS what it dropped (a ruling that vanished
without trace is the failure the persona itself warns about), and it never drops
everything.
"""

from __future__ import annotations

from jarvis import neo as neo_mod
from jarvis.neo_store import NeoStore


def rows(*contents: str) -> list[dict]:
    """Learning rows as `NeoStore.learnings` returns them: oldest first."""
    return [{"project": "proj_a", "content": c} for c in contents]


def entries(block: list[str]) -> list[str]:
    return [line for line in block if line.startswith("- [")]


def test_a_block_inside_the_budget_is_untouched():
    block = neo_mod.render_learnings(rows("alpha", "beta"), budget=1000)
    assert block == ["- [proj_a] alpha", "- [proj_a] beta"]


def test_an_empty_ledger_still_tells_neo_what_to_do():
    assert neo_mod.render_learnings([], budget=1000) == [
        "(none yet — escalate when unsure)"]


def test_the_oldest_learnings_are_the_ones_evicted():
    """Oldest-first, so an ordinary new learning EXTENDS the cached prefix.

    Trimming the newest would be the intuitive reading of "keep it small" and is exactly
    wrong: it would move the boundary on every single addition, and the newest ruling is
    the one the next question is likeliest to turn on.
    """
    ledger = rows(*(f"{name} {'x' * 90}" for name in ("oldest", "middle", "newest")))
    block = neo_mod.render_learnings(ledger, budget=220)
    kept = entries(block)
    assert len(kept) == 2
    assert "oldest" not in " ".join(kept)
    assert kept[0].startswith("- [proj_a] middle")
    assert kept[1].startswith("- [proj_a] newest")


def test_adding_a_learning_under_budget_only_appends():
    """The property the whole oldest-first rule exists to protect."""
    before = neo_mod.render_learnings(rows("alpha", "beta"), budget=1000)
    after = neo_mod.render_learnings(rows("alpha", "beta", "gamma"), budget=1000)
    assert after[:len(before)] == before


def test_the_omission_is_stated_and_stated_last():
    """Silent truncation is the one outcome that is worse than no budget.

    Last, so that the lines above it — which are the bulk of the prompt — stay
    byte-identical when the drop count changes.
    """
    block = neo_mod.render_learnings(
        rows(*(f"entry-{i} {'x' * 90}" for i in range(5))), budget=220)
    assert len(entries(block)) == 2
    note = block[-1]
    assert note == block[len(entries(block))], "the note comes after every entry"
    assert "3 older learnings not shown" in note
    assert "220 characters" in note


def test_one_dropped_learning_is_not_pluralised():
    block = neo_mod.render_learnings(
        rows(*(f"entry-{i} {'x' * 90}" for i in range(3))), budget=220)
    assert "1 older learning not shown" in block[-1]


def test_the_newest_learning_survives_even_alone_over_budget():
    """A block that came back empty would be a total regression dressed up as a cap."""
    block = neo_mod.render_learnings(rows("old", "a very long ruling " * 50), budget=10)
    kept = entries(block)
    assert len(kept) == 1
    assert kept[0].endswith("a very long ruling " * 50)
    assert "1 older learning not shown" in block[-1]


def test_the_system_prompt_is_bounded_however_large_the_ledger_grows(jarvis_home):
    """The end-to-end property: teaching Neo more must stop growing its every call.

    Asserted against a ledger far over budget rather than a contrived one — the
    production ledger that prompted this was 23.4k characters across 16 entries, and the
    failure mode is precisely that nobody notices it climbing.
    """
    store = NeoStore()
    try:
        for i in range(60):
            store.add_learning(f"ruling {i}: " + "y" * 900, project="proj_a")
        prompt = neo_mod.build_system_prompt(store, "proj_a", learnings_chars=20000)
        persona_only = neo_mod.build_system_prompt(store, "proj_a", learnings_chars=0)
    finally:
        store.close()
    # The budget bounds the learnings block, not the persona, so measure the difference.
    assert len(prompt) - len(persona_only) < 21000
    assert "older learnings not shown" in prompt


def test_the_shipped_budget_is_what_neo_actually_gets(jarvis_home):
    """`build_system_prompt`'s default is the module constant, not an ad-hoc number."""
    store = NeoStore()
    try:
        for i in range(40):
            store.add_learning(f"ruling {i}: " + "z" * 900, project="proj_a")
        default = neo_mod.build_system_prompt(store, "proj_a")
        explicit = neo_mod.build_system_prompt(
            store, "proj_a", learnings_chars=neo_mod.LEARNINGS_CHAR_BUDGET)
    finally:
        store.close()
    assert default == explicit


def test_a_panel_seat_inherits_the_same_bound(jarvis_home):
    """A panel round pays for the learnings block once PER SEAT.

    `panel.build_seat_system_prompt` deliberately mirrors `neo.build_system_prompt`; a
    bound that lived only in the latter would leave the more expensive path unbounded.
    """
    from jarvis import panel

    store = NeoStore()
    try:
        for i in range(60):
            store.add_learning(f"ruling {i}: " + "w" * 900, project="proj_a")
        prompt = panel.build_seat_system_prompt(store, "proj_a", "premise")
    finally:
        store.close()
    assert "older learnings not shown" in prompt
    assert len(prompt) < 30000
