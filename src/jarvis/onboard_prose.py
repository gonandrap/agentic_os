"""What each automatic behaviour does and costs, in words, plus the postures behind it (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §4)"""

from __future__ import annotations

from typing import Any

from .onboard_knobs import AREA_SCOPE, Area

__all__ = ["AREAS", "ENABLE", "EXAMPLES", "LINES", "POSTURES", "S"]

POSTURES = ("off", "cautious", "balanced", "autonomous")

_SPECS = "docs/superpowers/specs/"


def S(name: str, section: str) -> tuple[str, str]:
    """A ref into `docs/superpowers/specs/`, repo-relative"""
    return (f"{_SPECS}{name}.md", section)


# Verbatim from `ui/app.py` WIRING_GROUPS / WIRING_EVIDENCE, test-pinned: a leaf may not import the UI
_WIRING_MCP = ("Every server your Claude configuration has. Deselecting one keeps it out of "
               "the sessions this scope dispatches — your own Claude configuration is not "
               "touched.")
_WIRING_PLUGIN = ("An enabled plugin reaches a worker whole: its MCP servers, its skills and "
                  "its agents. Deselecting it takes all three.")
_WIRING_EVIDENCE = ("Measured: this block precedes a conversation-sized cache write 83.7% of "
                    "the time — the worst of any cohort measured. Nothing cleared any other "
                    "server.")


def _area(key: str, title: str, summary: str, on_costs: str, off_costs: str,
          refs: tuple[tuple[str, str], ...],
          presets: dict[str, dict[str, Any]] | None = None) -> Area:
    return Area(key=key, title=title, scope=AREA_SCOPE[key], summary=summary,
                on_costs=on_costs, off_costs=off_costs, refs=refs, presets=presets or {})


