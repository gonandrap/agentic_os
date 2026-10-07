"""The navigation LEAF: one strict classifier and one shell masker.

Spec: §3 of docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md
(`.jarvis/features/fo-b9a3fb06/sections/wo-f4b04708.md`), Neo q1238.

Stdlib only and no fixtures: `src/jarvis/navigation.py` is imported by `hooks.py` on
every Bash PreToolUse, so it imports nothing from `jarvis` and reads nothing from disk.
The fleet reader that used to live here is `tests/test_nav_volume.py`.
"""

import ast
import builtins

import pytest

from jarvis import navigation


# -- navigates_source ------------------------------------------------------------------

def test_a_grep_at_a_py_path_navigates_source():
    assert navigation.navigates_source("grep -n total_for src/pricing.py",
                                       (".py",)) is True
    assert navigation.navigates_source("cat src/jarvis/ops.py", (".py",)) is True
    assert navigation.navigates_source("head -40 a.py", (".py",)) is True
    # ANY statement of a chain or pipeline navigates (§3).
    assert navigation.navigates_source("uv run pytest && cat src/x.py",
                                       (".py",)) is True
    assert navigation.navigates_source("grep -n foo src/x.py | head", (".py",)) is True
    # §3: env assignments are stripped before word 0 is read.
    assert navigation.navigates_source("FOO=1 cat src/x.py", (".py",)) is True


def test_a_recursive_sweep_of_the_tree_navigates_source():
    """§3: a sweep navigates even though no token ends in a configured suffix."""
    assert navigation.navigates_source('grep -rn "def foo" .', (".py",)) is True
    assert navigation.navigates_source("rg foo src", (".py",)) is True
    assert navigation.navigates_source("find . -name Makefile", (".py",)) is True
    assert navigation.navigates_source("sudo find . -name '*.py'", (".py",)) is True


def test_bookkeeping_reads_do_not_navigate_source():
    for command in ("cat tool-log.jsonl",
                    "jq . payload.json",
                    # The trap: it ends in `.py` but word 0 is not in `NAV_COMMANDS`.
                    "uv run pytest tests/test_x.py",
                    "git log --oneline",
                    "jarvis wo show wo-1",
                    "echo hi",
                    "cat README.md",
                    ""):
        assert navigation.navigates_source(command, (".py",)) is False, command


def test_sed_counts_only_with_dash_n():
    assert navigation.navigates_source("sed -n '1,40p' a.py", (".py",)) is True
    assert navigation.navigates_source("sed -n 1,50p src/x.py", (".py",)) is True
    assert navigation.navigates_source("sed -i s/x/y/ a.py", (".py",)) is False


def test_a_py_path_inside_a_quoted_string_does_not_count():
    """Proves the masker is applied and not merely re-exported (§3)."""
    assert navigation.navigates_source('git commit -m "fix pricing.py"',
                                       (".py",)) is False
    assert navigation.navigates_source('echo "cat src/x.py"', (".py",)) is False


def test_the_suffix_set_is_an_argument_not_a_global():
    assert navigation.navigates_source("grep -rn x notes.md", (".py",)) is False
    assert navigation.navigates_source("grep -rn x a.py", (".py",)) is True
    assert navigation.navigates_source("grep -rn x notes.md", (".md",)) is True
    # §3: the command set is an optional third argument, for the catalog-configured one.
    assert navigation.navigates_source("bat src/x.py", (".py",), ("bat",)) is True
    assert navigation.navigates_source("cat src/x.py", (".py",), ("bat",)) is False


def test_a_path_is_a_code_read_only_for_a_configured_suffix():
    assert navigation.navigates_source("cat src/x.py", (".ts",)) is False
    assert navigation.navigates_source("cat src/x.ts", (".ts",)) is True


def test_a_command_too_long_for_params_is_still_classified():
    """`inspection.ParamCaps` truncates at 500/2,000/20,000; the leaf reads the raw
    command, so length changes nothing (§3)."""
    long_command = "grep -n " + "x" * 30_000 + " src/pricing.py"
    assert len(long_command) > 20_000
    assert navigation.navigates_source(long_command, (".py",)) is True
    assert navigation.navigates_source("echo " + "y" * 30_000, (".py",)) is False


