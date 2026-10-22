"""No test reads the real clock; exempt only via ALLOWED or a function-scoped `# clock-exempt` marker (docs/superpowers/specs/2026-10-15-no-test-reads-the-real-clock.md)"""

from __future__ import annotations

import ast
import io
import re
import subprocess
import tokenize
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Class 2 = rows back-dated with `time.time()
_BACKDATED = "class 2 — rows back-dated or stamped with time.time(); the code under test reads time.time() itself and takes no now="
_SPIN = "class 3 — monotonic-deadline / time.sleep spin-waits on real daemon threads and subprocesses"
_BOTH = (
    "class 2 — rows back-dated or stamped with time.time(), no now= on the code under "
    "test; also class 3 — spin-waits on real daemon threads and subprocesses"
)
# Section 0.8a: the one spelling of class 4 in this repo
_KERNEL = (
    "class 4 — the instant compared against is a stamp written by the kernel or by a "
    "subprocess, which no patched jarvis.db.now reaches"
)

ALLOWED: dict[str, str] = {
    "tests/test_budget.py": _SPIN,
    "tests/test_feature_validation.py": _BOTH,
    "tests/test_usage_window_is_account_wide.py": _SPIN,
    "tests/test_validation_usage_limit.py": _SPIN,
}

# The calls that read the real clock, as dotted names after import resolution
REAL_CLOCK_READS = frozenset(
    {
        "time.time",
        "time.monotonic",
        "time.sleep",
        "datetime.now",
        "datetime.utcnow",
        "date.today",
    }
)

# One or two digits, an optional `:MM`
_TIME_OF_DAY = re.compile(
    r"\d{1,2}(?::\d{2})?\s*(?:[ap]m(?:\s*\([^()]+\))?|\([^()]+\))",
    re.IGNORECASE,
)

_CLASS_REASON = re.compile(r"^class (2|3|4) — .+")

# Markers are per-function: scope re-derived from the AST, nothing keyed by a line
_MARKER = re.compile(r"#\s*clock-exempt:\s*(\S.*?)\s*$")
_WHOLE_FILE = 10**9  # a module-scope marker's end line
_PLACEMENT = (
    "put the marker inside the function it exempts, or at the top of the file above the "
    "first def/class when every read in the file is exempt"
)

_REMEDY = (
    'state the reset as a duration — reset="in 5h" — never a time of day; '
    "see kn-898350cc"
)


# --------------------------------------------------------------------------- walk


# src/jarvis/testing.py is outside this scan — its FAKE_CLAUDE runs in another process, a loophole
def _tracked_test_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        name for name in out.split("\0") if name.endswith(".py") and name.strip()
    )


def _texts() -> dict[str, str]:
    """Tracked test files, as source text — comments are not in the AST"""
    return {
        rel: (REPO_ROOT / rel).read_text(encoding="utf-8")
        for rel in _tracked_test_files()
    }


def _sources() -> dict[str, ast.Module]:
    """Tracked test files, parsed"""
    trees: dict[str, ast.Module] = {}
    for rel in _tracked_test_files():
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        try:
            trees[rel] = ast.parse(text, filename=rel)
        except SyntaxError as exc:  # pragma: no cover - a parse failure is the message
            raise AssertionError(
                f"{rel} does not parse: {exc}. A tracked test file that cannot be "
                "parsed is a defect in its own right."
            ) from exc
    return trees


# ------------------------------------------------------------- dotted-name helpers


def _dotted(node: ast.AST) -> str | None:
    """`a.b.c` -> "a.b.c"; anything else (subscripts, calls, literals) -> None"""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if not isinstance(cur, ast.Name):
        return None
    parts.append(cur.id)
    return ".".join(reversed(parts))


def _import_table(tree: ast.Module) -> dict[str, str]:
    """Local name -> dotted canonical name, from this file's own imports"""
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    table[alias.asname] = alias.name
                else:
                    table[alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                table[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
    return table


def _resolved_call_target(node: ast.Call, table: dict[str, str]) -> str | None:
    dotted = _dotted(node.func)
    if dotted is None:
        return None
    parts = dotted.split(".")
    root = table.get(parts[0])
    if root is None:
        return None  # not reached through an import in this file
    return ".".join([root, *parts[1:]])


def _is_real_clock_read(resolved: str) -> bool:
    if resolved in REAL_CLOCK_READS:
        return True
    parts = resolved.split(".")
    # `datetime.datetime.now` / `datetime.date.today`: the stdlib class sits between
    return (
        len(parts) > 2
        and parts[0] in {"time", "datetime"}
        and ".".join(parts[-2:]) in REAL_CLOCK_READS
    )


def real_clock_reads(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, dotted name) for every real-clock read in one parsed file"""
    table = _import_table(tree)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        resolved = _resolved_call_target(node, table)
        if resolved is not None and _is_real_clock_read(resolved):
            found.append((node.lineno, resolved))
    return sorted(found)


# ------------------------------------------------------------------ the markers


@dataclass(frozen=True)
class Marker:
    """One `# clock-exempt:` comment, with the scope its position gives it"""

    rel: str
    line: int
    reason: str
    start: int
    end: int
    module_scope: bool
    placed_well: bool

    @property
    def where(self) -> str:
        return f"{self.rel}:{self.line}"


