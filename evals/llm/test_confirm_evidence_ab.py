"""Two-arm A/B: does the TRIMMED confirmation packet judge the same as the full diff?

Design: docs/superpowers/specs/2026-09-26-bounded-model-inputs.md § 6.

§ 2 cut the assumption-confirmation question's diff from the validation panel's 150,000
characters to `validation.confirm_diff_chars` (12,000). A budget that changes verdicts is
not a saving, it is a downgrade with a nice bill, and this file is the only thing that
says which one was bought. Neo question 722 — a real confirmation question built at the
panel's budget — was 151,700 characters to ask for one word (kn-1be10a00).

THE ARMS. Both are built by the SHIPPED `autoreview._confirm_question` over a SHIPPED
`autoreview.confirm_evidence` packet, and ONLY THE LIMIT DIFFERS: the full arm collects at
`daemon.CONFIRM_COLLECT_CHARS`, so nothing is cut and no truncation marker appears; the
trimmed arm is what the daemon sends today. The arm discipline of
`evals/llm/test_house_style_ab.py` (kn-fe226ab1) is enforced IN CODE, not asserted in a
test: `build_arms` removes exactly `ev.what_changed()` from each question and raises
`ArmDrift` unless the two frames are byte-equal. Drift outside the evidence block means
the run measured the re-composition instead of the trim.

WHAT IS MEASURED, AND WHAT IS ASSERTED. The assertion is UNSAFE FLIPS — full arm
`deny`/escalate turning into trimmed arm `approve` — and it must be 0: confirming on
evidence the reviewer could not see is the only failure that costs the user something.
SAFE flips (full approve, trimmed escalates) are a cost, not a defect, and are reported as
a number. Agreement, each arm's agreement with the RECORDED outcome, and the prompt sizes
are reported beside them.

SIZES COME OFF THE TRANSPORT, never `len(prompt)`: `HeadlessResult.prompt_chars` and
`system_prompt_chars` are the columns § 3 added, so the saving and the agreement are read
off the same run and off the same numbers `jarvis cost` shows.

A REPLY THAT DID NOT PARSE IS NOT A VERDICT, and neither is a call that failed. They are
DIFFERENT FINDINGS and are counted apart: `reached` is False only on the transport's
exception path — the reviewer was never asked — and `parsed` is False when a reply came
back that `neo.parse_verdict` could not read. `parse_verdict` turns unreadable output into
`{"escalate": True, …}`, so an unreadable reply and a genuine escalation look identical
through `read_ruling`; `neo.UNPARSEABLE_PREFIX` on the reason is the only thing that tells
them apart. Both are excluded from the agreement number, and neither is ever scored as an
approve.

ROWS WHOSE TWO ARMS COME OUT BYTE-IDENTICAL — the diff was already under budget, so
nothing was cut — measure nothing and are EXCLUDED from the agreement number, reported as
a count of their own.

THE SPEC'S OPTIONAL CACHE-PREFIX EXPERIMENT IS NOT BUILT: nobody can read where the CLI
places a cache breakpoint inside a user message, so any number this eval produced for it
would be an inference, not a measurement (§ 6's own caveat).

THE CORPUS. `$JARVIS_CONFIRM_CORPUS` if set, else `$JARVIS_HOME/evals/confirm_corpus.json`
if it exists, else the committed synthetic fixture (Neo, question 1116). The real corpus is
production assumption prose and production diffs and is NOT committed — this repository is
public, the same reason as Neo question 650 for the stakes corpus —
`evals/tools/build_confirm_corpus.py` regenerates it from a live fleet. WHICH corpus ran is
printed with the results, because the two are not comparable numbers.

Opt-in (spends real tokens, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_confirm_evidence_ab.py -q
    JARVIS_EVALS_MODEL=sonnet            # optional, default sonnet
    JARVIS_CONFIRM_CORPUS=/path/to.json  # optional, see above
"""

from __future__ import annotations

import json
import os
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jarvis import autoreview, catalog, claude_cli, daemon, neo

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario

MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")
CORPUS_ENV = "JARVIS_CONFIRM_CORPUS"
SYNTHETIC = Path(__file__).resolve().parents[1] / "data" / "confirm_corpus_synthetic.json"
TIMEOUT = 300

#: The FULL arm: the daemon's collection bound, which trims nothing a confirmation would
#: have carried — spec § 2, daemon.py's `CONFIRM_COLLECT_CHARS`.
FULL_LIMIT = daemon.CONFIRM_COLLECT_CHARS
#: The TRIMMED arm: the catalog default for `validation.confirm_diff_chars`, read from the
#: shipped constant so this eval cannot drift from the budget it is measuring.
TRIMMED_LIMIT = catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS

