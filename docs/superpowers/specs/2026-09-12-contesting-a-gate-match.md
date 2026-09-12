# Contesting a gate match

*2026-09-12 — wo-e4ab6645. Extends `2026-09-11-holding-an-unargued-gate.md`.*

## The problem

The gate offers a blocked worker exactly one exit, and it is the wrong one for a false
positive:

```
jarvis gate request <wo> "<cmd>" --why "<why this is ready to ship>" --evidence "<PR, tests, checks>"
```

For a real merge that is right. For `cat > /tmp/probe.py <<'PY'` — matched because the
heredoc body quotes a release script — it is incoherent. There is no PR, no test result
and nothing "ready to ship". A worker asked to argue that a heredoc is ready to ship
either writes something false or abandons the command and works around the gate.

wo-5efc2de6 abandoned three in a row. None of the three performed a privileged action.
Neither the block message nor the `awaiting_case` retry message ever named
`jarvis gate explain`, which diagnoses a false positive, or `jarvis gate dismiss`, which
is the correct verdict for one.

The TTL then wrote the record the `dismissed` verdict exists to prevent. `sweep_unargued`
chose `denied` over `expired` deliberately — the worker has to be told, and nothing tells
it about an expiry — but `denied` asserts the worker made a bad request and blocks a
command that was never privileged (gates.py's own note on `VERDICTS` says so). Gate 95 is
permanently on the record as a `release` action denied by the OS. It was a `python3 -c`
that imported a module.

And the abandonment itself was discarded. A held request that times out with no case is,
on this evidence, usually a recogniser false positive the worker silently routed around —
the single clearest signal the classifier has about its own error rate, and nothing
counted it.

## The rule

**A worker that believes the recogniser misfired must be able to say so, and saying so
must never be able to authorise anything.**

Corollary: the OS must never write a verdict on a privileged action that nobody claimed
was privileged. Not an approval, and not a denial.

## The design

### 1. `jarvis gate contest`

```
jarvis gate contest <wo> "<cmd>" --why "<why this performs no privileged action>"
```

It files against the same `approvals` row and the same Neo queue that
`jarvis gate request` uses — so an upheld contest reaches `gates.apply_decision`,
`learn_from_dismissal` and the fleet-wide exemption rule by the path a dismissal already
takes. What differs is the claim on the record: `approvals.contested` is set, and the
reviewer is handed `build_contest_question` instead of `build_request_question`.

The contest IS the case, so it goes straight to review — no `awaiting_case` hold. The
hold exists so that no reviewer sees a privileged action nobody argued for; a contest
argues that there is no privileged action here at all, which is the argument the reviewer
needs.

The question carries the structural analysis `jarvis gate explain` prints — where the
literal sits, what owns that position, whether a rule could be learned from clearing it —
because the premise check is a fact about the command's shape, not a matter of opinion,
and a reviewer left to eyeball a 400-character shell string gets it wrong.

### 2. A contest can end two ways, and neither is an authorisation

`dismissed` (the worker was right) or `denied` (it was wrong: the command does perform
the action). Approving is not available:

- `REVIEWER_PERSONA` says so, in a carve-out at the top, beside the `SELF-HEAL` one.
- `gates.apply_decision` **enforces** it: `approved` on a contested row is recorded as
  `denied` with `CONTEST_NOT_AN_AUTHORISATION` appended to the reason. Persona text is a
  prompt; this is the invariant.
- `ops.decide_gate` refuses it earlier and louder, because a user typing
  `jarvis gate approve` on a contest can be told what to type instead.

A denied contest is an honest denial: the worker asserted something false about its own
command, and the routing back to `jarvis gate request` is exactly right for it.

Neo does not escalate a contest by default. A dismissal is a factual claim about the
classifier rather than an authorisation, and escalating it spends the user's attention on
an OS bug — the cost the gate exists to avoid. When Neo does escalate one, the inbox item
offers `dismiss` and `deny`, never `approve`.

### 3. Both exits are named wherever a worker is blocked

`gates.exits_advice(wo_id, command)` renders one block: `explain` first as the diagnosis,
then `request` and `contest` as the two exits, each with the condition that selects it. It
is called by the hook's fresh block, the hook's `awaiting_case` retry message and the
abandonment message. One renderer, because seven surfaces printing the same instruction
by hand is how six of them come to print something that no longer parses (kn-467d1ecd).

### 4. The TTL abandons; it does not refuse

`sweep_unargued` now writes `expired` with `closed_as='abandoned'` — never a verdict.

The original objection to `expired` was that the worker is never told about one. That was
a property of the code, not of the status: the sweep now queues `abandoned_message`, which
says plainly that nothing was authorised and nothing was refused, that the OS has no
opinion on the command, and which of the two exits to take next. The status stays out of
the authorisation record; the worker still hears about it.

`expired` already means "never decided, and can no longer be" — `supersede_approval` uses
it for the same reason. The command string stays blocked (`usable_grant` clears only
`approved` and `dismissed`), so a retry files a fresh request and gets a real review.

`closed_as` disambiguates the three ways a row reaches `expired`: `lapsed` (a grant ran
out of clock or uses), `superseded` (the world moved on), `abandoned` (the case never
came). Without it the count below could not be taken.

### 5. Abandonment is counted

`ProjectStore.abandoned_count()` beside `dismissed_count()`, surfaced by
`jarvis gate rules` next to the exemptions it learned, in `jarvis status` beside
`false_positives`, and on the dashboard beside the false-positive rate.

It is not a second false-positive count — an abandonment is evidence, not a verdict — so
it is reported separately and never folded into the rate. The rate's denominator is now
the three verdicts named positively, which also stops `awaiting_case` and `expired` rows
diluting it.

### 6. The ask belongs to the kind, not to `release`

The block message's second failure is narrower than the first and reaches every worker,
not just the ones facing a false positive: `--why "<why this is ready to ship>"` was
hardcoded for all six kinds. Nothing is "ready to ship" about a `jarvis config set`, and
there is no PR number to put behind a `systemctl restart`.

This matters past wording. The reviewer's prompt is built from what the worker wrote and
shows nothing else, so a worker told to supply a PR number for a service restart supplies
one — and the verdict is decided on the wrong evidence. The right question is a property
of the kind, and `gate_rules.KINDS` already carries a per-kind `summary` that nobody had
extended to the ask.

`GateKind` gains `why_ask` and `evidence_ask`, and every surface that renders the ask
renders them: the hook's fresh block and its `awaiting_case` retry (`gates.exits_advice`
takes `kind`), `abandoned_message`, `denied_message`'s route back, the standing worker
brief, `jarvis brief gates`, and `OPERATION.md`. The kind is in hand at all of them; none
of it needed plumbing. `jarvis gate request --help` stays kind-neutral because argparse
builds the parser before there is a command to classify, and says where the real question
is.

