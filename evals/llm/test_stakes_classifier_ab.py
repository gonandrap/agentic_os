"""Five-arm A/B: does a model read an assumption's stakes better than the regex? (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jarvis import autoreview, claude_cli, stakes

pytestmark = [
    pytest.mark.skipif(not os.environ.get("JARVIS_EVALS_LLM"),
                       reason="LLM evals are opt-in: set JARVIS_EVALS_LLM=1"),
]

scenario = pytest.mark.scenario

N_RUNS = int(os.environ.get("JARVIS_EVALS_N", "3"))
#: SS3.9 arm 5 is a CEILING — how well the reading can be done at all — not something that could ship
CEILING_N = max(1, int(os.environ.get("JARVIS_EVALS_STAKES_CEILING_N", "1")))
CONCURRENCY = max(1, int(os.environ.get("JARVIS_EVALS_CONCURRENCY", "8")))
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-5"
CORPUS_ENV = "JARVIS_STAKES_CORPUS"
CACHE_ENV = "JARVIS_EVALS_STAKES_CACHE"
CACHE_DEFAULT_NAME = "stakes_ab_cache.json"
#: Entries buffered before the cache is rewritten
CACHE_FLUSH_EVERY = 20
SYNTHETIC = Path(__file__).resolve().parents[1] / "data" / "stakes_corpus_synthetic.json"

#: The arm the feature would ship as `validation.stakes_classifier: classifier`
CANDIDATE = os.environ.get("JARVIS_EVALS_STAKES_ARM", "haiku")

#: `regex-tightened` is FREE, like `regex`: both are pure matchers
ARMS = ("regex", "regex-tightened", "haiku", "regex-or-haiku",
        "regex-prefilter-then-haiku-confirms", "sonnet")


def corpus_path() -> Path:
    named = os.environ.get(CORPUS_ENV, "").strip()
    if named and Path(named).expanduser().exists():
        return Path(named).expanduser()
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home and (Path(home).expanduser() / "evals" / "stakes_corpus.json").exists():
        return Path(home).expanduser() / "evals" / "stakes_corpus.json"
    return SYNTHETIC


def cache_path(corpus: Path) -> Path | None:
    """Where replies are cached, or None when `$JARVIS_EVALS_STAKES_CACHE` is set EMPTY"""
    named = os.environ.get(CACHE_ENV)
    if named is None:
        return corpus.parent / CACHE_DEFAULT_NAME
    named = named.strip()
    return Path(named).expanduser() if named else None


def fingerprint(corpus: Path) -> dict[str, str]:
    """EVERYTHING THAT CHANGES WHAT IS BEING MEASURED, hashed"""
    def digest(text: str | bytes) -> str:
        raw = text.encode() if isinstance(text, str) else text
        return hashlib.sha256(raw).hexdigest()[:16]

    return {"persona": digest(stakes.PERSONA),
            "question": digest(stakes.question("")),
            "corpus": digest(corpus.read_bytes())}


#: A model arm whose transport failed on more than this share of its calls IS NOT SCORED. 2% because a...
FAILURE_RATE_CEILING = 0.02


def _poisoned_entry(entry: dict[str, Any]) -> bool:
    """Was this cached entry written by a call that never reached the API?"""
    if "reached" in entry:
        return not entry.get("reached")
    return not entry.get("parsed") and float(entry.get("cost_usd") or 0.0) == 0.0


@dataclass
class Call:
    """One classifier call on one row"""

    row_id: str
    high: bool
    latency_ms: float
    cost_usd: float
    parsed: bool
    cached: bool = False
    reached: bool = True


@dataclass
class CallCache:
    """Replies on disk, keyed `(model, row_id, run_index)`, so an interrupted run resumes"""

    path: Path | None
    fp: dict[str, str]
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Why the file on disk was thrown away, printed with the results
    discarded: str = ""
    #: How many entries the load DROPPED while keeping the rest, and why
    repaired: str = ""
    hits: int = 0
    _pending: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: Path | None, fp: dict[str, str]) -> CallCache:
        if path is None or not path.exists():
            return cls(path, fp)
        try:
            raw = json.loads(path.read_text())
            stored = dict(raw["fingerprint"])
            entries = dict(raw["entries"])
        except Exception as exc:  # noqa: BLE001 — a corrupt cache is a cold cache
            return cls(path, fp, discarded=f"{path.name} unreadable ({exc})")
        changed = sorted(k for k in fp if stored.get(k) != fp[k])
        if changed:
            return cls(path, fp, discarded=(
                f"{path.name} discarded: {', '.join(changed)} changed since it was "
                f"written ({len(entries)} cached replies dropped)"))
        kept = {k: v for k, v in entries.items() if not _poisoned_entry(v)}
        repaired = ""
        if len(kept) != len(entries):
            repaired = (f"{path.name}: dropped {len(entries) - len(kept)} cached calls "
                        f"that never reached the API — a usage limit, most likely — and "
                        f"kept {len(kept)} that did. The dropped ones will be retried.")
            print(repaired)
        return cls(path, fp, entries=kept, repaired=repaired)

    @staticmethod
    def key(model: str, row_id: str, run: int) -> str:
        return f"{model}\x1f{row_id}\x1f{run}"

    def get(self, model: str, row_id: str, run: int) -> Call | None:
        if self.path is None:
            return None
        entry = self.entries.get(self.key(model, row_id, run))
        if entry is None:
            return None
        self.hits += 1
        return Call(row_id, bool(entry["high"]), float(entry["latency_ms"]),
                    float(entry["cost_usd"]), bool(entry["parsed"]), cached=True,
                    reached=True)

    def put(self, model: str, row_id: str, run: int, call: Call) -> None:
        """NOTHING THAT DID NOT REACH THE API IS EVER STORED, so a re-run retries it"""
        if self.path is None or not call.reached:
            return
        with self._lock:
            self.entries[self.key(model, row_id, run)] = {
                "high": call.high, "latency_ms": call.latency_ms,
                "cost_usd": call.cost_usd, "parsed": call.parsed, "reached": True}
            self._pending += 1
            due = self._pending >= CACHE_FLUSH_EVERY
        if due:
            self.flush()

    def flush(self) -> None:
        if self.path is None:
            return
        with self._lock:
            payload = json.dumps({"fingerprint": self.fp, "entries": self.entries})
            self._pending = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(payload)
        os.replace(tmp, self.path)


def classify_runs(rows: list[dict[str, Any]], model: str, cwd: Path, runs: int,
                  cache: CallCache, run_offset: int = 0) -> tuple[list[list[Call]],
                                                                  float]:
    """`runs` passes over `rows`, cache-first, the misses through a thread pool"""
    out: list[list[Call]] = []
    pending: list[tuple[int, int, dict[str, Any]]] = []
    for run in range(runs):
        hits: list[Call | None] = []
        for row in rows:
            hit = cache.get(model, row["id"], run_offset + run)
            hits.append(hit)
            if hit is None:
                pending.append((run, len(hits) - 1, row))
        out.append(hits)  # type: ignore[arg-type]

    started = time.monotonic()
    if pending:
        with concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            futures = {pool.submit(_classify, row, model, cwd): (run, idx, row)
                       for run, idx, row in pending}
            for future in concurrent.futures.as_completed(futures):
                run, idx, row = futures[future]
                call = future.result()
                out[run][idx] = call
                cache.put(model, row["id"], run_offset + run, call)
    wall_s = time.monotonic() - started
    cache.flush()
    return out, wall_s


@dataclass
class Metrics:
    precision: float
    recall: float
    f1: float
    calls: int = 0
    cached: int = 0
    mean_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    mean_cost_usd: float = 0.0
    #: TWO DIFFERENT FINDINGS, never one column: `failed` is the transport
    failed: int = 0
    unparsed: int = 0
    misses: list[str] = field(default_factory=list)


@pytest.fixture(scope="module")
def corpus() -> dict[str, Any]:
    path = corpus_path()
    if not path.exists():  # pragma: no cover - the fixture ships with the repo
        pytest.skip(f"no stakes corpus: set ${CORPUS_ENV} (see "
                    f"evals/tools/build_stakes_corpus.py)")
    rows = json.loads(path.read_text())["rows"]
    if not rows:
        pytest.skip(f"{path} has no rows")
    return {"path": path, "rows": rows,
            "scored": [r for r in rows if not r.get("uncertain")],
            "uncertain": [r for r in rows if r.get("uncertain")]}


@pytest.fixture(scope="module")
def neutral_cwd(tmp_path_factory) -> Path:
    """Outside this repo, so the classifier does not load CLAUDE.md on top of the one sentence it is meant..."""
    return tmp_path_factory.mktemp("jarvis-stakes-ab")


def _classify(row: dict[str, Any], model: str, cwd: Path) -> Call:
    """One call, and WHETHER IT HAPPENED AT ALL is part of the answer"""
    started = time.monotonic()
    try:
        result = claude_cli.run_headless_result(
            stakes.question(row["text"]), system_prompt=stakes.PERSONA, model=model,
            cwd=cwd, timeout=stakes.TIMEOUT, tools="")
    except Exception:  # noqa: BLE001 — the transport failed: no judgement was made
        return Call(row["id"], True, (time.monotonic() - started) * 1000, 0.0, False,
                    reached=False)
    verdict = stakes.read_verdict(result.text, model=result.model or model)
    cost = float((result.usage or {}).get("total_cost_usd") or 0.0)
    return Call(row["id"], verdict.high, (time.monotonic() - started) * 1000, cost,
                verdict.parsed, reached=True)


def _score(rows: list[dict[str, Any]], predicted: dict[str, bool],
           calls: list[Call]) -> Metrics:
    """Precision, recall and F1 on the `high` class, plus what the calls cost"""
    tp = sum(1 for r in rows if r["label"] == "high" and predicted.get(r["id"]))
    fp = sum(1 for r in rows if r["label"] != "high" and predicted.get(r["id"]))
    fn = sum(1 for r in rows if r["label"] == "high" and not predicted.get(r["id"]))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    latencies = sorted(c.latency_ms for c in calls)
    costs = [c.cost_usd for c in calls]
    return Metrics(
        precision=precision, recall=recall, f1=f1, calls=len(calls),
        cached=sum(1 for c in calls if c.cached),
        mean_latency_ms=statistics.mean(latencies) if latencies else 0.0,
        p95_latency_ms=latencies[min(len(latencies) - 1,
                                     int(0.95 * len(latencies)))] if latencies else 0.0,
        mean_cost_usd=statistics.mean(costs) if costs else 0.0,
        failed=sum(1 for c in calls if not c.reached),
        unparsed=sum(1 for c in calls if c.reached and not c.parsed),
        misses=[r["id"] for r in rows if r["label"] == "high"
                and not predicted.get(r["id"])])


def _terminal_line(config, line: str) -> None:
    """Write past pytest's capture: the margin is the finding"""
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
def results(corpus, neutral_cwd, request) -> dict[str, Any]:
    """Every arm scored — haiku N_RUNS times, sonnet CEILING_N times"""
    rows = corpus["rows"]
    scored = corpus["scored"]
    regex = {r["id"]: bool(autoreview.high_stakes_marker(r["text"])) for r in rows}
    tightened = {r["id"]: bool(autoreview.high_stakes_marker(r["text"], tightened=True))
                 for r in rows}

    cache = CallCache.load(cache_path(corpus["path"]), fingerprint(corpus["path"]))
    model_runs: dict[str, list[list[Call]]] = {}
    wall_s: dict[str, float] = {}
    for arm, model, runs in (("haiku", HAIKU, N_RUNS), ("sonnet", SONNET, CEILING_N)):
        model_runs[arm], wall_s[arm] = classify_runs(rows, model, neutral_cwd, runs,
                                                     cache)

    per_run: dict[str, list[Metrics]] = {
        "regex": [_score(scored, regex, [])],
        "regex-tightened": [_score(scored, tightened, [])]}
    for arm in ("haiku", "sonnet"):
        per_run[arm] = [_score(scored, {c.row_id: c.high for c in run}, run)
                        for run in model_runs[arm]]
    per_run["regex-or-haiku"] = [
        _score(scored, {c.row_id: c.high or regex[c.row_id] for c in run}, run)
        for run in model_runs["haiku"]]
    # The prefilter arm calls ONLY where the regex fired
    per_run["regex-prefilter-then-haiku-confirms"] = [
        _score(scored,
               {c.row_id: regex[c.row_id] and c.high for c in run},
               [c for c in run if regex[c.row_id]])
        for run in model_runs["haiku"]]

    out = {"corpus": corpus, "regex": regex, "tightened": tightened, "per_run": per_run,
           "haiku_runs": model_runs["haiku"], "wall_s": wall_s, "cache": cache}
    yield out

    cfg = request.config
    path = corpus["path"]
    _terminal_line(cfg, "")
    _terminal_line(cfg, f"stakes classifier A/B — corpus {path.name} "
                        f"({'SYNTHETIC' if path == SYNTHETIC else 'production'}), "
                        f"{len(scored)} scored rows, "
                        f"{len(corpus['uncertain'])} uncertain excluded, "
                        f"n={N_RUNS} (sonnet n={CEILING_N}), "
                        f"concurrency={CONCURRENCY}")
    if cache.discarded:
        _terminal_line(cfg, f"  cache: {cache.discarded}")
    if cache.repaired:
        _terminal_line(cfg, f"  cache: {cache.repaired}")
    poisoned = poisoned_arms(per_run)
    if poisoned:
        _terminal_line(cfg, f"  NOT MEASURABLE — {_poison_note(poisoned)}")
    # `ms/call` is per-subprocess and unaffected by the pool
    _terminal_line(cfg, f"  {'arm':<38} {'prec':>6} {'recall':>7} {'F1':>6} "
                        f"{'F1 sd':>6} {'calls':>6} {'cached':>7} {'failed':>7} "
                        f"{'unparsed':>9} {'ms/call':>8} "
                        f"{'p95':>8} {'$/call':>9} {'wall s':>8}")
    for arm in ARMS:
        runs = per_run[arm]
        f1s = [m.f1 for m in runs]
        wall = wall_s.get(arm, 0.0)
        _terminal_line(
            cfg,
            f"  {arm:<38} {statistics.mean(m.precision for m in runs):6.2f} "
            f"{statistics.mean(m.recall for m in runs):7.2f} "
            f"{statistics.mean(f1s):6.2f} "
            f"{(statistics.stdev(f1s) if len(f1s) > 1 else 0.0):6.3f} "
            f"{statistics.mean(m.calls for m in runs):6.1f} "
            f"{statistics.mean(m.cached for m in runs):7.1f} "
            f"{statistics.mean(m.failed for m in runs):7.1f} "
            f"{statistics.mean(m.unparsed for m in runs):9.1f} "
            f"{statistics.mean(m.mean_latency_ms for m in runs):8.0f} "
            f"{statistics.mean(m.p95_latency_ms for m in runs):8.0f} "
            f"{statistics.mean(m.mean_cost_usd for m in runs):9.5f} "
            f"{wall:8.1f}")
        if runs[0].misses:
            _terminal_line(cfg, f"      high-stakes rows missed: "
                                f"{sorted(set(sum((m.misses for m in runs), [])))}")
    _terminal_line(cfg, f"  disagreements, regex vs {CANDIDATE} (run 1):")
    first = {c.row_id: c.high for c in model_runs["haiku"][0]}
    candidate = _candidate_predictions(CANDIDATE, regex, model_runs, 0,
                                       {r["id"]: r["text"] for r in rows})
    for r in rows:
        if regex[r["id"]] != candidate.get(r["id"]):
            _terminal_line(cfg, f"      {r['id']:<16} label={r['label']:<7} "
                                f"regex={regex[r['id']]!s:<5} "
                                f"{CANDIDATE}={candidate[r['id']]!s:<5} "
                                f"{r['text'][:90]}")
    unchecked = corpus["uncertain"]
    if unchecked:
        agree = sum(1 for r in unchecked if first.get(r["id"]) == (r["label"] == "high"))
        _terminal_line(cfg, f"  uncertain rows (excluded): {len(unchecked)}, haiku "
                            f"agreed with the unchecked label on {agree}")


