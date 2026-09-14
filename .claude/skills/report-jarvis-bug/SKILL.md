---
name: report-jarvis-bug
description: Use when Jarvis OS itself misbehaves — a `jarvis` command fails, errors, hangs, produces wrong output, or the OS loses work orders, messages, notifications or state. Files a GitHub issue on the Jarvis OS tracker and pings the user. Do NOT use for bugs in the project you are working on.
allowed-tools: Bash(jarvis bug report:*)
---

# Reporting a Jarvis OS bug

You are running under Jarvis OS. When **the OS itself** gets in your way, report it —
that is how it gets fixed. Report and then carry on with your actual work; a bug report
is never a reason to abandon your work order.

## When this applies

Report when the fault is in Jarvis OS:

- a `jarvis …` command errors, hangs, or exits non-zero unexpectedly
- a `jarvis` command succeeds but does the wrong thing (message not delivered, work
  order in the wrong state, assumption not recorded, notification never arrives)
- the OS contradicts its own documented behavior in `OPERATION.md` or `--help`
- state looks corrupt or lost

**Do not** use this for bugs in the project you are working on. Those are that
project's business — use its issue tracker, or `jarvis backlog add`.

Do not report the same bug twice in one session.

## ⚠️ The tracker is PUBLIC

Issues are filed on a **public** GitHub repository. Anyone can read them.

- Describe the bug **in Jarvis OS terms only**.
- **Never** include: file contents from the project, credentials, tokens, API keys,
  customer or personal data, internal URLs, or private business context.
- Prefer relative paths (`src/foo.py`) over absolute ones, and redact anything you are
  unsure about. Say `<redacted>` rather than guessing.
- The project name and work order id are included automatically — that is fine and
  intentional. Nothing else about the project should appear.

If you cannot describe the bug without leaking private context, do not file it; instead
run `jarvis notify --level warning "…" "…"` so the user is told privately.

## How to report

One command. All five of `title`, `--description`, `--expected`, `--actual` and
`--priority` are required — a report without them is not actionable months later.

```bash
jarvis bug report "<short imperative title>" \
  --description "<what happened, and what you were doing when it happened>" \
  --expected    "<what Jarvis OS should have done>" \
  --actual      "<what it did instead, including the exact error text>" \
  --priority    <low|medium|high|critical|blocker> \
  --steps       "<optional: numbered steps to reproduce>"
```

### Choosing the priority

There is no default: you have to state one, and it decides what the fleet does next.

| level | means |
|---|---|
| `blocker` | **the fleet cannot work.** Work orders cannot be dispatched, the daemon is down or wedged, or every worker in a project is stuck behind this |
| `critical` | **work is LOST, or the OS LIES.** State, messages, assumptions or history destroyed or silently dropped; or a surface reports something untrue so the user acts on a false picture. A work order dying is not this; a work order dying WITHOUT SAYING SO is |
| `high` | a real defect with a workaround — it costs you time or a retry, but the work gets done and the record stays true |
| `medium` | annoying or wasteful but bounded: one surface, one command, one avoidable path |
| `low` | cosmetic, a rough edge, a wording problem, a suggestion |

**Rate the effect on the fleet, not the inconvenience to you.** You have just lost time
to this, which is exactly the state in which everything feels critical. The question is
whether work STOPPED, whether something was LOST, or whether the OS said something
UNTRUE — not how irritating it was.

`critical` and `blocker` commit the fleet to a fix and to a release, so your rating is a
CLAIM and Neo re-assesses it against this same rubric before anything acts on it. Claim
one honestly when it fits; a claim Neo downgrades is queued like any other bug and
costs nothing but the disagreement, which is a signal the user wants. `low`, `medium`
and `high` are not re-assessed — they queue in the backlog for the user to promote.

The running Jarvis OS version, your project and your work order id are attached
automatically — do not put them in the text.

### Example

`critical` because the OS reported `queued: true` and exited 0 over a message it never
delivered — the user acts on a false picture. Had the command errored instead, the same
fault would be `high`.

```bash
jarvis bug report "wo send silently drops messages when the worker is idle" \
  --description "Relayed user feedback with 'jarvis wo send wo-91 \"use squash merge\"'. The command printed queued: true and exited 0." \
  --expected "The message is delivered to the worker and appears as a new user turn." \
  --actual "It stayed queued. 'jarvis wo show wo-91' still lists it as queued 20 minutes later, and the worker never received it." \
  --priority critical \
  --steps "1. Create a work order and let the worker go idle. 2. Run jarvis wo send <id> \"hello\". 3. Run jarvis wo show <id>."
```

The command prints the issue URL, and under it one line saying what happened next: a
backlog id, or that Neo is re-assessing your claim, or a work order id. It never rounds
your claim up to a decision — a `critical` whose re-assessment is still queued says so.
Mention both lines in your final answer, then get back to your work order.

**Do not touch the issue afterwards.** The OS owns it from then on — the `priority:`
label moves if Neo re-rates it, an issue with a work order on it is labelled
`in progress`, and it closes itself, with the work order and the pull request on it,
once the code lands. Closing it by hand, labelling it, or filing a second issue for the
same fault all race the OS and leave the tracker saying something untrue.

## If reporting fails

`jarvis bug report` exits non-zero and explains why — it never pretends to have filed
an issue it did not file. Common cause: `gh` cannot authenticate from a daemon-spawned
worker. Do not retry in a loop. Fall back to telling the user directly:

```bash
jarvis notify --level warning "Could not file a Jarvis OS bug" "<the bug, plus the gh error>"
```