AREAS: tuple[Area, ...] = (
    _area(
        "evolution", "Self-evolution: shadow or enforce",
        "The steward judges every piece of work the OS wants to file for itself and records "
        "a decision for each. In shadow mode it only records; in enforce mode its decision "
        "is what actually dispatches.",
        "Enforce lets the steward's ledger and per-severity rules gate real dispatches, so "
        "a wrong rule now changes what work gets done rather than only what gets written "
        "down.",
        "Shadow leaves dispatch exactly as it is today and you only get the record, so you "
        "learn whether the steward would have been right before it is allowed to act.",
        (S("2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself", "3.3"),
         S("2026-09-27-self-evolution", "9")),
    ),
    _area(
        "validation", "The validation panel and what it may do without you",
        "When a work order or feature order says it is done, an independent panel of model "
        "seats judges the claim, sends it back with blockers, and files non-blocking "
        "findings as backlog follow-ups.",
        "Every finished order pays for a panel round before it reaches you, and with "
        "auto_merge or auto_review on the OS merges pull requests or decides assumptions "
        "itself once the panel passes.",
        "Nothing independent checks a claim of done: you read every pull request and every "
        "pending assumption yourself, and a broken change reaches review looking finished.",
        (S("2026-09-15-the-panel-blocks-on-blockers", "4.5"),
         S("2026-09-14-validated-auto-merge-design", "4"),
         S("2026-09-15-neo-decides-an-assumption", "2")),
        {
            "off": {"validation.enabled": False},
            "cautious": {"validation.enabled": True, "validation.auto_merge": False,
                         "validation.auto_review": False},
            "balanced": {"validation.enabled": True, "validation.auto_merge": False,
                         "validation.auto_review": True},
            "autonomous": {"validation.enabled": True, "validation.auto_merge": True,
                           "validation.auto_review": True},
        },
    ),
    _area(
        "neo", "Neo answers questions as you",
        "Neo answers a worker's question, a gate request or an assumption on your behalf, "
        "from your recorded rulings, and escalates to you only what it cannot settle.",
        "Each question costs a Neo model call, and a wrong answer in your name reaches the "
        "worker without you seeing it first; the panel option multiplies the calls per "
        "question.",
        "Every worker question waits in your inbox until you answer it, so a worker that "
        "asks stops moving for as long as you are away from the terminal.",
        (S("2026-08-02-neo-team-design", "The fast path"),
         S("2026-08-02-neo-team-design", "Why a team, and not simply a better prompt")),
        {
            "off": {"neo.enabled": False},
            "cautious": {"neo.enabled": True, "neo.panel.enabled": True},
            "balanced": {"neo.enabled": True, "neo.panel.enabled": False},
        },
    ),
    _area(
        "rules", "The self-evolution rule registry",
        "Detector rules registered for known gap classes are evaluated on the daemon's tick, "
        "and a rule that fires raises an alarm that can take the existing gate.",
        "Every tick pays for evaluating the registry, and an armed rule that misfires "
        "raises alarms until you mark enough false positives to send it back to dry run.",
        "A gap the OS has already diagnosed once is not watched for again, so the same "
        "class of failure has to be noticed by you or by a model every time it recurs.",
        (S("2026-09-27-self-evolution", "5.2"), S("2026-09-27-self-evolution", "3.3")),
        {
            "off": {"rules.enabled": False},
            "balanced": {"rules.enabled": True},
        },
    ),
    _area(
        "supervisor", "The supervisor: cost alarms and the health sweep",
        "The supervisor is a model that reviews each cost alarm and either acknowledges it "
        "or hands it to Neo; its health sweep looks at units that have stopped changing.",
        "Each alarm reviewed and each unit swept is a model call on the supervisor's model, "
        "a standing cost of watching that is paid whether or not anything is wrong.",
        "Cost alarms land in front of you unjudged and nothing looks at a unit that has "
        "quietly stopped moving, so you are the only reviewer of runaway spend.",
        (S("2026-08-31-the-supervisor", "It ships OFF"),
         S("2026-09-02-supervisor-health-and-healing",
           "Two boundaries this feature must not blur")),
        {
            "off": {"supervisor.enabled": False, "supervisor.health_enabled": False},
            "cautious": {"supervisor.enabled": True, "supervisor.health_enabled": False},
            "balanced": {"supervisor.enabled": True, "supervisor.health_enabled": True},
        },
    ),
    _area(
        "investigation", "Investigation orders: the diagnostician",
        "An investigation order runs a read-only investigator on a stuck or failed order, "
        "which writes nothing and returns a verdict that ops settles and files.",
        "Every investigation is a separate worker session billed against its own family "
        "budget, cheap by default on a smaller model and low effort because diagnosis is "
        "reading.",
        "A stuck order stays stuck until you read its record yourself and work out why, "
        "which is the loop a human used to run by hand for days.",
        (S("2026-09-27-investigation-orders", "2.8"),
         S("2026-09-27-investigation-orders", "2.4")),
    ),
    _area(
        "morpheus", "Morpheus validates shipped changes in production",
        "After a pull request reaches production, Morpheus writes a read-only check for it "
        "and the daemon runs that check until the change validates, fails or times out.",
        "Morpheus's session spends tokens writing each check, and a failure files an "
        "investigation or improvement order through the steward, which is spend nobody "
        "typed.",
        "Nothing confirms a merged change actually works where it runs, so a regression in "
        "production is found by whoever trips over it next.",
        (S("2026-10-21-morpheus-validates-releases-in-production",
           "Failed and timed out hand off"),
         S("2026-10-21-morpheus-validates-releases-in-production",
           "Three halves, and only one of them spends tokens")),
        {
            "off": {"morpheus.enabled": False},
            "cautious": {"morpheus.enabled": True, "morpheus.on_failed": "neo",
                         "morpheus.on_timeout": "neo"},
            "balanced": {"morpheus.enabled": True, "morpheus.on_failed": "investigate",
                         "morpheus.on_timeout": "neo"},
        },
    ),
    _area(
        "steward", "What the OS may spend on itself",
        "The master switch for work nobody typed: bug pickups, riders, red-main fixes and "
        "releases. The steward decides each candidate inside a weekly ledger and a "
        "per-order ceiling.",
        "The OS files and dispatches its own orders, inside os.evolution.weekly_usd per "
        "rolling week, so you wake up to work done overnight and to its bill.",
        "Everything the OS notices waits as an issue or an inbox item until you start it, "
        "so nothing is spent on the OS itself without you typing the command.",
        (S("2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself", "3.2"),
         S("2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself", "3.3")),
        {
            "off": {"autonomy.self_dispatch": False},
            "autonomous": {"autonomy.self_dispatch": True},
        },
    ),
    _area(
        "gates", "Privileged-action gates",
        "Merging a pull request, cutting a release and restarting services are attempted "
        "by a worker under review rather than forbidden; Neo or you decide each request.",
        "A gated worker can ship code once a reviewer approves, so an approval in your name "
        "is the last check between a worker's change and production.",
        "A worker cannot merge, release or restart at all, so every one of those steps "
        "waits for you to run it by hand after the work is finished.",
        (S("2026-09-12-a-gate-that-holds", "The rule"),
         S("2026-08-27-the-config-console", "7")),
    ),
    _area(
        "budgets", "Concurrency and dollar ceilings",
        "How many work orders run at once per project and across the account, and whether "
        "each new order or feature family carries a standing dollar ceiling.",
        "A ceiling stops an order when it is reached, so a big task can stop half done "
        "and wait for you to raise its budget before it finishes.",
        "No ceiling is the shipped default: nothing stops a runaway order except the "
        "alarms, the brake (`jarvis pause`) and your own attention.",
        (S("2026-09-18-a-budget-per-order", "6"), S("2026-09-18-a-budget-per-order", "3")),
    ),
    _area(
        "compaction", "Prompt cache lifetime and compaction",
        "Which cache lifetime a worker turn's writes are billed at, and when the OS "
        "compacts a conversation past an expired cache instead of re-sending it whole.",
        "The one-hour cache costs more per write but survives a long pause, so it pays only "
        "when workers really do sit idle between five minutes and an hour.",
        "The five-minute cache is cheaper per write, but every pause longer than that "
        "re-sends the whole conversation, which the compaction floor exists to bound.",
        (S("2026-09-18-compact-past-the-ttl", "3"),
         S("2026-10-15-the-hour-on-worker-turns-only", "1. The setting")),
    ),
    _area(
        "model", "Model, effort and permission mode",
        "Which model a worker runs on, at what effort, in which permission mode, and over "
        "which transport, fleet-wide with a per-project override.",
        "A larger model or higher effort costs more per turn on every order, and a "
        "permission mode that asks prompts stalls a background worker until you answer.",
        "These knobs are never off: leaving them alone keeps the shipped model and the "
        "auto permission mode, in which a background worker never waits on a prompt.",
        (S("2026-10-22-workers-on-background-sessions", "The switch"),
         S("2026-08-27-the-config-console", "4.2")),
    ),
    _area(
        "fleet_health", "Stuck-order sweep",
        "The daemon sweeps every open order and, when one has sat in a status past its "
        "threshold, opens an investigation into why it stopped moving.",
        "Every investigation the sweep opens is spend nobody typed, rate-limited per "
        "subject and per day, and it can investigate an order that was only slow.",
        "An order that stops moving stays where it is until you notice it on the dashboard "
        "or in `jarvis status`, however long that takes.",
        (S("2026-09-30-an-order-that-stops-moving-gets-investigated", "6"),
         S("2026-09-30-an-order-that-stops-moving-gets-investigated", "4")),
        {
            "off": {"fleet_health.enabled": False},
            "autonomous": {"fleet_health.enabled": True},
        },
    ),
    _area(
        "ui_health", "Dashboard self-heal",
        "The dashboard probes its own liveness, dumps its thread stacks when it wedges, and "
        "the daemon restarts it within a cooldown and a daily cap.",
        "A probe runs in the dashboard process on a short interval, and a restart drops any "
        "page you had open mid-request while the process comes back.",
        "A wedged dashboard stays wedged and silent until you restart it yourself, and no "
        "stack dump is left behind to say why it hung.",
        (S("2026-10-08-the-dashboard-reports-and-heals-its-own-wedge",
           "4. Detection and self-heal in the daemon"),),
        {
            "off": {"ui_health.enabled": False},
            "balanced": {"ui_health.enabled": True},
        },
    ),
    _area(
        "schedule", "Recurring work orders",
        "The scheduler files work orders nobody typed on a fixed cadence, starting with a "
        "daily doctor run, and surfaces a job it had to hold.",
        "Each scheduled job is a work order with its own spend, filed on the interval "
        "whether or not anything changed since the last one.",
        "Nothing runs on a cadence: a recurring check happens only when you remember to ask "
        "for it.",
        (S("2026-09-14-the-scheduler", "1"), S("2026-09-14-the-scheduler", "6")),
        {
            "off": {"schedule.enabled": False},
            "autonomous": {"schedule.enabled": True},
        },
    ),
    _area(
        "navigation", "How workers read code and specs",
        "Whether workers are steered or refused toward symbol tools and section reads "
        "instead of dumping whole source files and specs into their context.",
        "A refusing hook costs a worker a retry each time it reaches for the shell first, "
        "and a misclassified path can block a read that was legitimate.",
        "Workers read whole files freely, which is simpler for them and fills the context "
        "window, and therefore the cache bill, with text they did not need.",
        (S("2026-10-02-serena-the-cheap-path", "6"),
         S("2026-10-06-navigate-specs-like-code", "5"),
         S("2026-10-01-the-steer-that-beat-the-brief",
           "1. `worker.bash_first`: a catalog key, fleet-wide with per-project override")),
        {
            "off": {"worker.py_nav_hook": "off", "worker.doc_nav_hook": "off"},
            "balanced": {"worker.py_nav_hook": "on", "worker.doc_nav_hook": "on"},
        },
    ),
    _area(
        "concision", "House style caps",
        "How terse the OS holds its workers: the word cap on a finish summary and the "
        "character caps on messages, assumptions and Neo questions.",
        "A worker whose summary or question runs over the cap is refused and must rewrite "
        "it, which costs a retry but keeps the record short enough to read.",
        "Setting a cap to zero switches its check off, and the records you and Neo decide "
        "from grow as long as each worker cares to make them.",
        (S("2026-09-19-concision-enforced", "The cap"),
         S("2026-10-21-questions-to-neo-are-atomic", "1")),
    ),
    _area(
        "cost", "Cost reporting windows",
        "Where `jarvis cost --fleet` starts its week, which percentile it reports, and the "
        "usage meter's calibration and alarm shares.",
        "These settings change only what the cost report shows and when its meter raises "
        "an alarm, never what any worker is allowed to spend.",
        "There is no off: leaving them alone keeps the shipped week boundary and the "
        "measured dollars-per-point calibration the report uses.",
        (S("2026-10-06-fleet-cost-distribution", "6"),),
    ),
    _area(
        "inspect", "Live turn alarms",
        "`jarvis inspect` reports where a turn's time and cache writes went, and the OS "
        "raises a turn that is still running past its alarm thresholds.",
        "An alarm can interrupt you about a turn that was long but healthy, and each alarm "
        "is something you or the supervisor then has to look at.",
        "A turn burning money right now is not raised while it runs, and you find out what "
        "it cost only after it finishes.",
        (S("2026-08-30-the-anatomy-of-a-turn", "6.3"),),
        {
            "off": {"inspect.enabled": False},
            "balanced": {"inspect.enabled": True},
        },
    ),
    _area(
        "observability", "What debug data is collected",
        "Whether each order keeps its per-turn context ledger and a sealed autopsy, and "
        "whether that autopsy also retains every tool call's parameters.",
        "Full retains tool parameters in every sealed autopsy, which takes disk and may "
        "keep text from the worker's tool calls you would rather not store.",
        "Off stops the ledger and the sealed autopsy, so `jarvis wo context` says nothing "
        "was recorded; `jarvis watch` and `jarvis inspect` still work.",
        (S("2026-09-24-order-observability", "10"),),
        {
            "off": {"observability.level": "off"},
            "balanced": {"observability.level": "normal"},
            "cautious": {"observability.level": "full"},
        },
    ),
    _area(
        "messaging", "Messages to running workers",
        "How long a message queued for a worker may stay undelivered before the OS treats "
        "it as a defect rather than as a message still in flight.",
        "A shorter threshold flags a slow delivery sooner, and may raise one for a worker "
        "that was only between turns when the message arrived.",
        "A longer threshold keeps quiet about a lost message for longer, so feedback you "
        "sent can sit unread by its worker without anyone saying so.",
        (S("2026-10-22-messages-delivered-mid-turn", "Design"),),
    ),
    _area(
        "delivery", "Bugs, pull requests and releases",
        "How the OS labels a tracker issue it picked up, when a silent pull request becomes "
        "a stall, and how long a dropped release batch stays worth re-filing.",
        "The label is written to your tracker on every pickup, so anyone reading the issue "
        "sees the OS has claimed it while its order runs.",
        "These are never off: leaving them alone keeps the shipped label and windows, which "
        "only change how the OS reports its own delivery.",
        (S("2026-09-14-a-filed-bug-runs-itself", "4"),
         S("2026-09-14-a-filed-bug-runs-itself", "2")),
    ),
    _area(
        "knowledge", "What each worker is told from the knowledge base",
        "How many pinned entries a worker sees verbatim, and how many index lines and "
        "title-matched hints point it at the rest it can fetch on demand.",
        "Every pinned character and index line rides in every worker's prompt, so a larger "
        "budget is paid on every order whether the entries are used or not.",
        "A smaller budget leaves more of the knowledge base behind the retrieval verbs, so "
        "a worker that does not think to search repeats a mistake already written down.",
        (S("2026-07-27-knowledge-on-demand-design", "2"),
         S("2026-07-27-knowledge-on-demand-design", "3")),
    ),
    _area(
        "wiring", "Which of your MCP servers, skills and plugins reach workers",
        "MCP servers: " + _WIRING_MCP + " Plugins: " + _WIRING_PLUGIN,
        "Every wired server, skill and plugin rides in every worker's start-up context. "
        "On claude.ai connectors: " + _WIRING_EVIDENCE,
        "A server or plugin left out is not there when a worker needs it, and the worker "
        "falls back to slower tools or asks for what it cannot reach.",
        (S("2026-09-16-per-project-wiring", "2"), S("2026-09-16-per-project-wiring", "3")),
        {
            "cautious": {"wiring.claude_ai_connectors": False, "wiring.bundled_skills": False},
            "balanced": {"wiring.claude_ai_connectors": True, "wiring.bundled_skills": False},
        },
    ),
    _area(
        "worktree", "Worker checkouts",
        "Each worker runs in its own git worktree, and the daemon reclaims a settled "
        "order's checkout after a delay so disk does not fill.",
        "Reclaiming sooner frees disk sooner, and a settled order you wanted to look inside "
        "has had its checkout removed by the time you get to it.",
        "Never reclaiming keeps every checkout on disk for ever, until free space runs low "
        "and the daemon raises its disk warning.",
        (S("2026-10-22-a-settled-checkout-is-reclaimed", "1. The reclaim sweep"),),
    ),
    _area(
        "notifications", "Where the OS tells you things",
        "Which sinks notifications go to, the log by default or Telegram, and where the "
        "dashboard listens so deep links in a notification open the right page.",
        "A Telegram sink reaches your phone, so an escalation or a finished order "
        "interrupts you wherever you are rather than waiting for you at the terminal.",
        "With only the log sink nothing is pushed anywhere, and you learn what happened "
        "when you next run `jarvis status` or open the dashboard.",
        (S("2026-07-03-jarvis-os-design", "3.6"),),
    ),
    _area(
        "environment", "The worker's environment",
        "What a worker's session is allowed and given: denied tools, an MCP call timeout, "
        "an appended system prompt, and whether the lead must delegate edits to its crew.",
        "Requiring the crew refuses the lead's own file edits, so every code change goes "
        "through a subagent that re-reads the code and costs a second context.",
        "Without the requirement the lead edits files itself, which is cheaper per change "
        "and skips the separation between who plans and who writes.",
        (S("2026-09-23-the-crew-a-worker-must-use", "7"),
         S("2026-10-22-workers-on-background-sessions", "The lead does the work")),
        {
            "off": {"worker.require_crew": False},
            "cautious": {"worker.require_crew": True},
        },
    ),
)

