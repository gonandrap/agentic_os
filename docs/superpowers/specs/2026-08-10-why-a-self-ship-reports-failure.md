# Why a self-ship reports failure, and where a simple work order's time actually went

**Work order:** wo-67d4f8b0, investigating wo-2fa7c0e9 ("ship a new release : 0.5.1").
**Companion to:** `2026-08-09-where-the-tokens-go.md` (kn-1485b845), which settled the
token side; this one settles the turn/latency side and the self-ship failure mode.

wo-2fa7c0e9 ended `failed` with the attention flag "worker turn failed — review and
retry". The release it was asked to perform **succeeded completely**: production is at
tag `jarvis-0.5.1`, both systemd units restarted cleanly, and the 0.5.0 half-apply
(kn-58429229) did not recur. Every claim below is from the work order timeline, the
worker's own transcript, systemd, and the production checkout — not reconstruction.

## 1. Why it "failed": the ship kills the shipper, by construction

The timeline of the last ninety seconds:

| time (PDT) | event |
|---|---|
| 23:52:19 | user: "limit restored, retry" |
| 23:52:45 | worker runs `scripts/shipit.sh 0.5.1` (gate 29, approved) |
| 23:52:55 | `jarvis-ui.service` restarted by the script (new, post-#85 ordering) |
| 23:52:59 | script exits 0; its output is already in the worker's transcript |
| 23:53:02 | worker transcript ends: `[Request interrupted by user]` |
| 23:53:05 | `jarvis.service` back up (the script's detached `systemd-run` restart) |
| 23:53:05 | new daemon reaps the dead turn: `turn_failed: turn reported is_error` → WO `failed` |

The mechanism: every worker `claude` process is spawned by the daemon
(`claude_cli.spawn_turn`) and therefore lives **inside `jarvis.service`'s cgroup**.
The unit runs with the systemd default `KillMode=control-group`, so stopping the
service SIGTERMs every process in the cgroup — the daemon *and all of its workers*.
When the deploy's detached restart landed at 23:53:02, the shipping worker was three
seconds into composing its final report. It died mid-turn, Claude Code wrote an
`is_error` result, and the new daemon settled the work order as failed.

Two things make this worse than a one-off:

- **The protection that was supposed to prevent it does not work.** `spawn_turn`
  passes `start_new_session=True` with a comment saying, verbatim, that "`shipit`
  restarts jarvisd on every release, so a turn parented to the daemon would lose its
  reply on each deploy". `setsid` detaches the process group — it does **not** leave
  the systemd cgroup. The intent is right; the mechanism is illusory. Corollary:
  **every deploy (and every daemon restart or crash) kills every running worker
  fleet-wide**, not just the one doing the shipping.
- **The failure affordance is dangerous.** "Review and retry" on a release that
  succeeded invites re-running `shipit.sh 0.5.1` — the gate approval had 2 of its 3
  uses left. (The script would refuse on the existing tag, but the OS should not be
  suggesting it.)

So, to the question "why can't I ship a version via a work order": you can — the ship
itself worked, twice now. What cannot work under the current spawn design is the
worker *reporting* it. The final turn of a self-ship is structurally unable to
complete, so the OS will always record `failed` regardless of the outcome. For the
stated goal (Jarvis auto-fixing and self-shipping), this must be fixed at the OS
level, not by prompting workers harder. Two complementary fixes, both filed:

1. **Spawn workers outside the daemon's cgroup** (e.g. `systemd-run --user --scope`
   per turn). Deploys stop killing the fleet; a self-shipping worker survives its own
   deploy, watches both units come back, and reports success normally. This is the
   real fix. (backlog: worker processes must survive a daemon restart)
2. **Settle, don't fail, a turn that dies inside an open `release` gate.** On boot,
   if the daemon finds a turn that died while a release command was in flight, it
   should verify the post-conditions itself (prod tag == requested version, both
   units' `ExecMainStartTimestamp` newer than the gate opening — exactly kn-58429229's
   checklist) and settle the WO as completed with an auto-summary, instead of raising
   "retry". Belt-and-braces once (1) lands; the only honest path if it hasn't.
   Interposing a step before `done` must respect the settlement facts in kn-99d3f1d4.

## 2. How bloated is a simple work order? Measured, and it is turns, not tokens

**Input.** The user typed almost nothing: the description was literally empty (the
prompt says "no further description — the title is the task"). The 15,686-char opening
prompt (~4k tokens) is entirely OS-injected contract + knowledge index — consistent
with the 16,034 chars measured for a bare WO in the tokens write-up. The user's
impression of "a lot of input" is the OS's own briefing, and in dollars it is a
rounding error.

**Cost.** True figures for the session: **~$1.51 at list prices, 32 API calls,
~1.29M cache-read tokens, 57k peak context, ~25 minutes wall clock.** (`jarvis cost`
reported $0.51 / 1 turn — a real bug, fixed in this PR: the session's transcript
spans two project directories and `usage.index_sessions` kept only one path per
session id, silently dropping the larger segment.)

**Where the 25 minutes went.** Roughly 7 turns, of which ~1.5 were the actual work
(verify preconditions, run one script):

| minutes | spent on | class |
|---|---|---|
| ~6 | gate false positive #1: reading `shipit.sh` with `sed` tripped the `release` recogniser; Neo dismissed in 9s, but the worker sat `waiting_input` through two resume nudges | OS defect (bl-88545be9, issue #87) |
| ~5 | the worker's verification turn: full test suite + evals on a tip CI had already greened, plus a byte-for-byte evidence recap | contract-induced over-verification |
| ~2 | gate false positive #2: the *next line range* of the same file — dismissals are keyed to the exact command string | same OS defect |
| ~11 | two turn deaths on the Claude session limit, Neo's reviewer call dying on the same limit (escalating the gate to the user with a raw JSON error as the "reason"), and the user manually typing "limit restored, retry" | external limit, but the OS turned it into three user-visible events |
| ~1 | the ship itself, followed by the self-kill | the structural failure above |

Two compounding effects worth naming:

- **Every forced turn-end multiplies context.** The contract requires each turn to end
  self-contained, so after every gate bounce the worker restated its full evidence
  block (~4k chars, three times). Each restatement is then re-read by every subsequent
  API call — the exact re-write/re-read tax the tokens write-up identified, here
  *caused* by gate friction rather than by task size.
- **The rituals scale with the contract, not with the task.** Recording 2 assumptions
  (each raising an attention item on a trivial patch bump), evidence-first gate
  requests, and the full-suite re-run are all correct behaviors under the current
  contract — the contract just has no notion of "this order is one script whose
  preconditions the script itself enforces".

Verdict: for this class of order the briefing's token weight is fine; the bloat is
**seven user/Neo touchpoints and ~23 wasted minutes** on what is intrinsically a
one-command job.

## 3. Minimal-information work orders that pull context on demand

The asked-for mechanism half-exists, and the measured half works:

- The knowledge base already ships as an **index, not a paste** (25 truncated
  headlines, ~1.7k tokens); workers fetch full entries with `jarvis learn show` when
  a headline matches. That is exactly "start minimal, ask on demand", and wo-2fa7c0e9
  used it correctly (it fetched the release entry before touching anything).
- `jarvis wo ask` is the on-demand channel to a person: Neo answers in ~a minute,
  the user only when Neo escalates. Also already true to the vision.

What is actually missing, in order of leverage:

1. **A briefing scoped to the work order's kind.** Today every order gets the full
   worker contract: worktree, PR conventions, Serena guidance, assumption/backlog
   protocol — ~4k tokens and, more importantly, *rituals* sized for code changes. A
   release/runbook order needs none of it. kn-52e51faf established that a new WO kind
   is nearly free except for three named branch points (`build_worker_prompt`,
   `install_agent_assets`, `count_active`). An `ops` kind whose briefing is ~1 page —
   "run this runbook, verify these post-conditions, report" — removes the PR
   expectation, the worktree, and most of the ritual in one move. (backlog filed)
2. **For deterministic operations, no agent at all.** A release is a script with
   preconditions the script already enforces and post-conditions the daemon can check.
   The end state for self-shipping is `jarvis release 0.5.2`: the daemon (or a
   transient unit it spawns) runs the script detached, survives its own restart, and
   verifies post-conditions on boot — an LLM never holds the deploy in a
   conversation turn. The auto-fix future needs the *decision* made by an agent, not
   the *execution* held open in one.
3. **Answer-from-context for `wo ask`.** When Jarvis creates an order from a chat, the
   conversation that motivated it stays in Jarvis's session. Attaching a pointer to
   that session (not its text) would let Neo answer "what did the user mean" questions
   from the source instead of escalating. Cheap, and it makes *thin descriptions safe*:
   the description can stay one line because the worker can pull the intent on demand.

What NOT to do, per the measurements: do not shrink the description field by policy
(this one was already empty and it wasn't the problem), and do not spend effort
shaving the briefing's token count (kn-1485b845: rounding error). The scarce resources
are user attention and wall clock; the mechanisms above spend both only when a real
question exists.

## Addendum — what this same work order then implemented (user-directed)

After review, the user directed implementation, not just analysis. Shipped in the same
PR, in three workstreams:

1. **Staged self-ship** (`src/jarvis/release.py`, deploy-script `--stage <ver> --wo
   <id>` mode): the work order deploys code and writes
   `$JARVIS_HOME/run/pending_release.json`; the daemon restarts the services only once
   the shipping worker's turn has settled, and on boot verifies version-on-disk plus
   fresh `ExecMainStartTimestamp` on both units, settles the work order and notifies
   the user (attention + kept marker on failure; doctor invariant for stale markers).
   The cgroup fix (bl-a9589e0e) remains open and worthwhile — staging removes the need
   for a self-ship to survive, not the fleet-wide kill on deploys.
2. **Exact per-turn cost accounting**: the `claude -p --output-format json` envelope
   (total_cost_usd, token classes incl. ephemeral 1h/5m, per-API-call context sizes,
   context window) is recorded per turn in `wo_turns.usage_json` (done *and* failed
   turns), lazily backfilled for history from the turn files on disk, and rendered as
   per-turn tables in `jarvis cost <wo-id>` and a per-WO UI drill-down with the context
   growth curve. Provenance is explicit: `recorded` (exact) vs `transcript` (the old
   estimator, now fallback-only) — never silently mixed.
3. **Minimal worker briefing**: the opening prompt shrank ~50% (6,032 → 3,069 chars
   bare; contract core budget-asserted < 2,500 chars) to a load-bearing core plus a
   section index; `jarvis brief <section> [--wo id]` serves the full text on demand
   from the same single-sourced module, with a free composition test in CI and an
   opt-in A/B eval (kn-ea760e6e method) for the behavioral comparison.
