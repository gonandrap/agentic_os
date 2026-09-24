# Cross-session (peer) messaging — MEASURED, not read off release notes

Amends the auto-memory of the same name (reviewed 2026-08-17 against docs, v2.1.232
era), whose "Unverified (spike needed)" list is now answered. **Measured 2026-09-24
against Claude Code 2.1.281** — a measurement of an external tool expires when that tool
ships a version (kn-df5574d3), so re-run `scripts/spike_peer_message.py` before trusting
this on a newer CLI.

Spike: §3 of
`docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md`.
Script: `scripts/spike_peer_message.py` (hand-run; CANNOT run under pytest — the root
`conftest.py` gate points `JARVIS_CLAUDE_BIN` at a stub that exits 1).

## Verdict: DELIVERY WORKS (5/5 trials, plus 2/2 with `--autocompact 100000`).
## SENDER IDENTITY: NOT VERIFIED. Ship the queue path.

Receiver argv mirrored `claude_cli.turn_args()`/`spawn_turn()`: `-p --output-format json
--session-id <uuid> -n "[WO …] …" --permission-mode auto`, detached
(`start_new_session=True`), `stdin=DEVNULL`. ONE DELIBERATE DIFFERENCE: `--session-id` on
a FRESH session, where a real worker's later turns use `--resume`. A resumed session is
NOT covered by these numbers. Mid-turn is proven by construction: a `-p` session gets one
turn and exits, so a message in that turn's result arrived between tool calls.

1. **Mid-turn delivery under `-p`: YES.** Delivered between `Bash` calls, no new turn.
   Receiver was on sleep call 4 of 12 every time.
2. **`SendMessage`/inbox socket available in `-p`: YES, both ends.** A headless sender
   sees headless receivers in `ListAgents` by session name, and a headless receiver binds
   `uds:/run/user/<uid>/cc-socks/<pid>.sock` while its turn runs.
3. **Appears in the receiver's transcript: YES.** Three rows, all with the session id:
   `queue-operation`/`enqueue`, an `attachment` row (`attachment.origin`), and a
   `queue-operation`/`remove` with **`"reason": "absorbed_mid_turn"`** — the exact string
   to look for. So `usage.read_session`, `bill` and `jarvis inspect` do NOT go blind.
4. **Sender identity: NOT VERIFIED.** A hook CAN refuse a sender that is not on a pid
   allow-list, and a forged `from-name` does not help the forger — measured. But nothing
   measured says the PID in the envelope cannot itself be forged. See below.

## The cost claim is real

The absorbing turn's usage, 5/5 trials: `cache_creation_input_tokens` **447–451**,
`cache_read_input_tokens` **38,678**, `input_tokens` 2. The prefix is NOT re-written.
A queued message arriving as a fresh turn re-writes the whole conversation at 1.25x —
that boundary is ~12% of this project's token spend.

## Sender identity, and how to refuse one

What the model SEES is a `<cross-session-message from="uds:…/<pid>.sock"
from-name="…" from-mode="prompting">` envelope wrapped in a `<system-reminder>`. The
`from-name` is **self-declared and forgeable by any same-UID session** — do not trust it.

The transcript's `attachment.origin` carries `verifiedPeerPid` and
`verifiedPeerProcStart`. In two HONEST trials the socket-path pid equalled
`verifiedPeerPid` (2195073; 2870319). **Agreement, not verification** — no trial made a
sender present another process's pid, so DO NOT call the envelope pid trustworthy.

**A hook never sees `verifiedPeerPid`.** Measured: the `UserPromptSubmit` payload is
exactly `{cwd, hook_event_name, permission_mode, prompt, prompt_id, session_id,
session_title, transcript_path}`. The only sender fact a hook can read is the envelope
TEXT; the kernel-verified fields land in the transcript, too late to answer with.

`--mode identity` (1 trial each, 2026-09-24): honest sender, pid published to the hook's
allow-list — absorbed mid-turn, receiver stopped at call 4 of 12. Forger, same binary,
same self-declared `-n jarvis-daemon`, pid not published — REFUSED, receiver ran all 12
calls, `received: false`.

NOT MEASURED, and it is what blocks the peer path: a same-UID process presenting a `from`
path naming ANOTHER pid. Discovery is `~/.claude/sessions/<pid>.json` (plain per-session
files carrying `name` and `messagingSocketPath`); a fabricated entry there was NOT listed
by a real sender's `ListAgents` and its socket was never contacted, so the wire protocol
was never captured and no raw forging client was written.

**`UserPromptSubmit` FIRES on an absorbed peer message** (this corrects the auto-memory's
"no hook fires on receive"). Its payload's `prompt` is the raw
`<cross-session-message …>` envelope; `session_title` is the receiver's `-n` name.
Grepping the 2.1.281 binary finds no peer-specific hook event — `UserPromptSubmit` is the
seam.

**A `UserPromptSubmit` hook exiting 2 REFUSES the message**: measured, the receiver ran
all 8 sleep calls and reported `received: false`. That is the seam `jarvis _hook` would
sit in — but it can only be as trustworthy as the envelope pid, which is the open
question above.

## The off switches — and the one that bites

- `DISABLE_TELEMETRY=1` on BOTH sender and receiver does **NOT** disable peer messaging on
  2.1.281. ONE variable, ONE measurement: `DO_NOT_TRACK`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` and `DISABLE_GROWTHBOOK` were NOT tested.
  What follows is only that the docs-era list of four is wrong about at least one, so
  reading it is no substitute for testing the one you care about.
- `crossSessionInbound: "refuse"` in the RECEIVER's settings **does** drop it silently.

**SENDER-SIDE SUCCESS IS NOT A DELIVERY RECEIPT.** Against a refusing receiver
`SendMessage` still returns `{"success":true, …, "msg_id":…}`, saying only that a
"[Cross-session delivery notice] follows if that session holds it … or refuses it". That
notice arrives as a later inbound message — which a headless `-p` sender has already
exited before it can see. So a peer send from a one-shot process can never observe its own
refusal. Anything built on this transport must confirm delivery from the RECEIVER's side
(the `absorbed_mid_turn` transcript row) and keep the queue path as the fallback, exactly
as the record-then-send principle says.
