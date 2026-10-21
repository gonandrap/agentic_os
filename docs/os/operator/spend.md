---
type: Jarvis Operator Reference
title: Spend and time
description: The brake, budgets, what work cost in tokens, where its time went, and the alarms raised while it burned.
tags: [operator, cli]
---
# Spend and time

Moved verbatim from the CLAUDE.md crib sheet. `jarvis <command> --help` is the
authoritative flag list; this is the WHY behind each command.

## `jarvis pause [--allow <wo-id,...>] [--reason "…"]`

THE BRAKE. No work order starts a turn — dispatch, relaunch, message delivery, validation — unless allow-listed. Turns in flight finish; nothing is killed or lost. Reach for this, never `systemctl stop` plus kill, when spend is out of control. The dashboard has the same controls.

## `jarvis resume <wo-id...> | --all`

let named orders through a pause (one at a time serialises spend), or lift it. After a usage window reopens the OS ramps back by itself — at most 2 turns in flight for 30min, 1 for 60min if the last window was spent again right after reopening (that also puts a critical item in the inbox) — and compacts a cold, large conversation before relaunching it rather than re-sending it whole.

## `jarvis wo budget <id> [<usd>] [--clear]`

a DOLLAR CEILING on one order. `jarvis wo create ... --budget` sets it up front; this shows it, changes it, or removes it. It governs the WHOLE bill `jarvis cost <id>` reports — the worker's turns PLUS what Jarvis spent on that order (Neo, the panel) — so the number the user typed is the number they can check. Every turn is launched with no more than what is left; at the cap the order stops in `budget_exhausted`, a state of its own because nothing went WRONG, it ran out of money. RAISING IT RESUMES IT, in the same session with its half-finished work intact — so an order stopped this way is never lost. Raising it by less than the overshoot leaves it where it is and says so. NO BUDGET IS THE DEFAULT and means no ceiling, exactly as before. A project can set a standing one in the catalog (`worker.budget_usd`).

## `jarvis fo budget <id> [<usd>] [--clear]`

the same one level up, and it is a FAMILY budget: it bounds the feature's whole rollup — planner, manager, every child. A child takes a slice of what is UNRESERVED when it is dispatched, so two children running at once can never both spend the same remainder, and an unspent slice returns to the pool when its child settles. A child that runs out says what the feature still has unreserved, so "top up the child" and "top up the feature" are distinguishable.

## `jarvis cost [project|wo-id|fo-id]`

what the work cost in tokens. TWO HALVES, shown split and added: the WORKER's own session, read back from Claude Code's transcripts, and what JARVIS ITSELF spent on that order — every Neo answer, every panel seat, every dashboard digest, each recorded as it happens. A work order that asked Neo four questions paid for four calls, and the `jarvis` column is where they show up. A feature order rolls up its planner AND children — the planner is usually the dearest session of an unfinished one. Breaks out the RE-WRITE TAX: every turn after the first re-sends the whole conversation at the cache-WRITE rate, ~12% of fleet spend. Dollars are list prices, a common unit for comparing token kinds — not a bill.

## `jarvis inspect <wo-id|fo-id>`

where the TIME went, which `cost` cannot say. Splits each turn's wall clock into generating / blocked on a subagent / running tools, names every blocking join, profiles the tools, quotes the prompt that started each turn — and LABELS every big cache write by cause: `cold-start`, `ttl-expiry` (the cache had expired) or `prefix-miss` (it had NOT, and the conversation was re-sent anyway — a defect). Those two look identical on a bill, cost the same, and have completely different fixes. Read-only, no model call. The OS also raises this WHILE it happens: a turn past an hour, a join past the cache TTL, or a 300k re-write becomes an attention item on the next reconcile tick. Every threshold is a setting, fleet-wide or per project: `jarvis config set <project> inspect.alarm_turn_minutes 90`. A project naming one keeps the fleet answer for the rest.

## `jarvis alarms [project]`

the other end of that: every turn the OS has raised WHILE it burned, newest first, live ones marked `!`. `jarvis wo ack` answers one — the flag goes down, the alarm stays on the record, and it never re-fires for that turn. Same page on the dashboard at /alarms, where the ack is a button.