#: The project name interpolated into both arms. Identical in both, so it is part of the
#: frame and cannot affect the comparison.
PROJECT = "reports_app"


def corpus_path() -> Path:
    named = os.environ.get(CORPUS_ENV, "").strip()
    if named and Path(named).expanduser().exists():
        return Path(named).expanduser()
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home and (Path(home).expanduser() / "evals" / "confirm_corpus.json").exists():
        return Path(home).expanduser() / "evals" / "confirm_corpus.json"
    return SYNTHETIC


def load_corpus(path: Path) -> dict[str, Any]:
    rows = json.loads(Path(path).read_text())["rows"]
    return {"path": Path(path), "rows": rows}


class ArmDrift(AssertionError):
    """The two arms differ somewhere other than the evidence block."""


@dataclass(frozen=True)
class Arms:
    """One row's two questions and the two packets they were rendered from."""

    full: str
    trimmed: str
    full_ev: autoreview.ConfirmEvidence
    trimmed_ev: autoreview.ConfirmEvidence

    @property
    def identical(self) -> bool:
        """Nothing was cut, so this row measures nothing."""
        return self.full == self.trimmed


def frame_of(question: str, ev: autoreview.ConfirmEvidence) -> str:
    """The question with EXACTLY the evidence block removed — cut, never re-composed."""
    block = ev.what_changed()
    at = question.find(block)
    if at == -1:
        raise ArmDrift("the evidence block is not in the question it was rendered for: "
                       "`_confirm_question` no longer interpolates `what_changed()` "
                       "verbatim, so the arms cannot be compared")
    return question[:at] + question[at + len(block):]


def _packet(row: dict[str, Any]) -> SimpleNamespace:
    """The row's recorded evidence in the shape `confirm_evidence` reads a packet in."""
    ev = row["evidence"]
    return SimpleNamespace(
        stat=ev.get("stat") or "", diff=ev.get("diff") or "",
        files=tuple(ev.get("files") or ()), diff_truncated=bool(ev.get("diff_truncated")),
        dropped_files=tuple(ev.get("dropped_files") or ()),
        pr_url=ev.get("pr_url") or "", source=ev.get("source") or "",
        head=ev.get("head") or "")


def build_arms(row: dict[str, Any]) -> Arms:
    """Both questions for one row. PURE — no git, no store, no model.

    Only the limit differs, and the frames are proven byte-equal here rather than in a
    test, because a drifted pair must never reach a paid call (kn-fe226ab1).
    """
    packet = _packet(row)
    assumption, wo = row["assumption"], row["work_order"]
    siblings = list(row.get("siblings") or [])
    full_ev = autoreview.confirm_evidence(packet, assumption, FULL_LIMIT,
                                          collect_limit=FULL_LIMIT)
    trimmed_ev = autoreview.confirm_evidence(packet, assumption, TRIMMED_LIMIT,
                                             collect_limit=FULL_LIMIT)
    full = autoreview._confirm_question(PROJECT, wo, assumption, siblings, full_ev)
    trimmed = autoreview._confirm_question(PROJECT, wo, assumption, siblings, trimmed_ev)
    if frame_of(full, full_ev) != frame_of(trimmed, trimmed_ev):
        raise ArmDrift(
            f"{row['id']}: the two arms differ outside the evidence block, so this run "
            f"would measure the re-composition and not the trim (kn-fe226ab1)")
    return Arms(full=full, trimmed=trimmed, full_ev=full_ev, trimmed_ev=trimmed_ev)


@dataclass(frozen=True)
class Judgement:
    """One arm's reply on one row, and WHETHER IT IS A VERDICT AT ALL.

    `reached` is False only on the transport's exception path and is never inferred from
    a cost; `parsed` is False when a reply came back that `neo.parse_verdict` could not
    read. `approve` is `autoreview.read_ruling(...).accept` — the shipped reading, with
    both of its nets armed — and is False on either failure, which is a refusal to score
    a failure as a confirmation, not a verdict of its own.
    """

    approve: bool
    reached: bool
    parsed: bool
    reason: str
    prompt_chars: int
    system_prompt_chars: int
    latency_ms: float

    @property
    def scored(self) -> bool:
        return self.reached and self.parsed


