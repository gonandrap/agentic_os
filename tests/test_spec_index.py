"""§4.4 pins for `spec_index`: delegation, one matcher, two documents."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from jarvis import sections, spec_index

REPO_ROOT = Path(__file__).resolve().parents[1]
REAL_SPEC = REPO_ROOT / "docs" / "specs" / "2026-09-24-order-observability.md"

FIXTURE = """# Exporter

Intro prose.

## 1. Data model

Rows are dicts, the union of keys.

## 2. Failure handling

An empty result set is not an error.

### 2.1 Retries

Three, then give up.

## Agent profile

Writes exporters.
"""


@pytest.mark.parametrize("which", ["2", "9", "failure HANDLING", "no such heading"])
def test_section_delegates_to_sections_extract_section(which):
    assert spec_index.section(FIXTURE, which) == sections.extract_section(FIXTURE, which)


def test_section_number_without_a_numbered_heading_is_none():
    assert spec_index.section(FIXTURE, "9") is None


def test_section_name_matches_case_insensitively_as_a_substring():
    got = spec_index.section(FIXTURE, "failure HANDLING")
    assert got is not None and "An empty result set" in got


def test_section_includes_nested_heading_and_stops_at_the_next_peer():
    got = spec_index.section(FIXTURE, "2")
    assert got is not None
    assert "### 2.1 Retries" in got and "Three, then give up." in got
    assert "Agent profile" not in got and "Rows are dicts" not in got


def test_spec_index_defines_no_heading_regex():
    """§4.1: a second heading matcher in this tree is the bug."""
    src = Path(inspect.getsourcefile(spec_index)).read_text()
    assert not re.search(r"re\.compile\([^)]*#", src)
    assert "#{1,6}" not in src


def test_toc_derives_from_sections_heading_re_by_identity(monkeypatch):
    """Identity, so a retyped copy that merely passes equality fails here."""
    seen: list[str] = []
    real = sections.HEADING_RE

    class Spy:
        def finditer(self, text):
            seen.append(text)
            return real.finditer(text)

    monkeypatch.setattr(sections, "HEADING_RE", Spy())
    spec_index.toc(FIXTURE)
    assert seen == [FIXTURE]


def test_toc_over_the_fixture():
    got = spec_index.toc(FIXTURE)
    assert [(s.number, s.level) for s in got] == [
        ("", 1), ("1", 2), ("2", 2), ("2.1", 3), ("", 2),
    ]
    lines = FIXTURE.splitlines()
    for s in got:
        assert lines[s.line - 1].startswith("#") and s.name in lines[s.line - 1]
    data_model = got[1]
    assert data_model.tokens == len(sections.extract_section(FIXTURE, "1")) // 4
    assert data_model.tokens > 0


@pytest.mark.parametrize("source", ["fixture", "real"])
def test_every_toc_ref_resolves_back_to_its_own_heading(source):
    """§4.1: the ref is one `jarvis spec section` accepts, verbatim.

    A `### 2.1` row carrying number `2` lands on `## 2.`, a different section — the
    bug §4.1 names, one row down.
    """
    md = FIXTURE if source == "fixture" else REAL_SPEC.read_text()
    heading_lines = md.splitlines()
    for s in spec_index.toc(md):
        ref = s.number or s.name
        got = spec_index.section(md, ref)
        assert got is not None, ref
        assert got.splitlines()[0] == heading_lines[s.line - 1], ref


def test_toc_over_the_real_committed_spec():
    """§4.4: one tidy fixture is where this suite goes vacuous."""
    md = REAL_SPEC.read_text()
    got = spec_index.toc(md)
    lines = md.splitlines()
    top = [s for s in got if s.level == 2]
    assert [s.number for s in top] == [str(n) for n in range(1, 12)] + [""]
    assert top[-1].name == "Agent profile"
    assert lines[top[-1].line - 1] == "## Agent profile"
    assert lines[top[0].line - 1].startswith("## 1. The evidence")
    for s in top:
        assert s.tokens == len(sections.extract_section(md, s.number or s.name)) // 4
