"""Rebuild the labelled stakes corpus from a live fleet. The OUTPUT IS NOT COMMITTED.

docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md SS3.9.

WHY THE OUTPUT IS NOT COMMITTED (Neo, question 650). The corpus is verbatim production
assumption prose from the fleet's own work orders, and `gonandrap/agentic_os` is a PUBLIC
repository. So it lives OUTSIDE the repo, at `$JARVIS_STAKES_CORPUS`, defaulting to
`$JARVIS_HOME/evals/stakes_corpus.json`, and the A/B eval SKIPS when that file is absent
rather than failing.

**PARAPHRASING WAS CONSIDERED AND REJECTED**, in the same ruling: a paraphrase changes the
exact tokens the regex fires on — `delet` is 21 of its 91 hits — so the regex arm would be
scored on rewritten text instead of production text, and the A/B would be measuring the
paraphraser. What IS committed is this script and a small synthetic fixture
(`evals/data/stakes_corpus_synthetic.json`), which is what the eval reads on a clean
checkout.

Run it against a fleet you own:

    python evals/tools/build_stakes_corpus.py              # -> $JARVIS_STAKES_CORPUS
    python evals/tools/build_stakes_corpus.py --out /tmp/corpus.json

The labels are yours to write and are kept BESIDE the output, in `stakes_labels.json`:

    {"wo-fca1ac5b#4": ["none", false, "regex fired on 'delet'; a conflict note"],
     "wo-3819e654#1": ["publishing", false, "chose the tag and pushed it"]}

`[category, uncertain, why]`. Anything absent is `routine` — the honest default, because
~78 of the regex's 91 hits are routine and a corpus that assumed otherwise would flatter
every arm. `uncertain: true` marks a label the user has not checked, and the eval EXCLUDES
those rows from its assertion and reports them separately.

Hidden and settled orders are included on purpose (`--all --include-hidden`): the fleet's
whole history is the population the 498-row measurement was taken over, and dropping the
closed ones would drop most of the acts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Where the real corpus lives, outside the repository. The eval reads the same name.
CORPUS_ENV = "JARVIS_STAKES_CORPUS"
LABELS_FILE = "stakes_labels.json"
HIGH_CATEGORIES = ("production-or-live-credentials", "spending-money",
                   "destroying-data", "publishing", "legal-or-personal-data",
                   "breaking-change")


def default_out() -> Path:
    named = os.environ.get(CORPUS_ENV, "").strip()
    if named:
        return Path(named).expanduser()
    home = os.environ.get("JARVIS_HOME", "").strip() or "~/.jarvis"
    return Path(home).expanduser() / "evals" / "stakes_corpus.json"


def jarvis(*args: str) -> Any:
    """One `jarvis … --json` call. Raises loudly: a half-read fleet is a wrong corpus."""
    out = subprocess.run(["jarvis", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def work_order_ids() -> list[str]:
    listing = jarvis("wo", "list", "--all", "--include-hidden", "--json")
    rows = listing if isinstance(listing, list) else listing.get("work_orders", [])
    return [str(r["id"]) for r in rows if r.get("id")]


def rows_of(wo_id: str, labels: dict[str, Any]) -> list[dict[str, Any]]:
    detail = jarvis("wo", "show", wo_id, "--json")
    out = []
    for a in detail.get("assumptions") or []:
        text = str(a.get("content") or "").strip()
        if not text:
            continue
        row_id = f"{wo_id}#{a.get('n')}"
        category, uncertain, why = (labels.get(row_id)
                                    or ["none", False, "unlabelled: routine by default"])
        if category not in (*HIGH_CATEGORIES, "none"):
            raise SystemExit(f"{row_id}: {category!r} is not a stakes category")
        out.append({"id": row_id, "text": text,
                    "label": "high" if category in HIGH_CATEGORIES else "routine",
                    "category": category, "why": why, "uncertain": bool(uncertain)})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=None,
                    help=f"where to write; default ${CORPUS_ENV}")
    ap.add_argument("--labels", type=Path, default=None,
                    help=f"label map; default {LABELS_FILE} beside the output")
    args = ap.parse_args(argv)

    out = args.out or default_out()
    labels_path = args.labels or out.parent / LABELS_FILE
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}

    rows: list[dict[str, Any]] = []
    ids = work_order_ids()
    for i, wo_id in enumerate(ids, 1):
        rows.extend(rows_of(wo_id, labels))
        print(f"\r{i}/{len(ids)} work orders, {len(rows)} assumptions",
              end="", file=sys.stderr)
    print("", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "about": ("PRODUCTION assumption text. Not for a public repository — see this "
                  "file's builder, evals/tools/build_stakes_corpus.py, and Neo question "
                  "650."),
        "labels": str(labels_path),
        "rows": rows,
    }, indent=2) + "\n")
    high = sum(1 for r in rows if r["label"] == "high")
    unsure = sum(1 for r in rows if r["uncertain"])
    print(f"{out}: {len(rows)} rows, {high} high, {len(rows) - high} routine, "
          f"{unsure} uncertain (excluded from the eval's assertion)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - a hand-run tool
    raise SystemExit(main())
