"""The stakes A/B's call cache, proven without spending a cent.

`evals/llm/test_stakes_classifier_ab.py` is 495 corpus rows x N runs x 2 models of `claude
-p`, so it runs concurrently and caches every reply on disk to survive an interrupt. Both
of those can make the eval LIE rather than fail: a cache that keeps serving replies after
the prompt or the corpus changed reports a measurement of something that no longer exists,
and a resumed run that reports zero latency and zero cost for its cached half reports a
stopwatch that was not running.

The eval itself is gated on `JARVIS_EVALS_LLM` and never runs here. Everything below
drives the same code paths with a FAKE classifier: no model call, no network.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from jarvis import stakes

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_stakes_classifier_ab.py"


@pytest.fixture(scope="module")
def ab():
    spec = importlib.util.spec_from_file_location("_stakes_ab_under_test", EVAL_PATH)
    assert spec and spec.loader, f"cannot load {EVAL_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves its string annotations through
    # `sys.modules[cls.__module__]`, which is None for a module loaded by hand.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.fixture
def corpus_file(tmp_path) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({"rows": [
        {"id": "r1", "text": "cut the release", "label": "high"},
        {"id": "r2", "text": "renamed a local", "label": "routine"},
    ]}))
    return path


def _rows(corpus_file: Path) -> list[dict]:
    return json.loads(corpus_file.read_text())["rows"]


def _fake(ab, counter: list[int], high=lambda row: row["label"] == "high"):
    def fake_classify(row, model, cwd):
        counter[0] += 1
        return ab.Call(row["id"], high(row), 1234.5, 0.004, True)
    return fake_classify


def test_a_resumed_run_makes_no_call_it_already_paid_for(ab, corpus_file, tmp_path,
                                                         monkeypatch):
    calls = [0]
    monkeypatch.setattr(ab, "_classify", _fake(ab, calls))
    cache_path = tmp_path / "cache.json"
    rows = _rows(corpus_file)

    first_cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    first, _wall = ab.classify_runs(rows, "haiku-x", tmp_path, 2, first_cache)
    first_cache.flush()
    assert calls[0] == 4, "2 rows x 2 runs is 4 real calls on a cold cache"

    second_cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    second, _wall2 = ab.classify_runs(rows, "haiku-x", tmp_path, 2, second_cache)

    assert calls[0] == 4, "a warm cache must make no further calls"
    assert not second_cache.discarded
    assert [[(c.row_id, c.high, c.latency_ms, c.cost_usd, c.parsed) for c in run]
            for run in second] == \
           [[(c.row_id, c.high, c.latency_ms, c.cost_usd, c.parsed) for c in run]
            for run in first], "a cached run must reproduce the same table"
    assert all(c.cached for run in second for c in run)
    assert not any(c.cached for run in first for c in run)


def test_the_cache_is_keyed_per_run_not_per_row(ab, corpus_file, tmp_path, monkeypatch):
    """Run 2 must not be served run 1's reply: the variance assertion measures exactly the
    difference between two identical calls, and a row-keyed cache would report 0.000."""
    seen: list[int] = []
    run_index = [0]

    def fake_classify(row, model, cwd):
        seen.append(run_index[0])
        return ab.Call(row["id"], run_index[0] == 0, 1.0, 0.0, True)

    monkeypatch.setattr(ab, "_classify", fake_classify)
    cache = ab.CallCache.load(tmp_path / "c.json", ab.fingerprint(corpus_file))
    rows = _rows(corpus_file)
    runs = []
    for i in range(2):  # sequential so the fake can tell the runs apart
        run_index[0] = i
        runs += ab.classify_runs(rows, "m", tmp_path, 1, cache, run_offset=i)[0]

    assert len(seen) == 4
    assert [c.high for c in runs[0]] == [True, True]
    assert [c.high for c in runs[1]] == [False, False]


def test_the_cache_is_discarded_when_the_prompt_changes(ab, corpus_file, tmp_path,
                                                        monkeypatch):
    calls = [0]
    monkeypatch.setattr(ab, "_classify", _fake(ab, calls))
    cache_path = tmp_path / "cache.json"
    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)
    cache.flush()

    monkeypatch.setattr(stakes, "PERSONA", stakes.PERSONA + "\nAND ALSO: be terse.")
    reopened = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))

    assert reopened.entries == {}
    assert "persona" in reopened.discarded, reopened.discarded
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, reopened)
    assert calls[0] == 4, "a discarded cache must re-run every call"


def test_the_cache_is_discarded_when_the_corpus_changes(ab, corpus_file, tmp_path,
                                                        monkeypatch):
    monkeypatch.setattr(ab, "_classify", _fake(ab, [0]))
    cache_path = tmp_path / "cache.json"
    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)
    cache.flush()

    rows = json.loads(corpus_file.read_text())
    rows["rows"][1]["text"] = "force-pushed the release branch"
    corpus_file.write_text(json.dumps(rows))
    reopened = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))

    assert reopened.entries == {}
    assert "corpus" in reopened.discarded, reopened.discarded


def test_the_cache_survives_an_interrupt_before_the_run_ends(ab, corpus_file, tmp_path,
                                                             monkeypatch):
    """Written DURING the run, not at the end — the interrupted run is the case it is for.

    The flush threshold is bypassed for the same reason a real interrupt would be: the
    file on disk must already name the calls that completed.
    """
    monkeypatch.setattr(ab, "CACHE_FLUSH_EVERY", 1)
    monkeypatch.setattr(ab, "_classify", _fake(ab, [0]))
    cache_path = tmp_path / "cache.json"
    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))

    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)

    on_disk = json.loads(cache_path.read_text())
    assert len(on_disk["entries"]) == 2
    assert on_disk["fingerprint"] == ab.fingerprint(corpus_file)


def test_an_empty_cache_env_disables_the_cache(ab, corpus_file, tmp_path, monkeypatch):
    monkeypatch.setenv(ab.CACHE_ENV, "")
    assert ab.cache_path(corpus_file) is None

    monkeypatch.delenv(ab.CACHE_ENV)
    assert ab.cache_path(corpus_file) == corpus_file.parent / ab.CACHE_DEFAULT_NAME

    monkeypatch.setattr(ab, "_classify", _fake(ab, [0]))
    cache = ab.CallCache.load(None, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)
    cache.flush()
    assert not (tmp_path / ab.CACHE_DEFAULT_NAME).exists()
    assert not list(tmp_path.glob("*cache*")), "a disabled cache must write nothing"


def test_the_ceiling_arm_defaults_to_one_run_and_variance_skips(ab):
    """`CEILING_N=1` is the point of the knob; the variance assertion is about the
    CANDIDATE, so with a one-run arm it must skip rather than take stdev of one sample."""
    assert ab.CEILING_N == 1
    assert ab.N_RUNS == 3

    one_run = {"per_run": {ab.CANDIDATE: [ab.Metrics(1.0, 1.0, 1.0)]}}
    with pytest.raises(BaseException) as excinfo:
        ab.test_the_candidate_answers_the_same_way_twice(one_run)
    assert excinfo.typename == "Skipped"


# -- the cache must never store a call that never reached the API -----------------------
#
# MEASURED, and it is why this section exists: of 1485 haiku entries in the run of
# 2026-09-25, 1280 had `parsed=False` and `cost_usd == 0.0` — every one made while the
# session's usage limit was in force, turned into a HELD `Call` by `_classify`'s except
# path and then CACHED. The scorecard reported haiku precision 0.04 as haiku's judgement
# when it was a transport outage. `HELD` is the right SHIPPED behaviour for the daemon; a
# measurement is not a decision, and an unreachable model made no judgement to record.


def _failed(ab, row_id: str = "r1"):
    return ab.Call(row_id, True, 5.0, 0.0, False, reached=False)


def test_a_call_that_never_reached_the_api_is_not_cached(ab, corpus_file, tmp_path,
                                                        monkeypatch):
    def fake_classify(row, model, cwd):
        return _failed(ab, row["id"]) if row["id"] == "r1" else \
            ab.Call(row["id"], False, 9.0, 0.004, True)

    monkeypatch.setattr(ab, "_classify", fake_classify)
    cache_path = tmp_path / "cache.json"
    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)

    on_disk = json.loads(cache_path.read_text())["entries"]
    assert [k.split("\x1f")[1] for k in on_disk] == ["r2"], on_disk


def test_a_re_run_retries_every_failed_call(ab, corpus_file, tmp_path, monkeypatch):
    attempts: list[str] = []
    fail = [True]

    def fake_classify(row, model, cwd):
        attempts.append(row["id"])
        if fail[0] and row["id"] == "r1":
            return _failed(ab, row["id"])
        return ab.Call(row["id"], row["label"] == "high", 9.0, 0.004, True)

    monkeypatch.setattr(ab, "_classify", fake_classify)
    cache_path = tmp_path / "cache.json"
    first = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, first)

    fail[0] = False
    attempts.clear()
    second = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    runs, _wall = ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, second)

    assert attempts == ["r1"], "only the failed row is re-bought"
    assert {c.row_id: c.reached for c in runs[0]} == {"r1": True, "r2": True}


def test_reached_is_not_inferred_from_a_zero_cost(ab, corpus_file, tmp_path, monkeypatch):
    """A cheap call served off a cached prefix can legitimately report $0.00. Dropping it
    would throw away real judgements, so `reached` is set explicitly and only False on
    `_classify`'s exception path."""
    monkeypatch.setattr(ab, "_classify",
                        lambda row, model, cwd: ab.Call(row["id"], True, 9.0, 0.0, True))
    cache_path = tmp_path / "cache.json"
    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    ab.classify_runs(_rows(corpus_file), "m", tmp_path, 1, cache)

    assert len(json.loads(cache_path.read_text())["entries"]) == 2
    reopened = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))
    assert len(reopened.entries) == 2, reopened.repaired
    assert not reopened.repaired


