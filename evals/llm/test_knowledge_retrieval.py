"""LLM-graded eval: does a worker handed a knowledge INDEX actually go and read it?

`tests/test_knowledge_ondemand.py` proves the MECHANISM — the prompt stays bounded, the
index carries the right ids, the retrieval verbs return the right rows. None of that
proves the BEHAVIOUR the whole design rests on: that a worker shown a headline about an
area it is about to touch stops and runs `jarvis learn search` before touching it. That
gap is the accepted risk of shipping an index instead of a payload (see
`docs/superpowers/specs/2026-07-27-knowledge-on-demand-design.md`), and it is only
visible behaviourally, so it is measured here.

Unlike the other LLM evals, the subject is **tooled and does real work**. Asking a
tool-less model "what command would you run next?" primes the answer it is being graded
on; the only honest measurement is to put a subject in a sandbox with a real `jarvis` on
its PATH, hand it a real task, and look at what it actually ran.

Each case is rigged the same way:

  * the sandbox knowledge base holds the entry that makes the task correct, **index-only**
    (headline in the prompt, full text only via a retrieval verb) and buried among ~20
    others, so finding it is a lookup and not a coincidence;
  * the headline names the area without giving the answer — enough to know there is
    something to ask about, not enough to act on;
  * the sandbox itself is silent or actively misleading about the answer (the migrations
    dir has no down-file, the existing HTTP call passes no timeout). A subject that
    imitates local precedent gets it wrong. Nothing but the knowledge base gets it right.

Four batteries, in increasing strength, plus a control:

  * retrieval/lookup       — a read verb was run at all
  * retrieval/before-acting — ...before the first file in the sandbox was written
  * retrieval/applies      — ...and the change reflects what the entry actually said
  * retrieval/bounded      — and it did not just dump the base into its own context,
                             which is the problem the index was built to remove
  * retrieval/control      — the two least guessable cases, run again with NO knowledge
                             base, must FAIL. An eval whose subject can pass blind is
                             scoring the model's priors, not the OS's plumbing, and this
                             battery is what stops that going unnoticed.

If these score badly the lever is a louder nudge (a SessionStart hook, a stronger
contract bullet) — not a return to bulk injection, which fails for cost reasons the
behaviour of any one worker cannot fix.

Measured on 2026-07-28 (sonnet, PR #27's prompt): 5/5 on lookup, before-acting and
applies across two runs, 0 bulk dumps, control clean. The thresholds below sit one or
two cases under that on purpose — a single miss on an LLM battery is noise, a second is
the signal.

Opt-in (spends tokens and runs a real agent, needs a logged-in Claude Code):
    JARVIS_EVALS_LLM=1 pytest evals/llm/test_knowledge_retrieval.py -q
    JARVIS_EVALS_MODEL=opus  # optional, default sonnet
"""

from __future__ import annotations

import os
import re
import stat
import sys
import textwrap

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from jarvis import claude_cli
from jarvis.catalog import ProjectSpec
from jarvis.central_store import CentralStore
from jarvis.dispatch import build_worker_prompt

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario
MODEL = os.environ.get("JARVIS_EVALS_MODEL", "sonnet")
PROJECT = "reports_app"

#: Tool set the subject gets. Same shape as a real worker's: it must be able to look
#: around, run `jarvis`, and edit files.
TOOLS = "Bash,Read,Write,Edit,Glob,Grep"

#: Real workers are dispatched with `auto` (catalog.DEFAULT_PERMISSION_MODE) because a
#: non-interactive session cannot answer a permission prompt. Same constraint here, so
#: same mode — this is the production configuration, not an escalation of it.
PERMISSION_MODE = "auto"

#: One case is one agent run in a sandbox; generous, because a stalled run that times
#: out is indistinguishable from a subject that chose not to retrieve.
CASE_TIMEOUT = 420

WO = {
    "id": "wo-eval-kb",
    "title": "small change in reports_app",
    "description": "See the task in this turn.",
}


