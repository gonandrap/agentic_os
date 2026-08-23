"""The five validation seats, as they SHIP.

Every assertion about a seat's prose reads the markdown under `bootstrap.ASSETS /
"validator-seats"`, never a Python constant. The file the runtime loads is the enforcement;
a constant asserted against itself proves that two lines of Python agree.

THE PROSE TESTS EXIST FOR ONE FAILURE. `arbitrate` says `security` and `tester` can block
and `architect` and `maintainer` cannot. A mandate that told the architect it held a veto
would produce a seat that blocks in its own head, writes as if it had stopped the work, and
watches the panel pass anyway — and nothing in the code would look wrong. So the prompt and
the table are asserted against each other here.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jarvis import paths, seats, validation
from jarvis.bootstrap import ASSETS
from jarvis.catalog import ValidationConfig
from jarvis.evidence import EvidencePacket
from jarvis.project_store import VALIDATOR_SEATS, ProjectStore

SEAT_DIR = ASSETS / "validator-seats"
NON_CHAIR = tuple(s for s in VALIDATOR_SEATS if s != "chair")


def text(seat: str) -> str:
    return (SEAT_DIR / f"{seat}.md").read_text()


def packet(**kw) -> EvidencePacket:
    base = dict(
        unit="work_order", subject_id="wo-1", title="Add the thing",
        description="the brief", summary="I added the thing",
        declared="I ran `uv run pytest tests/test_thing.py`", pr_url="",
        base="aaa", head="bbb", stat=" src/thing.py | 2 +-", files=("src/thing.py",),
        diff="--- a/src/thing.py\n+++ b/src/thing.py\n+THE_DIFF_MARKER = 1\n",
        diff_truncated=False, dropped_files=(), diff_sha="sha", children=())
    return EvidencePacket(**{**base, **kw})


@pytest.fixture()
def store(tmp_path):
    s = ProjectStore(tmp_path / "proj")
    yield s
    s.close()


# -- what ships ---------------------------------------------------------------------------


@pytest.mark.parametrize("seat", VALIDATOR_SEATS)
def test_a_seat_ships_as_markdown_with_frontmatter(seat):
    meta, body = seats.parse_definition(text(seat))

    assert meta["name"] == seat, "the roster resolves a seat by its file and its name key"
    assert meta["description"], "a seat with no description is undocumented in the record"
    assert body.strip(), "a seat is its mandate; an empty body is an empty seat"
    # A `tools:` key is meaningful for a subagent and meaningless for a headless call.
    # Absent rather than empty: an empty allowlist would read as a deliberate lockdown.
    assert "tools" not in meta


def test_every_seat_in_the_vocabulary_ships():
    assert validation.shipped_seats() == VALIDATOR_SEATS
    assert {p.stem for p in SEAT_DIR.glob("*.md")} == set(VALIDATOR_SEATS)


def test_the_seats_do_not_ship_in_the_planners_agents_directory():
    """`bootstrap._rebuild` copytrees `assets/agents/` WHOLESALE into every feature-order
    planner's `.claude/agents/`. A validation seat dropped there becomes a bogus subagent
    every planner session can invoke — a `security` reviewer with the planner's tools,
    answering questions nobody asked it."""
    agents = {p.name for p in (ASSETS / "agents").glob("*")}

    assert agents == {"jarvis-architect.md", "jarvis-test-lead.md"}
    assert not (ASSETS / "agents" / "validator-seats").exists()


# -- the veto table, asserted against the prose --------------------------------------------


@pytest.mark.parametrize("seat", validation.VETO_SEATS)
def test_a_veto_seat_is_told_it_holds_one(seat):
    body = text(seat).lower()

    assert "you hold a veto" in body
    assert "blocking" in body, "and the flag that expresses it is named"


@pytest.mark.parametrize("seat", ["architect", "maintainer"])
def test_a_non_veto_seat_is_told_it_holds_none(seat):
    """PAIRED with the row above by the parametrisation itself: the same phrase, negated,
    read out of the same directory. A seat told it can block, by a table that says it
    cannot, is the failure this design lineage exists to prevent."""
    body = text(seat).lower()

    assert "you hold no veto" in body
    assert "cannot block this submission" in body
    assert "you hold a veto" not in body


def test_the_prose_and_the_table_name_the_same_two_seats():
    """The table read out of the mandates, compared with the table in the code. Either one
    moving without the other is what this catches."""
    holders = {s for s in NON_CHAIR if "you hold a veto" in text(s).lower()}

    assert holders == set(validation.VETO_SEATS) == {"security", "tester"}


@pytest.mark.parametrize("seat", NON_CHAIR)
def test_every_non_chair_seat_states_its_strict_json_shape(seat):
    body = text(seat)

    for key in ('"verdict"', '"blocking"', '"reason"', '"asks"'):
        assert key in body
    assert "STRICT JSON" in body


def test_the_chair_states_the_only_two_outcomes_it_may_emit():
    body = text("chair")

    assert '"outcome": "passed"' in body and '"outcome": "rejected"' in body
    assert "escalate" not in body.lower().split("# output")[-1], (
        "the chair's schema is outcome/reason; an `escalate` key belongs to Neo's chair")


@pytest.mark.parametrize("seat", VALIDATOR_SEATS)
def test_every_seat_is_told_what_the_packet_contains(seat):
    """Including the three fields that let a seat catch a claim the diff does not support:
    the file list (never truncated), the stat, and the announced truncation."""
    body = text(seat)

    assert "file list is never truncated" in body
    assert "truncated diff is announced" in body or "TRUNCATED" in body
    assert "diff --stat" in body


@pytest.mark.parametrize("seat", VALIDATOR_SEATS)
def test_every_seat_handles_a_feature_order_packet(seat):
    """Another work order in this feature sends packets with `unit="feature"`, whose diff
    is integrated merged work and whose `children` say what each child claimed. A seat
    that has never been told that reads a five-child feature as one enormous work order."""
    body = text(seat)

    assert "FEATURE ORDER" in body
    assert "child" in body


@pytest.mark.parametrize("seat", NON_CHAIR)
def test_a_seat_is_told_its_words_reach_the_submitter_and_name_no_seat(seat):
    """A forced rejection delivers the seat's own reason VERBATIM and unattributed. A seat
    that wrote "the security seat found…" would narrate a panel the submitter is never
    told exists."""
    body = text(seat).lower()

    assert "second person" in body
    assert "never mention a panel, a seat or a vote" in body


def test_the_seats_are_told_to_cite_a_knowledge_base_id():
    """The `kn-` id is stored verbatim in the opinion row, so a rejection can be traced
    back to the standing instruction that caused it."""
    for seat in NON_CHAIR:
        assert "kn-" in text(seat)


# -- how the seats are run -----------------------------------------------------------------


def test_the_seats_run_at_jarvis_home_with_no_tools_and_still_see_the_diff(
        store, jarvis_home, fake_claude):
    """PAIRED IN ONE TEST, and the pairing is the point. "The seat cannot read the repo"
    is satisfied perfectly by a seat that was handed nothing at all — so the same test
    that proves `--tools ""` and `cwd == $JARVIS_HOME` also proves the packet's diff text
    reached the prompt.

    A headless call carries no settings file, so what a tooled seat could reach would
    depend on the user's global configuration rather than on anything Jarvis controls.
    """
    wo = store.create_work_order("t")
    round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="f")

    validation.decide(store, round_row, packet(), ValidationConfig(enabled=True))

    calls = [c for c in fake_claude.calls if "-p" in c["argv"]]
    assert len(calls) == 5
    for call in calls:
        argv = call["argv"]
        assert argv[argv.index("--tools") + 1] == "", "a seat judges the packet only"
        assert Path(call["cwd"]) == paths.ensure_home()
    prompts = [c["argv"][c["argv"].index("-p") + 1] for c in calls]
    assert all("THE_DIFF_MARKER" in p for p in prompts), (
        "a seat that sees nothing passes the tools assertion and reviews nothing")


def test_the_packet_prompt_carries_what_a_seat_needs_to_catch_an_unsupported_claim():
    prompt = validation.build_packet_prompt(packet(
        files=("src/thing.py", "docs/x.md"), declared="I ran the tests"))

    assert "src/thing.py" in prompt and "docs/x.md" in prompt
    assert "I ran the tests" in prompt
    assert "NEVER truncated" in prompt


def test_a_truncated_diff_is_announced_in_the_prompt_with_the_files_it_dropped():
    """A silently truncated diff read as complete is how a security seat passes the file
    it never opened."""
    prompt = validation.build_packet_prompt(packet(
        diff_truncated=True, dropped_files=("src/big.py",),
        files=("src/thing.py", "src/big.py")))

    assert "TRUNCATED" in prompt
    assert "src/big.py" in prompt

    assert "TRUNCATED" not in validation.build_packet_prompt(packet())


def test_a_feature_packet_shows_what_each_child_claimed():
    prompt = validation.build_packet_prompt(packet(
        unit="feature", subject_id="fo-1",
        children=({"id": "wo-a", "title": "first half", "summary": "did the first half",
                   "declared": "ran its tests"},)))

    assert "feature order fo-1" in prompt
    assert "wo-a" in prompt and "did the first half" in prompt and "ran its tests" in prompt


def test_the_first_line_of_a_seat_prompt_is_its_machine_readable_header():
    """And it is A DIFFERENT LITERAL from Neo's. `chair` is a legal seat name in both
    rosters, so a shared header would leave nothing able to tell the two calls apart —
    including the test fake, which would answer a validation chair with a Neo verdict."""
    from jarvis import panel

    first = validation.build_seat_system_prompt("chair", "proj_a").splitlines()[0]

    assert first == "# Jarvis validation seat: chair"
    assert validation.SEAT_HEADER != panel.SEAT_HEADER


# -- the layer this module sits at ------------------------------------------------------------


def _imports(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = "." * node.level + (node.module or "")
            found.update([base] if node.module else [base + a.name for a in node.names])
    return found


def test_validation_imports_neither_neo_nor_the_bus():
    """Walks the AST, FUNCTION BODIES INCLUDED — the house style is a lazy import inside
    the function that needs it, and a `sys.modules` check would miss every one.

    `neo_store` is the sharpest of the four: its `learnings` table is one OS-wide ledger
    keyed by a seat vocabulary that also contains `chair`, so reading it here would let a
    ruling the user taught NEO'S chair steer a validation verdict. `bus` is the other
    half of the rule: the round machine posts, and the panel only returns a value.
    """
    found = _imports(Path(validation.__file__))

    for forbidden in (".neo", ".neo_store", ".panel", ".bus"):
        assert forbidden not in found, f"validation.py must never import {forbidden}"
    assert ".seats" in found or ".seats" in str(found)


def test_the_two_vocabularies_intersect_in_exactly_the_chair():
    """`neo_store.SEATS` must NOT gain the validator names: a catalog could then seat
    `security` on Neo's panel, where no definition ships for it and nothing arbitrates it.
    """
    from jarvis.catalog import CatalogError, parse_catalog
    from jarvis.neo_store import SEATS

    assert set(SEATS) & set(VALIDATOR_SEATS) == {"chair"}

    with pytest.raises(CatalogError, match="security"):
        parse_catalog({"os": {"neo": {"panel": {"roster": ["security", "chair"]}}},
                       "projects": []})


def test_a_neo_learning_taught_to_the_chair_never_reaches_the_validators_chair(
        store, jarvis_home):
    """THE COLLISION THIS DESIGN EXISTS TO AVOID, asserted directly.

    `jarvis neo review` distils a learning from the user reviewing NEO'S ANSWERS, and it
    is stored against a seat name. `chair` is a legal seat name in both rosters. If the
    validation seats read that ledger, a ruling about how Neo should answer a gate
    question would silently start deciding whether a diff was adequately tested.
    """
    from jarvis.neo_store import NeoStore

    neo_store = NeoStore()
    neo_store.add_learning("always dismiss a grep that merely names a release script",
                           project="proj_a", seat="chair")
    neo_store.add_learning("a learning every Neo seat sees", project="proj_a")
    neo_store.close()

    prompt = validation.build_seat_system_prompt("chair", "proj_a")

    assert "merely names a release script" not in prompt
    assert "a learning every Neo seat sees" not in prompt, (
        "the unscoped ones are the ones a shared ledger would leak first")
    assert prompt == "\n".join(["# Jarvis validation seat: chair", "",
                                validation.definition("chair")[1]])


def test_the_seat_prompt_carries_the_projects_knowledge_and_not_neos_learnings(
        store, jarvis_home):
    """PAIRED IN ONE TEST, because "Neo's text is absent" is satisfied perfectly by a
    prompt with no knowledge section at all — which would be a panel that cannot learn
    the user's standards, the thing this section exists to provide."""
    from jarvis.central_store import CentralStore
    from jarvis.neo_store import NeoStore

    central = CentralStore()
    kn = central.add_knowledge("this project requires an eval for any change to a prompt",
                               project="proj_a", topic="testing")
    brief = central.knowledge_brief("proj_a")
    central.close()

    neo_store = NeoStore()
    neo_store.add_learning("NEO_LEARNING_MARKER: answer gate questions tersely",
                           project="proj_a")
    neo_store.close()

    prompt = validation.build_seat_system_prompt("tester", "proj_a", brief)

    assert "requires an eval for any change to a prompt" in prompt
    assert kn["id"] in prompt, "the id is cited in an opinion, so it must be in the prompt"
    assert "NEO_LEARNING_MARKER" not in prompt