# The knob an area's on/off question is about; areas with no asked knob have none
ENABLE: dict[str, str] = {
    "evolution": "evolution.mode",
    "validation": "validation.enabled",
    "neo": "neo.enabled",
    "rules": "rules.enabled",
    "supervisor": "supervisor.enabled",
    "investigation": "worker.investigation_budget_usd",
    "morpheus": "morpheus.enabled",
    "steward": "autonomy.self_dispatch",
    "budgets": "worker.budget_usd",
    "compaction": "worker.cache_ttl",
    "model": "worker.permission_mode",
    "fleet_health": "fleet_health.enabled",
    "ui_health": "ui_health.enabled",
    "schedule": "schedule.enabled",
    "navigation": "navigation.enabled",
    "inspect": "inspect.enabled",
    "observability": "observability.level",
    "delivery": "bugs.label",
    "wiring": "wiring.claude_ai_connectors",
    "environment": "worker.require_crew",
}

LINES: dict[str, str] = {
    "evolution.mode": "Whether the steward's decisions only get recorded (shadow) or actually "
                      "decide what dispatches (enforce). Read fleet-wide only.",
    "validation.enabled": "Whether an independent panel judges every claim of done before it "
                          "reaches you.",
    "validation.auto_merge": "Whether the OS may merge this project's pull requests itself "
                             "once the panel passes them.",
    "validation.auto_review": "Whether the OS may decide this project's pending assumptions "
                              "itself instead of leaving them for you.",
    "validation.chair_model": "Which model the panel's chair runs on; empty means the shipped "
                              "choice.",
    "validation.feature_units": "Whether a whole feature order is validated once all its "
                                "children are done.",
    "validation.follow_ups": "Whether a non-blocking finding is kept as a backlog item or "
                             "thrown away.",
    "validation.stakes_classifier": "Which net decides that an assumption is high-stakes and "
                                    "must reach you, before any model call.",
    "neo.digest_model": "Which model shortens an over-long question for the dashboard.",
    "neo.enabled": "Whether Neo answers questions in your name or every one waits for you.",
    "neo.model": "Which model Neo answers on.",
    "neo.panel.chair_model": "Which model chairs Neo's panel; empty means the shipped choice.",
    "neo.panel.enabled": "Whether Neo answers as a panel of profiled seats instead of one "
                         "agent.",
    "neo.panel.fast_path": "Whether a low-stakes decision the premise seat routes fast skips "
                           "the full panel.",
    "rules.enabled": "Whether the self-evolution detector registry is evaluated at all.",
    "rules.false_positive_disarm": "How many false positives you mark on an armed detector "
                                   "before it drops back to dry run.",
    "supervisor.enabled": "Whether a model reviews each cost alarm before it reaches you.",
    "supervisor.health_enabled": "Whether the health sweep looks at units that stopped "
                                 "changing.",
    "supervisor.model": "Which model the supervisor reviews on.",
    "supervisor.remedies.enabled": "Whether the supervisor may propose a remedy, from its "
                                   "allowed list, for a unit it judged ill.",
    "neo.review_findings": "Whether Neo decides each improvement-order finding for you.",
    "worker.investigation_budget_usd": "The dollar ceiling on one investigation, the order "
                                       "plus its investigator.",
    "worker.investigation_effort": "The effort an investigator runs at.",
    "worker.investigation_model": "The model an investigator runs on.",
    "morpheus.enabled": "Whether shipped changes are checked in production at all.",
    "morpheus.model": "Which model writes Morpheus's checks.",
    "morpheus.on_failed": "What a failed production check hands off to: an investigation, or "
                          "Neo.",
    "morpheus.on_timeout": "What a check that never decided hands off to: an investigation, "
                           "or Neo.",
    "autonomy.self_dispatch": "Whether the OS may create and dispatch orders nobody typed.",
    "evolution.critical_weekly_usd": "The part of the weekly ledger expedited "
                                     "critical/blocker work may use.",
    "evolution.max_depth": "How many generations of OS work may dispatch from one "
                           "user-originated parent.",
    "evolution.order_budget_usd": "The dollar ceiling the steward sets on each order it "
                                  "dispatches.",
    "evolution.weekly_usd": "What the OS may spend on itself over a rolling week, at list "
                            "price.",
    "evolution.yield_above_usd": "Total fleet spend over the week above which nothing new "
                                 "from the steward dispatches.",
    "defaults.max_in_flight": "How many worker turns may run at once across the whole "
                              "account.",
    "max_concurrent": "How many work orders may run at once in one project.",
    "worker.budget_usd": "A standing dollar ceiling on every new order; null means none.",
    "worker.feature_budget_usd": "A standing dollar ceiling on every feature family; null "
                                 "means none.",
    "worker.cache_ttl": "Which prompt-cache lifetime a worker turn's writes are billed at.",
    "model": "The model every worker runs on unless overridden.",
    "worker.effort": "The effort level a worker runs at; null means the model's own default.",
    "worker.model": "The model this project's workers run on.",
    "worker.permission_mode": "Which Claude Code permission mode a worker runs in.",
    "worker.transport": "Whether a worker turn runs on a background session or a headless "
                        "call.",
    "fleet_health.enabled": "Whether an order that stopped moving gets investigated "
                            "automatically.",
    "ui_health.enabled": "Whether the dashboard reports its own wedge and gets restarted.",
    "schedule.enabled": "Whether recurring work orders are filed on a cadence.",
    "navigation.enabled": "Whether `jarvis navigation` measures how workers read code.",
    "worker.bash_first": "Whether auto mode's bash-first steer reaches a worker.",
    "worker.doc_nav_hook": "Whether a whole-file read of a spec is refused with the section "
                           "command named.",
    "worker.py_nav_hook": "Whether a shell read of a .py file is refused with the symbol tool "
                          "named.",
    "worker.tool_search": "Whether a worker's MCP tools are deferred behind ToolSearch or "
                          "listed with full schemas.",
    "inspect.enabled": "Whether the OS raises a turn still running past its thresholds.",
    "observability.level": "How much debug data each order keeps: off, normal or full.",
    "bugs.label": "The tracker label that says the OS picked an issue up.",
    "wiring.bundled_skills": "Whether the skills Claude Code itself ships reach workers.",
    "wiring.claude_ai_connectors": "Whether your claude.ai connectors reach workers.",
    "worker.require_crew": "Whether the lead must delegate file edits to its crew.",
}