# --------------------------------------------------------------------------------------
# Scenarios
# --------------------------------------------------------------------------------------

@dataclass
class Case:
    name: str
    #: What the work order asks for. Deliberately says nothing about the knowledge base.
    task: str
    #: Sandbox project tree: relative path -> contents.
    files: dict[str, str]
    #: Topic the deciding entry is filed under, and its full text. Only the first line
    #: reaches the prompt (central_store.headline), so the actionable part must not be
    #: on it.
    topic: str
    learning: str
    #: Did the resulting tree act on what the entry said?
    applied: Callable[[Path], bool] = field(repr=False)


def _read_all(root: Path) -> str:
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                out.append(p.read_text())
            except (OSError, UnicodeDecodeError):
                pass
    return "\n".join(out)


def _deploy_applied(root: Path) -> bool:
    return "vendor-freeze" in _read_all(root)


def _migration_applied(root: Path) -> bool:
    downs = list((root / "migrations").glob("*_down.sql")) if (root / "migrations").is_dir() \
        else list(root.rglob("*_down.sql"))
    return bool(downs)


def _timeout_applied(root: Path) -> bool:
    return bool(re.search(r"timeout\s*=\s*90\b", _read_all(root)))


def _staging_applied(root: Path) -> bool:
    return "staging-2" in _read_all(root)


def _error_style_applied(root: Path) -> bool:
    """The house rule, not the obvious half of it.

    `ReportError` is guessable from errors.py alone — a subject that never searched can
    land on it by reading three class names. The lowercase, no-trailing-period message
    is not guessable; it exists only in the knowledge base. Both must hold.
    """
    text = (root / "reports.py").read_text() if (root / "reports.py").exists() else ""
    if "ReportError" not in text:
        return False
    raised = re.findall(r"ReportError\(\s*(?:f?)([\"'])(.*?)\1", text, re.S)
    msgs = [m for _, m in raised]
    return bool(msgs) and all(
        m and not m[0].isupper() and not m.rstrip().endswith(".") for m in msgs)


