---
type: Jarvis Operator Reference
title: Orders
description: Work, feature, improvement and investigation orders: creating, ordering, unblocking, retrying and closing them.
tags: [operator, cli]
---
# Orders

Moved verbatim from the CLAUDE.md crib sheet. `jarvis <command> --help` is the
authoritative flag list; this is the WHY behind each command.

## `jarvis start --catalog <path-to-catalog>`

boot the OS (user catalogs live untracked under catalogs/)

## `jarvis stop`

(no further notes)

## `jarvis wo create <project> "title" -d "details" [--model m]`

(no further notes)

## `jarvis wo create ... --depends-on <wo-id,...>`

don't dispatch until those COMPLETE. Order a multi-step job in one go instead of watching the first piece and typing the second. A blocked order stays `pending` and says what it waits for; it never becomes an attention item just for waiting. Same project only. `waiting_pr_merge` does NOT count as done — the dependency's code is still on an unmerged branch — but the daemon completes it within ~2min of the merge, so an edge costs the user no extra step.

## `jarvis wo unblock <id> [--all]`

cut the edges holding one back. By default only the ones that can never clear (the dependency was cancelled, failed or deleted); those DO raise attention, because the order would otherwise wait for ever. --all cuts live edges too: it runs now, without the work it was told to build on.

## `jarvis fo create <project> "title" -d "..."`

a FEATURE order: one ask too big for a single session. The project plans it into work orders ITSELF — a planner agent reads the codebase, decomposes it, and the plan comes back for review before any work order exists. -d is REQUIRED: the planner sees only that text. Use this instead of typing six `wo create` calls, and instead of deciding the split in chat.

## `jarvis fo list [project] / show <id>`

show renders the plan + the child tree

## `jarvis fo approve <id> [--reject] [--feedback "why"]`

only when Neo escalated: a plan it can decide never reaches the user. It escalates a plan at or over 8 children, one whose children need a gated action, or one it cannot square with a learning. --reject sends the planner back to revise in its existing session, so the feedback must say what to CHANGE.

## `jarvis fo cancel <id>`

stops the planner and every child running

## `jarvis fo agent <id>`

rebuild this feature's AGENT TYPE from its spec. Every feature order builds one from its spec's "Agent profile" appendix, every child work order runs as it, and it is deleted when the feature settles. The spec outlives it, so this hands the persona back — to a repaired live feature, or to a session the user opens by hand afterwards.

## `jarvis fo resume <id> [--fix "what still needs doing"]`

a FAILED feature back to work. A feature fails when a child does, and `failed` is settled — nothing re-derives it, so a child that later recovers used to leave the feature dead for ever. The OS now reopens that case itself within a tick. Reach for this when the child is GENUINELY dead: it marks each dead child superseded (it stops holding the feature back, and stays on the list saying so) and files --fix as a new child. Omit --fix when the work is already done. There is a text box on the feature's dashboard page that does the same thing.

## `jarvis io create <project> "title" -d "<what you saw>" --ref <id|`

n|url> ... an IMPROVEMENT order ("io" = improvement order): when the OS MISBEHAVES and you want the CAUSE, not a patch. An analyst reads the evidence, is required to argue AGAINST the obvious fix, and reports findings — root cause, why the cheap fix is not enough, and the orders that would fix it. It never writes code and never opens a pull request. -d and at least one --ref are REQUIRED: an observation with no evidence is a request for an opinion. A --ref is a wo-/fo-/io-/al- id, #<issue>, a URL or free text, stored verbatim and resolved by the analyst; one that does not resolve is a FINDING about the OS's records, not an error.

## `jarvis io list [project] [--all] / show <id>`

show leads with the counts — `3 findings: 1 accepted, 1 rejected, 1 with Neo` — then the observation and each finding, with Neo's reading and the steward's decision when either touched it

## `jarvis io review <id> [--accept <key>] [--reject <key>] [--feedback "why"] [--accept-all]`

