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

## Verdict: WORKS. 5/5 trials, plus 2/2 with `--autocompact 100000`.

Receiver argv mirrored `claude_cli.turn_args()`/`spawn_turn()`: `-p --output-format json
--session-id <uuid> -n "[WO …] …" --permission-mode auto`, detached
(`start_new_session=True`), `stdin=DEVNULL`. Mid-turn is proven by construction: a `-p`
session gets one turn and exits, so a message in that turn's result arrived between tool
calls.

1. **Mid-turn delivery under `-p`: YES.** Delivered between `Bash` calls, no new turn.
   Receiver was on sleep call 4 of 12 every time.
2. **`SendMessage`/inbox socket available in `-p`: YES, both ends.** A headless sender
   sees headless receivers in `ListAgents` by session name, and a headless receiver binds
   `uds:/run/user/<uid>/cc-socks/<pid>.sock` while its turn runs.
3. **Appears in the receiver's transcript: YES.** Three rows, all with the session id:
   `queue-operation`/`enqueue`, an `attachment` row (`attachment.origin`), and a
   `queue-operation`/`remove` with **`"reason": "absorbed_mid_turn"`** — the exact string
   to look for. So `usage.read_session`, `bill` and `jarvis inspect` do NOT go blind.
4. **Sender identity: YES, and refusable — via a hook, not via a setting.** See below.

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
`verifiedPeerProcStart`, kernel-verified by the CLI over the unix socket. Measured: the
pid in the socket path equals `verifiedPeerPid` (2195073 both), so the pid in the
envelope the hook sees is trustworthy.

**`UserPromptSubmit` FIRES on an absorbed peer message** (this corrects the auto-memory's
"no hook fires on receive"). Its payload's `prompt` is the raw
`<cross-session-message …>` envelope; `session_title` is the receiver's `-n` name.
Grepping the 2.1.281 binary finds no peer-specific hook event — `UserPromptSubmit` is the
seam.

**A `UserPromptSubmit` hook exiting 2 REFUSES the message**: measured, the receiver ran
all 8 sleep calls and reported `received: false`. So `jarvis _hook` can authenticate a
sender by pid and drop anything that is not the daemon.

## The off switches — and the one that bites

- `DISABLE_TELEMETRY=1` on BOTH sender and receiver does **NOT** disable peer messaging on
  2.1.281. The docs-era claim (DISABLE_TELEMETRY / DO_NOT_TRACK /
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC / DISABLE_GROWTHBOOK switch the feature off) is
  stale for at least the first of them. Do not gate the feature on reading those.
- `crossSessionInbound: "refuse"` in the RECEIVER's settings **does** drop it silently.

**SENDER-SIDE SUCCESS IS NOT A DELIVERY RECEIPT.** Against a refusing receiver
`SendMessage` still returns `{"success":true, …, "msg_id":…}`, saying only that a
"[Cross-session delivery notice] follows if that session holds it … or refuses it". That
notice arrives as a later inbound message — which a headless `-p` sender has already
exited before it can see. So a peer send from a one-shot process can never observe its own
refusal. Anything built on this transport must confirm delivery from the RECEIVER's side
(the `absorbed_mid_turn` transcript row) and keep the queue path as the fallback, exactly
as the record-then-send principle says.
