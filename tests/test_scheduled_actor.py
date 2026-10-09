"""Every open work order has an actor or a blocker — never neither.

Section 3 of
docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md:
`ops.scheduled_actor` answers "somebody is going to act on this" from the row alone,
over the predicates the daemon passes themselves use. Four pins:

1. coverage — every `(status, origin)` pair derives an actor or a blocker, and the
   pairs that correctly derive NEITHER are a table with a sentence each
2. population — the registry's keys are exactly `OPEN_STATUSES`
3. the predicate rule — every entry's test is a symbol IMPORTED from the pass's own
   module, never a lambda and never a condition restated in `ops`
4. no fallthrough — a status missing from the registry raises rather than guessing
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from jarvis import invariants, ops
from jarvis.project_store import OPEN_STATUSES, ProjectStore

ORIGINS = ("jarvis", "injected", "adhoc")

PR = "https://github.com/acme/proj/pull/7"

#: (status, origin) pairs that correctly derive NEITHER an actor nor a blocker,
#: each mapped to the one sentence saying why.
EXPECT_NEITHER: dict[tuple[str, str], str] = {
    ("dispatching", "adhoc"):
        "`Daemon.settle_turns` skips `UNGOVERNED_ORIGINS` and `true_blockers` derives "
        "nothing from `dispatching` — correct per `Daemon.retire_ungoverned` / "
        "INV-ADHOC-LEGACY-RETIRED",
    ("dispatching", "injected"):
        "`Daemon.settle_turns` skips `UNGOVERNED_ORIGINS`; nothing claims an injected "
        "row that has not started a turn",
    ("running", "adhoc"):
        "both `Daemon.settle_turns` and `Daemon.retry_paused_turns` skip "
        "`UNGOVERNED_ORIGINS`, and an `adhoc` row is asked for nothing "
        "(INV-ADHOC-LEGACY-RETIRED)",
    ("running", "injected"):
        "`Daemon.track_injected_sessions` follows this row, but its eligibility keys "
        "on the live `claude_cli` agents view (`sessions_by_cwd`), not on a row read",
    ("needs_review", "adhoc"):
        "`true_blockers`' triage is behind `governed`",
    ("needs_review", "injected"):
        "`true_blockers`' triage is behind `governed`",
}

#: `pending`'s actor is the claim SQL's, and that is section 4's.
PENDING_XFAIL = "pending's actor lands in the claim-SQL child"


def _params() -> list:
    out = []
    for status in OPEN_STATUSES:
        for origin in ORIGINS:
            marks = ([pytest.mark.xfail(strict=True, reason=PENDING_XFAIL)]
                     if status == "pending" else [])
            out.append(pytest.param(status, origin, marks=marks,
                                    id=f"{status}-{origin}"))
    return out


def _row(store: ProjectStore, status: str, origin: str) -> dict:
    """The minimal realistic row for this status: what the passes select on."""
    kw: dict = {"origin": origin, "status": status}
    if status == "budget_exhausted":
        kw["budget_usd"] = 1.0
    if status == "idle":  # `idle` is only ever written to a feature manager
        kw["kind"] = "manager"
        kw["parent_id"] = store.create_feature_order("a feature")["id"]
    wo = store.create_work_order("add feature X", **kw)
    if status == "waiting_pr_merge":
        store.update_work_order(wo["id"], pr_url=PR)
    if status == "validating":
        store.open_validation_round(wo_id=wo["id"], fingerprint="fp-1")
    return store.get_work_order(wo["id"])


@pytest.mark.parametrize(("status", "origin"), _params())
def test_an_actor_or_a_blocker_never_neither(jarvis_home, project, status, origin):
    store = ProjectStore(project)
    wo = _row(store, status, origin)
    actor = ops.scheduled_actor(store, wo)
    blockers = invariants.true_blockers(store, wo)
    why = EXPECT_NEITHER.get((status, origin))
    if why is not None:
        # The table bites both ways: an exemption that started deriving something is
        # a stale exemption, which is the defect this feature deletes one level up.
        assert actor is None, f"{status}/{origin} now derives {actor} — {why}"
        assert blockers == [], f"{status}/{origin} now derives {blockers} — {why}"
    else:
        assert actor is not None or blockers != [], (
            f"{status}/{origin} derives neither an actor nor a blocker; if that is "
            "correct, add it to EXPECT_NEITHER with the sentence saying why")


def test_every_open_status_is_in_the_registry():
    assert set(ops.SCHEDULED_ACTORS) == set(OPEN_STATUSES)


def _ops_imports() -> dict[str, str]:
    """name -> module it was imported from, for every `from . import`/`from .x import`."""
    tree = ast.parse(Path(ops.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            name = alias.asname or alias.name
            found[name] = node.module or alias.name
    return found


def test_every_predicate_is_an_imported_symbol():
    imported = _ops_imports()
    for status, entry in ops.SCHEDULED_ACTORS.items():
        for p in entry.passes:
            module, _, name = p.predicate.rpartition(".")
            assert module and name, f"{status}: {p.predicate} is not dotted"
            assert module != "ops", (
                f"{status}: {p.predicate} is defined in ops — a predicate belongs to "
                "the pass's own module")
            assert imported.get(name) == module, (
                f"{status}: ops.py does not import {name} from {module}")
            resolved = getattr(importlib.import_module(f"jarvis.{module}"), name)
            assert resolved is p.test, f"{status}: {p.predicate} is not the test called"
            assert p.test.__name__ != "<lambda>", f"{status}: {p.predicate} is a lambda"


def test_no_fallthrough(jarvis_home, project):
    for status, entry in ops.SCHEDULED_ACTORS.items():
        assert bool(entry.none_because) is not bool(entry.passes), (
            f"{status}: `none_because` must be non-empty exactly when there are no "
            "passes")
    store = ProjectStore(project)
    wo = store.create_work_order("add feature X")
    with pytest.raises(KeyError):
        ops.scheduled_actor(store, {**wo, "status": "completed"})