def _candidate_predictions(arm: str, regex: dict[str, bool],
                           model_runs: dict[str, list[list[Call]]],
                           run: int,
                           texts: dict[str, str] | None = None) -> dict[str, bool]:
    calls = model_runs["haiku" if arm != "sonnet" else "sonnet"][run]
    texts = texts or {}
    if arm == "regex":
        return dict(regex)
    if arm == "regex-tightened":
        return {r_id: bool(autoreview.high_stakes_marker(text, tightened=True))
                for r_id, text in texts.items()}
    if arm == "regex-or-haiku":
        return {c.row_id: c.high or regex[c.row_id] for c in calls}
    if arm == "regex-prefilter-then-haiku-confirms":
        return {c.row_id: regex[c.row_id] and c.high for c in calls}
    return {c.row_id: c.high for c in calls}


def poisoned_arms(per_run: dict[str, list[Metrics]]) -> list[tuple[str, float, int, int]]:
    """Which MODEL arms made too many calls that never reached the API, worst first"""
    out: list[tuple[str, float, int, int]] = []
    for arm in ("haiku", "sonnet"):
        runs = per_run.get(arm) or []
        calls = sum(m.calls for m in runs)
        failed = sum(m.failed for m in runs)
        if calls and failed / calls > FAILURE_RATE_CEILING:
            out.append((arm, failed / calls, failed, calls))
    return sorted(out, key=lambda row: -row[1])


