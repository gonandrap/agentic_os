**Verdict: GO-WITH-CONDITIONS.** The artifact is a read-only report surface. Questions go in through `jarvis do send`. Answers land on the decision order's record and in a republished report revision. Artifact comment threads stay out of the design (Neo q4234, user ruling kn-49a1861c).

# Artifact decision reports: design note (wo-afc4e74a)

Spec: `docs/superpowers/specs/2026-11-10-artifact-report-revive-spike.md` §3. Evidence:
`docs/findings/2026-11-artifact-revive-probe.md` (wo-d614af29, CLI `2.1.322`), cited as "probe".

## Answers

- **Q1, publish: YES.** A researcher-kind `--bg` session published a private artifact under `auto` with no prompt. Probe: `TOOL_RESULT Published ... at https://claude.ai/artifact/JU754gmR89snAzsXVVZyim (Version 1, version id 1794321981-b64b)`, `PERMISSION none: no tool asked for permission or was refused`.
- **Q2, revive: YES.** A bare `resume_bg_turn` of the removed session kept its id and answered both activated threads. Probe: `same_session_id: true` and an independent read showing `2 comment threads (0 open, 2 activated for Claude, 2 with comments sent to you)`, both `resolved (by Claude)`.
- **Q3, poll: NO. The user must trigger.** Probe: `3 of 3 print sessions: the tool is absent`, and `Not found: any claude.ai-side signal a daemon could watch without an LLM session.`
- **Q4, revive cost: $0.208147, 4 API calls.** Probe: `revive_envelope: {"input": 8, "cache_read": 253176, "cache_write": 16780, "output": 1162, "total_cost_usd": 0.208147, "api_calls": 4, ...}`

## Why the comment threads are out (the condition)

Reply and resolve need an activated thread (Send to Claude). Under remove-then-revive no session holds a watch, so every Send to Claude shows the user `Comment posted, but a conversation with Claude couldn't start here.Open in Claude` (probe, "Park on the user"). The user ruled that this message must be avoided (kn-49a1861c). A plain comment avoids it but cannot be replied to or resolved. A live watching session is ruled out (§1 Fixed). So threads stay out until an activation path is measured that does not show the message. Two such paths are unmeasured and filed as follow-ups, not blockers: an in-thread `@claude` mention, and what "Open in Claude" does (the probe says `What "Open in Claude" does was not measured.`).

What GO keeps: the user reads the report as a page, and talks to the researcher days later through `jarvis do send` (CLI, or the dashboard's existing do-send path in `ui/app.py`). The revive mechanism is the same one Q2 measured. Only the inbound channel differs.

## Q3 threshold (computed before the verdict)

Polling is viable only if `c_poll × artifacts_open × polls_per_day < c_revive × user_revives_per_day`.

- `c_poll` has no valid value. Every `-p` read failed. The failed calls cost $0.00535715 (haiku) and $0.1322352 (opus). For scale, if a haiku read worked: 3 open artifacts × 96 polls/day (every 15 min) × $0.00536 = $1.54/day. That breaks even at about 7 revives/day at $0.208, before counting the revive itself.
- Moot on 2.1.322: a print session has no `ArtifactComments` tool, so a poll cannot see a thread at any price. Polling is out, and the user triggers.

## Shape if GO (touch points, no code)

1. **Researcher brief** (`dispatch._researcher_prompt`): after the report validates, render it as HTML under `.jarvis/scratch/`, publish it as a PRIVATE artifact, and put the URL in the submitted report. The page footer says to ask questions with `jarvis do send <do-id>` and not with Send to Claude.
2. **Where the URL lives: an `artifact_url` field in the decision report** (the JSON stored in the order's `plan`, read by `ops.decision_report`, parsed by `decision_report.parse_report`). This beats an event on the researcher's timeline. The report already travels to `jarvis do show`, search, the daemon brief and the `decision_report` side effect, so the link rides along with no new reader. A timeline event would also be lost when the researcher order is deleted (`send_decision_message`: `it was deleted`). `parse_report` must accept only `https://claude.ai/artifact/<id>`, because this is a trust boundary.
3. **Surfaces:** the `decision_delivered` notification (`project_store.DECISION_DELIVERED_EVENT`, `timeline.py`) and `jarvis do show` (`cli.py`, report render) print the link and name `jarvis do send <do>` as the way to ask.
4. **Revive prompt** (the message that `ops.send_decision_message` hands to `send_message`): answer the question on the record, then republish the same URL with a "Questions and answers" revision. No `ArtifactComments` call.
5. **Dashboard button:** none. Q3 says the user triggers, and the dashboard already sends through `send_decision_message`.

**Composition with fo-dc681c11** (experiment phase, `jarvis do experiment`): its results land as a report revision on the same decision order through `submit_decision_report`. This feature owns the artifact surface. Any accepted report revision, whether an experiment's or a Q&A's, is republished to the stored `artifact_url`. fo-dc681c11 never touches artifacts. Unmeasured risk: that feature's "fresh researcher" path is a new session, not the one that published. The tool contract says a session must read an artifact before it publishes to it. A fresh session republishing to an existing URL was not measured. Proposal 3 carries this as a check.

## Cost

- Per question: one revive, about **$0.208** and 4 API calls on opus (Q4). The revive in this design also republishes the page, which the probe did not measure. Expect it to cost more than Q4.
- Per report: the probe's publish turn cost $0.308066 with 4 calls, but that was a whole synthetic turn. The extra cost of a publish inside a real researcher turn is unmeasured.
- Polling: not priced. There is no tool to poll with (Q3).

## What stays out

- **Watches held by live sessions.** Ruled out by §1, and holding one costs a resident process per open report.
- **A new order kind.** Ruled out by §1. The decision order and its researcher carry everything.
- **Daemon polling.** It cannot see threads (Q3).
- **Comment-thread Q&A.** Out until the user ruling's condition is met (see above).
- **Deleting artifacts.** Needs the user's confirmation every time. The spike artifact https://claude.ai/artifact/JU754gmR89snAzsXVVZyim is left in place.

## Proposals filed (`--ref fo-3389ebc2`)

- `bl-768ce4f7`: the researcher publishes the report and `artifact_url` is added to the report (items 1 and 2).
- `bl-87310800`: link the artifact on `do show` and in `decision_delivered` (item 3). Depends on bl-768ce4f7.
- `bl-b8110d86`: republish on every revision and every do-send answer, including fo-dc681c11 revisions (item 4). Depends on bl-768ce4f7.
- `bl-30a9a782`: measure whether an in-thread `@claude` avoids the banned message (follow-up).
- `bl-e8054fc5`: measure what "Open in Claude" does (follow-up).