def test_a_transport_failure_and_an_unreadable_reply_are_different_findings(
        ab, tmp_path, monkeypatch):
    """Both used to land as `parsed=False`. One says the model was never asked, the other
    says it answered something nobody can read; the scorecard shows them apart."""
    def transport(*_a, **_kw):
        raise RuntimeError("usage limit reached")

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    failed = ab._classify({"id": "r1", "text": "x"}, "m", tmp_path)
    assert (failed.reached, failed.parsed) == (False, False)

    monkeypatch.setattr(ab.claude_cli, "run_headless_result",
                        lambda *a, **kw: ab.claude_cli.HeadlessResult(
                            text="I think it's fine", usage={}, session_id="s",
                            model="m"))
    unparsed = ab._classify({"id": "r2", "text": "x"}, "m", tmp_path)
    assert (unparsed.reached, unparsed.parsed) == (True, False)

    metrics = ab._score(
        [{"id": "r1", "label": "high"}, {"id": "r2", "label": "high"},
         {"id": "r3", "label": "high"}],
        {"r1": True, "r2": True, "r3": True},
        [failed, unparsed, ab.Call("r3", True, 1.0, 0.004, True)])
    assert (metrics.failed, metrics.unparsed) == (1, 1)


def test_the_cache_drops_poisoned_entries_on_load(ab, corpus_file, tmp_path):
    """The one-shot repair: the file written by the poisoned run heals itself, keeping
    every call that did reach the API."""
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({
        "fingerprint": ab.fingerprint(corpus_file),
        "entries": {
            "m\x1fr1\x1f0": {"high": True, "latency_ms": 9.0, "cost_usd": 0.004,
                             "parsed": True, "reached": True},
            "m\x1fr2\x1f0": {"high": True, "latency_ms": 5.0, "cost_usd": 0.0,
                             "parsed": False, "reached": False},
            "m\x1fr3\x1f0": {"high": True, "latency_ms": 5.0, "cost_usd": 0.0,
                             "parsed": False},
            "m\x1fr4\x1f0": {"high": False, "latency_ms": 9.0, "cost_usd": 0.004,
                             "parsed": True},
        }}))

    cache = ab.CallCache.load(cache_path, ab.fingerprint(corpus_file))

    assert sorted(k.split("\x1f")[1] for k in cache.entries) == ["r1", "r4"]
    assert "2" in cache.repaired and "never reached" in cache.repaired, cache.repaired
    assert not cache.discarded, "a repair is not a discard: the good calls are kept"
    assert cache.get("m", "r2", 0) is None