# -- is_symbol_call --------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "mcp__serena__find_symbol",
    "mcp__plugin_serena_serena__find_symbol",
    "find_symbol",
    "mcp__plugin_serena_serena__find_referencing_symbols",
    "LSP_document_symbols",
    "LSP",
])
def test_is_symbol_call_accepts_both_serena_prefixes(name):
    assert navigation.is_symbol_call(name) is True


@pytest.mark.parametrize("name", [
    # Text search with a Serena name: counting it as a symbol call is the vacuity trap
    # kn-a397fb52 documents.
    "mcp__serena__search_for_pattern",
    "search_for_pattern",
    "Grep",
    "Glob",
    "Bash",
    "",
])
def test_serena_text_search_is_not_a_symbol_call(name):
    assert navigation.is_symbol_call(name) is False
    assert "search_for_pattern" not in navigation.SYMBOL_TOOLS


def test_the_leaf_names_exactly_the_measured_sets():
    assert navigation.NAV_COMMANDS == ("cat", "head", "sed", "grep", "rg", "find")
    assert navigation.SOURCE_SUFFIXES == (".py",)
    assert navigation.TEXT_SEARCH_TOOLS == ("Grep", "Glob")


# -- one masker, one leaf --------------------------------------------------------------

def test_hooks_re_exports_the_moved_helpers_rather_than_copying_them():
    """Identity, not equality: a copied body passes equality and then drifts
    (kn-7f5f2d0d)."""
    from jarvis import hooks

    assert hooks._mask_shell_text is navigation._mask_shell_text
    assert hooks._statements is navigation._statements


def test_the_leaf_imports_nothing_from_jarvis():
    """`hooks.py` imports it on every Bash PreToolUse, so an import of `catalog` here is
    a per-command cost (§3)."""
    parsed = ast.parse(open(navigation.__file__).read())
    local = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom) and node.level:
            if node.module:
                local.add(node.module)
            else:
                local.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            local.update(a.name.split(".")[0] for a in node.names
                         if a.name.startswith("jarvis"))

    assert local == set()


def test_the_catalog_defaults_are_the_leafs_sets_and_not_a_second_definition():
    from jarvis import catalog

    assert catalog.DEFAULT_NAVIGATION_BASH_COMMANDS is navigation.NAV_COMMANDS
    assert catalog.DEFAULT_NAVIGATION_SYMBOL_TOOLS is navigation.SYMBOL_TOOLS
    assert catalog.DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS is navigation.TEXT_SEARCH_TOOLS
    assert catalog.DEFAULT_NAVIGATION_CODE_SUFFIXES is navigation.SOURCE_SUFFIXES


def test_the_eval_uses_the_shipped_classifier():
    """§7: the LLM nav eval imports this leaf rather than keeping a retyped copy.

    Identity, so a copy that merely passes equality fails here. The eval module imports
    fine without `JARVIS_EVALS_LLM` — the marker only skips.
    """
    from evals.llm import test_navigation_judgment as ev

    assert ev.NAV_COMMANDS is navigation.NAV_COMMANDS
    assert ev.SOURCE_SUFFIXES is navigation.SOURCE_SUFFIXES
    assert "def bash_navigates_code" not in open(ev.__file__).read()


# -- the doc classifiers (§3.1, §3.4) --------------------------------------------------

def _called_names_per_function(source: str) -> list[tuple[str, str]]:
    """Every called NAME paired with the `FunctionDef` enclosing it.

    Enclosure, not reachability: reachability is not decidable from an AST, which is
    `tests/test_remedies.py::test_the_acting_calls_stay_inside_the_handlers`' argument.
    """
    tree = ast.parse(source)
    enclosing: dict[ast.AST, str] = {}

    def walk(node: ast.AST, fn: str) -> None:
        for child in ast.iter_child_nodes(node):
            here = child.name if isinstance(child, ast.FunctionDef) else fn
            enclosing[child] = here
            walk(child, here)

    walk(tree, "")
    # Builtins dropped: the property pinned is which HELPERS a body reaches, and `any`
    # is not one.
    return [(node.func.id, fn) for node, fn in enclosing.items()
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and not hasattr(builtins, node.func.id)]


def test_navigates_source_calls_exactly_what_it_calls_today():
    """§3.4's AST enclosure pin: no branch and no helper was added to the counted body
    (§2.2 — the fleet baseline was measured with it as it stands)."""
    found = _called_names_per_function(open(navigation.__file__).read())
    assert found, "the walk found no calls at all — it would pass on any module"
    assert {name for name, fn in found if fn == "navigates_source"} == {
        "_statements", "_mask_shell_text", "_command_word", "_reads_with_sed",
        "_sweeps_the_tree"}


