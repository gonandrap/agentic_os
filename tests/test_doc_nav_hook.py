"""A whole-file or oversized read of a SPEC is refused, naming `jarvis spec`.

§5 of docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md. The bar is a catalog setting
(`worker.doc_read_limit_lines`, fallback 200) and the switch is `worker.doc_nav_hook`,
DEFAULT OFF — nothing here changes the behaviour of a running worker.

§5.4 is a MATRIX: seven inputs by two switch states. With the key off or absent
`doc_nav_decision` returns None for ALL seven, including the two it denies when on, so
the pair that flips is only visible as fourteen cells.

§5.2's MUST NOT: a small targeted dump (`head -40`, `sed -n '40,80p'`) and a markdown
text search (`grep -rn`) pass. A blanket Bash refusal fails the section.
"""

from __future__ import annotations

import ast
import inspect
import json

import pytest

from jarvis import catalog, hooks
from jarvis.catalog import CatalogError, ProjectSpec, WorkerDefaults
from jarvis.dispatch import _write_worker_settings

SPEC = "docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md"
SECTION = ".jarvis/features/fo-x/sections/wo-y.md"

ON = {"JARVIS_DOC_NAV_HOOK": "on", "JARVIS_WO_ID": "wo-1"}
OFF_STATES = [
    {"JARVIS_WO_ID": "wo-1"},
    {"JARVIS_DOC_NAV_HOOK": "off", "JARVIS_WO_ID": "wo-1"},
    {},
]


@pytest.fixture
def repo(tmp_path):
    """A managed project: `.jarvis/` is how `hooks.find_project_root` resolves one
    (kn-6c033672). NO `.serena/project.yml` on purpose — the doc arms are not gated on a
    symbol index, because the mitigation they name is a `jarvis` command (§5.2)."""
    (tmp_path / ".jarvis").mkdir()
    return tmp_path


def _bash(command, cwd) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


def _read(path, cwd, **tool_input) -> dict:
    return {"tool_name": "Read",
            "tool_input": {"file_path": path, **tool_input},
            "cwd": str(cwd)}


def _reason(decision) -> str:
    return decision["hookSpecificOutput"]["permissionDecisionReason"]


def _denied(decision) -> bool:
    return (decision or {}).get(
        "hookSpecificOutput", {}).get("permissionDecision") == "deny"


# -- §5.4: seven inputs by two switch states -------------------------------------------

#: (id, payload builder, verdict with the key ON). The three PASS rows on the Bash side
#: are the ones that keep this feature inside its MUST NOT.
MATRIX = [
    ("read-no-limit", lambda r: _read(SPEC, r), True),
    ("read-limit-40", lambda r: _read(SPEC, r, limit=40), False),
    ("sed-1-2000", lambda r: _bash(f"sed -n '1,2000p' {SPEC}", r), True),
    ("sed-40-80", lambda r: _bash(f"sed -n '40,80p' {SPEC}", r), False),
    ("head-40", lambda r: _bash(f"head -40 {SPEC}", r), False),
    ("grep-docs", lambda r: _bash('grep -rn "words" docs/', r), False),
    ("read-own-section", lambda r: _read(SECTION, r), False),
]


@pytest.mark.parametrize("build,denies", [(b, d) for _, b, d in MATRIX],
                         ids=[i for i, _, _ in MATRIX])
def test_the_matrix_with_the_key_on(repo, build, denies):
    assert _denied(hooks.doc_nav_decision(build(repo), ON)) is denies


@pytest.mark.parametrize("env", OFF_STATES, ids=["absent", "off", "no-wo-id"])
@pytest.mark.parametrize("build", [b for _, b, _ in MATRIX],
                         ids=[i for i, _, _ in MATRIX])
def test_the_matrix_with_the_key_off(repo, build, env):
    """All seven return None, including the two the key denies when on."""
    assert hooks.doc_nav_decision(build(repo), env) is None


@pytest.mark.parametrize("command", [
    f"cat {SPEC}",
    f"head {SPEC}",
], ids=["cat", "bare-head"])
def test_an_unranged_dump_is_refused(repo, command):
    """§5.2: `None` from `_dump_span` means UNRANGED, which is over any bar."""
    assert _denied(hooks.doc_nav_decision(_bash(command, repo), ON))


def test_a_read_over_the_bar_is_refused_and_one_at_it_is_not(repo):
    """The bar governs the `Read` arm too, at its fallback 200."""
    assert _denied(hooks.doc_nav_decision(_read(SPEC, repo, limit=2000), ON))
    assert hooks.doc_nav_decision(_read(SPEC, repo, limit=200), ON) is None


def test_a_dump_of_a_non_spec_markdown_file_is_untouched(repo):
    """`cat README.md` names a `.md` that is not a spec, so every named path must be
    `is_spec_path` before the refusal fires."""
    assert hooks.doc_nav_decision(_bash("cat README.md", repo), ON) is None
    assert hooks.doc_nav_decision(
        _bash(f"cat {SPEC} README.md", repo), ON) is None


