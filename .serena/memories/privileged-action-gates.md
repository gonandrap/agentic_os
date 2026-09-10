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
   - approve/deny/dismiss → `gates.apply_decision` → row decided + a message queued for
     the worker. Approve and deny also post to the inbox; **dismiss does not** (see below).
   - escalate → row stays `pending` with `escalated=1`; inbox + attention name
     `jarvis gate approve <id>`.
5. Worker retries the **byte-identical** command → `usable_grant` hits → `allow` +
   `consume_grant`.

## Four verdicts, not three — `dismissed`

`gates.VERDICTS = ("approved", "denied", "dismissed")`. The fourth outcome exists because
the first three all answer *"should this privileged action proceed"*, and when the
recogniser misfires that question has not arisen. For a command that performs no
privileged action every original verdict recorded something false: **approve** writes an
authorisation for an act nobody performed, **deny** accuses the worker and tells it not to
retry, **escalate** spends the user's attention on an OS bug. The user ruled both ways as
a result, and the identical command (`grep -rn <deploy script> src/jarvis/gates.py`) was
denied as request 2 and approved as request 4.

`dismissed` unblocks the command, authorises nothing, and is counted separately.
Non-obvious properties, all load-bearing:

- **It never expires and has no use limit.** It asserts a fact about the command string,
  not a time-boxed permission. `decide_approval` leaves `expires_at` NULL and
  `usable_grant` returns dismissed rows without checking `uses`/`expires_at`.
- **`expire_approvals` must keep excluding it.** Its `WHERE status='approved'` is doing
  double duty: sweeping dismissals into `expired` would also erase the false-positive
  count, which is the only reason the verdict is separate. Pinned by
  `test_the_expiry_sweep_leaves_dismissed_rows_alone`.
- **Scope is the containment, since there is no clock**: `(wo_id, kind, exact command)`,
  never a prefix. Pinned by `test_a_dismissal_does_not_widen_beyond_the_exact_command`.
- **No inbox item**, deliberately — an inbox ping for a classifier defect is the attention
  cost the gate exists to avoid. Surfaced as a rate instead: `dismissed_count()`,
  `os_status()["gates"]["false_positives"]`, a line in `jarvis gate list`, and a separate
  table + percentage on `/gates`.
- **Distinct event.** `gate_dismissed`, not `gate_decided`; `gate_opened` carries
  `clearance: approved|dismissed` so the timeline never says "ran the approved command"
  about a false positive.
- **`REVIEWER_PERSONA` asks the premise question FIRST.** Position is the fix. The old
  persona opened by asserting the command shipped code and required of every approval that
  work "landed in a pull request with checks passing" — unsatisfiable for a misclassified
  `grep`, so DENY was structurally the closest fit and Neo was following orders. The HARD
  LIMIT clause (a command that *actually* deploys/merges/restarts is never dismissible;
  ambiguity resolves towards the privileged reading) is what bounds mis-dismissals.
- **`neo.parse_verdict` reads `verdict` and falls back to the old `approve` bool.** Not
  politeness: the persona ships in the code but Neo's learnings live in the production
  state directory, so the two can disagree across a release in either direction. An
  unrecognised verdict falls back to the boolean, which defaults False — fail closed.
- A dismissal leaves `approve` **False**. Anything conflating the two puts the
  authorisation straight back into the audit trail.

## Code vs data: `gates.scannable`

`classify` matches the whole command (pipelines and `&&` chains must not be a bypass), so
it used to fire on any command that merely *contained* a gated verb. Real escalations this
produced: `jarvis learn add "…never run shipit…"` and
`grep -n "shipit\|def classify" src/jarvis/gates.py` both gated as **releases**, and
`git commit -m "document systemctl restart…"` as a **service restart**.

`scannable()` blanks quoted spans before matching — a quoted argument is data. The
exception is `sh -c` / `bash -c` / `eval` / `xargs`, which hand their payload back to a
shell: those are scanned whole, because there the quotes really are code. Erring that
direction is deliberate — a spurious gate costs one review, a missed one ships unreviewed.

Pinned by `test_merely_naming_a_privileged_action_is_not_gated` and
`test_a_quoted_payload_handed_to_a_shell_is_still_gated`.

## Scope of a grant

`(wo_id, kind, exact command)`, `GRANT_TTL_SECONDS=3600`, `GRANT_MAX_USES=3`. Exact-match on
the command string is the security model: a reworded retry gets a fresh gate. Expiry and use
count are checked in `ProjectStore.usable_grant`, never trusted from `status`. A `dismissed`
row also clears a command through `usable_grant` but is bounded by scope alone — see below.

## Where things live

| Thing | Where |
|---|---|
| kinds, patterns, config, personas, request text, `apply_decision` | `gates.py` (leaf; `catalog.py` imports it) |
| `approvals` table + `usable_grant`/`consume_grant`/`decide_approval`/`escalated_approvals` | `project_store.py` |
| the gate itself | `hooks.gate_decision` / `hooks._resolve_gate`, called first in `preflight_decision` |
| `questions.kind` (+ its ALTER migration) | `neo_store.py` `ADDED_COLUMNS` |
| verdict routing | `daemon._deliver_gate_verdict` |
| `request_gate_approval` / `decide_gate` / `list_gates` / `show_gate` | `ops.py` |
| `jarvis gate request|list|show|approve|deny|dismiss` | `cli.cmd_gate` |

Gate kinds: `pr_merge`, `release`, `service_restart`, `push_protected`, `config_write`,
`self_heal`.

## `self_heal` — the kind that is unlike the others in three ways

Added by wo-805b4319 (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md
§5). It is how the SUPERVISOR gets permission to act on a work order or feature order it
judged unhealthy, and the whole reason to reuse the gate is that it buys Neo's review,
the escalation path, `jarvis gate list|show|approve|deny`, `/gates` and the audit trail
with **no new Neo question kind** — the request rides `kind="approval"`, which is where
`Daemon._deliver_gate_verdict` already looks its subject up in `approvals`.

1. **Nothing classifies into it.** No `SEED_MATCHES`, no `SEED_CANARIES`, no
   `conflict_markers`. `gates.classify` returns it for no string, and a test pins that
   against `gate_rules`' own canary corpus. Every other kind catches a command a worker
   typed; this one is filed programmatically by `remedies.propose`.
2. **It does not ride `GateConfig.enabled`.** The command gates are opt-in per project
   because gating them trades one bottleneck for a slower one. This one is MANDATORY —
   it is the only thing between a health judgement and a running session — so
   `remedies.propose` never consults `GateConfig`. What a project controls is
   `os.supervisor.remedies.allowed`, the other direction.
3. **`approvals.command` is not a command.** It holds a rendered intent
   (`heal al-1a2b: nudge wo-3c4d — "…"`). Nothing executes it.

Two guards keep the worker-facing plumbing switched off, and both are easy to undo by
accident. `gates.apply_decision` delegates a `self_heal` verdict to
`remedies.record_verdict` instead of `queue_message` + `end_wait_if_nothing_is_out` — a
message into a turn nobody asked about is precisely the act this gate fences. And
`ops.decide_gate` skips its `clear_attention` for this kind: on a denial the flag
standing afterwards is the ALARM's, and `invariants.true_blockers` has no branch that
re-derives a live alarm, so clearing it would put it down for good.

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