def test_dumps_doc_never_calls_the_sweep_classifier():
    """§3.1: with `grep`/`rg`/`find` absent from the command set there is no sweep to
    classify, and reaching for it inherits the issue-936 defect."""
    found = _called_names_per_function(open(navigation.__file__).read())
    assert found
    assert "_sweeps_the_tree" not in {name for name, fn in found if fn == "dumps_doc"}
    assert navigation.dumps_doc("find docs -name '*.md'") is False


def test_the_enclosure_pin_would_catch_the_move_it_forbids():
    """A guard nobody has ever seen fail is a guard nobody knows works. The same shape,
    over synthetic source that violates it."""
    found = _called_names_per_function(
        "def dumps_doc(c):\n    return _sweeps_the_tree('grep', [c])\n")
    assert found
    assert "_sweeps_the_tree" in {name for name, fn in found if fn == "dumps_doc"}


def test_dumps_doc_counts_every_dump_and_narrows_only_on_a_keyword():
    """§3.4, both halves: `nav_volume` passes nothing and counts the ranged dump too; the
    hook passes its resolved bar and the same dump falls under it."""
    assert navigation.dumps_doc("sed -n '40,80p' docs/specs/<spec>.md") is True
    assert navigation.dumps_doc("sed -n '40,80p' docs/specs/<spec>.md",
                                limit_lines=200) is False


@pytest.mark.parametrize("command,span", [
    ("sed -n '40,80p' docs/specs/<spec>.md", 41),
    ("sed -n '40p' docs/specs/<spec>.md", 1),
    ("head -40 docs/specs/<spec>.md", 40),
    ("head -n 40 docs/specs/<spec>.md", 40),
    ("cat docs/specs/<spec>.md", None),
    ("head docs/specs/<spec>.md", None),
    # Deliberately conservative: one dump named no range, so the chain is UNRANGED.
    ("cat docs/a.md; sed -n '1,5p' docs/b.md", None),
    ("echo hi", None),
])
def test_dump_span_reads_the_widest_named_range(command, span):
    assert navigation._dump_span(command) == span


def test_the_dump_command_set_is_an_argument_to_the_extractor_too():
    """A catalog-added command the extractor cannot see names NO range, so every ranged
    read of it would be refused at any bar — the false positive this feature must not
    have."""
    assert navigation._dump_span("tail -n 40 docs/a.md", ("tail",)) == 40
    assert navigation._dump_span("tail -n 40 docs/a.md") is None


def test_an_unranged_dump_never_compares_as_zero():
    """§3.1: `None` from `_dump_span` means UNRANGED, which is over any bar."""
    assert navigation.dumps_doc("cat docs/specs/<spec>.md", limit_lines=200) is True
    assert navigation.dumps_doc("head docs/specs/<spec>.md", limit_lines=200) is True


def test_a_md_path_inside_a_quoted_string_does_not_dump():
    assert navigation.dumps_doc('git commit -m "fix README.md"') is False


def test_the_doc_classifiers_are_siblings_and_not_a_widened_source_one():
    """§2.2: a new sibling, and `navigates_source` is not edited or aliased."""
    assert navigation.DOC_SUFFIXES == (".md",)
    assert navigation.DOC_DUMP_COMMANDS == ("cat", "head", "sed")
    assert navigation.DOC_SUFFIXES is not navigation.SOURCE_SUFFIXES
    assert navigation.dumps_doc is not navigation.navigates_source


@pytest.mark.parametrize("path,expected", [
    ("docs/specs/<spec>.md", True),
    (".jarvis/features/fo-1/spec.md", True),
    # §2.3 class 3: the child's own assigned section, at any size.
    (".jarvis/features/fo-1/sections/wo-1.md", False),
    ("notes.md", False),
    ("docs/specs/<spec>.py", False),
    # COMPONENTS, never substrings.
    ("mydocs/specs/<spec>.md", False),
    ("docsy/<spec>.md", False),
    ("", False),
])
def test_is_spec_path_is_three_valued_and_component_wise(path, expected):
    assert navigation.is_spec_path(path) is expected