def judge(question: str, cwd: Path, model: str) -> Judgement:
    """One call, through the shipped transport and the shipped reply parser.

    `tools=""` leaves the reviewer nothing to read with: a reviewer that can open the
    repository is a different reviewer from the one the daemon runs, and these numbers are
    about the daemon's.
    """
    started = time.monotonic()
    try:
        result = claude_cli.run_headless_result(
            question, system_prompt=autoreview.ASSUMPTION_REVIEWER_PERSONA, model=model,
            cwd=cwd, tools="", timeout=TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — the transport failed: nothing was judged
        return Judgement(approve=False, reached=False, parsed=False,
                         reason=f"transport failed: {exc}", prompt_chars=0,
                         system_prompt_chars=0,
                         latency_ms=(time.monotonic() - started) * 1000)
    verdict = neo.parse_verdict(result.text)
    parsed = not str(verdict.get("reason") or "").startswith(neo.UNPARSEABLE_PREFIX)
    ruling = autoreview.read_ruling(verdict, default_model=result.model or model)
    return Judgement(approve=bool(ruling.accept) and parsed, reached=True, parsed=parsed,
                     reason=ruling.reason, prompt_chars=result.prompt_chars,
                     system_prompt_chars=result.system_prompt_chars,
                     latency_ms=(time.monotonic() - started) * 1000)


@dataclass(frozen=True)
class RowResult:
    row: dict[str, Any]
    arms: Arms
    full: Judgement
    trimmed: Judgement


def run_row(row: dict[str, Any], cwd: Path, model: str) -> RowResult:
    arms = build_arms(row)
    return RowResult(row=row, arms=arms, full=judge(arms.full, cwd, model),
                     trimmed=judge(arms.trimmed, cwd, model))


def run_corpus(rows: list[dict[str, Any]], cwd: Path, model: str) -> list[RowResult]:
    """Every row, both arms. Sequential: this corpus is small and a pool would buy
    nothing but a contended latency."""
    return [run_row(row, cwd, model) for row in rows]


@dataclass
class Score:
    """What the run found. Every exclusion is a COUNT, never a silent drop."""

    scored: int = 0
    agreed: int = 0
    unsafe_flips: list[str] = field(default_factory=list)
    safe_flips: list[str] = field(default_factory=list)
    identical_ids: list[str] = field(default_factory=list)
    unparsed_ids: list[str] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    full_agreed_recorded: int = 0
    trimmed_agreed_recorded: int = 0

    @property
    def identical(self) -> int:
        return len(self.identical_ids)

    @property
    def unparsed(self) -> int:
        return len(self.unparsed_ids)

    @property
    def failed(self) -> int:
        return len(self.failed_ids)

    @property
    def agreement(self) -> float:
        return self.agreed / self.scored if self.scored else 0.0

    @property
    def full_vs_recorded(self) -> float:
        return self.full_agreed_recorded / self.scored if self.scored else 0.0

    @property
    def trimmed_vs_recorded(self) -> float:
        return self.trimmed_agreed_recorded / self.scored if self.scored else 0.0


def score(pairs: list[tuple[dict[str, Any], Judgement, Judgement]]) -> Score:
    """Agreement and the two flip directions, over the rows the trim actually changed.

    PURE over `(row, full verdict, trimmed verdict)` triples: byte-identity is re-derived
    from the row itself through `build_arms`, so a fabricated pair scores exactly as a
    paid one does.
    """
    out = Score()
    for row, full, trimmed in pairs:
        row_id = str(row["id"])
        if build_arms(row).identical:
            out.identical_ids.append(row_id)
            continue
        if not full.reached or not trimmed.reached:
            out.failed_ids.append(row_id)
            continue
        if not full.parsed or not trimmed.parsed:
            out.unparsed_ids.append(row_id)
            continue
        out.scored += 1
        if full.approve == trimmed.approve:
            out.agreed += 1
        elif trimmed.approve:
            # THE ONE FAILURE THAT COSTS THE USER SOMETHING — spec § 6.
            out.unsafe_flips.append(row_id)
        else:
            out.safe_flips.append(row_id)
        recorded = str(row.get("outcome") or "") == "approved"
        out.full_agreed_recorded += int(full.approve == recorded)
        out.trimmed_agreed_recorded += int(trimmed.approve == recorded)
    return out


def _sizes(results: list[RowResult], arm: str) -> tuple[int, int, int, int]:
    """(median prompt_chars, total prompt_chars, median system_prompt_chars, calls).

    OVER THE CALLS THAT REACHED THE MODEL ONLY. A call that raised carries
    `prompt_chars=0`, which is an absence and not a size (kn-a61f5183): folding it in
    would drag the arm's median down and understate the saving with a number from a call
    that never happened. `calls` rides into the report so a run with exclusions cannot be
    read as a full one.
    """
    judged = [getattr(r, arm) for r in results if getattr(r, arm).reached]
    if not judged:
        return 0, 0, 0, 0
    prompts = [j.prompt_chars for j in judged]
    systems = [j.system_prompt_chars for j in judged]
    return (int(statistics.median(prompts)), sum(prompts),
            int(statistics.median(systems)), len(judged))


def report_lines(corpus: Path, results: list[RowResult], sc: Score) -> list[str]:
    """The scorecard. The MARGIN is the finding and a green tick hides it."""
    full_median, full_total, full_system, full_calls = _sizes(results, "full")
    trim_median, trim_total, trim_system, trim_calls = _sizes(results, "trimmed")
    saving = full_total - trim_total
    share = (saving / full_total * 100) if full_total else 0.0
    lines = [
        "",
        f"confirmation trim A/B — model={MODEL}, corpus={Path(corpus).name} "
        f"({len(results)} rows)",
        f"  arms: full at {FULL_LIMIT:,} chars (nothing cut) vs trimmed at "
        f"{TRIMMED_LIMIT:,} chars (validation.confirm_diff_chars)",
        f"  scored rows          {sc.scored}",
        f"  agreement            {sc.agreement:.3f} ({sc.agreed}/{sc.scored})",
        f"  UNSAFE flips         {len(sc.unsafe_flips)} "
        f"{sc.unsafe_flips or ''}".rstrip(),
        f"  safe flips           {len(sc.safe_flips)} {sc.safe_flips or ''}".rstrip(),
        f"  vs recorded outcome  full {sc.full_vs_recorded:.3f}, "
        f"trimmed {sc.trimmed_vs_recorded:.3f}",
        f"  byte-identical rows  {sc.identical} (nothing was cut; excluded) "
        f"{sc.identical_ids or ''}".rstrip(),
        f"  unreadable replies   {sc.unparsed} (excluded) {sc.unparsed_ids or ''}".rstrip(),
        f"  transport failures   {sc.failed} (excluded) {sc.failed_ids or ''}".rstrip(),
        f"  prompt_chars full    median {full_median:,}  total {full_total:,}  "
        f"system {full_system:,}  over {full_calls} calls that reached the model",
        f"  prompt_chars trimmed median {trim_median:,}  total {trim_total:,}  "
        f"system {trim_system:,}  over {trim_calls} calls that reached the model",
        f"  saving               {saving:,} chars over the run ({share:.1f}%)"
        + ("" if full_calls == trim_calls else
           f" — NOT COMPARABLE: the arms were sized over {full_calls} and {trim_calls} "
           f"calls, so the two totals cover different rows"),
    ]
    return lines


def _terminal_line(config, line: str) -> None:
    """Write past pytest's capture: the arm numbers are wanted on a PASSING run, which is
    exactly the run a bare `print` is swallowed on (`test_house_style_ab.py`'s reason)."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only with -p no:terminal
        print(line)
        return
    capman = config.pluginmanager.get_plugin("capturemanager")
    if capman is None:  # pragma: no cover - capture is on by default
        reporter.write_line(line)
        return
    with capman.global_and_fixture_disabled():
        reporter.write_line(line)


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Outside this repo, so the reviewer does not load CLAUDE.md on top of the persona
    being measured — `neo.answer_question`'s reason for the same cwd."""
    return tmp_path_factory.mktemp("jarvis-confirm-ab")


@pytest.fixture(scope="module")
def run(neutral_cwd, request) -> dict[str, Any]:
    path = corpus_path()
    corpus = load_corpus(path)
    if not corpus["rows"]:  # pragma: no cover - the fixture ships with the repo
        pytest.skip(f"{path} has no rows")
    results = run_corpus(corpus["rows"], neutral_cwd, MODEL)
    sc = score([(r.row, r.full, r.trimmed) for r in results])
    yield {"corpus": path, "results": results, "score": sc}
    for line in report_lines(path, results, sc):
        _terminal_line(request.config, line)


@scenario("confirm-evidence-ab", "the trim never turns a refusal into a confirmation")
def test_the_trimmed_packet_never_turns_a_refusal_into_a_confirmation(run):
    """THE ASSERTION. Everything else in the scorecard is reported, not asserted.

    A trimmed packet that confirms where the full diff refused has confirmed a change it
    could not see, which is what the truncation marker exists to prevent and the only
    outcome of this trade that costs the user something.
    """
    sc = run["score"]
    assert sc.unsafe_flips == [], (
        f"{len(sc.unsafe_flips)} of {sc.scored} scored rows flipped UNSAFELY — the full "
        f"diff refused and the trimmed packet confirmed: {sc.unsafe_flips}. The budget "
        f"of {TRIMMED_LIMIT:,} chars is buying confirmations on evidence the reviewer "
        f"could not see.")


@scenario("confirm-evidence-ab", "the run measured something")
def test_the_run_scored_rows_the_trim_actually_changed(run):
    """A corpus whose every row was under budget proves nothing about the budget, and a
    run whose calls all failed proves nothing at all — both would pass the assertion
    above in silence."""
    sc = run["score"]
    assert sc.scored, (
        f"no row was scored: {sc.identical} byte-identical (nothing cut), {sc.unparsed} "
        f"unreadable replies, {sc.failed} transport failures. Nothing was measured.")
