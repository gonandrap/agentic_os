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

a bug in the OS itself -> GitHub issue on the (PUBLIC) tracker + Telegram ping. Every agent has the report-jarvis-bug skill. FILING IS ALL IT DOES BY DEFAULT: whatever the --priority, the issue waits on the tracker for `jarvis issues start`. Neo re-assesses a critical/blocker CLAIM and settles the `priority:` label; it dispatches nothing and no release is cut on landing — releases go out via the user's shipit. The OS creates orders nobody typed only with `os.autonomy.self_dispatch: true` (ships off). --expedite is the USER's: from an interactive session it files AND dispatches a work order at once; from a work-order session (an agent) it is ignored with a note. A picked-up issue is labelled `in progress` and closes ITSELF once the code lands, so never close one by hand.

## `jarvis config wiring [project]`

which of the USER'S OWN MCP servers, skills and plugins reach this project's workers. Everything is wired by default, including anything they install later; a project DESELECTS what it does not want, on /config. Their Claude configuration is read to fill the list and NEVER written — a deselection only changes the settings file a dispatch spawns a worker with, so it reaches work orders and feature orders and never a session they opened themselves.

## `jarvis doctor [project] [--repair]`

check the OS's own post-conditions; read-only unless --repair. The daemon runs the same checks every reconcile tick.

## `jarvis morpheus status / show <mv-id> / waive <mv-id> --reason "…" / requeue <mv-id>`

Morpheus validates every PR that reaches production, in every project.

* **What queues a PR.** For the OS itself that is a release. For any other project it
  is a merge.
* **How a check gets written.** A subagent turns the PR's `## How to validate in
  production` hints into a read-only check.
* **How the check runs.** The daemon runs it with no model call: exit 0 = validated,
  1 = failed, anything else = not yet.
* **Failed and timed out.** Morpheus never diagnoses. `failed` is handed to an
  investigation (or to Neo), and `timedout` (3 days by default) to Neo.
* **The two commands.** `waive` is the user ruling a PR fine. `requeue` watches it again
  with a fresh deadline.

Dashboard: `/morpheus`. Spec:
docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md.

## `jarvis ui`

dashboard at http://127.0.0.1:8787
