# Privileged-action gates — how a worker ships code

Added by wo-8150b33b. Lets a worker on a gated project merge a PR or cut a release
*under independent review*, replacing the hard `deny` rules that stalled OS development.

## The load-bearing constraint

From the Claude Code permission docs, verbatim:

> Hook decisions don't bypass permission rules. Claude Code evaluates deny and ask rules
> regardless of what a PreToolUse hook returns: a matching deny rule blocks the call.

So **a gated command must not also appear in `permissions.deny`**. If it does, the whole
flow runs and reports success — request filed, Neo approves, timeline says `gate_opened` —
and the command still never executes. `INV-GATE-DENY-CONFLICT` (`invariants.py:check_gate_deny_conflict`)
exists only to catch that, because nothing else surfaces it.

Confirmed live while building this: a `jarvis wo assume …` command was blocked merely for
quoting the release script's name, even though the hook allows every `jarvis` command.

Second constraint: a PreToolUse hook is synchronous with a ~30s timeout; a Neo review takes
minutes. **The hook never waits.** It denies the attempt, files the request, and the verdict
arrives later through the ordinary message-delivery path.

## The flow

1. Worker runs a privileged command (or, better, `jarvis gate request <wo> "<cmd>" --why … --evidence …`).
2. `hooks.gate_decision` → `hooks._resolve_gate` classifies it (`gates.classify`) and, with
   no live grant, calls `gates.file_request`: an `approvals` row + a `neo.db` question with
   `kind='approval'`. WO → `waiting_input`. The attempt is **denied** with instructions to
   end the turn.
3. `Daemon._neo_drain` → `neo.answer_question` picks `gates.REVIEWER_PERSONA` (not the
   general `neo.PERSONA`, which is told to escalate anything production-touching and would
   send every release to the user).
4. `Daemon._deliver_gate_verdict`:
   - approve/deny → `gates.apply_decision` → row decided + a message queued for the worker.
   - escalate → row stays `pending` with `escalated=1`; inbox + attention name
     `jarvis gate approve <id>`.
5. Worker retries the **byte-identical** command → `usable_grant` hits → `allow` +
   `consume_grant`.

## Scope of a grant

`(wo_id, kind, exact command)`, `GRANT_TTL_SECONDS=3600`, `GRANT_MAX_USES=3`. Exact-match on
the command string is the security model: a reworded retry gets a fresh gate. Expiry and use
count are checked in `ProjectStore.usable_grant`, never trusted from `status`.

## Where things live

| Thing | Where |
|---|---|
| kinds, patterns, config, personas, request text, `apply_decision` | `gates.py` (leaf; `catalog.py` imports it) |
| `approvals` table + `usable_grant`/`consume_grant`/`decide_approval`/`escalated_approvals` | `project_store.py` |
| the gate itself | `hooks.gate_decision` / `hooks._resolve_gate`, called first in `preflight_decision` |
| `questions.kind` (+ its ALTER migration) | `neo_store.py` `ADDED_COLUMNS` |
| verdict routing | `daemon._deliver_gate_verdict` |
| `request_gate_approval` / `decide_gate` / `list_gates` / `show_gate` | `ops.py` |
| `jarvis gate request|list|show|approve|deny` | `cli.cmd_gate` |

Gate kinds: `pr_merge`, `release`, `service_restart`, `push_protected`.

## Two behaviours that are deliberate, not bugs

- **Fail closed.** Any error in the gate path denies (`hooks.gate_decision`'s `except`).
  A broken gate must not become an open door.
- **Off by default.** `ProjectSpec.gates` is empty unless the catalog says otherwise.
  Enabling fleet-wide would put a review in front of every `gh pr merge` in every repo.

## Attention accounting

A gate pending **with Neo** must cost the user nothing — that is the entire point.
`invariants._waiting_on_neo_gate` suppresses the `waiting_input` blocker for those, and
`daemon.reconcile_project` skips the "idle without `jarvis wo finish`" verdict when
`pending_approvals` is non-empty (a worker told to end its turn and wait is complying).
Only `escalated_approvals` reach `jarvis status`, and `ops.os_status` drops the work order's
own duplicate flag so the actionable line is the only one shown.

Tests: `tests/test_gates.py` (units, both directions of the boundary),
`tests/test_gates_pipeline.py` (the loop through the real daemon and Neo drain).
The fake `claude` in `jarvis.testing` speaks the approval verdict shape and defaults to
escalating unless the prompt contains `FORCE_APPROVE`/`FORCE_DENY`.