def test_the_deny_message_names_the_three_spec_commands(repo):
    """The refusal IS the mitigation (§4.3), so it has to be actionable alone."""
    reason = _reason(hooks.doc_nav_decision(_read(SPEC, repo), ON))

    assert "jarvis spec toc" in reason
    assert "jarvis spec section" in reason
    assert "jarvis spec search" in reason


# -- §5.4 sixth proof: the bar is RESOLVED, not hardcoded -------------------------------


def test_a_project_setting_the_bar_is_honoured_by_both_arms(repo):
    """A number resolved from a project's catalog and one hardcoded in `hooks.py` are
    indistinguishable at the fallback. 80 is between the project's 50 and 200, so the
    verdict FLIPS with the setting — on `Read` and on `Bash`, one shared bar."""
    at_50 = {**ON, "JARVIS_DOC_READ_LIMIT_LINES": "50"}
    at_200 = {**ON, "JARVIS_DOC_READ_LIMIT_LINES": "200"}

    assert _denied(hooks.doc_nav_decision(_read(SPEC, repo, limit=80), at_50))
    assert hooks.doc_nav_decision(_read(SPEC, repo, limit=80), at_200) is None

    ranged = _bash(f"sed -n '1,80p' {SPEC}", repo)
    assert _denied(hooks.doc_nav_decision(ranged, at_50))
    assert hooks.doc_nav_decision(ranged, at_200) is None


def test_the_catalog_carries_the_bar_fleet_wide_and_per_project():
    from jarvis.catalog import DEFAULT_WORKER_DOC_READ_LIMIT_LINES

    assert DEFAULT_WORKER_DOC_READ_LIMIT_LINES == 200

    cat = catalog.parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.default_doc_read_limit_lines == 200
    assert cat.projects[0].worker.doc_read_limit_lines == 200

    cat = catalog.parse_catalog({
        "os": {"defaults": {"doc_read_limit_lines": 120}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "worker": {"doc_read_limit_lines": 50}},
        ],
    })
    assert cat.os.default_doc_read_limit_lines == 120
    assert cat.projects[0].worker.doc_read_limit_lines == 120    # inherits the fleet
    assert cat.projects[1].worker.doc_read_limit_lines == 50


@pytest.mark.parametrize("raw", [
    {"projects": [{"name": "a", "path": "/x", "worker": {"doc_read_limit_lines": "abc"}}]},
    {"projects": [{"name": "a", "path": "/x", "worker": {"doc_read_limit_lines": 0}}]},
    {"projects": [{"name": "a", "path": "/x", "worker": {"doc_read_limit_lines": -5}}]},
    {"os": {"defaults": {"doc_read_limit_lines": "abc"}}},
    {"os": {"defaults": {"doc_read_limit_lines": 0}}},
], ids=["project-abc", "project-zero", "project-negative", "os-abc", "os-zero"])
def test_a_bad_bar_is_refused_loudly_naming_the_key(raw):
    """The CATALOG is where a bad value is rejected (the hook absorbs one quietly)."""
    with pytest.raises(CatalogError) as e:
        catalog.parse_catalog(raw)

    assert "doc_read_limit_lines" in str(e.value)


def test_the_switch_defaults_off_and_overrides_per_project():
    from jarvis.catalog import DEFAULT_WORKER_DOC_NAV_HOOK, VALID_DOC_NAV_HOOK

    assert DEFAULT_WORKER_DOC_NAV_HOOK == "off"
    assert VALID_DOC_NAV_HOOK == ("off", "on")

    cat = catalog.parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.default_doc_nav_hook == "off"
    assert cat.projects[0].worker.doc_nav_hook == "off"

    cat = catalog.parse_catalog({
        "os": {"defaults": {"doc_nav_hook": "on"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "worker": {"doc_nav_hook": "off"}},
        ],
    })
    assert cat.projects[0].worker.doc_nav_hook == "on"
    assert cat.projects[1].worker.doc_nav_hook == "off"


@pytest.mark.parametrize("raw", [
    {"projects": [{"name": "a", "path": "/x", "worker": {"doc_nav_hook": "true"}}]},
    {"os": {"defaults": {"doc_nav_hook": "yes"}}},
], ids=["project", "os-defaults"])
def test_an_invalid_switch_names_the_key_and_the_valid_values(raw):
    with pytest.raises(CatalogError) as e:
        catalog.parse_catalog(raw)

    assert "doc_nav_hook" in str(e.value)
    for value in ("off", "on"):
        assert value in str(e.value)


def _worker_env(project, worker=None, wo_id="wo-doc01") -> dict:
    spec = ProjectSpec(name="proj_a", path=project, description="",
                       worker=worker or WorkerDefaults())
    return json.loads(
        _write_worker_settings(spec, {"id": wo_id, "title": "t"}).read_text())["env"]


def test_the_key_ships_off(project, jarvis_home):
    """The off literal Jarvis actually writes into a worker's settings file. The single
    best anti-flip pin (§5.5)."""
    assert _worker_env(project)["JARVIS_DOC_NAV_HOOK"] == "off"


