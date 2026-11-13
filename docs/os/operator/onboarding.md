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
  An existing draft is resumed; `--fresh` starts over. `--like <project>` starts a new
  project's draft from what that project sets, never its path, description, deploy copy,
  tracker or launcher, and leaves every must-have area for you to confirm.
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
then the project's row, so `jarvis status` sees it. The running daemon hot-adds a NEW
project on its next tick (`Daemon.reload_catalog` loads an added project that has a row),
so no restart is needed for it. Only restart-class keys of an existing project need one:
`apply` then prints the exact `jarvis restart --reason "…"` to run; `jarvis start` leaves
a running OS as it is. `jarvis doctor` flags a catalog project the daemon has not loaded
(`INV-CATALOG-PROJECT-LOADED`).

## Onboarding from the dashboard

The same draft, as pages: `jarvis ui`, then **onboard** in the sidebar (`/onboard`).
Spec: `docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md`.

- **The index** lists drafts in progress (answered of 27, the project copied from, last
  edit; a draft that does not load shows its error) and a form: repository path, optional
  name, and **start from** — shipped defaults, or "copy settings from <project>" (the
  `--like` of the terminal; identity is never copied). "Start over" is `--fresh`. Each
  onboarded project without a draft has a **revise** button that opens its re-run draft.
  A refusal (not a git repository, a draft that already exists) shows its error with what
  you typed kept.
- **The rail** on every draft page lists every section with its status and progress. Its
  **filter** (`f`) matches section titles and every setting's path and line across all
  sections; a match is a link straight to that row (`/onboard/<name>/<section>?focus=<path>`).
  `[` and `]` step through sections. Each section offers its postures, "accept defaults",
  "keep these" when pre-filled, and per-setting editors; every edit saves the draft.
- **Help** ("Ask for help", or "?" on a row) opens a background `onboard_helper` Claude
  session about that section. It reads, it never writes: it tells you which value and
  links to where to set it.
- **Review** is the last section: what still needs deciding, the real diff with each row
  linked to its setting, the reason the safety keys require, and **apply** — the same one
  write as `jarvis onboard apply`.
- A **new** project, or a restart-class setting, needs the OS restarted after apply: the
  applied page (and `/config`) shows **Restart now**, the same as
  `jarvis restart --reason "…"`. `jarvis start` leaves a running OS as it is.

The terminal and the dashboard share one draft: start in one, continue in the other.

## Revising a project

Run `jarvis onboard start <path>` again on an onboarded project. The draft arrives
pre-filled from its live settings — each area at the posture its values match, else
`custom` — so the interview revises rather than resets, and an unchanged re-run writes no
new version.
