---
okf_version: "0.2"
---
# Jarvis OS knowledge map

An [Open Knowledge Format](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
bundle. Read top-down and stop as soon as you know enough: this index, then one
playbook, then only the concepts that playbook names. Concepts point at deeper
sources (Serena memories, spec sections, `jarvis learn` entries) rather than copying
them. Links are bundle-relative (`/` = `docs/os/`).

## Order playbooks — what each kind of order needs to load

- [Release](/playbooks/release.md) — cut and ship a release; needs almost nothing.
- [Bug fix](/playbooks/bugfix.md) — one defect, one area of the code.
- [Feature child](/playbooks/feature.md) — one section of a planned feature's spec.
- [Refactor](/playbooks/refactor.md) — a structural change; needs the architecture.
- [Investigation](/playbooks/investigation.md) — read-only diagnosis of one order or misbehaviour.
- [General change](/playbooks/general.md) — anything the other playbooks do not match.

## Architecture

- [Codebase map](/architecture/codebase-map.md) — modules, layering, the three SQLite DBs.
- [Work-order lifecycle](/architecture/work-order-lifecycle.md) — states, turns, settlement, retries.
- [Feature orders](/architecture/feature-orders.md) — planner, plan review, children, manager; the io/inv/do kinds.
- [Privileged-action gates](/architecture/gates.md) — how a worker merges, releases, restarts.
- [Neo and the panels](/architecture/neo-and-panels.md) — the answerer, its seats, the validation panel.
- [Worker context](/architecture/worker-context.md) — what a worker starts with, and its 10K budget.
- [Knowledge base](/architecture/knowledge-base.md) — `jarvis learn`, pins, the index in prompts.

### Agents and how they interact (mermaid diagrams)

- [Agents of the OS](/architecture/agents/overview.md) — the roster, the topology, who answers to whom.
- [Work order](/architecture/agents/work-order.md) — user, Jarvis, daemon, worker and crew, Neo.
- [Validation rounds](/architecture/agents/validation.md) — panel seats, chair, rejection over the bus, auto-merge.
- [Feature order](/architecture/agents/feature-order.md) — planner and seats, Neo's plan review, children, manager.
- [Gates and assumptions](/architecture/agents/gates-and-assumptions.md) — Neo's question kinds, gates, auto-review.
- [Investigation order](/architecture/agents/investigation.md) — the read-only investigator and its four verdicts.
- [Improvement order](/architecture/agents/improvement.md) — the analyst's findings and the user's review.
- [Supervisor and self-heal](/architecture/agents/supervisor.md) — alarms, health sweep, remedies via `self_heal`.
- [Bugs and releases](/architecture/agents/bugs-and-releases.md) — triage, expedite, release on landing.

## Engineering

- [Testing](/engineering/testing.md) — how to run targeted tests; what the evals cost.
- [Environments and releases](/engineering/environments.md) — dev vs prod checkouts, shipit.
- [Concision](/engineering/concision.md) — house style for comments, PR bodies, messages.

## Operating the OS

- [Orders](/operator/orders.md) — work, feature, improvement, investigation and decision orders in depth.
- [Reviews, gates and Neo](/operator/reviews.md) — assumptions, validation, gates, Neo's ledgers.
- [Spend and time](/operator/spend.md) — pause/resume, budgets, cost, inspect, alarms.
- [Records, bugs and knowledge](/operator/records.md) — search, issues, bug reports, learnings, wiring.
- [Onboarding](/operator/onboarding.md) — the interview that picks a project's settings, applied as one config version.
- [Projects](/operator/projects.md) — adopting, offboarding, archive versus purge, re-adopting.