CASES = [
    Case(
        name="deploy-pre-step",
        task=("Add a `deploy-staging` target to the Makefile. It should build the app "
              "and push it to the staging environment, following whatever this "
              "project's deploy conventions are."),
        files={
            "Makefile": textwrap.dedent("""\
                .PHONY: test lint fmt

                test:
                \tpytest -q

                lint:
                \truff check src tests

                fmt:
                \truff format src tests
                """),
            "README.md": "# reports_app\n\nReporting service.\n",
        },
        topic="deploy",
        learning=(
            "Deploying reports_app: every deploy entrypoint has one mandatory pre-step, "
            "and the Makefile does not encode it.\n"
            "`make vendor-freeze` MUST run immediately before the push step of any "
            "deploy target. Skip it and the deploy ships whatever happened to be in "
            "vendor/ after the last local build — we shipped a stale bundle to "
            "production twice that way before writing this down. There is no CI check "
            "for it; this entry is the only place the rule exists."),
        applied=_deploy_applied,
    ),
    Case(
        name="migration-down-file",
        task=("Add a database migration that adds an `archived_at` timestamp column to "
              "the `reports` table."),
        files={
            "migrations/001_init.sql": (
                "CREATE TABLE reports (\n"
                "    id TEXT PRIMARY KEY,\n"
                "    tenant_id TEXT NOT NULL,\n"
                "    created_at TIMESTAMP NOT NULL\n"
                ");\n"),
            "migrate.py": textwrap.dedent("""\
                \"\"\"Migration runner. Applies migrations/ in filename order.\"\"\"


                def main() -> None:
                    raise NotImplementedError
                """),
        },
        topic="migrations",
        learning=(
            "reports_app migrations are two files, not one — the single-file shape in "
            "migrations/ is legacy and the runner rejects it.\n"
            "Everything after 001 must ship as BOTH `migrations/NNN_<slug>_up.sql` and "
            "`migrations/NNN_<slug>_down.sql`. migrate.py exits 2 without a word if the "
            "down file is missing, so a one-file migration looks like a runner crash "
            "rather than a missing file. 001_init.sql predates the rule and is "
            "special-cased."),
        applied=_migration_applied,
    ),
    Case(
        name="reports-endpoint-timeout",
        task=("Add a `fetch_report_summary(report_id)` helper to client.py that GETs "
              "/reports/summary?id=<report_id> and returns the parsed body."),
        files={
            "client.py": textwrap.dedent("""\
                import http_client

                BASE = "https://api.reports.internal"


                def fetch_health():
                    return http_client.get(f"{BASE}/health")
                """),
        },
        topic="reports-api",
        learning=(
            "Calls into reports_app's /reports/* endpoints need an explicit timeout; "
            "the client default is right for the rest of the API and wrong for those.\n"
            "http_client.get defaults to a 5 second timeout. That suits /health and "
            "nothing under /reports — those routinely take 40-60 seconds once a tenant "
            "has history, so every /reports call must pass timeout=90 explicitly. The "
            "global default is deliberately low; raising it is not the fix and has been "
            "rejected once already."),
        applied=_timeout_applied,
    ),
    Case(
        name="staging-hostname",
        task=("Add a `STAGING_URL` setting to config.py pointing at the staging API, "
              "next to the existing settings."),
        files={
            "config.py": textwrap.dedent("""\
                API_URL = "https://api.reports.internal"
                LOG_LEVEL = "info"
                REQUEST_ID_HEADER = "X-Request-Id"
                """),
            "docs/environments.md": (
                "# Environments\n\n"
                "| env | notes |\n"
                "|---|---|\n"
                "| production | the real thing |\n"
                "| staging | a full copy, safe to break |\n"),
        },
        topic="environments",
        learning=(
            "reports_app's staging API is not at the hostname the docs and the older "
            "configs point at.\n"
            "Staging moved to https://staging-2.reports.internal in June. The old "
            "staging.reports.internal still resolves and still answers 200, but it "
            "serves a frozen snapshot from before the move — so anything aimed at it "
            "looks healthy and silently tests nothing. Always use staging-2; the old "
            "name is kept alive only for a decommissioned dashboard."),
        applied=_staging_applied,
    ),
    Case(
        name="user-facing-error-style",
        task=("Make `load_report()` in reports.py fail cleanly when the report id is "
              "unknown, instead of returning None."),
        files={
            "errors.py": textwrap.dedent('''\
                class ReportError(Exception):
                    """Base for anything the UI shows the user."""


                class ConfigError(Exception):
                    """Bad configuration, surfaced at boot."""


                class TransientError(Exception):
                    """Retryable; never reaches the UI."""
                '''),
            "reports.py": textwrap.dedent('''\
                REPORTS: dict[str, dict] = {}


                def load_report(report_id: str):
                    """Return the report, or None if there is no such id."""
                    return REPORTS.get(report_id)


                def rename_report(report_id: str, name: str):
                    report = REPORTS[report_id]
                    if not name:
                        raise ValueError("Name must not be empty.")
                    report["name"] = name
                '''),
        },
        topic="conventions",
        learning=(
            "reports_app has a house rule for user-facing failure messages that "
            "reviewers reject PRs over, and the existing code does not follow it.\n"
            "Raise ReportError (errors.py) and give it a message that starts lowercase "
            "and has NO trailing period — the UI appends its own punctuation, so "
            "\"Report not found.\" renders as \"Report not found..\". Bare "
            "ValueError/KeyError escaping into a request handler is the most common "
            "review comment on this repo; rename_report is the un-migrated example, not "
            "the pattern to copy."),
        applied=_error_style_applied,
    ),
]

