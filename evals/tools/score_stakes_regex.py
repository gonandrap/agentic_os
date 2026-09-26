"""Score the SHIPPED net against the TIGHTENED one on the labelled corpus. NO MODEL CALL.

docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md §6-§7.

The A/B (`evals/llm/test_stakes_classifier_ab.py`) costs real calls and a session's usage
limit, so it is opt-in and it caches. THIS COSTS NOTHING: both nets are regexes, so every
narrowing can be re-scored on every row in under a second, and each rule in
`autoreview.HIGH_STAKES_TIGHTENED` was derived with this loop rather than by reading.

Reads the corpus from `$JARVIS_STAKES_CORPUS` (or `$JARVIS_HOME/evals/stakes_corpus.json`,
or the committed synthetic fixture). THE PRODUCTION CORPUS IS NOT IN THIS REPO and must not
be — it is verbatim production prose and the repository is public (Neo, question 650).

    JARVIS_STAKES_CORPUS=/tmp/wo8a3/stakes_corpus.json \
        uv run python evals/tools/score_stakes_regex.py

`--diff-only` prints just the rows the two nets disagree on. Precision and recall are over
the CERTAIN rows; the uncertain ones are reported separately and are never scored, the same
split the A/B asserts under.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from jarvis import autoreview  # noqa: E402 — after the path insert

SYNTHETIC = ROOT / "evals" / "data" / "stakes_corpus_synthetic.json"
#: The bar §3.9 set for the A/B, and the bar the tightened net has to clear too: the
#: shipped regex's own recall, at better than its precision.
BAR_RECALL = 0.667
BAR_PRECISION = 0.140


@dataclass
class Metrics:
    name: str
    n: int
    held: int
    tp: int
    fp: int
    fn: int
    precision: float
    recall: float
    f1: float


def corpus_path() -> Path:
    named = os.environ.get("JARVIS_STAKES_CORPUS", "").strip()
    if named and Path(named).expanduser().exists():
        return Path(named).expanduser()
    home = os.environ.get("JARVIS_HOME", "").strip()
    if home and (Path(home).expanduser() / "evals" / "stakes_corpus.json").exists():
        return Path(home).expanduser() / "evals" / "stakes_corpus.json"
    return SYNTHETIC


def score(name: str, rows: list[dict], marker) -> Metrics:
    tp = fp = fn = 0
    for r in rows:
        held = bool(marker(r["text"]))
        gold = r["label"] == "high"
        if held and gold:
            tp += 1
        elif held:
            fp += 1
        elif gold:
            fn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return Metrics(name, len(rows), tp + fp, tp, fp, fn, precision, recall, f1)


def line(m: Metrics) -> str:
    return (f"  {m.name:<16} n={m.n:<4} held={m.held:<4} TP={m.tp:<3} FP={m.fp:<3} "
            f"FN={m.fn:<3} precision={m.precision:.3f} recall={m.recall:.3f} "
            f"F1={m.f1:.3f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--diff-only", action="store_true",
                    help="only the rows the two nets disagree on")
    ap.add_argument("--chars", type=int, default=120, help="row text shown, in chars")
    args = ap.parse_args()

    path = corpus_path()
    rows = json.loads(path.read_text())["rows"]
    certain = [r for r in rows if not r.get("uncertain")]
    uncertain = [r for r in rows if r.get("uncertain")]

    def wide(text: str) -> str:
        return autoreview.high_stakes_marker(text)

    def tight(text: str) -> str:
        return autoreview.high_stakes_marker(text, tightened=True)

    print(f"corpus {path} — {len(rows)} rows, {len(certain)} certain "
          f"({sum(1 for r in certain if r['label'] == 'high')} high), "
          f"{len(uncertain)} uncertain excluded\n")
    shipped = score("regex", certain, wide)
    tightened = score("regex-tightened", certain, tight)
    print(line(shipped))
    print(line(tightened))
    verdict = ("CLEARS" if (tightened.recall >= BAR_RECALL
                            and tightened.precision > BAR_PRECISION) else "DOES NOT CLEAR")
    print(f"\n  bar: recall >= {BAR_RECALL} AND precision > {BAR_PRECISION} — "
          f"regex-tightened {verdict} it")

    if args.diff_only:
        print("\ndisagreements (shipped vs tightened):")
        for r in rows:
            w, t = bool(wide(r["text"])), bool(tight(r["text"]))
            if w != t:
                print(f"  {r['id']:<16} label={r['label']:<7} regex={w!s:<5} "
                      f"tightened={t!s:<5} {r['text'][:args.chars]}")
        return 0

    for name, marker in (("regex", wide), ("regex-tightened", tight)):
        fps = [r for r in certain if r["label"] != "high" and marker(r["text"])]
        print(f"\n{name}: {len(fps)} false positives")
        for r in fps:
            print(f"  {marker(r['text'])!r:<22} {r['id']:<16} {r['text'][:args.chars]}")
        misses = [r for r in certain if r["label"] == "high" and not marker(r["text"])]
        print(f"\n{name}: {len(misses)} misses")
        for r in misses:
            print(f"  {r['category']:<32} {r['id']:<16} {r['text'][:args.chars]}")

    print(f"\nuncertain rows ({len(uncertain)}, NOT scored):")
    for r in uncertain:
        print(f"  {r['id']:<16} label={r['label']:<7} "
              f"regex={bool(wide(r['text']))!s:<5} "
              f"tightened={bool(tight(r['text']))!s:<5} {r['text'][:args.chars]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