def _marker_comments(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):  # pragma: no cover - parse rule fires
        return found
    for token in tokens:
        if token.type != tokenize.COMMENT:
            continue
        match = _MARKER.search(token.string)
        if match:
            found.append((token.start[0], match.group(1)))
    return found


def _enclosing_function(tree: ast.Module, line: int) -> ast.AST | None:
    """The innermost def/async def whose body the line falls in"""
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= line <= (node.end_lineno or node.lineno):
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def _first_definition_line(tree: ast.Module) -> int | None:
    lines = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    return min(lines) if lines else None


def markers(rel: str, text: str) -> list[Marker]:
    """Every marker in one file, scoped from the AST — never from a stored line number"""
    tree = ast.parse(text, filename=rel)
    first_def = _first_definition_line(tree)
    out: list[Marker] = []
    for line, reason in _marker_comments(text):
        fn = _enclosing_function(tree, line)
        if fn is not None:
            out.append(Marker(rel, line, reason, fn.lineno,
                              fn.end_lineno or fn.lineno, False, True))
        else:
            above_first_def = first_def is None or line < first_def
            out.append(Marker(rel, line, reason, 1, _WHOLE_FILE, True, above_first_def))
    return out


def marker_scopes(files: dict[str, str]) -> dict[str, list[Marker]]:
    """Well-placed markers per file; a stranded one exempts nothing"""
    return {
        rel: [m for m in markers(rel, text) if m.placed_well]
        for rel, text in files.items()
    }


def _covered(scoped: list[Marker], line: int) -> bool:
    return any(m.start <= line <= m.end for m in scoped)


def marker_reason_offenders(files: dict[str, str]) -> list[str]:
    """Markers whose reason names no class"""
    return [
        f"{m.where} {m.reason!r} — a reason must match '^class (2|3|4) — .+'"
        for rel, text in sorted(files.items())
        for m in markers(rel, text)
        if not _CLASS_REASON.match(m.reason)
    ]


def marker_class_two_offenders(files: dict[str, str]) -> list[str]:
    """Back-dating is never an exemption"""
    return [
        f"{m.where} claims class 2 — back-dating is never exempt; convert the site to "
        "db.now() per spec section 0.5"
        for rel, text in sorted(files.items())
        for m in markers(rel, text)
        if m.reason.startswith("class 2")
    ]


def misplaced_marker_offenders(files: dict[str, str]) -> list[str]:
    """Module-level markers below the first def/class: a whole-file claim, hidden"""
    return [
        f"{m.where} sits at module level below the first def/class — {_PLACEMENT}"
        for rel, text in sorted(files.items())
        for m in markers(rel, text)
        if m.module_scope and not m.placed_well
    ]


