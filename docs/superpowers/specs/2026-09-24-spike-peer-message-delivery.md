# Spike: can a peer message reach a headless worker mid-turn?

Answers §3 of `2026-09-23-an-assumption-judged-while-the-worker-still-runs.md` (that spec
lives on the planner's branch; this file is where §3's findings land, per Neo question
566). Script: `scripts/spike_peer_message.py`, hand-run.

**Measured 2026-09-24 against Claude Code 2.1.281.** A measurement of an external tool
expires when that tool ships a version (kn-df5574d3) — re-run the script before trusting
this on a newer CLI.

## Verdict: delivery WORKS. Sender identity NOT verified.

Unknowns 1-3 are positive. Unknown 4 is not: a receiver-side hook CAN refuse a sender
that is not on a pid allow-list (measured), but whether the pid it reads can be FORGED
by another same-UID process was NOT measured. §3.2 says a "no" on unknown 4 means the
peer transport does not ship, so the `objection` child ships the QUEUE path. The peer
path needs the forgery trial below closed first.

| batch | trials | delivered mid-turn AND in transcript |
|---|---|---|
| plain | 5 | 5 |
| `--autocompact 100000` | 2 | 2 |

No mixed batch. Receiver argv mirrored `claude_cli.turn_args()` / `spawn_turn()`: `-p
--output-format json --session-id <uuid> -n "[WO …] …" --permission-mode auto`, detached
with `start_new_session=True` and `stdin=DEVNULL`.

**One deliberate difference: `--session-id` on a FRESH session.** A real worker's second
and later turns use `--resume` on an existing one. Nothing measured here distinguishes
the two, and a resumed session is not covered by these numbers.

Mid-turn is proven by construction, not asserted: a `-p` session gets one turn and exits,
so a message that shows up in that turn's result arrived between tool calls. Every
receiver was on sleep call 4 of 12 when it absorbed the message.

## The four unknowns

1. **Mid-turn delivery under `-p`** — yes. No new turn.
2. **`SendMessage` / the inbox socket available in `-p`** — yes, both ends. A headless
   sender sees headless receivers in `ListAgents` by session name; a headless receiver
   binds `uds:/run/user/<uid>/cc-socks/<pid>.sock` while its turn runs.
3. **Appears in the receiver's transcript** — yes. Three rows against the session id:
   `queue-operation`/`enqueue`, an `attachment` row carrying `attachment.origin`, and a
   `queue-operation`/`remove` with `"reason": "absorbed_mid_turn"`. `usage.read_session`,
   `bill` and `jarvis inspect` do not go blind.
4. **Sender identity, and refusing an unknown sender** — **NOT VERIFIED.** A hook can
   refuse a sender whose pid is not allow-listed, and a forged `from-name` does not get
   past it. But the pid itself is only a string in the envelope, and no trial made a
   sender present someone else's pid. See below.

## Cost

Usage on the absorbing turn, 5/5 plain trials: `cache_creation_input_tokens` 447–451,
`cache_read_input_tokens` 38,678, `input_tokens` 2. The prefix is not re-written. The
queue path's turn boundary re-writes the whole conversation at 1.25x — ~12% of this
project's token spend.

## Identity

What the model sees is `<cross-session-message from="uds:…/<pid>.sock" from-name="…"
from-mode="prompting">`, wrapped in a `<system-reminder>`. `from-name` is self-declared
and forgeable by any same-UID session.

The transcript's `attachment.origin` carries `verifiedPeerPid` and `verifiedPeerProcStart`.
Measured: in two HONEST trials the pid in the socket path equalled `verifiedPeerPid`
(2195073, and 2870319). **That is agreement, not verification** — no trial had a sender
present another process's pid, so nothing here says the envelope pid cannot be forged by
a same-UID process. Do not authenticate on it yet.

**The hook never sees `verifiedPeerPid`.** Measured, both identity trials: the
`UserPromptSubmit` payload is `{cwd, hook_event_name, permission_mode, prompt,
prompt_id, session_id, session_title, transcript_path}` and nothing else. The only thing
a hook can read is the envelope TEXT. The kernel-verified fields reach the transcript,
which the hook cannot consult before it must answer.

### The identity trial, and what it does and does not show

`--mode identity`, 2026-09-24, 1 trial each:

| case | sender declares | sender pid published to the hook | outcome |
|---|---|---|---|
| honest | `-n jarvis-daemon` | yes | absorbed mid-turn, receiver stopped at call 4 of 12 |
| forger | `-n jarvis-daemon` | no | REFUSED; receiver ran all 12 calls, `received: false` |

So a receiver CAN tell an allow-listed sender from one it does not know, and the
self-declared `from-name` buys the forger nothing.

**Not measured, and it is the blocking gap:** a same-UID process presenting a `from`
path that names ANOTHER pid. Two routes were tried and neither produced the trial.
Registering a fabricated peer in `~/.claude/sessions/<pid>.json` (the discovery registry
— plain files, one per session, carrying `name` and `messagingSocketPath`) did NOT make
it visible: a real sender's `ListAgents` never listed it and never connected to its
socket (zero bytes on a decoy listener), so the wire protocol was never captured and a
raw forging client was never written.

**`UserPromptSubmit` fires on an absorbed peer message**, with the raw envelope as its
`prompt`. This corrects the 2026-08-17 review's "no hook fires on receive". Grepping the
2.1.281 binary finds no peer-specific hook event; `UserPromptSubmit` is the seam.

**A `UserPromptSubmit` hook exiting 2 refuses the message**: measured — the receiver ran
all 8 sleep calls and reported `received: false`. That is the seam `jarvis _hook` would
sit in. What it can authenticate on is only as good as the envelope pid, which is the
open question above.

## Off switches, and the one that bites

- `DISABLE_TELEMETRY=1` on both sender and receiver does **not** disable peer messaging on
  2.1.281. ONE variable, one measurement: `DO_NOT_TRACK`,
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` and `DISABLE_GROWTHBOOK` were NOT tested and
  nothing here says what they do. What the measurement does say is that the docs-era list
  is wrong about at least one of its four, so reading it is not a substitute for testing
  the one you care about.
- `crossSessionInbound: "refuse"` in the receiver's settings does drop it, silently.

**Sender-side success is not a delivery receipt.** Against a refusing receiver
`SendMessage` still returns `{"success":true, …, "msg_id":…}` and says only that a
"[Cross-session delivery notice] follows if that session holds it … or refuses it". That
notice arrives as a later inbound message, and a one-shot `-p` sender has exited before it
can see one. So confirm delivery from the receiver's side — the `absorbed_mid_turn`
transcript row — and keep the queue path as the fallback. Record-then-send.
