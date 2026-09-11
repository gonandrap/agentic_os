# Holding an unargued gate

*2026-09-11 — wo-3e802661, extending the fix for GitHub issue 185.*

## The problem

A worker that runs a gated command directly is blocked, and the OS files the approval
request on its behalf with a placeholder justification: *"the worker ran the command
directly rather than filing a request, so no case was made for it"*. The same block
message then tells the worker to run `jarvis gate request … --why … --evidence …` to make
the case properly.

That advice could not be followed. The request was already queued to Neo, and the daemon
polls every 5 seconds (`Daemon.poll_interval`) while the worker's next tool call costs a
full model turn — so the reviewer read the placeholder first, nearly every time. Issue 185
fixed the narrower half of this: the second command deduped onto the pending row and threw
the `--why` and `--evidence` text away entirely. Attaching the case late is right, but it
does not win a race it enters several seconds behind.

## The rule

**No reviewer is ever shown a privileged action nobody argued for.**

Not "usually not", and not "unless the drain got there first" — the ordering is a property
of the state machine rather than of the clock.

## The design

A sixth approval status, `awaiting_case` (`gates.AWAITING_CASE`).

- The hook files into it (`gates.file_request(hold=True)`). The row exists, the work order
  parks, the timeline records that a gated command was run — and **no Neo question is
  created**, so nothing in the OS can act on it.
- `gates.queue_for_review` is the single door out: it writes the reviewer's question from
  the amended row and flips the status to `pending`. It is called from exactly one place,
  `ops.request_gate_approval`, immediately after the worker's case is merged in.
- `jarvis gate request` refuses an empty case (`--why` and `--evidence` both blank). The
  one command that can argue a request must not be able to file an unargued one.

### Why it must expire

Holding a request back from review means nothing else will ever close it: there is no
question for Neo to answer and no escalation for the user to see. A worker that wanders
off would leave an unargued privileged action open for ever.

`Daemon.refuse_unargued_gates` sweeps on each reconcile tick and **denies** — not expires —
every held request older than `gates.case_ttl_seconds` (per project, default 600s;
kn-67cdb54b). A denial because the worker is who has to act: the denial message says what
was missing, and the hook's denied branch already routes the retry into a fresh, argued
request. Nothing is authorised and nothing is lost — the command string stays blocked.

### What the user can still do

`jarvis gate approve|deny|dismiss` accepts a held request. The hold keeps *Neo* from
ruling on something nobody argued; the user is not Neo, can read the command, and would
otherwise have no way out but the TTL. A dismissal is the common case — a classifier
false positive needs no case at all.

## What this is not

It is not "don't file a request without a case". That was considered and rejected: every
gate message correctly tells the worker to end its turn, so a block that records nothing
leaves a worker parked with no request for anyone to review and nothing to re-derive it
from. The record is filed either way; only the *review* is deferred.