def uncovered_marker_offenders(files: dict[str, str]) -> list[str]:
    """Markers over no real-clock read — the marker's own stale-entry rule"""
    out: list[str] = []
    for rel, text in sorted(files.items()):
        reads = [line for line, _ in real_clock_reads(ast.parse(text, filename=rel))]
        for m in markers(rel, text):
            if not m.placed_well:  # a stranded marker fails its own rule instead
                continue
            if not any(m.start <= line <= m.end for line in reads):
                out.append(
                    f"{m.where} {m.reason!r} covers no real-clock read — delete it, "
                    "markers are shrink-only and cannot go stale"
                )
    return out


def duplicate_marker_reasons(files: dict[str, str]) -> list[str]:
    """One unique reason per exempt function: a duplicated reason is a generic one"""
    seen: dict[str, list[str]] = {}
    for rel, text in sorted(files.items()):
        for m in markers(rel, text):
            seen.setdefault(m.reason, []).append(m.where)
    return [
        f"{reason!r} is used at {', '.join(places)} — give each its own reason"
        for reason, places in sorted(seen.items())
        if len(places) > 1
    ]


def redundant_allowed_entries(
    allowed: dict[str, str], files: dict[str, str]
) -> list[str]:
    """ALLOWED files whose every read a marker already carries"""
    out: list[str] = []
    for rel in sorted(allowed):
        text = files.get(rel)
        if text is None:
            continue
        reads = real_clock_reads(ast.parse(text, filename=rel))
        scoped = marker_scopes({rel: text})[rel]
        if reads and all(_covered(scoped, line) for line, _ in reads):
            out.append(rel)
    return out


# ------------------------------------------------------------------- assertion A


def _test_functions(
    tree: ast.Module,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]


def _parameter_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    names = [arg.arg for arg in [*a.posonlyargs, *a.args, *a.kwonlyargs]]
    for extra in (a.vararg, a.kwarg):
        if extra is not None:
            names.append(extra.arg)
    return names


def _is_db_now_chain(node: ast.AST) -> bool:
    dotted = _dotted(node) or ""
    return dotted.endswith("jarvis.db.now") or dotted == "db.now"