EXAMPLES: dict[str, str] = {
    "evolution.mode": "In shadow, a red-main fix the steward would drop still dispatches as "
                      "today and `jarvis steward decisions` shows the drop; in enforce, it "
                      "is not dispatched. Set it with `jarvis config set os.evolution.mode "
                      "enforce --reason \"…\"`.",
    "validation.enabled": "With it on, an order that says done but left a test red is sent "
                          "back with the blocker before you see it; off, it lands in "
                          "needs_review looking finished.",
    "validation.auto_merge": "On, a pull request the panel passes at the current head is "
                             "merged without you; off, it parks in waiting_pr_merge until "
                             "you merge it.",
    "validation.auto_review": "On, a low-stakes assumption is accepted by the OS and you are "
                              "told afterwards; off, every assumption waits for "
                              "`jarvis wo review`.",
    "validation.chair_model": "Empty keeps the shipped chair; naming a smaller model makes "
                              "each round cheaper and the final verdict weaker.",
    "validation.feature_units": "On, a feature whose children all passed is judged once more "
                                "as a whole; off, the children's passes are the only check.",
    "validation.follow_ups": "On, a seat's non-blocking note becomes a backlog item you can "
                             "promote; off, it is discarded with the round.",
    "validation.stakes_classifier": "With regex-tightened, an assumption that touches money "
                                    "or deletion is held for you by pattern before any "
                                    "model call is spent on it.",
    "neo.digest_model": "With haiku, a 40-line worker question shows on the dashboard as a "
                        "short digest, for the price of one small call.",
    "neo.enabled": "On, a worker asking which of two designs you prefer gets an answer from "
                   "your past rulings in about a minute; off, it waits in `jarvis neo list` "
                   "for you.",
    "neo.model": "With opus, each answer costs an opus call; a smaller model answers "
                 "cheaper and matches your past rulings less reliably.",
    "neo.panel.chair_model": "Empty keeps the shipped chair; a smaller model makes each "
                             "panel answer cheaper and its tie-breaks weaker.",
    "neo.panel.enabled": "On, a gate request is read by several seats and a chair, each a "
                         "call; off, one Neo call decides it.",
    "neo.panel.fast_path": "On, a gate request the premise seat routes fast, such as a "
                           "classifier false positive, is answered by the chair in one call; "
                           "off, every decision seats the whole panel.",
    "rules.enabled": "On, a detector for a gap already fixed once fires the next time that "
                     "gap appears; off, it is never evaluated.",
    "rules.false_positive_disarm": "At 2, the second false positive you mark on an armed "
                                   "detector sends it back to dry run.",
    "supervisor.enabled": "On, a cost alarm on an order looping on one tool call is judged "
                          "and acked or sent to Neo; off, it lands in your inbox unjudged.",
    "supervisor.health_enabled": "On, an order whose fingerprint has not changed for hours "
                                 "is looked at by the sweep; off, nobody looks.",
    "supervisor.model": "With opus, each alarm review is an opus call.",
    "supervisor.remedies.enabled": "On, a unit judged ill may get a proposed remedy from the "
                                   "allowed list; off, the supervisor only reports.",
    "neo.review_findings": "On, an improvement order's findings are accepted or rejected by "
                           "Neo for you; off, each one waits for `jarvis io review`.",
    "worker.investigation_budget_usd": "At $4, an investigation that is still reading after "
                                       "two typical passes stops at its ceiling instead of "
                                       "running on.",
    "worker.investigation_effort": "At low, an investigator quotes the record and names the "
                                   "cause without long deliberation.",
    "worker.investigation_model": "With sonnet, a diagnosis costs a fraction of an opus "
                                  "worker, because it is reading and quoting, not design.",
    "morpheus.enabled": "On, a merged fix gets a read-only production check that runs every "
                        "15 minutes until it passes, fails or times out; off, nothing checks "
                        "it.",
    "morpheus.model": "With sonnet, each check Morpheus writes costs one sonnet subagent.",
    "morpheus.on_failed": "With investigate, two failing checks in a row file an "
                          "investigation on the PR's order through the steward; with neo, "
                          "Neo gets a question and spends no worker.",
    "morpheus.on_timeout": "With neo, a change whose code path production never exercised "
                           "becomes one Neo question when its deadline passes.",
    "autonomy.self_dispatch": "On, a probe failure at 03:00 files an improvement order and "
                              "dispatches it the same night, inside os.evolution.weekly_usd; "
                              "off, the same failure waits in `jarvis inbox` until you read "
                              "it.",
    "evolution.critical_weekly_usd": "At $60, expedited critical work stops dispatching once "
                                     "it has used $60 of the week's ledger.",
    "evolution.max_depth": "At 1, an order the OS dispatched cannot itself cause another "
                           "OS-dispatched order.",
    "evolution.order_budget_usd": "At $25, each order the steward dispatches stops at $25 "
                                  "and waits for you to raise it.",
    "evolution.weekly_usd": "At $150, once the OS has spent $150 on itself in seven days, a "
                            "new candidate is queued instead of dispatched.",
    "evolution.yield_above_usd": "At $900, when the fleet's week passes $900 the steward "
                                 "dispatches nothing new until spend falls back.",
    "defaults.max_in_flight": "At 3, a fourth worker turn waits for one of three running "
                              "turns to end, whichever project it is in.",
    "max_concurrent": "At 5, a sixth order in this project stays pending until one of five "
                      "finishes.",
    "worker.budget_usd": "With null, an order runs until it finishes or you pause it; with "
                         "a number, it stops at that many dollars and waits for "
                         "`jarvis wo budget`.",
    "worker.feature_budget_usd": "With null, a feature's children together have no ceiling; "
                                 "with a number, the whole family stops when it is reached.",
    "worker.cache_ttl": "With 5m, a worker idle ten minutes re-sends its context at the next "
                        "turn; with 1h, that write survived the pause but cost more.",
    "model": "With claude-opus-5, every worker in a project that names no model of its own "
             "runs on it.",
    "worker.effort": "With null, the model's own effort default applies to each turn.",
    "worker.model": "With claude-opus-5, this project's workers run on it unless an order "
                    "names another.",
    "worker.permission_mode": "With auto, a classifier vets each action and a background "
                              "worker never stops for a prompt; with manual, it stalls "
                              "until you answer.",
    "worker.transport": "With background, one long-lived session carries the conversation; "
                        "with headless, each turn is a fresh one-shot call.",
    "fleet_health.enabled": "On, an order sitting in running with no progress past its "
                            "threshold gets an investigation opened; off, it sits until you "
                            "notice.",
    "ui_health.enabled": "On, a dashboard that stops answering is detected, its stacks are "
                         "dumped and it is restarted; off, it stays wedged.",
    "schedule.enabled": "On, a doctor work order is filed every day without anyone typing "
                        "it; off, it runs only when you ask.",
    "navigation.enabled": "On, `jarvis navigation` reports how many reads were symbol calls "
                          "versus shell dumps.",
    "worker.bash_first": "With off, auto mode's steer toward the shell is not added to a "
                         "worker's settings.",
    "worker.doc_nav_hook": "With on, a worker reading a whole spec is refused and told the "
                           "`jarvis spec section` command to use; with off, it reads it.",
    "worker.py_nav_hook": "With on, a `cat` of a .py file is refused and the worker is told "
                          "to use find_symbol; with off, it goes through.",
    "worker.tool_search": "With on, a worker's MCP tools start as names and load their "
                          "schemas only when it searches for them.",
    "inspect.enabled": "On, a turn still running long after it should have ended is raised "
                       "as an alarm while it burns; off, you see it only afterwards.",
    "observability.level": "At normal, every order keeps its context ledger and sealed "
                           "autopsy; at full, the autopsy also keeps tool parameters.",
    "bugs.label": "With \"in progress\", an issue the OS picks up is labelled so on your "
                  "tracker while its order runs.",
    "wiring.bundled_skills": "Off, the skills Claude Code ships stay out of a worker's "
                             "start-up context; on, they ride in every start.",
    "wiring.claude_ai_connectors": "On, your claude.ai connectors reach every worker in "
                                   "scope; off, they stay out of its sessions.",
    "worker.require_crew": "On, a lead that tries to edit a file itself is refused and "
                           "delegates to jarvis-implementer; off, it edits directly.",
}