def _poison_note(poisoned: list[tuple[str, float, int, int]]) -> str:
    arms = "; ".join(f"{arm}: {rate * 100:.1f}% ({failed} of {calls} calls never reached "
                     f"the API)" for arm, rate, failed, calls in poisoned)
    return (f"{arms}. Over the {FAILURE_RATE_CEILING * 100:.0f}% ceiling, so these "
            f"numbers are not a measurement of the model. THE LIKELY CAUSE IS THE "
            f"SESSION'S USAGE LIMIT: a call that never reached the API made no judgement, "
            f"and the `high` it carries is the shipped HOLD rule. Wait for the limit to "
            f"reset and re-run — the cache keeps every call that did reach.")


def _skip_if_poisoned(results: dict[str, Any]) -> None:
    """The other assertions SKIP on a poisoned run rather than pass"""
    poisoned = poisoned_arms(results["per_run"])
    if poisoned:
        pytest.skip(f"arm not measurable — {_poison_note(poisoned)}")


@scenario("stakes-classifier-ab", "no arm is scored off calls that never happened")
def test_no_arm_reports_numbers_from_calls_that_never_reached_the_model(results):
    """THE MEASUREMENT'S OWN PRE-CONDITION"""
    poisoned = poisoned_arms(results["per_run"])

    assert not poisoned, f"REFUSING TO SCORE — {_poison_note(poisoned)}"