def fakes_db_now(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Does this test function fake `jarvis.db.now`?"""
    if "clock" in _parameter_names(fn):
        return True

    subtree: list[ast.AST] = []
    for node in [*fn.decorator_list, *fn.body]:
        subtree.extend(ast.walk(node))

    for node in subtree:
        if isinstance(node, ast.Call):
            func = _dotted(node.func) or ""
            # A `_MovedNow`-style context manager
            if func.split(".")[-1].endswith("MovedNow"):
                return True
            if func == "setattr" or func.endswith(".setattr"):
                consts = [
                    arg.value
                    for arg in node.args
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                ]
                if "jarvis.db.now" in consts:
                    return True
                if "jarvis.db" in consts and "now" in consts:
                    return True
                if (
                    len(node.args) >= 2
                    and (_dotted(node.args[0]) or "").endswith(("jarvis.db", "db"))
                    and "now" in consts
                ):
                    return True
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(_is_db_now_chain(target) for target in targets):
                return True
    return False


def wall_clock_literals(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """String constants in this test that stage a wall-clock time of day"""
    found: list[str] = []
    for node in [*fn.decorator_list, *fn.body]:
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Constant)
                and isinstance(sub.value, str)
                and _TIME_OF_DAY.fullmatch(sub.value.strip())
            ):
                found.append(sub.value)
    return found


def flake_offenders(sources: dict[str, ast.Module]) -> list[str]:
    """One message per test function that both fakes `db.now` and stages a time of day"""
    offenders: list[str] = []
    for rel, tree in sorted(sources.items()):
        for fn in _test_functions(tree):
            if not fakes_db_now(fn):
                continue
            literals = wall_clock_literals(fn)
            if literals:
                offenders.append(
                    f"{rel}:{fn.lineno} {fn.name}() fakes jarvis.db.now and stages "
                    f"wall-clock {', '.join(repr(lit) for lit in literals)} — {_REMEDY}"
                )
    return offenders


# ------------------------------------------------------------------- assertion B


def clock_offenders(
    sources: dict[str, ast.Module],
    scopes: dict[str, list[Marker]] | None = None,
) -> list[str]:
    """One message per unlisted file that reads the real clock outside every exemption"""
    offenders: list[str] = []
    for rel, tree in sorted(sources.items()):
        if rel in ALLOWED:
            continue
        scoped = (scopes or {}).get(rel, [])
        reads = [
            (line, name) for line, name in real_clock_reads(tree)
            if not _covered(scoped, line)
        ]
        if reads:
            shown = ", ".join(f"{name} (line {line})" for line, name in reads[:5])
            if len(reads) > 5:
                shown += f", … and {len(reads) - 5} more"
            offenders.append(f"{rel}: {shown}")
    return offenders


def stale_entries(
    allowed: dict[str, str], sources: dict[str, ast.Module]
) -> list[str]:
    """Allowlisted files that contain no real-clock read any more"""
    return sorted(
        rel
        for rel, tree in sources.items()
        if rel in allowed and not real_clock_reads(tree)
    )


# ----------------------------------------------------------------------- the rule


def test_every_tracked_test_file_parses() -> None:
    assert _sources(), "no tracked tests/**/*.py files found — the walk is broken"


def test_no_test_pairs_a_faked_clock_with_a_wall_clock_literal() -> None:
    offenders = flake_offenders(_sources())
    assert not offenders, (
        f"{len(offenders)} test(s) fake jarvis.db.now AND stage a wall-clock time of "
        "day. The two clocks disagree across a date boundary:\n  "
        + "\n  ".join(offenders)
    )


def test_unlisted_test_files_do_not_read_the_real_clock() -> None:
    offenders = clock_offenders(_sources(), marker_scopes(_texts()))
    assert not offenders, (
        f"{len(offenders)} test file(s) read the real clock unexempted:\n  "
        + "\n  ".join(offenders)
        + "\n\nFake the clock instead. If the read is genuinely unavoidable, put a "
        "`# clock-exempt: class 3 — <reason>` (or class 4) comment inside the function "
        "that holds it."
    )


def test_allowed_reasons_name_their_class() -> None:
    bad = {rel: reason for rel, reason in ALLOWED.items() if not _CLASS_REASON.match(reason)}
    assert not bad, (
        "every ALLOWED reason must match '^class (2|3|4) — .+' — an entry with no class "
        f"and no reason is a shrug, not an exemption: {sorted(bad)}"
    )


def test_allowed_entries_point_at_existing_files() -> None:
    missing = sorted(rel for rel in ALLOWED if not (REPO_ROOT / rel).is_file())
    assert not missing, (
        f"ALLOWED names {len(missing)} path(s) that do not exist (renamed or deleted) "
        f"— delete these entries: {missing}"
    )


def test_allowed_has_no_stale_entries() -> None:
    stale = stale_entries(ALLOWED, _sources())
    assert not stale, (
        f"{len(stale)} ALLOWED entry/entries no longer read the real clock — delete "
        f"this entry, the list is shrink-only: {stale}"
    )


def test_marker_reasons_name_their_class() -> None:
    bad = marker_reason_offenders(_texts())
    assert not bad, (
        "every clock-exempt marker must match '^class (2|3|4) — .+' — a marker with no "
        f"class and no reason is a shrug, not an exemption:\n  " + "\n  ".join(bad)
    )


def test_no_marker_claims_class_2() -> None:
    bad = marker_class_two_offenders(_texts())
    assert not bad, (
        f"{len(bad)} marker(s) claim class 2:\n  " + "\n  ".join(bad)
    )


def test_module_level_markers_sit_above_the_first_definition() -> None:
    bad = misplaced_marker_offenders(_texts())
    assert not bad, (
        f"{len(bad)} module-level marker(s) are stranded mid-file:\n  " + "\n  ".join(bad)
    )


def test_every_marker_covers_a_real_clock_read() -> None:
    bad = uncovered_marker_offenders(_texts())
    assert not bad, (
        f"{len(bad)} marker(s) cover no real-clock read — markers are shrink-only and "
        f"cannot go stale:\n  " + "\n  ".join(bad)
    )


# Unique reasons supersede spec 2.1's `rg | uniq -d` check
def test_marker_reasons_are_unique() -> None:
    bad = duplicate_marker_reasons(_texts())
    assert not bad, (
        f"{len(bad)} marker reason(s) are used more than once — a duplicated reason is a "
        f"generic reason; one unique reason per exempt function:\n  " + "\n  ".join(bad)
    )


def test_allowed_has_no_entry_the_markers_carry() -> None:
    carried = redundant_allowed_entries(ALLOWED, _texts())
    assert not carried, (
        f"{len(carried)} ALLOWED entry/entries have every real-clock read covered by a "
        f"marker — delete this entry, the markers carry it: {carried}"
    )


# ------------------------------------------------------- proof the guard is armed


def _parse(source: str) -> ast.Module:
    return ast.parse(source)


# The pre-fix text of test_a_refused_sweep_is_held_and_not_recorded_as_a_failure
_PRE_FIX_FLAKE = '''
def test_a_refused_sweep_is_held_and_not_recorded_as_a_failure(clock, fake_claude):
    fake_claude.health_rate_limited(reset="11:50pm (America/Los_Angeles)")
    assert daemon.health_sweeping
'''

_POST_FIX = '''
def test_a_refused_sweep_is_held_and_not_recorded_as_a_failure(clock, fake_claude):
    fake_claude.health_rate_limited(reset="in 5h")
    assert daemon.health_sweeping
'''

_LITERAL_WITHOUT_FAKE_CLOCK = '''
def test_reset_moment_parses_a_time_of_day():
    assert _reset_moment("11:50pm") is not None
    assert _reset_moment("3am") is not None
    assert _reset_moment("12:01am (UTC)") is not None
'''


def test_assertion_a_fires_on_the_pre_fix_flake_shape() -> None:
    offenders = flake_offenders({"tests/synth.py": _parse(_PRE_FIX_FLAKE)})
    assert len(offenders) == 1, offenders
    message = offenders[0]
    assert "tests/synth.py" in message
    assert "test_a_refused_sweep_is_held_and_not_recorded_as_a_failure" in message
    assert "11:50pm (America/Los_Angeles)" in message
    assert 'reset="in 5h"' in message
    assert "kn-898350cc" in message


def test_assertion_a_is_silent_on_a_duration_reset() -> None:
    assert flake_offenders({"tests/synth.py": _parse(_POST_FIX)}) == []


def test_assertion_a_is_silent_on_a_wall_clock_literal_without_a_faked_clock() -> None:
    assert flake_offenders({"tests/synth.py": _parse(_LITERAL_WITHOUT_FAKE_CLOCK)}) == []


def test_assertion_a_sees_every_way_of_faking_db_now() -> None:
    forms = [
        "def test_x(clock):\n    pass\n",
        "def test_x():\n    with _MovedNow(3):\n        pass\n",
        "def test_x():\n    with db._MovedNow(3):\n        pass\n",
        'def test_x(monkeypatch):\n    monkeypatch.setattr("jarvis.db.now", f)\n',
        'def test_x(monkeypatch):\n    monkeypatch.setattr("jarvis.db", "now", f)\n',
        'def test_x():\n    setattr(jarvis.db, "now", f)\n',
        "def test_x():\n    jarvis.db.now = f\n",
    ]
    for source in forms:
        fn = _test_functions(_parse(source))[0]
        assert fakes_db_now(fn), source


def test_assertion_b_fires_on_an_unlisted_file_reading_the_real_clock() -> None:
    forms = {
        "import time\n\ndef test_x():\n    return time.time()\n": "time.time",
        "import time\n\ndef test_x():\n    time.sleep(0.01)\n": "time.sleep",
        "import time as t\n\ndef test_x():\n    return t.monotonic()\n": "time.monotonic",
        "from time import sleep\n\ndef test_x():\n    sleep(0.01)\n": "time.sleep",
        "from datetime import datetime\n\ndef test_x():\n    datetime.now()\n": "datetime.now",
        "import datetime\n\ndef test_x():\n    datetime.datetime.utcnow()\n": "datetime.utcnow",
        "from datetime import date\n\ndef test_x():\n    date.today()\n": "date.today",
    }
    for source, expected in forms.items():
        offenders = clock_offenders({"tests/synth.py": _parse(source)})
        assert len(offenders) == 1, source
        assert expected in offenders[0], source


def test_assertion_b_is_silent_on_a_mention_in_a_comment_or_docstring() -> None:
    source = (
        "import time\n\n"
        'def test_x():\n    """We deliberately avoid time.time() here."""\n'
        "    # time.sleep(1) would make this flaky\n"
        '    return "time.monotonic"\n'
    )
    assert clock_offenders({"tests/synth.py": _parse(source)}) == []


def test_assertion_b_is_silent_on_a_listed_file() -> None:
    sources = {rel: _parse("import time\n\ndef test_x():\n    time.time()\n") for rel in ALLOWED}
    assert clock_offenders(sources) == []


def test_stale_entry_rule_fires_on_a_listed_file_with_no_real_clock_read() -> None:
    sources = {"tests/synth.py": _parse("def test_x():\n    assert True\n")}
    assert stale_entries({"tests/synth.py": "class 3 — spin-waits"}, sources) == [
        "tests/synth.py"
    ]
    assert stale_entries({}, sources) == []


def test_class_reason_regex_refuses_a_shrug() -> None:
    assert _CLASS_REASON.match("class 2 — rows back-dated with time.time() - N")
    assert _CLASS_REASON.match("class 3 — spin-waits on a real subprocess dying")
    assert _CLASS_REASON.match("class 4 — a mtime the kernel wrote")
    assert not _CLASS_REASON.match("needed")
    assert not _CLASS_REASON.match("class 1 — the flake itself")
    assert not _CLASS_REASON.match("class 3")


# ------------------------------------------------------- the marker rules, armed

_SYNTH_MODULE_TOP = '''# clock-exempt: class 3 — a synthetic module-top marker
import time


def test_x():
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        time.sleep(0.01)
'''

_SYNTH_FUNCTION_SCOPE = '''import time


def test_x():
    # clock-exempt: class 3 — a synthetic function-scope marker
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        time.sleep(0.01)


def test_y():
    return time.time()
'''

_SYNTH_LATE_MODULE_MARKER = '''import time


def test_x():
    time.sleep(0.01)


# clock-exempt: class 3 — a synthetic marker stranded below the first def
'''

_SYNTH_CLASS_TWO_MARKER = '''import time


def test_x():
    # clock-exempt: class 2 — a synthetic back-dated row
    return time.time() - 60
'''

_SYNTH_COVERS_NOTHING = '''def test_x():
    # clock-exempt: class 3 — a synthetic marker over no read at all
    assert True
'''

_SYNTH_SHRUG_MARKER = '''import time


def test_x():
    # clock-exempt: needed
    time.sleep(0.01)
'''

_SYNTH_DUPLICATE_A = '''import time


def test_x():
    # clock-exempt: class 3 — a synthetic reason used in two places
    time.sleep(0.01)
'''


def test_a_module_top_marker_exempts_every_read_in_the_file() -> None:
    files = {"tests/synth.py": _SYNTH_MODULE_TOP}
    assert clock_offenders({"tests/synth.py": _parse(_SYNTH_MODULE_TOP)},
                           marker_scopes(files)) == []
    assert uncovered_marker_offenders(files) == []


def test_a_function_marker_leaves_a_read_in_another_function_caught() -> None:
    files = {"tests/synth.py": _SYNTH_FUNCTION_SCOPE}
    offenders = clock_offenders({"tests/synth.py": _parse(_SYNTH_FUNCTION_SCOPE)},
                                marker_scopes(files))
    assert len(offenders) == 1, offenders
    assert "time.time (line 12)" in offenders[0], offenders
    assert "time.sleep" not in offenders[0], "the marked spin-wait was reported"


def test_a_module_marker_below_the_first_definition_is_refused() -> None:
    offenders = misplaced_marker_offenders({"tests/synth.py": _SYNTH_LATE_MODULE_MARKER})
    assert len(offenders) == 1, offenders
    assert "tests/synth.py:8" in offenders[0]
    assert "inside the function it exempts" in offenders[0]


def test_a_class_2_marker_is_refused() -> None:
    offenders = marker_class_two_offenders({"tests/synth.py": _SYNTH_CLASS_TWO_MARKER})
    assert len(offenders) == 1, offenders
    assert "db.now()" in offenders[0]
    assert marker_class_two_offenders({"tests/synth.py": _SYNTH_MODULE_TOP}) == []


def test_a_marker_covering_no_real_clock_read_is_refused() -> None:
    offenders = uncovered_marker_offenders({"tests/synth.py": _SYNTH_COVERS_NOTHING})
    assert len(offenders) == 1, offenders
    assert "delete" in offenders[0]


def test_a_marker_reason_that_names_no_class_is_refused() -> None:
    offenders = marker_reason_offenders({"tests/synth.py": _SYNTH_SHRUG_MARKER})
    assert len(offenders) == 1, offenders
    assert "needed" in offenders[0]


def test_a_duplicated_marker_reason_is_refused() -> None:
    files = {"tests/a.py": _SYNTH_DUPLICATE_A, "tests/b.py": _SYNTH_DUPLICATE_A}
    offenders = duplicate_marker_reasons(files)
    assert len(offenders) == 1, offenders
    assert "tests/a.py" in offenders[0] and "tests/b.py" in offenders[0]
    assert duplicate_marker_reasons({"tests/a.py": _SYNTH_DUPLICATE_A}) == []


def test_an_allowed_entry_the_markers_carry_is_refused() -> None:
    files = {"tests/synth.py": _SYNTH_MODULE_TOP}
    assert redundant_allowed_entries({"tests/synth.py": _SPIN}, files) == ["tests/synth.py"]
    assert redundant_allowed_entries(
        {"tests/synth.py": _SPIN}, {"tests/synth.py": _SYNTH_FUNCTION_SCOPE}) == []


_SYNTH_TRIM_TARGET = '''# clock-exempt: class 3 — a synthetic module-top marker whose reason runs well past the hundred characters trim keeps
# A prose comment directly under it,
# spread over two lines.
import time


def test_x():
    # A prose comment directly above,
    # also two lines.
    # clock-exempt: class 3 — a synthetic function-scope marker
    time.sleep(0.01)
'''


def test_the_comment_trimmer_leaves_every_marker_byte_for_byte() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("trim_comments", REPO_ROOT / "scripts/trim_comments.py")
    trim = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trim)
    out = trim.trim_source(_SYNTH_TRIM_TARGET, "tests/synth.py")
    markers = [ln for ln in _SYNTH_TRIM_TARGET.splitlines() if "clock-exempt:" in ln]
    assert len(markers) == 2
    for marker in markers:
        assert marker in out.splitlines(), out
    assert out != _SYNTH_TRIM_TARGET, "the prose around the markers should still be trimmed"
