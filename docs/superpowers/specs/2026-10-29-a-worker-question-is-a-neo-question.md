# A worker question is a Neo question

wo-0d358979. Extends the "Neo is your first responder" rule every worker brief carries.

## The problem

Claude Code's `AskUserQuestion` tool opens a multiple-choice dialog in the session's own
terminal. A work-order session has nobody at that terminal, so the call sits there, and the
OS never sees what was asked:

- The only signal is the `Notification` hook's generic "Claude needs your permission". The
  order goes to `waiting_input` with that text and nothing else: the questions do not appear
  in `jarvis wo show`, the dashboard or the inbox.
- When the user attaches to answer and then detaches, Claude Code rejects the pending tool
  call ("The user doesn't want to proceed with this tool use… STOP"). The worker reads that
  as "the user declined", makes its own decision and carries on. In wo-0d358979 it parked on
  PR merge with two unanswered sequencing questions that existed only in its transcript.

The OS already models this input, and models it well: `jarvis wo ask` records the question,
moves the order to `waiting_input` without flagging the user, and Neo answers as the user or
escalates. The brief tells workers to use it. The native tool is a second channel that
bypasses all of that, and nothing enforced the brief.

## The fix

The worker PreToolUse matcher in `assets/settings.base.json` now names `AskUserQuestion`.
`hooks.native_question_decision`, the hook arm behind it, converts the call instead of
letting it through:

1. Each entry in `tool_input.questions` becomes one `ops.ask_question`, the same call
   `jarvis wo ask` makes. That keeps questions atomic, one per Neo question. The text is
   `[header] question Options, pick one of: (1) label — description; …`. When an entry is
   over `question_max_chars`, its option descriptions are dropped.
2. The tool call is denied. The reason lists the `Q<id>`s it filed, says the dialog's absence
   is NOT the user declining, and tells the worker to end its turn: the answers arrive as
   its next user turn, through the same delivery `jarvis wo ask` uses.
3. If an entry cannot be filed, the call is still denied, and the reason names
   `jarvis wo ask <wo> --question-file` for the entries that failed. A dialog is never the
   fallback.

Scope: only sessions with `JARVIS_WO_ID` set, which means OS-spawned workers and their
subagents. The user's own sessions, injected ones included, keep the dialog, because a
human is at the terminal.

## What it does not do

- `ExitPlanMode` is the other dialog that waits for a human. Workers run in `auto`
  permission mode and never enter plan mode, so it is left alone until it shows up.
- The worker brief is unchanged. It already says to use `jarvis wo ask`. The deny reason
  teaches the rule the one time it is broken, and editing the brief would move every
  worker's cached prompt prefix.
