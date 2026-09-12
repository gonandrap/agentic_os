# A gate that holds

*2026-09-12 — wo-cd06ddfa, GitHub issue 44.*

## The problem

The gate blocked one tool call and asked nicely for the rest. `hooks._resolve_gate`
returns a PreToolUse `permissionDecision: "deny"` whose reason is prose — *"END YOUR TURN
NOW — the verdict arrives as your next user turn."* That deny stops one invocation and
comes back as a tool error; the turn continues and the session may do anything else.

Two ways round it, both observed:

- **Ending the turn anyway, or declaring the work done.** `wo-6e7caf6c` filed two gate
  requests and reached `needs_review` without ever receiving either verdict. The Stop
  hook — the only mechanism the runtime offers for holding a session at a turn boundary —
  was purely observational.
- **Taking a different route.** `store.latest_approval_for` matches the command string
  exactly, and nothing else was restricted, so the same outcome by another command tripped
  nothing.

## The rule

**A worker that trips a gate waits.** Not "is told to wait".

## The design

Three enforcement points, because there are three routes out of a turn and each needs its
own; a gate closed on two of them is not closed.

### 1. The Stop hook holds a turn that would leave a request unargued

`hooks.held_request_turn_block` returns `{"decision": "block", "reason": …}` while the
work order has an `awaiting_case` request, naming the request and the command that argues
it.

**`awaiting_case`, and deliberately NOT `pending`** — the issue proposed the opposite and
it deadlocks. A pending request is with Neo and its verdict arrives as a queued message,
which `Daemon.deliver_messages` skips for as long as `worker_session.busy` reports a turn
in flight. A turn that cannot end can never receive the thing that would let it end. A
held request is the other shape: one in-turn command (`jarvis gate request`) clears it, so
the block terminates by construction.

`stop_hook_active` caps it at one continuation. A worker that ignored the reason twice is
better parked than spun.

### 2. PreToolUse narrows the session while a request is under review

`hooks.under_review_decision` denies every Bash call that is not `jarvis …`
(`is_jarvis_command_chain`) or read-only (`gate_rules.reads_only`), and every file write,
for as long as any request is `pending`. This is the half that closes the different-route
walk-around: re-matching one command string is not a control over a goal that has other
commands.

Here the mirror image of §1 applies: **`pending`, and NOT `awaiting_case`.** A held
request is one the worker still has to argue, and a case is made of test output, a branch
and a PR — narrowing the surface it is gathered from would hold a worker to a standard it
could no longer meet.

It sits between `gate_decision` and the PR rules in `preflight_decision`: after the first,
so a command a live grant covers still runs; before the rest, so it is not reachable
around. It fails OPEN, unlike `gate_decision`, because an unreadable database here would
block every tool call in every worker session — and the privileged command itself stays
blocked regardless, since the gate that judges it fails closed.

### 3. `jarvis wo finish` refuses while a request is open

`ops.finish` raises while any request is `pending` or `awaiting_case`. This is the route
neither of the others can close, because the command that takes it is a `jarvis …`
contract command that §2 must keep allowed. The refusal names the way on, which differs by
status: argue a held request, or end the turn and wait for a pending one.

Not a trap. Every open request reaches a verdict without the worker doing anything —
Neo's drain for a pending one, `Daemon.refuse_unargued_gates` for a held one — and the
verdict arrives as a turn the worker can finish from.

`jarvis wo done` and `jarvis wo review` are untouched: those are the user closing a work
order, and the user is who a gate escalates to.

### The tripwire behind all three

`INV-GATE-ORPHAN` already superseded a pending request left on a terminal work order; it
now counts held ones too. One is further from a verdict than a pending one, not closer —
no question exists for Neo to answer, and the only thing that would ever have closed it is
a worker that is gone.

## What this is not

It is not a claim that a worker cannot misbehave. It is a claim that the three ways it
could walk past a gate now each end in a refusal rather than a sentence of English.
