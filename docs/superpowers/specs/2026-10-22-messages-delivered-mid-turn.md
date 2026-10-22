# Messages delivered mid-turn

**Status:** implemented · **Date:** 2026-10-22

## Problem

A message for a worker (the user's feedback via `jarvis wo send`, Neo's objection to an
assumption, a gate decision) waited in `wo_messages` until the worker's turn ended. Then
the daemon sent it as the next turn. Turns of 20 minutes are common, so feedback that
would have changed the work arrived after the work was done, and the worker spent another
turn undoing it.

The daemon cannot push into a running turn:
- On the background transport, a live session only takes a prompt through `claude stop`
  plus a bare resume. Stopping kills the in-flight tool call and subagents, and resuming
  a live session starts a copy.
- On headless, the turn's prompt is fixed when it starts.

## Design

A `PostToolUse` hook can return `additionalContext`, and Claude Code puts it in front of
the model on its very next call. The compaction re-assertion already relies on this. The
worker settings run the hook after every tool call, so the lead can receive what waits
for it at its next tool call:

1. `ProjectStore.queue_message` touches `.jarvis/inbox/<wo>.pending` for every queued
   user-to-agent row.
2. `hooks._post_tool_delivery` is on the hot path, so it costs one `stat` when no flag
   exists.
3. When the flag exists, `hooks.deliver_mid_turn` checks the order:
   - it is `running`;
   - its latest turn is running and is not a compaction;
   - the hook's session is the order's session.

   Otherwise it leaves the flag and returns, and the daemon delivers as before.
4. It spends the flag *before* reading. A message queued later re-arms it.
5. It claims each deliverable row with a conditional `UPDATE … WHERE status='queued'`
   (`claim_message`), so a row is never sent twice.
6. It skips rows that are a whole turn's prompt: `relaunch` (a paused turn's resume) and
   `background-orphan`.
7. It stamps Neo objections as delivered (`ops.stamp_objection_delivery`, shared with the
   daemon) and records `message_delivered` with `mid_turn: true`.
8. It returns the messages under `MID_TURN_HEADER` as `additionalContext`.

A subagent's tool call (`agent_type` in the payload) never takes the lead's message.
Context injected there would reach the subagent, not the lead.

Both transports get this. The hook does not care how the turn was launched.

## Limits

- **A message arrives at the next tool call, not instantly.** A lead blocked on one long
  foreground command gets it when that command returns.
- **A background session waiting on its own task makes no tool calls.** That covers both
  `busy` with no model activity, and subagents still running. The message waits until the
  task wakes the session and the session calls a tool, or until the turn ends and the
  daemon sends it. Delivering earlier would mean stopping the session, which this design
  rejects.
- **A message that arrives after the last tool call of a turn** is still seen: the model
  makes one more call after every tool result.
