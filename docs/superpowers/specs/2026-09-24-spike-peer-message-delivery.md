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

## 2026-10-21: re-measured on 2.1.301, and a design that does not need the sender authenticated

Recorded from improvement order io-ce566253 (`nudge-not-content`), whose analyst re-ran
`scripts/spike_peer_message.py` against the CLI installed on 2026-10-21. Nothing below was
re-run for this note; it records that run's figures and the design they lead to.

**Re-measurement, Claude Code 2.1.301** (beside the 2.1.281 numbers above, which stand for
that version):

| measurement | 2.1.281 (2026-09-24) | 2.1.301 (2026-10-21) |
|---|---|---|
| plain delivery, mid-turn and in transcript | 5/5 | 2/2, verdict `works` |
| forger with an allow-listed `from-name` | refused | refused, `receiver_received: false` |
| `UserPromptSubmit` sees `verifiedPeerPid` | no | no (`hook_saw_verified_peer_pid: false`) |
| disablers set / `crossSessionInbound` | none / unset | none / unset |

Two things are new on 2.1.301, read off the registry rather than the binary:

- a per-session key file `~/.claude/sessions/<pid>.<sha256>.key`, mode `0600`, holding
  `{peerToken (32 chars), procStart, pidDomain}`;
- the registry entry declares `peerProtocol: 1` and `peerFeatures: [notify_idle,
  reply_across_default_dirs, artifact_yield]` — a versioned protocol, where 2.1.281 offered
  only an undocumented surface.

**The veto stands, and the key file does not lift it.** Every Jarvis worker runs as the
same UID as jarvisd, so a `0600` file owned by that UID is readable by every worker, every
subagent and any stray script on the machine. A bearer token the adversary can read is not
an authenticator. Re-measuring does not change §3.2's answer: the receiver cannot tell who
is talking.

### The proposed design: the envelope carries no guidance

The veto is about the PAYLOAD, not the transport. §6.4 of the parent spec put the
objection's WORDS in the envelope, so the receiver had to trust the sender. It need not:
§6.1 already records the objection in the project store BEFORE anything is sent ("Record,
then send"), so the authoritative copy is on the record and the envelope can be empty.

1. **A content-free nudge.** jarvisd sends a peer message carrying at most an assumption
   number and one fixed line: re-read your record. No reason text, no instruction.
2. **A read verb that is the receipt.** A narrow `jarvis` read command returns the
   worker's own outstanding objections from the project store. That fetch, and only that
   fetch, stamps `objection_delivered_ts`. This also answers §3.3 ("a send is not a
   receipt"): the OS records a fact it actually has, on the receiver's side.
3. **A rate limit per order.** The worst a forger can do is make a worker re-read its own
   genuine record. Bounding nudges per order keeps that from being a context-exhaustion
   attack.
4. **The queue path stays as the fallback**, unchanged. A nudge that is never fetched
   leaves the objection outstanding, and it rides the next turn boundary exactly as today.

The nudge bypasses no hold: it is not a message on `wo_messages`, and
`worker_session.delivery_hold` and `autoreview.decide_early` are untouched by it.

### Open gate: can jarvisd send a peer message without a model call?

**Not measured, and nothing ships until it is.** jarvisd is plain Python, not a Claude
session. The 2026-09-24 sender was a `claude -p` process: about 10.6s and one model call
per send. A nudge must not cost a model call. `peerProtocol: 1` makes speaking the uds
socket directly more tractable than it was, but the 2026-09-24 spike failed to capture the
wire format and nobody has tried since.

That is a spike, with its kill criterion stated up front:

- **Success:** jarvisd delivers a nudge to a running `claude -p` worker in under ~1s with
  zero model tokens. Anything else is NO, and the spike prices the `claude -p` sender
  instead.
- **Also settle:** whether one worker can read another session's `0600` key file (the
  threat model above assumes yes), and whether a `--resume` session absorbs mid-turn (this
  spike covered fresh sessions only).
- **Out of scope for the spike:** product code, or any change under `src/jarvis/`.

The spike reads live session registries under `~/.claude/sessions`, so it waits for the
user's go-ahead. It is the remaining step on this transport.
