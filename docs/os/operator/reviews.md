---
type: Jarvis Operator Reference
title: Reviews, gates and Neo
description: Assumption review, validation rounds, privileged-action gates and Neo's ledgers.
tags: [operator, cli]
---
# Reviews, gates and Neo

Moved verbatim from the CLAUDE.md crib sheet. `jarvis <command> --help` is the
authoritative flag list; this is the WHY behind each command.

## `jarvis wo review <id> [--reject] [--feedback "why"]`

feedback teaches Neo; on --reject it also goes to the worker. A project may hand the ROUTINE ones to Neo: `jarvis config set <project> validation.auto_review true --reason "…"`. Ships OFF everywhere, and it only ever ACCEPTS — anything Neo cannot defend, and anything high-stakes, still waits for the user, with Neo's reading attached. What it decided, and why, is on `jarvis wo show` against each assumption, saying the OS decided it and never you. Correct one with `jarvis neo review <qid> --correct "…"`.

## `jarvis validation show <wo-id|fo-id>`

HOW a unit was judged. `wo show` and `fo show` already carry each round — its number, its outcome and the reason the submitter was sent back. This is the deliberation behind them: every seat's verdict, model, latency and raw reply, plus the envelopes the feedback travelled in. On demand only, so nothing pushes it at the user; reach for it when they ask WHY a unit was rejected, not to report that it was. Takes either id.

## `jarvis validation force <wo-id> --reason "…"`

judge this work order AGAIN, now. The panel's verdict is about a COMMIT, and every round judged before 0.10.0 recorded none — so those orders can never auto-merge, however green. This opens a fresh round that reads the CURRENT pull request and records the commit, with no worker and no `finished` event on the timeline. --reason is required and is stored on the round, so a forced re-judgement never reads afterwards like a worker's own re-delivery. ONLY on an order that has DELIVERED and whose worker is not typing: `waiting_pr_merge` (the case it exists for) or `needs_review`. A running one is refused — an open round owns the worker's session, so forcing there would put the panel's feedback into a session mid-task — and so is a settled one. It leaves the order `validating` until the verdict, back to `waiting_pr_merge` if it passes. It spends a round number like any other, so a REJECTION at or past `max_rounds` comes to you rather than to a worker. Also refuses with no pull request or with a round already open. The work order's dashboard page carries the same control, disabled with the reason whenever one of those refusals applies — and it is where the user can SEE why an order is parked: the commit each round judged against the live head of the pull request.

## `jarvis gate list [--pending]`

privileged-action approvals (merge a PR, ship a release). Workers attempt these and get blocked; Neo reviews and decides, so most never reach the user. Only the ones Neo escalates show up in `jarvis status`.

## `jarvis gate show <id>`

the request exactly as the reviewer saw it

## `jarvis gate approve <id> --reason "…"`

open the gate: the worker may run the command

## `jarvis gate deny <id> --reason "…"`

refuse it; the reason goes to the worker

## `jarvis gate dismiss <id> --reason "…"`

NOT a gated action: the recogniser matched a command that ships nothing (a release script named in a grep pattern, a path quoted in a PR body). Unblocks it, records a classifier defect rather than an authorisation, and is counted separately so the false-positive rate is visible. Never approve or deny one: both write something false into the record. A dismissal also TEACHES the recogniser: the OS derives a standing rule from the shape and stops asking about it, fleet-wide.

## `jarvis gate rules`

what the OS believes is privileged, and what it has learned is not. Seeded from the builtins, grown from dismissals. The last line is the one to read: whether every command that MUST gate still does.

## `jarvis gate rule-retract <id> --reason "…"`

the user overruling a rule the OS learned. Retracting an exemption re-arms a gate

## `jarvis gate explain "<command>"`

why a command would or would not be gated — paste the exact string from a gate record instead of guessing at a false positive

## `jarvis neo list`

Neo's Q&A: pending reviews + escalations

## `jarvis neo review <qid> [--correct "…"]`

approve or teach; corrections become learnings

## `jarvis neo answer <qid> "…"`

answer a question Neo escalated to the user

## `jarvis neo learnings [--project p]`

what Neo has been taught, with ids

## `jarvis neo stats [--project p] [--days n]`

IS NEO EARNING ITS KEEP: volume, outcomes, the escalation rate day by day, what it escalates FOR, per-order averages, spend and latency. The causes read as three answers to "who decided": Neo handed it back, Neo answered and the OS overrode it, or Neo never answered. Same report at /neo/stats

## `jarvis neo retract <id> --reason "…"`

retire a ruling the user has REVERSED. Both ledgers are append-only, so without this a superseded ruling stays in every prompt beside its replacement. NOT a delete: the row stays listed, marked ⊘ with the reason, and only leaves the prompt. --reason is required. Use it the moment the user contradicts something they told you before.
