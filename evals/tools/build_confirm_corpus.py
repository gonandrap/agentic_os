"""Rebuild the confirmation-case corpus from a live fleet. The OUTPUT IS NOT COMMITTED.

docs/superpowers/specs/2026-09-26-bounded-model-inputs.md § 6.

WHY THE OUTPUT IS NOT COMMITTED (Neo, question 1116, and question 650 before it for the
stakes corpus). A row carries verbatim production assumption prose, a work order's brief
and its DIFF, and `gonandrap/agentic_os` is a PUBLIC repository. So the corpus lives
outside the repo, at `$JARVIS_CONFIRM_CORPUS`, defaulting to
`$JARVIS_HOME/evals/confirm_corpus.json`, and the A/B reads the committed synthetic
fixture (`evals/data/confirm_corpus_synthetic.json`) when it is absent rather than
failing. WHICH corpus ran is printed with the results: they are not comparable numbers.

WHAT MAKES A ROW USABLE. A confirmation case is an assumption that was CONFIRMED AT
DELIVERY — it has a `confirm_question_id` — and whose outcome is recorded, so the eval can
report each arm's agreement with what actually happened. An assumption still pending has
no outcome and is skipped rather than defaulted: a default here would be a fabricated
label on the only column the arms are compared against.

THE DIFF IS COLLECTED AT `daemon.CONFIRM_COLLECT_CHARS`, the same bound the daemon
collects at, so the eval's FULL arm is the untrimmed packet the OS would have had and not
a second, smaller truncation nobody declared. A work order whose branch is gone collects
nothing; that row is skipped and counted.

Run it against a fleet you own:

    python evals/tools/build_confirm_corpus.py              # -> $JARVIS_CONFIRM_CORPUS
    python evals/tools/build_confirm_corpus.py --out /tmp/confirm_corpus.json
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
CORPUS_ENV = "JARVIS_CONFIRM_CORPUS"

#: The two settled `assumptions.status` values (`ProjectStore.review_assumption`), mapped
#: to what the eval compares an arm's `approve` against. `pending` is not here on purpose:
#: an assumption nobody has settled has no outcome, and a default would be a fabricated
#: label on the only column the arms are scored against.
OUTCOMES = {"accepted": "approved", "rejected": "escalated"}


def default_out() -> Path:
    named = os.environ.get(CORPUS_ENV, "").strip()
    if named:
        return Path(named).expanduser()
    home = os.environ.get("JARVIS_HOME", "").strip() or "~/.jarvis"
    return Path(home).expanduser() / "evals" / "confirm_corpus.json"


def jarvis(*args: str) -> Any:
    """One `jarvis … --json` call. Raises loudly: a half-read fleet is a wrong corpus."""
    out = subprocess.run(["jarvis", *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def work_order_ids() -> list[str]:
    listing = jarvis("wo", "list", "--all", "--include-hidden", "--json")
    rows = listing if isinstance(listing, list) else listing.get("work_orders", [])
    return [str(r["id"]) for r in rows if r.get("id")]


def evidence_of(project_path: Path, wo: dict[str, Any]) -> dict[str, Any]:
    """The packet the daemon would have collected, at the daemon's collection bound."""
    from jarvis import daemon, evidence

    packet = evidence.collect_work_order(project_path, wo, declared="",
                                         diff_chars=daemon.CONFIRM_COLLECT_CHARS)
    return {"stat": packet.stat, "diff": packet.diff, "files": list(packet.files),
            "dropped_files": list(packet.dropped_files), "pr_url": packet.pr_url,
            "source": packet.source, "head": packet.head,
            "diff_truncated": bool(packet.diff_truncated)}


def rows_of(wo_id: str, project_paths: dict[str, Path],
            skipped: dict[str, int]) -> list[dict[str, Any]]:
    # `jarvis wo show --json` is FLAT: `{"project": name, **wo, "assumptions": [...]}`.
    wo = detail = jarvis("wo", "show", wo_id, "--json")
    assumptions = detail.get("assumptions") or []
    out: list[dict[str, Any]] = []
    packet: dict[str, Any] | None = None
    for a in assumptions:
        if not a.get("confirm_question_id"):
            skipped["never confirmed"] = skipped.get("never confirmed", 0) + 1
            continue
        outcome = OUTCOMES.get(str(a.get("status") or "").lower())
        if not outcome:
            skipped["no recorded outcome"] = skipped.get("no recorded outcome", 0) + 1
            continue
        if packet is None:
            path = project_paths.get(str(wo.get("project") or ""))
            if path is None:
                skipped["project path unknown"] = \
                    skipped.get("project path unknown", 0) + 1
                continue
            packet = evidence_of(path, wo)
        if not packet.get("diff"):
            skipped["no diff left to collect"] = \
                skipped.get("no diff left to collect", 0) + 1
            continue
        out.append({
            "id": f"{wo_id}#{a.get('n')}",
            "assumption": {
                "id": a.get("id"), "n": a.get("n"), "content": a.get("content") or "",
                "provisional_verdict": a.get("provisional_verdict") or "",
                "provisional_reason": a.get("provisional_reason") or "",
                "provisional_model": a.get("provisional_model") or ""},
            "work_order": {
                "id": wo_id, "title": wo.get("title") or "",
                "description": wo.get("description") or "",
                "result_summary": wo.get("result_summary") or ""},
            "siblings": [{"id": s.get("id"), "n": s.get("n"),
                          "status": s.get("status") or "",
                          "content": s.get("content") or ""}
                         for s in assumptions if s.get("id") != a.get("id")],
            "evidence": packet,
            "outcome": outcome,
            "outcome_note": a.get("decided_reason") or ""})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", type=Path, default=None,
                    help=f"where to write; default ${CORPUS_ENV}")
    args = ap.parse_args(argv)

    out = args.out or default_out()
    # `jarvis status --json` is where a project's path is readable (ops.py's status
    # payload); there is no `jarvis project list`.
    status = jarvis("status", "--json")
    project_paths = {str(p["name"]): Path(str(p["path"]))
                     for p in (status.get("projects") or [])
                     if p.get("name") and p.get("path")}

    rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}
    ids = work_order_ids()
    for i, wo_id in enumerate(ids, 1):
        rows.extend(rows_of(wo_id, project_paths, skipped))
        print(f"\r{i}/{len(ids)} work orders, {len(rows)} confirmation cases",
              end="", file=sys.stderr)
    print("", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "about": ("PRODUCTION assumption text and production diffs. Not for a public "
                  "repository — see this file's builder, "
                  "evals/tools/build_confirm_corpus.py, and Neo question 1116."),
        "rows": rows,
    }, indent=2) + "\n")
    approved = sum(1 for r in rows if r["outcome"] == "approved")
    print(f"{out}: {len(rows)} rows, {approved} approved, "
          f"{len(rows) - approved} escalated", file=sys.stderr)
    for why, n in sorted(skipped.items()):
        print(f"  skipped {n}: {why}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - a hand-run tool
    raise SystemExit(main())