#: Filler so the index is a real index — the deciding entry has to be picked out of a
#: crowd, the way it would be in the fleet's actual base.
NOISE = [
    ("ci", "CI runs on push to any branch; the matrix is python 3.11 and 3.12 only."),
    ("ci", "The flaky-test quarantine list lives in ci/quarantine.txt and is reviewed "
           "monthly."),
    ("ci", "Coverage gate is 80% on changed lines, not on the whole repo."),
    ("dashboard", "The ops dashboard is served from the same process as the API; a "
                  "dashboard 500 does not mean the API is down."),
    ("dashboard", "Dashboard templates are Jinja and are NOT auto-reloaded in prod."),
    ("tooling", "This repo uses uv, not pip-tools; requirements.txt is generated."),
    ("tooling", "ruff replaced flake8 and isort in April; the old configs are gone."),
    ("tooling", "Type checking is pyright in basic mode, run only in CI."),
    ("onboarding", "New contributors need read access to the reports S3 bucket before "
                   "anything local works."),
    ("onboarding", "The seed dataset is 400MB; fetch it with scripts/fetch-seed.sh "
                   "rather than cloning it."),
    ("history", "The tenant_id column was backfilled in March; rows before that date "
                "have a synthetic value."),
    ("history", "The v1 export format was retired in February and is not supported."),
    ("history", "Report ids were renumbered once; anything that hardcodes an id from "
                "before the renumber is wrong."),
    ("performance", "The reports list query is the slowest endpoint and is already "
                    "indexed; do not add more indexes without a plan."),
    ("performance", "Pagination in the API uses cursors; offset/limit is only in the "
                    "internal admin views."),
    ("security", "Tenant scoping is enforced in the repository layer, never in the "
                 "handlers."),
    ("security", "Audit log entries are append-only and must never be rewritten."),
    ("docs", "The public API docs are generated from the OpenAPI file, which is "
             "hand-maintained and drifts."),
]

#: Two safety rails, injected verbatim in every prompt. They also make the eval sensitive
#: to a subject that confuses "pinned, already given to me" with "indexed, go fetch it".
PINNED = [
    ("safety", "Never run destructive SQL against a database whose name does not start "
               "with 'dev_'."),
    ("safety", "Do not commit anything under secrets/ — the directory is gitignored on "
               "purpose and the ignore has been lost twice."),
]


# --------------------------------------------------------------------------------------
# Sandbox
# --------------------------------------------------------------------------------------

#: Read verbs from the retrieval contract. `add` is deliberately not here: writing to the
#: knowledge base is the other half of the bullet and is not what this eval measures.
READ_VERBS = {"search", "show", "list", "topics"}

SHIM = '''\
#!{python}
"""Stand-in `jarvis` for the retrieval eval.

`learn` reaches the real CLI against the sandbox JARVIS_HOME, so retrieval is genuinely
end-to-end: the index the subject was handed and the rows it gets back come from one
store. Every other subcommand is acknowledged and not run — the sandbox has no work
order, and a subject stuck in a retry loop against `jarvis wo finish` tells us nothing
about retrieval while costing a full run.
"""
import os
import runpy
import sys
import time

LOG = {log!r}
HOME = {home!r}

with open(LOG, "a") as fh:
    fh.write(str(time.time()) + "\\t" + "\\x1f".join(sys.argv[1:]) + "\\n")

if sys.argv[1:2] == ["learn"]:
    os.environ["JARVIS_HOME"] = HOME
    sys.path.insert(0, {srcdir!r})
    sys.argv[0] = "jarvis"
    runpy.run_module("jarvis.cli", run_name="__main__")
else:
    print("(sandbox) noted: jarvis " + " ".join(sys.argv[1:]))
'''


def _seed_knowledge(home: Path) -> None:
    """Fill the sandbox store. Addressed by explicit path, never by JARVIS_HOME: the
    harness process must stay in its own (pytest-isolated) home, or the subject's store
    and the test runner's start fighting over one env var."""
    central = CentralStore(home / "os.db")
    for topic, content in NOISE:
        central.add_knowledge(content, project=PROJECT, topic=topic)
    for case in CASES:
        central.add_knowledge(case.learning, project=PROJECT, topic=case.topic)
    for topic, content in PINNED:
        central.add_knowledge(content, project=PROJECT, topic=topic, tags="pinned")
    central.close()