def test_the_resolved_bar_reaches_the_worker_as_a_string(project, jarvis_home):
    """Claude Code's settings `env` is a `Record<string,string>`."""
    assert _worker_env(project)["JARVIS_DOC_READ_LIMIT_LINES"] == "200"
    assert _worker_env(project, WorkerDefaults(doc_read_limit_lines=50),
                       wo_id="wo-doc02")["JARVIS_DOC_READ_LIMIT_LINES"] == "50"


def test_the_config_effect_table_names_both_keys():
    from jarvis.ops import APPLY_RULES

    effects = dict(APPLY_RULES)
    assert effects["*.doc_nav_hook"] == "next-dispatch"
    assert effects["*.doc_read_limit_lines"] == "next-dispatch"


# -- §5.5: reachability, through `preflight_decision` and never the arm alone -----------


def test_the_bash_arm_is_reached_before_the_jarvis_auto_allow(repo):
    """`is_jarvis_command_chain` waves every `cd … && jarvis …` chain through, so an arm
    placed after it passes its unit test and does nothing in production."""
    decision = hooks.preflight_decision(
        _bash(f"cd {repo} && sed -n '1,2000p' {SPEC}", repo), ON)

    assert _denied(decision)
    assert "jarvis spec toc" in _reason(decision)


def test_the_read_branch_is_wired_into_preflight(repo):
    """There is no `Read` branch in `preflight_decision` today, so a unit test on the
    decision function alone is green with the branch never wired in."""
    assert _denied(hooks.preflight_decision(_read(SPEC, repo), ON))
    assert hooks.preflight_decision(_read(SPEC, repo, limit=40), ON) is None


def test_the_investigator_refusal_wins_over_a_navigation_one(repo):
    """Position: the doc arm sits after `investigator_bash_decision`, so an
    investigator's mutating command gets the investigator's own message."""
    env = {**ON, hooks.WO_KIND_ENV: "investigator"}
    decision = hooks.preflight_decision(
        _bash(f"jarvis wo send wo-2 hi && cat {SPEC}", repo), env)

    assert _denied(decision)
    assert "jarvis spec toc" not in _reason(decision)


@pytest.mark.parametrize("value", ["abc", "", "0", "-5", None],
                         ids=["abc", "empty", "zero", "negative", "absent"])
def test_a_malformed_bar_falls_back_to_200_and_never_raises(repo, value):
    """A hook that raises takes every tool call in the session with it. A `"0"` that
    resolved as the bar would refuse every targeted read in the fleet."""
    env = dict(ON)
    if value is not None:
        env["JARVIS_DOC_READ_LIMIT_LINES"] = value

    assert hooks.preflight_decision(_read(SPEC, repo, limit=40), env) is None
    assert _denied(hooks.preflight_decision(_read(SPEC, repo, limit=2000), env))
    assert hooks.preflight_decision(_bash(f"head -40 {SPEC}", repo), env) is None


# -- §5.5: AST enclosures ---------------------------------------------------------------


def _body(name):
    tree = ast.parse(inspect.getsource(hooks))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in hooks")


def _called_names(node) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.id if isinstance(f, ast.Name) else getattr(f, "attr", ""))
    return out


def test_the_decision_calls_the_shared_predicates_and_parses_no_range():
    """`dumps_doc` OWNS the range extraction via `navigation._dump_span`. A second
    extractor here would make the counter and the refusal disagree about 40 lines."""
    node = _body("doc_nav_decision")
    called = _called_names(node)

    assert "is_spec_path" in called
    assert "dumps_doc" in called
    for forbidden in ("startswith", "compile", "split", "findall", "search", "match"):
        assert forbidden not in called, forbidden
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert "re" not in names
    # The docstring CITES the spec path, so the literals are read off the CODE.
    code = [s for s in node.body if not (isinstance(s, ast.Expr)
                                         and isinstance(s.value, ast.Constant))]
    literals = {n.value for s in code for n in ast.walk(s)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not any("docs" in s for s in literals), literals
    assert "p" not in literals and "," not in literals, literals


@pytest.mark.parametrize("name", ["doc_nav_decision", "py_nav_decision"])
def test_a_navigation_decision_never_allows(name):
    """No `_allow` branch, EVER: it denies or it returns None, so it can hand out
    nothing a gate would have caught. Today that contract is only a docstring claim."""
    assert "_allow" not in _called_names(_body(name))


def test_hooks_imports_neither_spec_index_nor_catalog():
    """`hooks` runs on EVERY tool call of every managed worker, so an import here is a
    fleet-wide per-command cost. §4.3's three command strings are literals instead."""
    tree = ast.parse(inspect.getsource(hooks))
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree)
                 if isinstance(n, ast.Import) for a in n.names}

    assert not any((m or "").endswith("spec_index") for m in imported), imported
    assert not any((m or "").endswith("catalog") for m in imported), imported
