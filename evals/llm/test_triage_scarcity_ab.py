"""Three-arm A/B: can a reviewer be made to file only what is really needed?

The self-evolution loop burns tokens because every finder the OS runs (the panel, the
investigators, the bug filers) is a model asked "what is wrong?", and a model asked that
almost never answers "nothing". Filing is free to the filer; the order it buys is not.

The subject is a cheap model (haiku) reviewing one small module with TWELVE seeded issues
of known severity (evals/data/triage/inventory_key.json): one major, four medium, three
minor, four cosmetic. A model that treats this the way a busy engineer would files the
major and the mediums and leaves the rest alone.

ARMS
    baseline   — "review this code, report every issue". The panel today.
    scarcity   — the same model told resources are restricted: each filed issue is a paid
                 work order, file only what a user or operator would actually notice,
                 filing nothing is fine. Pure prompt; the model decides what to file.
    mechanism  — the baseline prompt, but every finding must carry a severity and a
                 concrete failure scenario; CODE then drops anything below `medium` and
                 anything without a scenario. The model classifies, the gate decides.

THE ASSERTION is on `scarcity`, because that is the claim under test: in EVERY run it
files the major and files no minor or cosmetic issue. The other two arms are reported
beside it so the prompt can be compared with no intervention and with a mechanical gate.

FIRST RESULT (2026-10-20, haiku, 5 runs/arm, ~$0.001 a call): baseline filed 13-14
findings a run, 5-6 of them minor/cosmetic. Scarcity filed 4: the major every run, no
low issue ever, and missed the same medium every run (`swallowed-error`) — a scarce
reviewer's blind spot is the silent failure. The mechanism arm leaked once: haiku rated
the unclosed file `medium` and wrote a failure for it, so a gate that trusts the model's
own severity is only as good as that rating.

GRADING is deterministic: the code is shown with line numbers, each finding must cite
one, and a finding maps to the seeded issue whose anchor line it names (±1 line). A
finding that maps to nothing is `unseeded` — not necessarily wrong, but work the key did
not ask for.

Opt-in (spends real tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_triage_scarcity_ab.py -q -s
    JARVIS_EVALS_N=3                 # runs per arm, default 3
    JARVIS_EVALS_TRIAGE_MODEL=haiku  # the reviewer, default haiku
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jarvis import claude_cli

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario

N_RUNS = max(1, int(os.environ.get("JARVIS_EVALS_N", "3")))
MODEL = os.environ.get("JARVIS_EVALS_TRIAGE_MODEL", "haiku")
DATA = Path(__file__).resolve().parents[1] / "data" / "triage"
LOW = ("minor", "cosmetic")

OUTPUT_CONTRACT = (
    "Reply with ONLY a JSON object, no prose and no code fences, of the form\n"
    '{"findings": [{"line": <int>, "severity": "major|medium|minor|cosmetic", '
    '"title": "<short>", "failure_scenario": "<concrete input or state -> wrong '
    'outcome, or empty>"}]}\n'
    "`line` is the number shown at the start of the offending line. An empty list is a "
    "valid answer."
)

PROMPTS = {
    "baseline": (
        "You are a code reviewer. Review the Python module below and report every issue "
        "that should be fixed.\n\n" + OUTPUT_CONTRACT),
    "scarcity": (
        "You are a reviewer on a team with a small, fixed engineering budget. Every issue "
        "you file becomes a work order that a paid engineer must pick up, and this week's "
        "budget covers only a few. File an issue ONLY if leaving it unfixed would cause a "
        "failure that a user or an operator would actually notice: wrong results, lost or "
        "corrupted data, a security exposure, a crash. Do NOT file style, naming, typos, "
        "micro-optimisations or hygiene that changes no behaviour — those cost money and "
        "buy nothing. Filing nothing is an acceptable answer; when in doubt, do not file. "
        "List only the issues you decide to file.\n\n" + OUTPUT_CONTRACT),
    "mechanism": (
        "You are a code reviewer. Review the Python module below and report every issue "
        "you find. For each, rate its severity honestly and give a concrete failure "
        "scenario (an input or state and the wrong outcome it produces); leave the "
        "scenario empty if the issue changes no behaviour.\n\n" + OUTPUT_CONTRACT),
}
ARMS = tuple(PROMPTS)


def gate(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The mechanism arm's filter: severity at least medium AND a stated failure."""
    return [f for f in findings
            if str(f.get("severity", "")).lower() not in LOW
            and str(f.get("failure_scenario") or "").strip()]


@dataclass
class Run:
    arm: str
    filed: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    parsed: bool = True
    matched: dict[str, str] = field(default_factory=dict)   # issue id -> severity
    unseeded: int = 0
    duplicates: int = 0

    @property
    def major(self) -> bool:
        return "major" in self.matched.values()

    @property
    def mediums(self) -> int:
        return sum(1 for s in self.matched.values() if s == "medium")

    @property
    def low_filed(self) -> list[str]:
        return sorted(i for i, s in self.matched.items() if s in LOW)


