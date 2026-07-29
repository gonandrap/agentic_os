"""Eval fixtures + the scorecard reporter.

Fixtures come from jarvis.testing (same isolated fake-claude world as tests/).
Every eval carries a `scenario("category", "name")` marker; the reporter groups
results by category, prints the scorecard, and writes evals/results.json.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from jarvis.testing import (  # noqa: F401
    catalog_file,
    claude_json,
    fake_claude,
    jarvis_home,
    make_git_project,
    project,
)

RESULTS_PATH = Path(__file__).parent / "results.json"


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "scenario(category, name): behavioral eval scenario identity")
    config._eval_results = []


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    m = item.get_closest_marker("scenario")
    if not m:
        return
    category, name = m.args[0], m.args[1]
    # parametrized batteries: include the case id so every case is a scenario
    if "[" in item.name:
        name = f"{name} [{item.name.split('[', 1)[1].rstrip(']')}]"
    item.config._eval_results.append(
        {"category": category, "name": name, "passed": report.passed}
    )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    results = getattr(config, "_eval_results", [])
    if not results:
        return
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)
    tw = terminalreporter
    tw.section("eval scorecard")
    total_pass = total = 0
    for cat in sorted(by_cat):
        rows = by_cat[cat]
        passed = sum(1 for r in rows if r["passed"])
        total_pass += passed
        total += len(rows)
        mark = "green" if passed == len(rows) else "red"
        tw.write_line(f"  {cat:<38} {passed}/{len(rows)}", **{mark: True})
        for r in rows:
            if not r["passed"]:
                tw.write_line(f"      ✗ {r['name']}", red=True)
    tw.write_line(f"  {'TOTAL':<38} {total_pass}/{total}",
                  bold=True, green=total_pass == total, red=total_pass != total)
    RESULTS_PATH.write_text(json.dumps(
        {"total": total, "passed": total_pass,
         "categories": {c: {"passed": sum(1 for r in v if r["passed"]),
                            "total": len(v)} for c, v in by_cat.items()},
         "scenarios": results}, indent=2))