A contest has no per-kind ask, and that is the point: it asserts the command performs no
privileged action of *any* kind, so the question is the same whichever recogniser fired.

`build_request_question` labels the evidence with the same string, so the reviewer sees
what the worker was asked for and can tell a non-answer from a missing one.

### 7. The hold must fit inside the prompt cache's TTL

A blocked worker is told to end its turn, and when it obeys, nothing wakes it until a
verdict arrives. The hold's clock is therefore the worker's idle time, and idle time is
not free: the next turn re-sends the whole conversation, at the cache-**write** rate if
the cache has expired in between.

Measured on wo-5efc2de6 (`jarvis inspect`): six cache writes, every one labelled
`ttl-expiry`, gaps of 8.4–10.2 minutes — the old 600s `case_ttl_seconds` almost exactly.
~914k tokens re-written. The record also settles what the worker did: the turn after the
gate-95 denial is 30.4s, 92% idle, one call. It ended its turn as instructed. The ten
minutes are the OS's own clock, not a worker ignoring the instruction, so the fix is the
clock.

`DEFAULT_CASE_TTL_SECONDS` drops 600 → 240, under `usage.WRITE_TTL_SECONDS` (300s) with
room for a reconcile tick either side. `CASE_TTL_CEILING_SECONDS` states the constraint
as a constant and a test pins it against `usage.WRITE_TTL_SECONDS`, because gates is on
the hook's hot path and cannot import usage (kn-1449447a §3 is the same pattern).

A configured value at or over the ceiling is **clamped with a warning, not refused** —
unlike the sanity checks beside it, which raise. `GateConfig.parse` runs inside the
PreToolUse hook, and a settings file written by the previous release legally carries 600:
raising there would block every gated command on every worker mid-upgrade.

A contest is never held at all (§1), so this clock never applies to one: the worker that
takes the exit this spec adds waits on a reviewer, not on a timer.

### 8. An exit addressed by the command string is not an exit

Everything above assumes a blocked worker can *run* the command it is told to run. For a
large part of what trips a gate, it cannot.

A session working in a git worktree runs under an isolation guard that inspects a
command's **arguments** and refuses any whose text it cannot prove is not a git
operation. Verified live from this work order's own worktree:

```
$ jarvis gate explain "git status"
This session is isolated in the worktree …, but this command runs jarvis with the
text git status in a plain command, so what it runs cannot be shown not to be git.
Refusing to run it …
```

The overlap is total by construction: the commands that trip a gate are the ones whose
text mentions shipping, merging, pushing or restarting, and those are exactly the strings
the guard will not carry. wo-4fc128ca is the measurement — three `release` gates on
read-only commands, one dismissed by the user as a recogniser defect, **two TTL-denied
because every attempt to file their case was refused by the guard**. That is not a worker
walking away, and the abandonment count would have recorded it as one.

The guard is not this OS's code and cannot be fixed from here. What can be fixed is the
*need* to put the command string in an argument at all: the OS has had the string on the
`approvals` row since the moment it blocked the command, and the block already prints the
request number. So all three exits take that number:

```
jarvis gate explain <request-number>
jarvis gate request <request-number> --why "…" --evidence "…"
jarvis gate contest <request-number> --why "…"
```

A number needs no quoting and names no shell, so it clears the guard. `<wo-id> "<cmd>"`
still works — it is the only spelling available before a block has happened — and
`ops.resolve_gate_target` is the one place that tells them apart (a lone all-digit
positional is a request number). `gates._handle` is the matching renderer, so every
blocking surface prints the spelling the worker can actually type, and
`tests/test_gates.py::_names_both_exits` asserts that no exit line re-quotes the blocked
command.

Worth stating plainly, because it is the property the guard is protecting and the reason
this is safe: neither exit ever *runs* the string. `gate request` and `gate contest`
write a row; `gate explain` reads the rule base. Jarvis's own recogniser already knew
this — a bare `jarvis gate …` invocation does not trip a gate, while
`jarvis gate explain "…"; ./scripts/shipit.sh` still does.

## What this is not

It is not a way for a worker to clear its own gate. A contest is a claim, reviewed by
someone else, and the only thing it can win is a dismissal — which is a statement about
the OS's recogniser, not permission to do anything.
