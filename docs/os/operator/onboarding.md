---
type: Jarvis Operator Reference
title: Onboarding
description: The interview that decides what the OS does automatically on a project, and the one validated write that applies it.
tags: [operator, cli, config]
---
# Onboarding

`jarvis adopt` makes a project OS-ready and leaves every setting at its shipped default,
which nobody chose. Onboarding is the other path: an interview in which the user decides,
area by area, what the OS may do on their code without them — validation, Neo, the
supervisor, gates, budgets, self-evolution and the rest — and the answers land as ONE
configuration version. Spec: `docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md`.

## The four commands

- `jarvis onboard start <path> [--name n] [--print-prompt]` — opens the interview: an
  interactive Claude session in this terminal, run by the `greeting_agent` persona.
  `--print-prompt` prints the persona root and the opening prompt instead, for when
  `claude` is not on `PATH` or you want to paste it into a session already open.
- `jarvis onboard questions [--area a] [--json]` — the whole interview as data: every area,
  its postures, the knobs it asks about. No model needed.
- `jarvis onboard explain <area|path> [--project p]` — what an area or one setting does,
  what it costs on and off, a worked example, when a change takes effect, and the spec it
  is grounded in.
- `jarvis onboard apply --from <draft.json> [--dry-run]` — the one write. `--dry-run`
  prints the diff and writes nothing.

## How an interview runs

`start` refuses a path that is not a git repository, writes a draft to
`$JARVIS_HOME/onboarding/<name>.json` (never into the project), and launches `claude` with
its working directory at `$JARVIS_HOME`: the project's workspace trust is only written by
the bootstrap, after the interview. The agent then takes one area at a time, in registry
order, must-have areas first. For each it offers the area's **postures** — named presets,
such as `balanced` or `off`, each a fixed set of values from `jarvis onboard questions` —
or per-knob answers, or "accept the defaults". Any "what happens if…" is answered from
`jarvis onboard explain`, never from the agent's own knowledge. Before applying, it asks
why you chose what you chose: most of these are safety keys, and the write is refused
without a reason in your words. It shows the dry-run diff and waits for you to say go.

**Accepting a default writes nothing.** The area is recorded as answered and the catalog
stays silent about it, so a later change to the shipped default still reaches the project.

**Fleet-wide areas.** An area scoped `os` changes every project. When the catalog already
holds other projects, the agent says which it would reach before touching one, and leaves
the fleet setting alone unless you choose otherwise.

## What apply does

One configuration version, visible in `jarvis config history` with actor `onboard` and
your reason; then the bootstrap, from the spec read back out of the committed catalog;
then the project's row, so `jarvis status` sees it. A NEW project does not exist for the
running daemon until `jarvis start` — `Daemon.reload_catalog` refuses to reload when the
set of project names changes, and freezes every other setting change in the file until
then. `apply` prints the exact `jarvis start --catalog <file>` to run.

## Revising a project

Run `jarvis onboard start <path>` again on an onboarded project. The draft arrives
pre-filled from its live settings — each area at the posture its values match, else
`custom` — so the interview revises rather than resets, and an unchanged re-run writes no
new version.