only for what Neo handed back. NEO DECIDES EACH FINDING FIRST, one atomic question per finding: it rejects the ones it cannot defend, rates the rest on the steward's severity scale, and escalates the high-stakes ones. An accepted finding's orders then go to the STEWARD, which files them as OS work under its budget, holds them in its queue until there is room, or drops a minor one. You are asked only when Neo escalates, when Neo could not be reached, or when filing needs your yes (`os.autonomy.self_dispatch` off, or the steward's critical ceiling is spent). `--accept-all` takes everything left for you. ACCEPT writes the root cause to the knowledge base and FILES the proposed orders as your own work (never steward-gated), as ordinary independent orders with a back-link. REJECT needs --feedback: the reason teaches Neo. Neo's own decisions never do. `os.neo.review_findings: false` leaves every finding to you, as before.

## `jarvis io ask-neo <id>`

put the findings awaiting you back to Neo, by hand: a report Neo was never asked about, a finding Neo escalated, or one it could not be reached for. The daemon already offers never-asked reports by itself; this is for the rest and for "do it now". It skips findings Neo ACCEPTED: those wait only on your yes to the spend, and asking again would not change that. Works while the fleet is paused, because you typed it.

## `jarvis io cancel <id>`

stops the order and its analyst. Orders already filed from accepted findings stay: those were your decision.

## `jarvis investigate <wo-id|fo-id|io-id> --why "what you saw"`

an INVESTIGATION order: one order is not moving and you want the cause. A read-only session reads the record and NEVER writes code — a hook refuses every write and every mutating command. It SETTLES ITSELF, unlike `io`, with one of five verdicts: GAP (files an issue after a duplicate check, nothing dispatched), WAITING_ON_USER (the only one that asks for you), TRANSIENT, ALREADY_TRACKED, SCRIPT_WRONG (Morpheus-opened only: the item goes back for a new script, once; past that cap it asks you). One live per subject.

## `jarvis investigate list / show <inv-id> / cancel <inv-id>`

(no further notes)

## `jarvis backlog promote <id> --as feature`

intake -> feature order, not a work order

## `jarvis wo list [project] / show <id> / send <id> "msg" / cancel <id>`

(no further notes)

## `jarvis wo ack <id> / --all`

"seen it" — puts the attention flag down for good (the reconciler re-derives attention every tick, so nothing else makes it stick). Refuses on pending assumptions: those want `jarvis wo review`, not a dismissal.

## `jarvis wo done <id>`

the user closing it: the work is finished. Stops the worker if one is still running. Refuses on pending assumptions (same rule as ack) — closing would accept them silently. Rarely needed for a `waiting_pr_merge` work order now: the worker finished behind a PR (`jarvis wo finish --pr <url>`), it sits on the open list with the link and WITHOUT an attention flag, and the daemon closes it itself once GitHub says the PR merged. Use `wo done` when the PR will never merge, or when `gh` can't reach it (the OS says so once, in the inbox). A PR CLOSED unmerged goes to `needs_review` and asks for you: the work was delivered and refused. NEVER tell a parked order to go and resolve its merge conflicts — the same poll sees CONFLICTING and asks the worker itself, up to three times. It only reaches you if all three failed, and then the attention line says so and the timeline shows what was tried.

## `jarvis wo hide <id> / unhide <id>`

declutter: keeps the record, drops it from listings, the summary and the attention list

## `jarvis wo delete <id> --yes`

irreversible: erases the WO and its whole history (timeline, messages, assumptions)

## `jarvis wo resume-auto <id>`

what is this work order ACTUALLY waiting on? Says so — a Neo question, a gate, a queued message, a turn in flight, or nothing — and nudges the worker only when a permission prompt is the last explanation left. In a fleet running `auto` (the default) nothing can prompt, so the nudge it used to send unconditionally only interrupted workers that were waiting correctly and re-sent their whole conversation. `--force` sends it anyway.

## `jarvis wo retry <id> [--message "…"]`

RELAUNCH A FAILED ORDER in its own session, from where its worker died — the named form of the revive `wo send` has always done as a side effect. `--message` is optional: empty sends the OS's own relaunch note, which asks the worker where it got to rather than telling it. `failed` ONLY — a delivered order has `validation force` and `wo review`, a live one has a worker to talk to, and one that failed before ever opening a conversation is refused: there is no session to resume. It writes no status, so the order stays `failed` — and stays flagged — until jarvisd launches the turn. Pending assumptions do NOT block it and are NOT buried: they stay on `jarvis wo review`. NOT AUTOMATIC: a turn that died with no result is never replayed by the OS, only by the user. Same control on the work order's dashboard page.

## `jarvis wo inject <session-id>`

hand the user's OWN Claude session to Jarvis. Jarvis never adopts a session it finds: one the user started is theirs. Injecting only creates the record — nothing is written into the session until a `wo send`/`resume-auto`.

## `jarvis backlog list / add <project> "title" [--depends-on id] / promote <id> [--force]`

(no further notes)

## `jarvis adopt <path>`

migrate a project into the OS
