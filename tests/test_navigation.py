"""The navigation LEAF: one strict classifier and one shell masker.

Spec: §3 of docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md
(`.jarvis/features/fo-b9a3fb06/sections/wo-f4b04708.md`), Neo q1238.

Stdlib only and no fixtures: `src/jarvis/navigation.py` is imported by `hooks.py` on
every Bash PreToolUse, so it imports nothing from `jarvis` and reads nothing from disk.
The fleet reader that used to live here is `tests/test_nav_volume.py`.
"""

import ast

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
