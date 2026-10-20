---
type: Jarvis Operator Reference
title: Records, bugs and knowledge
description: Finding records, the tracker, bug reports, the knowledge base, wiring and self-checks.
tags: [operator, cli]
---
# Records, bugs and knowledge

Moved verbatim from the CLAUDE.md crib sheet. `jarvis <command> --help` is the
authoritative flag list; this is the WHY behind each command.

## `jarvis status [--json]`

whole-OS pulse; --attention for the short list

## `jarvis search "<words>" [--project p]`

FIND A RECORD AGAIN — work orders, feature orders, Neo questions, alarms, gates, backlog items and knowledge, across the fleet or inside one project. SETTLED AND HIDDEN RECORDS INCLUDED: the listings hide completed work behind a count, and this is what replaces expanding it. `--kind` narrows to one kind, an id typed in full jumps straight to it, and each hit prints the `jarvis` command that shows it. Same box sits in the dashboard's header, and on each project page scoped to that project.

## `jarvis issues [project]`

TRACKER ISSUES THE FLEET KEEPS RUNNING INTO, most-referenced first. A validation panel files its non-blocking findings as GitHub issues; this is the other end of that — how many DISTINCT work orders have pointed at each one, which is the signal for what to pick up next. Three things count and only three: the order whose review RAISED it, an order DISPATCHED to fix it, and an order whose brief CITES it. A passing mention is not a reference — over-counting would destroy the ranking. Local, no network; the same list is a section on each project's dashboard page, and the count also rides on the issue itself as a `referenced: N` label so it shows when scanning GitHub. Each order's own list — every follow-up it raised, across every round — is on `jarvis wo show` and its page.

## `jarvis issues start <issue-url|`

number>    # open a work order ON a tracker issue. THE ROUTE A BUG THAT WAS NOT DISPATCHED TAKES: `jarvis bug report` files the issue and nothing else — no backlog item, because GitHub issues replaced the backlog and the user stopped reading it. So a `low`/`medium` /`high` filing, and a `critical` Neo downgraded or could not settle, all wait on the TRACKER, and this is what picks one up. Hands back the existing work order if one is already live on that issue. `--project` when the issue is not on the OS's own tracker.

## `jarvis inbox / jarvis inbox ack [id]`

(no further notes)

## `jarvis learn add "insight" [--project p] [--pin] / search <term> [--project p]`

(no further notes)

## `jarvis learn show <kn-id> / list [--topic t] / topics / pin <id> / unpin <id>`

worker prompts carry an INDEX of the knowledge base (headline + id, bounded); workers fetch full text on demand. `pin` = ride along verbatim in every prompt — safety rails only.

## `jarvis learn stats [--project p] [--days n]`

what memory COSTS and whether anyone reads it: the index's share of a dispatch prompt, entries nobody has ever opened, searches that came back empty, and orders that completed without one read. Reach for it when the base feels bloated — the entries themselves never reach a prompt, so the number that matters is retrieval, not size.

## `jarvis learn retract <id> --reason "…"`

same for the knowledge base: retire a superseded entry so it stops reaching workers — it leaves the index too, not just the payload — without erasing that it was true

## `jarvis bug report "title" -d "..." -e "expected" -a "actual" -p <priority>`

a bug in the OS itself -> GitHub issue on the (PUBLIC) tracker + Telegram ping. Every agent has the report-jarvis-bug skill. --priority is REQUIRED and is the ONLY thing that routes the bug: low/medium/high wait on the tracker for `jarvis issues start`; critical/blocker are RE-ASSESSED BY NEO against the rubric in `--help`, and only if Neo confirms does a work order get created and a release ship once that fix LANDS. The filing agent states a claim, not a verdict — the level it claimed stays in the issue body, the level after Neo is the `priority:` label, and both are on the issue. Neo's REASONING never is — it is private, in the inbox and in `jarvis neo show`, because nothing a model wrote in prose goes on a public tracker unread. Neo unreachable = it stays queued, unconfirmed: nothing is dispatched off an unconfirmed blocker. A picked-up issue is labelled `in progress` and closes ITSELF — with the work order and the PR on it — once the code lands, so never close one by hand: you would be racing the daemon. --expedite is the ONE way to jump that queue, and it is a SCHEDULING decision, not a rating: at ANY priority it files the issue as usual AND dispatches a work order on it immediately — the same thing `jarvis issues start` would do, in one step, printed as the wo-id. Use it so the priority can stay an honest description of the defect instead of being inflated to buy attention. AN EXPEDITED FIX SHIPS A RELEASE WHEN IT LANDS, at ANY priority and whatever Neo later says about the rating: expediting IS the user saying they want this in production now, so making them ask again once it merges is asking twice. Several landing close together make ONE release, and a red `main` holds it rather than shipping it. The rule that has NOT changed is the other one: a CLAIMED critical/blocker buys no release ON ITS OWN — an unexpedited claim is dispatched only once Neo confirms, and an expedited one is dispatched carrying NO rating while the re-assessment runs alongside. Neo's verdict decides the RATING; the user's --expedite decides the release. Don't cut one by hand.

## `jarvis config wiring [project]`

which of the USER'S OWN MCP servers, skills and plugins reach this project's workers. Everything is wired by default, including anything they install later; a project DESELECTS what it does not want, on /config. Their Claude configuration is read to fill the list and NEVER written — a deselection only changes the settings file a dispatch spawns a worker with, so it reaches work orders and feature orders and never a session they opened themselves.

## `jarvis doctor [project] [--repair]`

check the OS's own post-conditions; read-only unless --repair. The daemon runs the same checks every reconcile tick.

## `jarvis ui`

dashboard at http://127.0.0.1:8787