@dataclass
class Run:
    case: Case
    calls: list[tuple[float, list[str]]]
    first_mutation: float | None
    reply: str
    root: Path

    @property
    def reads(self) -> list[tuple[float, list[str]]]:
        return [(ts, a) for ts, a in self.calls
                if a[:1] == ["learn"] and a[1:2] and a[1] in READ_VERBS]

    @property
    def retrieved(self) -> bool:
        return bool(self.reads)

    @property
    def retrieved_before_acting(self) -> bool:
        if not self.reads:
            return False
        if self.first_mutation is None:  # never touched a file: nothing to be late for
            return True
        return self.reads[0][0] < self.first_mutation

    @property
    def bulk_dumped(self) -> bool:
        """Pulled the base into context wholesale instead of asking a question of it.

        `learn list --full` and a huge --limit are exactly the payload the index
        replaced; a subject that reaches for them has kept the old habit and the bound
        on prompt cost is only nominal.
        """
        for _, argv in self.reads:
            if "--full" in argv:
                return True
            if "--limit" in argv:
                idx = argv.index("--limit")
                if idx + 1 < len(argv) and argv[idx + 1].isdigit() \
                        and int(argv[idx + 1]) > 60:
                    return True
            if argv[1:2] == ["show"] and len(argv) - 2 > 10:
                return True
        return False

    def summary(self) -> str:
        shown = "; ".join(" ".join(a) for _, a in self.calls[:6]) or "(no jarvis calls)"
        return f"{self.case.name}: {shown}"


def _snapshot(root: Path) -> dict[Path, tuple[float, int]]:
    return {p: (p.stat().st_mtime, p.stat().st_size)
            for p in root.rglob("*") if p.is_file()}


def _first_mutation(root: Path, before: dict[Path, tuple[float, int]]) -> float | None:
    """When the subject first wrote to the project tree, by mtime.

    Same clock the shim stamps its log with, so the two are directly comparable — which
    is what turns "it retrieved" into "it retrieved *before it acted*".
    """
    changed = [st[0] for p, st in _snapshot(root).items() if before.get(p) != st]
    return min(changed) if changed else None


def _sandbox(root: Path, case: Case, home: Path) -> tuple[Path, Path, dict[str, str]]:
    """Lay out one case: the project tree, a `jarvis` shim, and the env that finds it.

    Returns (project dir, call log, env overrides). The shim always exists, even for the
    control runs against an empty store — otherwise `jarvis` resolves to whatever is on
    the machine's PATH and the eval quietly depends on the developer's install.
    """
    project = root / "project"
    for rel, body in case.files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    log = root / "jarvis-calls.tsv"
    bindir = root / "bin"
    bindir.mkdir()
    shim = bindir / "jarvis"
    shim.write_text(SHIM.format(
        python=sys.executable, log=str(log), home=str(home),
        srcdir=str(Path(__file__).resolve().parents[2] / "src")))
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {
        "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
        # The subject's `jarvis` must not see the harness's home, and the harness must
        # not see the subject's.
        "JARVIS_HOME": str(home),
    }
    return project, log, env


def _read_log(log: Path) -> list[tuple[float, list[str]]]:
    if not log.exists():
        return []
    calls = []
    for line in log.read_text().splitlines():
        ts, _, argv = line.partition("\t")
        calls.append((float(ts), argv.split("\x1f") if argv else []))
    return calls


TURN = (
    "\n\n# This turn\n"
    "Do this task in the current directory:\n\n"
    "{task}\n\n"
    "This work order is scoped to the change itself: no git, no branch, no PR, and no "
    "`jarvis wo finish` — landing it is a separate work order. When the change is made, "
    "reply with a short paragraph describing what you did and why.\n"
)