def test_a_poisoned_arm_refuses_to_be_scored(ab):
    """An arm with a 5% transport-failure rate reporting a precision to three decimals is
    exactly what happened. A printed warning would not have stopped it."""
    clean = [ab.Metrics(0.5, 0.5, 0.5, calls=100, failed=2)]
    poisoned = [ab.Metrics(0.04, 0.9, 0.07, calls=100, failed=5)]

    assert ab.poisoned_arms({"haiku": clean, "sonnet": clean}) == []
    named = ab.poisoned_arms({"haiku": poisoned, "sonnet": clean})
    assert [a[0] for a in named] == ["haiku"]

    with pytest.raises(AssertionError) as excinfo:
        ab.test_no_arm_reports_numbers_from_calls_that_never_reached_the_model(
            {"per_run": {"haiku": poisoned, "sonnet": clean}})
    message = str(excinfo.value)
    assert "haiku" in message and "5.0%" in message
    assert "usage limit" in message.lower(), "the likely cause must be named"


def test_the_other_assertions_skip_rather_than_pass_on_a_poisoned_arm(ab):
    """Their numbers are computed from holds nobody ruled on. Passing green off those is
    the failure mode; skipping says the run has to be repeated."""
    poisoned = {"per_run": {
        "haiku": [ab.Metrics(1.0, 1.0, 1.0, calls=100, failed=50)],
        "sonnet": [ab.Metrics(1.0, 1.0, 1.0, calls=100, failed=0)],
        "regex": [ab.Metrics(0.14, 0.667, 0.23)],
        ab.CANDIDATE: [ab.Metrics(1.0, 1.0, 1.0, calls=100, failed=50),
                       ab.Metrics(1.0, 1.0, 1.0, calls=100, failed=50)]},
        "corpus": {"scored": []}, "regex": {}}

    for test in (ab.test_the_candidate_recall_is_at_least_the_regexs,
                 ab.test_the_candidate_precision_beats_the_regexs,
                 ab.test_the_candidate_answers_the_same_way_twice,
                 ab.test_the_corpus_contains_cases_both_nets_can_get_wrong):
        with pytest.raises(BaseException) as excinfo:
            test(poisoned)
        assert excinfo.typename == "Skipped", f"{test.__name__} did not skip"
        assert "haiku" in str(excinfo.value)


def test_the_eval_gives_the_classifier_no_tools_either(ab, tmp_path, monkeypatch):
    """The same pin as the daemon's. A classifier that can read the repository is a
    different classifier from the one these numbers measure."""
    seen: dict = {}

    def transport(prompt, **kw):
        seen.update(kw)
        return ab.claude_cli.HeadlessResult(
            text=json.dumps({"high": False, "category": "none", "reason": "a mention"}),
            usage={"total_cost_usd": 0.001}, session_id="s", model="m")

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    call = ab._classify({"id": "r1", "text": "renamed a local"}, "m", tmp_path)

    assert call.reached and call.parsed
    assert seen["tools"] == "", f"the eval left the classifier its tools: {seen!r}"