@pytest.fixture(scope="module")
def key() -> dict[str, Any]:
    key = json.loads((DATA / "inventory_key.json").read_text())
    lines = (DATA / key["fixture"]).read_text().splitlines()
    for issue in key["issues"]:
        hits = [n for n, text in enumerate(lines, 1) if issue["anchor"] in text]
        assert len(hits) == 1, f"anchor for {issue['id']} must match exactly one line"
        issue["line"] = hits[0]
    key["listing"] = "\n".join(f"{n:>3}  {text}" for n, text in enumerate(lines, 1))
    return key


def parse(text: str) -> list[dict[str, Any]] | None:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return None
    try:
        findings = json.loads(match.group(0)).get("findings")
    except (json.JSONDecodeError, AttributeError):
        return None
    return findings if isinstance(findings, list) else None


def grade(run: Run, key: dict[str, Any]) -> Run:
    """Map each filed finding to the nearest seeded issue within one line."""
    for finding in run.filed:
        try:
            line = int(finding["line"])
        except (KeyError, TypeError, ValueError):
            run.unseeded += 1
            continue
        near = sorted((abs(i["line"] - line), i["id"], i["severity"])
                      for i in key["issues"] if abs(i["line"] - line) <= 1)
        fresh = [(d, i, s) for d, i, s in near if i not in run.matched]
        if fresh:
            run.matched[fresh[0][1]] = fresh[0][2]
        elif near:
            run.duplicates += 1
        else:
            run.unseeded += 1
    return run


def review(arm: str, key: dict[str, Any], cwd: Path) -> Run:
    """One hermetic call: no tools, no repo, the listing and the arm's prompt only."""
    result = claude_cli.run_headless_result(
        "MODULE inventory.py\n\n" + key["listing"], system_prompt=PROMPTS[arm],
        model=MODEL, cwd=cwd, tools="", timeout=180)
    cost = float((result.usage or {}).get("total_cost_usd") or 0.0)
    findings = parse(result.text)
    if findings is None:
        return Run(arm, cost_usd=cost, parsed=False)
    filed = gate(findings) if arm == "mechanism" else findings
    return grade(Run(arm, filed=filed, cost_usd=cost), key)


@pytest.fixture(scope="module")
def runs(key, tmp_path_factory) -> dict[str, list[Run]]:
    cwd = tmp_path_factory.mktemp("triage-eval")
    jobs = [arm for arm in ARMS for _ in range(N_RUNS)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        done = list(pool.map(lambda arm: review(arm, key, cwd), jobs))
    out: dict[str, list[Run]] = {arm: [] for arm in ARMS}
    for run in done:
        out[run.arm].append(run)
    _report(out, key)
    return out


def _report(out: dict[str, list[Run]], key: dict[str, Any]) -> None:
    mediums = sum(1 for i in key["issues"] if i["severity"] == "medium")
    print(f"\nTRIAGE A/B — model={MODEL}, runs/arm={N_RUNS}, seeded: 1 major, "
          f"{mediums} medium, {sum(1 for i in key['issues'] if i['severity'] in LOW)} "
          "minor+cosmetic")
    print(f"{'arm':<10} {'filed':>6} {'major':>6} {'medium':>7} {'low filed':>10} "
          f"{'unseeded':>9} {'unparsed':>9} {'$/call':>8}")
    for arm, rs in out.items():
        ok = [r for r in rs if r.parsed]
        avg = (lambda xs: statistics.mean(xs) if xs else 0.0)
        print(f"{arm:<10} {avg([len(r.filed) for r in ok]):>6.1f} "
              f"{sum(r.major for r in ok):>3}/{len(ok):<2} "
              f"{avg([r.mediums for r in ok]):>5.1f}/{mediums} "
              f"{avg([len(r.low_filed) for r in ok]):>10.1f} "
              f"{avg([r.unseeded for r in ok]):>9.1f} "
              f"{len(rs) - len(ok):>9} {avg([r.cost_usd for r in rs]):>8.4f}")
        missed = sorted({i["id"] for r in ok for i in key["issues"]
                         if i["severity"] in ("major", "medium") and i["id"] not in r.matched})
        if missed:
            print(f"           needed but missed (any run): {', '.join(missed)}")
        for r in ok:
            if r.low_filed:
                print(f"           low filed: {', '.join(r.low_filed)}")


@scenario("triage", "scarcity-prompt-files-major-discards-low")
def test_scarcity_prompt_files_major_and_discards_minor_and_cosmetic(runs):
    scarce = runs["scarcity"]
    assert all(r.parsed for r in scarce), "every scarcity run must return parseable JSON"
    assert all(r.major for r in scarce), \
        f"the major issue must be filed in every run: {[r.matched for r in scarce]}"
    leaked = [r.low_filed for r in scarce if r.low_filed]
    assert not leaked, f"minor/cosmetic issues were filed: {leaked}"
