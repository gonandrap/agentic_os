"""Neo's assumption packet carries the cited spec item, not the whole section (wo-ea3c6f0c)"""

from __future__ import annotations

from pathlib import Path

from jarvis import autoreview, sections

ROOT = Path(__file__).resolve().parents[1]
OFFBOARD = "docs/superpowers/specs/2026-10-28-offboard-a-project.md"

SPEC = """## 6. The live fleet

Intro paragraph that settles nothing.

### 6.1 Filter

Filter text.

### 6.2 Five things

Lead-in:

1. **One.** first item
   continues here.
2. **Two.** second item
   - a nested bullet
3. **Three.** third item

Trailing paragraph after the list.

#### 6.2.1 Deeper

Deeper text.

### 6.3 The invariant

Invariant text.
"""


def test_a_cited_item_is_that_item_only():
    got = sections.cited_excerpts(SPEC, "6.2 item 2 needs no guard")
    assert got == ["2. **Two.** second item\n   - a nested bullet"]


def test_a_cited_nested_heading_is_that_subsection():
    assert sections.cited_excerpts(SPEC, "per §6.2.1") == ["#### 6.2.1 Deeper\n\nDeeper text."]


def test_an_item_in_a_subsection_stops_before_the_trailing_paragraph():
    got = sections.cited_excerpts(SPEC, "section 6.2 step 3")
    assert got == ["3. **Three.** third item"]


def test_nothing_cited_here_resolves_nothing():
    assert sections.cited_excerpts(SPEC, "section 7 step 2 owns it; version 2.1.232") == []
    assert sections.cited_excerpts(SPEC, "§6") == []  # the whole section the caller has


def test_a_missing_item_falls_back_to_its_subsection():
    got = sections.cited_excerpts(SPEC, "6.1 item 9")
    assert got == ["### 6.1 Filter\n\nFilter text."]


def test_the_packet_without_a_citation_keeps_the_whole_section():
    spec = {"repo_path": "x.md", "section": "6", "section_text": SPEC}
    block = autoreview._spec_block(spec, 10_000, "no citation at all")
    assert "Intro paragraph" in block and "Invariant text." in block


# The packet that became Neo question #2393: §6 cut at 6,000 chars, right before item 5
ASSUMPTION = (
    "6.2 item 5 (schedule.os_owner fallback) gets no guard here: section 7 step 2 refuses "
    "to offboard the OS-owning project with no override, which is what closes it; sibling "
    "offboard owns that refusal. Also: _supervisor_config is the only daemon "
    "self.catalog.project(name) caller and already falls back to SupervisorConfig on "
    "CatalogError, so no raise-vs-skip change was needed.")
DESCRIPTION = (
    "The hardest piece and the one the feature fails without. You consume "
    "ops.registered_project_paths's include_archived parameter, which `registry` ships; you "
    "do not change its signature. Do not add ops.offboard_project or the CLI verb (sibling "
    "`offboard`), the store deletes (`purge`) or the repo strip (`strip`). "
    "Catalog.without_projects lives in catalog.py, which is a stdlib-only leaf and may not "
    "read a database — the daemon passes the names in. Daemon.catalog stays a plain "
    "attribute; a re-reading property breaks Fleet.reconfigure and the tests that assign it "
    "in memory.")


def test_question_2393_rebuilt_is_small_and_carries_item_5_verbatim():
    doc = (ROOT / OFFBOARD).read_text()
    section = sections.extract_section(doc, "6")
    item5 = sections.list_item(sections.extract_section(section, "6.2"), "5")
    assert item5 and "schedule.os_owner(..., fallback=True)" in item5
    wo = {"id": "wo-b40e0a4b", "title": "Evict an offboarded project from the running fleet",
          "description": DESCRIPTION}
    # Siblings and record at #2393's real lengths: `sibling_line` cuts each at 200 chars
    siblings = [
        {"id": 1, "n": 1, "status": "pending", "content": "INV-NEO-ESCALATION-STALE " * 12},
        {"id": 2, "n": 2, "status": "pending", "content": ASSUMPTION},
        {"id": 3, "n": 3, "status": "pending", "content": "INV-OFFBOARDED-ACTIVITY " * 12},
    ]
    record = "  Q2385 [answered by neo] " + "x" * 430
    packet = autoreview._ruling_question(
        "jarvis_os", wo, siblings[1], siblings, early=True, record=record,
        spec={"repo_path": OFFBOARD, "section": "6", "section_text": section},
        spec_chars=6000)
    assert item5 in packet
    assert "Morpheus runs off a global queue" not in packet
    assert packet.count("The hardest piece") == 1
    assert len(packet) < 3000, len(packet)  # per Q2403: ~2.5 KB is a target, not a wall