@scenario("stakes-classifier-ab", "the candidate never loses a high-stakes case")
def test_the_candidate_recall_is_at_least_the_regexs(results):
    """THE SAFETY HALF, and the bar SS3.9 names"""
    _skip_if_poisoned(results)
    regex = statistics.mean(m.recall for m in results["per_run"]["regex"])
    runs = results["per_run"][CANDIDATE]
    worst = min(m.recall for m in runs)

    assert worst >= regex, (
        f"{CANDIDATE} recall {worst:.2f} is below the regex's {regex:.2f} — it loses "
        f"high-stakes cases the regex holds: "
        f"{sorted(set(sum((m.misses for m in runs), [])))}. SS3.4's rule: ship "
        f"`regex OR haiku` instead.")


@scenario("stakes-classifier-ab", "the candidate is strictly more precise")
def test_the_candidate_precision_beats_the_regexs(results):
    """THE THING BEING BOUGHT"""
    _skip_if_poisoned(results)
    regex = statistics.mean(m.precision for m in results["per_run"]["regex"])
    candidate = statistics.mean(m.precision for m in results["per_run"][CANDIDATE])

    assert candidate > regex, (
        f"{CANDIDATE} precision {candidate:.2f} does not beat the regex's {regex:.2f}. "
        f"The classifier costs a call per pending assumption; equal precision is not "
        f"worth it.")


