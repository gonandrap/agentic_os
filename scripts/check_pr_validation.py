#!/usr/bin/env python3
"""Fail a PR whose body lacks `## How to validate in production` (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis import morpheus  # noqa: E402


def main() -> int:
    body = os.environ.get("PR_BODY")
    if body is None:
        body = sys.stdin.read()
    problem = morpheus.section_problem(body)
    if problem:
        print(f"::error::{problem}")
        return 1
    section = morpheus.parse_section(morpheus.find_section(body) or "")
    kind = ("waived: " + section.waived if section.waived
            else "script" if section.script else "hints (Morpheus will write the script)")
    print(f"ok — {morpheus.SECTION}: {kind}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
