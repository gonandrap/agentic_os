"""No test reads the real clock (docs/superpowers/specs/2026-10-15-no-test-reads-the-real-clock.md)"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Class 2 = rows back-dated with `time.time()
_BACKDATED = "class 2 — rows back-dated or stamped with time.time(); the code under test reads time.time() itself and takes no now="
_SPIN = "class 3 — monotonic-deadline / time.sleep spin-waits on real daemon threads and subprocesses"
_BOTH = (
    "class 2 — rows back-dated or stamped with time.time(), no now= on the code under "
    "test; also class 3 — spin-waits on real daemon threads and subprocesses"
)

ALLOWED: dict[str, str] = {
    "tests/test_alarm_escalation.py": _SPIN,
    "tests/test_auth_resume.py": _BACKDATED,
    "tests/test_base_red.py": _BACKDATED,
    "tests/test_budget.py": _SPIN,
    "tests/test_config_reload.py": _BOTH,
    "tests/test_feature_validation.py": _BOTH,
    "tests/test_forced_validation.py": _SPIN,
    "tests/test_health_sweep.py": _SPIN,
    "tests/test_nav_volume.py": _BACKDATED,
    "tests/test_pr_recorded.py": _SPIN,
    "tests/test_rejudge_moved_head.py": _SPIN,
    "tests/test_release_red_defer.py": _BACKDATED,
    "tests/test_release_staging.py": _BACKDATED,
    "tests/test_remedies.py": _SPIN,
    "tests/test_rules_tick.py": _SPIN,
    "tests/test_seats.py": _SPIN,
    "tests/test_supervisor.py": _SPIN,
    "tests/test_turn_transport.py": _BOTH,
    "tests/test_ui_observability.py": _BOTH,
    "tests/test_ui_wiring.py": _BACKDATED,
    "tests/test_uilog.py": _BACKDATED,
    "tests/test_uncollected_task_block.py": _BACKDATED,
    "tests/test_usage_window_is_account_wide.py": _SPIN,
    "tests/test_validation_loop.py": _SPIN,
    "tests/test_validation_usage_limit.py": _SPIN,
    "tests/test_wo_fix.py": _SPIN,
    "tests/test_worker_session.py": _BOTH,
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

_CLASS_REASON = re.compile(r"^class (2|3) — .+")

_REMEDY = (
    'state the reset as a duration — reset="in 5h" — never a time of day; '
    "see kn-898350cc"
)


# --------------------------------------------------------------------------- walk


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


def clock_offenders(sources: dict[str, ast.Module]) -> list[str]:
    """One message per unlisted file that reads the real clock"""
    offenders: list[str] = []
    for rel, tree in sorted(sources.items()):
        if rel in ALLOWED:
            continue
        reads = real_clock_reads(tree)
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
    offenders = clock_offenders(_sources())
    assert not offenders, (
        f"{len(offenders)} test file(s) read the real clock and are not in ALLOWED:\n  "
        + "\n  ".join(offenders)
        + "\n\nFake the clock instead. If the read is genuinely unavoidable, add the "
        "file to ALLOWED with a reason naming its class "
        '("class 2 — …" or "class 3 — …").'
    )


def test_allowed_reasons_name_their_class() -> None:
    bad = {rel: reason for rel, reason in ALLOWED.items() if not _CLASS_REASON.match(reason)}
    assert not bad, (
        "every ALLOWED reason must match '^class (2|3) — .+' — an entry with no class "
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
    assert not _CLASS_REASON.match("needed")
    assert not _CLASS_REASON.match("class 1 — the flake itself")
    assert not _CLASS_REASON.match("class 3")
