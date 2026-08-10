---
name: jarvis-inject-session
description: Use when the user wants THIS conversation handed over to Jarvis OS — "inject this session into jarvis", "put this in jarvis", "track this conversation", "make this a work order", "jarvis should know about this". Hands the running session to Jarvis as a work order so it shows up in the dashboard and on `jarvis status`. Do NOT use to create a new work order for work that has not started; that is `jarvis wo create`.
allowed-tools: Bash(jarvis wo inject:*)
---

# Handing this session to Jarvis

This project is managed by Jarvis OS. Jarvis does **not** watch the sessions the user
starts — a conversation you opened yourself is private until the user says otherwise.
This skill is how they say otherwise: it hands **this very session** over, as a work
order, so it appears on the dashboard and in `jarvis status` alongside the work Jarvis
dispatched itself.

## When this applies

The user is talking about *this* conversation and wants Jarvis to know about it. They
started here ad hoc, the thread turned out to matter, and they want it tracked.

**Not** for creating a work order for work that hasn't started — that is
`jarvis wo create <project> "<title>"`, and it spawns a *separate* worker.
Injection tracks the conversation you are already in; it starts nothing.

## What injection does — and does not do

It **creates a record and nothing else**. It does not rename this session, does not send
it a turn, and writes nothing into it. Everything after that stays the user's explicit
act: `jarvis wo send <id>` and `jarvis wo resume-auto <id>` are the first things that
write here, and only the user runs those.

An injected session is also **not held to the worker contract**. This session never
received a worker briefing, so it owes no `jarvis wo finish`, and its ending is not a
failure. Say so if the user asks.

## How to inject

Claude Code exports this session's id as `$CLAUDE_CODE_SESSION_ID`. Pass it straight
through — do not go hunting for the id in `claude agents` output and do not guess it:

```bash
jarvis wo inject "$CLAUDE_CODE_SESSION_ID"
```

Optional flags, only when they are needed:

- `--title "<something meaningful>"` — the default title is this session's display name,
  which is often auto-generated and vague. Offer a better one if the conversation has a
  clear subject.
- `--project <name>` — only if the command complains that this directory is not inside a
  registered project, or the user says it belongs elsewhere.

The command prints the work order id. Tell the user that id, and that the session is now
visible on the dashboard.

## Before you run it — two checks

**1. Refuse if this session is already a Jarvis worker.** If `JARVIS_WO_ID` is set, this
conversation *is* a work order already; injecting would file a second record against the
same session.

```bash
if [ -n "$JARVIS_WO_ID" ]; then
  echo "already a Jarvis work order: $JARVIS_WO_ID — nothing to inject"
fi
```

Tell the user their work order id and stop. Do not run `jarvis wo inject`.

**2. Fail loudly if the session id is missing.** `$CLAUDE_CODE_SESSION_ID` is set by
Claude Code itself. If it is empty, something is wrong with the environment — do **not**
fall back to a guess, to a session id scraped from `claude agents`, or to running the
command with an empty argument:

```bash
if [ -z "$CLAUDE_CODE_SESSION_ID" ]; then
  echo "CLAUDE_CODE_SESSION_ID is not set — cannot identify this session"
fi
```

Report that to the user verbatim and stop. The likely causes are an old Claude Code
version or a shell that scrubs the environment. They can still inject by hand: find the
session in `claude agents` and run `jarvis wo inject <session-id>` themselves.

Both checks together:

```bash
if [ -n "$JARVIS_WO_ID" ]; then
  echo "already a Jarvis work order: $JARVIS_WO_ID — nothing to inject"
elif [ -z "$CLAUDE_CODE_SESSION_ID" ]; then
  echo "CLAUDE_CODE_SESSION_ID is not set — cannot identify this session"
else
  jarvis wo inject "$CLAUDE_CODE_SESSION_ID"
fi
```

## Afterwards

- **Injecting twice is safe.** The second run reports the existing work order instead of
  splitting the history across two records. If the session had been retired for going
  idle, re-injecting picks tracking back up and says so.
- **Jarvis follows this session's state from here** — running, blocked, ended. When the
  user closes it, the work order is retired as housekeeping, not as a failure.
- If the command fails, show the user the error as it came out. `jarvis wo inject` is
  explicit about why it refused: an unknown session id, or a directory that is not inside
  any registered project.
