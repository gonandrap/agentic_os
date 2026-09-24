# Spike: can a peer message reach a headless worker mid-turn?

Answers §3 of `2026-09-23-an-assumption-judged-while-the-worker-still-runs.md` (that spec
lives on the planner's branch; this file is where §3's findings land, per Neo question
566). Script: `scripts/spike_peer_message.py`, hand-run.

**Measured 2026-09-24 against Claude Code 2.1.281.** A measurement of an external tool
expires when that tool ships a version (kn-df5574d3) — re-run the script before trusting
this on a newer CLI.

## Verdict: works

| batch | trials | delivered mid-turn AND in transcript |
|---|---|---|
| plain | 5 | 5 |
| `--autocompact 100000` | 2 | 2 |

No mixed batch. Receiver argv mirrored `claude_cli.turn_args()` / `spawn_turn()`: `-p
--output-format json --session-id <uuid> -n "[WO …] …" --permission-mode auto`, detached
with `start_new_session=True` and `stdin=DEVNULL`.

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
4. **Sender identity, and refusing an unknown sender** — yes, but via a hook, not a
   setting. See below.

## Cost

Usage on the absorbing turn, 5/5 plain trials: `cache_creation_input_tokens` 447–451,
`cache_read_input_tokens` 38,678, `input_tokens` 2. The prefix is not re-written. The
queue path's turn boundary re-writes the whole conversation at 1.25x — ~12% of this
project's token spend.

## Identity

What the model sees is `<cross-session-message from="uds:…/<pid>.sock" from-name="…"
from-mode="prompting">`, wrapped in a `<system-reminder>`. `from-name` is self-declared
and forgeable by any same-UID session.

The transcript's `attachment.origin` carries `verifiedPeerPid` and `verifiedPeerProcStart`
— kernel-verified by the CLI over the unix socket. Measured: the pid in the socket path
equals `verifiedPeerPid` (2195073 both), so the pid visible in the envelope is
trustworthy.

**`UserPromptSubmit` fires on an absorbed peer message**, with the raw envelope as its
`prompt`. This corrects the 2026-08-17 review's "no hook fires on receive". Grepping the
2.1.281 binary finds no peer-specific hook event; `UserPromptSubmit` is the seam.

**A `UserPromptSubmit` hook exiting 2 refuses the message**: measured — the receiver ran
all 8 sleep calls and reported `received: false`. So `jarvis _hook` can authenticate a
sender by pid and drop anything that is not the daemon.

## Off switches, and the one that bites

- `DISABLE_TELEMETRY=1` on both sender and receiver does **not** disable peer messaging on
  2.1.281. The docs-era claim (DISABLE_TELEMETRY / DO_NOT_TRACK /
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC / DISABLE_GROWTHBOOK) is stale for at least the
  first. Do not gate the feature on reading those.
- `crossSessionInbound: "refuse"` in the receiver's settings does drop it, silently.

**Sender-side success is not a delivery receipt.** Against a refusing receiver
`SendMessage` still returns `{"success":true, …, "msg_id":…}` and says only that a
"[Cross-session delivery notice] follows if that session holds it … or refuses it". That
notice arrives as a later inbound message, and a one-shot `-p` sender has exited before it
can see one. So confirm delivery from the receiver's side — the `absorbed_mid_turn`
transcript row — and keep the queue path as the fallback. Record-then-send.