@scenario("stakes-classifier-ab", "the answer is stable across identical calls")
def test_the_candidate_answers_the_same_way_twice(results):
    """A classifier that answers differently on two identical calls is a fact about the arm"""
    _skip_if_poisoned(results)
    runs = results["per_run"][CANDIDATE]
    if len(runs) < 2:
        pytest.skip("JARVIS_EVALS_N=1: no variance to measure")
    spread = statistics.stdev([m.f1 for m in runs])

    assert spread < 0.15, (
        f"{CANDIDATE} F1 varies by {spread:.3f} across {len(runs)} identical runs — the "
        f"arm is not reproducible enough to hand the settle path to.")


@scenario("stakes-classifier-ab", "the corpus discriminates the arms")
def test_the_corpus_contains_cases_both_nets_can_get_wrong(results):
    """An A/B on a corpus every arm scores 1.0 on proves nothing"""
    _skip_if_poisoned(results)
    rows = results["corpus"]["scored"]
    regex = results["regex"]
    false_positives = [r["id"] for r in rows
                       if r["label"] == "routine" and regex[r["id"]]]
    invisible = [r["id"] for r in rows if r["label"] == "high" and not regex[r["id"]]]

    assert false_positives, "no routine row the regex holds — nothing to improve on"
    assert invisible, "no high-stakes row the regex misses — SS1.1 is not represented"