@pytest.fixture(scope="module")
def sandbox_home(tmp_path_factory) -> Path:
    home = tmp_path_factory.mktemp("kb-home")
    _seed_knowledge(home)
    return home


@pytest.fixture(scope="module")
def contract(sandbox_home) -> str:
    """The REAL dispatch prompt, carrying the REAL index of the seeded store.

    Not a paraphrase and not a hand-written index: if `render_knowledge_block` changes
    shape, this eval re-grades the new shape, which is the only reason it is worth
    running.
    """
    central = CentralStore(sandbox_home / "os.db")
    brief = central.knowledge_brief(PROJECT)
    central.close()
    spec = ProjectSpec(name=PROJECT, path=Path("/nonexistent"))
    prompt = build_worker_prompt(WO, spec, brief)
    assert "This section is an INDEX" in prompt, (
        "the worker prompt is not carrying a knowledge index — this eval is measuring "
        "nothing. (Is this branch still on bulk injection?)")
    for case in CASES:
        assert case.learning.split("\n", 1)[1][:40] not in prompt, (
            f"{case.name}: the deciding text is IN the prompt, so retrieval is not "
            f"needed to answer — the case grades nothing.")
    return prompt


@pytest.fixture(scope="module")
def runs(contract, sandbox_home, tmp_path_factory) -> dict[str, Run]:
    out: dict[str, Run] = {}
    for case in CASES:
        root = tmp_path_factory.mktemp(f"kb-{case.name}")
        project, log, env = _sandbox(root, case, sandbox_home)
        before = _snapshot(project)
        reply = claude_cli.run_headless(
            TURN.format(task=case.task), system_prompt=contract, model=MODEL,
            cwd=project, tools=TOOLS, permission_mode=PERMISSION_MODE,
            timeout=CASE_TIMEOUT, env_extra=env)
        out[case.name] = Run(case=case, calls=_read_log(log),
                             first_mutation=_first_mutation(project, before),
                             reply=reply, root=project)
    _print_breakdown(out)
    return out


def _print_breakdown(runs: dict[str, Run]) -> None:
    """Per-case detail, because four aggregate pass/fails do not tell you whether the
    battery discriminates or whether every case simply happens to be easy. Visible with
    `-s`; the scorecard above it stays the summary."""
    print("\n  case                       looked  first  applied  reads")
    for case in CASES:
        r = runs[case.name]
        first = "yes" if r.retrieved_before_acting else "no"
        print(f"  {case.name:<26} {'yes' if r.retrieved else 'NO':<7} {first:<6} "
              f"{'yes' if case.applied(r.root) else 'NO':<8} "
              f"{len(r.reads)}  {'; '.join(' '.join(a) for _, a in r.reads)[:120]}")


def _report(runs: dict[str, Run], names) -> str:
    return " | ".join(runs[n].summary() for n in names)


@scenario("retrieval-llm/lookup", "an indexed area is looked up before it is touched")
def test_retrieval_happens(runs) -> None:
    """The load-bearing claim of the whole index design.

    A worker that never runs a read verb has been handed a table of contents and treated
    it as the book — which is strictly worse than the bulk injection it replaced, because
    at least that put the text in front of it.
    """
    got = [c.name for c in CASES if runs[c.name].retrieved]
    missed = [c.name for c in CASES if c.name not in got]
    assert len(got) >= 4, (
        f"retrieval {len(got)}/{len(CASES)} — worked from the headline alone: "
        + _report(runs, missed))


@scenario("retrieval-llm/before-acting", "the lookup precedes the first edit")
def test_retrieval_precedes_action(runs) -> None:
    """Retrieving after writing the file is a worker checking its homework, not a
    worker informed by the knowledge base. It still beats never looking, so this is
    graded separately rather than folded into the battery above."""
    ok = [c.name for c in CASES if runs[c.name].retrieved_before_acting]
    late = [c.name for c in CASES if c.name not in ok]
    assert len(ok) >= 3, (
        f"looked first in {len(ok)}/{len(CASES)} — acted then looked (or never "
        f"looked): " + _report(runs, late))