def test_a_seat_is_never_pointed_at_a_command_it_cannot_run(store, jarvis_home):
    """The seats have no tools. The worker prompt's knowledge block tells its reader to
    run `jarvis learn show <id>`; pointing a tool-less seat at that is pointing it at a
    resource it cannot reach, which is the one thing the OS's prompt rules forbid."""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    central.add_knowledge("a standing rule", project="proj_a")
    brief = central.knowledge_brief("proj_a")
    central.close()

    prompt = validation.build_seat_system_prompt("security", "proj_a", brief)

    assert "a standing rule" in prompt
    assert "jarvis learn" not in prompt
    assert "You have no tools" in prompt


def test_a_project_with_an_empty_knowledge_base_gets_no_section_at_all(store, jarvis_home):
    """kn-97c41de7, applied here: a prompt never points at a resource that may not exist.
    An empty index is a heading promising standing instructions that are not there."""
    from jarvis.central_store import CentralStore

    central = CentralStore()
    brief = central.knowledge_brief("proj_a")
    central.close()

    prompt = validation.build_seat_system_prompt("tester", "proj_a", brief)

    assert "# The project's standing instructions" not in prompt
    assert prompt == validation.build_seat_system_prompt("tester", "proj_a"), (
        "an empty base and no base are the same prompt")


def test_a_seat_prompt_is_byte_stable_for_the_same_project(store, jarvis_home):
    """A round is five calls and the prefix is what they cache on."""
    a = validation.build_seat_system_prompt("tester", "proj_a")
    b = validation.build_seat_system_prompt("tester", "proj_a")

    assert a == b
    assert json.dumps(a) == json.dumps(b)
