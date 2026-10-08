"""Specs live in exactly one place: `docs/superpowers/specs/`.

A second spec directory under `docs/` grew for two months because the feature-order
planner prompt's `design_doc` placeholder named it, and it was the only place in the OS
that told an agent where a spec file goes. These guard against it coming back — by the
directory's own path, and by the literal prefix in any tracked file, which is how the
drift spread.

The forbidden literal is composed below rather than written out, so this file does not
trip its own check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Composed so this file does not itself contain the forbidden literal.
FORBIDDEN = "docs/" + "specs/"
CANONICAL = "docs/superpowers/specs/"

REPO_ROOT = Path(__file__).resolve().parent.parent


def _tracked_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


def test_legacy_spec_directory_does_not_exist() -> None:
    legacy = REPO_ROOT / "docs" / "specs"
    assert not legacy.exists(), (
        f"{legacy} exists again. Specs live ONLY in {CANONICAL} — "
        "move its contents there and delete the directory."
    )


def test_no_tracked_file_names_the_legacy_spec_directory() -> None:
    offenders: list[str] = []
    for path in _tracked_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, ValueError):
            continue  # binary
        if FORBIDDEN in text:
            offenders.append(str(path.relative_to(REPO_ROOT)))
    named = sorted(offenders)
    shown = ", ".join(named[:20])
    if len(named) > 20:
        shown += f", … and {len(named) - 20} more"
    assert not offenders, (
        f"{len(offenders)} tracked file(s) name the legacy spec directory "
        f"{FORBIDDEN!r}: {shown}. Specs live ONLY in "
        f"{CANONICAL}<YYYY-MM-DD>-<slug>.md — repoint these and never create "
        "another spec directory."
    )