@scenario("retrieval-llm/applies", "what was retrieved changes what was built")
def test_retrieved_knowledge_is_applied(runs) -> None:
    """Retrieval that does not reach the diff bought nothing.

    Every case is rigged so local precedent is absent or points the other way, so a pass
    here is evidence the entry was read and believed, not evidence of a lucky default.
    """
    applied = [c.name for c in CASES if c.applied(runs[c.name].root)]
    missed = [c.name for c in CASES if c.name not in applied]
    assert len(applied) >= 3, (
        f"applied {len(applied)}/{len(CASES)} — the knowledge base did not reach the "
        f"change: " + _report(runs, missed))


#: Cases whose right answer exists nowhere but the knowledge base — no local precedent,
#: no plausible default, nothing to infer `timeout=90` or `staging-2` from. They are the
#: control group: with the index removed they must FAIL, or `applies` above is scoring
#: coincidences.
CONTROL = ["reports-endpoint-timeout", "staging-hostname"]


@pytest.fixture(scope="module")
def blind_runs(tmp_path_factory) -> dict[str, Run]:
    """The same cases, same sandbox, same tools — against an EMPTY knowledge base.

    The store is empty rather than absent, and the shim is still installed, so the only
    variable between this and `runs` is whether the knowledge exists. A difference in
    outcome is therefore attributable to the knowledge and to nothing else.
    """
    empty_home = tmp_path_factory.mktemp("kb-empty-home")
    CentralStore(empty_home / "os.db").close()
    spec = ProjectSpec(name=PROJECT, path=Path("/nonexistent"))
    contract = build_worker_prompt(WO, spec, knowledge=None)
    out: dict[str, Run] = {}
    for case in [c for c in CASES if c.name in CONTROL]:
        root = tmp_path_factory.mktemp(f"blind-{case.name}")
        project, log, env = _sandbox(root, case, empty_home)
        reply = claude_cli.run_headless(
            TURN.format(task=case.task), system_prompt=contract, model=MODEL,
            cwd=project, tools=TOOLS, permission_mode=PERMISSION_MODE,
            timeout=CASE_TIMEOUT, env_extra=env)
        out[case.name] = Run(case=case, calls=_read_log(log), first_mutation=None,
                             reply=reply, root=project)
    print("\n  control (no knowledge base) — 'applied' here must be NO")
    for name, run in out.items():
        print(f"  {name:<26} applied={'YES' if run.case.applied(run.root) else 'no':<4} "
              f"jarvis calls={len(run.calls)}")
    return out


@scenario("retrieval-llm/control", "the same task fails without the knowledge base")
def test_control_cases_are_unguessable(blind_runs) -> None:
    """The eval's own tripwire.

    `retrieval-llm/applies` is only evidence of retrieval if the answer could not have
    been reached any other way. So the two least guessable cases are also run blind — no
    index, no store, same task, same sandbox. If a blind subject lands on `timeout=90`
    or `staging-2` anyway, the case is measuring a default rather than a lookup and must
    be replaced; failing here invalidates the battery above, it does not excuse it.
    """
    guessed = [n for n, run in blind_runs.items() if run.case.applied(run.root)]
    assert not guessed, (
        "a subject with NO knowledge base produced the supposedly KB-only answer for "
        + ", ".join(guessed) + " — those cases do not prove retrieval; replace them.")


@scenario("retrieval-llm/bounded", "retrieval stays a query, not a download")
def test_retrieval_stays_bounded(runs) -> None:
    """The index exists because prompts grew with the base. A worker that answers it
    with `learn list --full` has moved that growth from the prompt into its own context
    and the bound is decorative."""
    dumped = [c.name for c in CASES if runs[c.name].bulk_dumped]
    assert not dumped, (
        "pulled the knowledge base in wholesale instead of querying it: "
        + _report(runs, dumped))
