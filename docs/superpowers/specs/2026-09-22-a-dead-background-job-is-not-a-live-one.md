# A dead background job is not a live one

Issue #575. Companion to issue #573, which owns the status header; this owns the
MESSAGE printed under it.

## 1. The fault

A worker turn is one `claude -p` process. Ending the turn kills whatever it left
running in the background — there is no notification, nothing wakes the worker. A
worker that signs off with "the suite is running in the background; I'll report when it
lands" has written a sentence that is false at the moment it is written, and
`record_agent_reply` stores it verbatim as the work order's latest word.

wo-d81fcc15 did it twice: 62 hours parked on job `b51fl7bhe`, then a `wo send resume`
whose entire turn was the same promise again, 28 seconds long.

`worker_brief.py`'s CORE budget already forbids it — that bullet IS `TEMPLATE_VERSION`
v10, added after wo-2df8828c. Prod ships v11. The worker read it and did it anyway, on
two consecutive turns. **A rule the OS only asks for and never checks is not enforced.**
So this is a detector, not a sentence.

## 2. Detection — mechanical, at turn end

`worker_session._reap` settles a turn exactly once and already reads the session
transcript for other reasons. `background.orphaned_in_turn` reads it for three facts:

| Fact | Where it is in the transcript (verified against a real one) |
|---|---|
| a job was launched | `tool_use` block whose `input.run_in_background` is true |
| its id | the matching `tool_result` row's `toolUseResult.backgroundTaskId` |
| it was resolved | a `<task-notification>` for that id whose `<status>` is not `running`, a `KillShell` naming it, or a `BashOutput` poll reporting a non-running status |

A job launched in the turn's window and never resolved inside it is ORPHANED. In a
worker turn the notification never arrives — that is the whole bug — so in practice
every uncollected launch is orphaned, and the resolution cases exist so that an
interactive session, or a worker that polled the job to completion, is not accused.

One exemption: a turn that recorded a `finished` event is not judged. The worker's
`wo finish` summary is the authoritative last word there, and a job it collected before
finishing must not be reported as abandoned.

The finding is a `background_orphaned` event carrying the turn's `seq`, the reply's
`msg_id` and each job's id and command. Written once, where the turn is reaped — never
from a reconciler branch that re-derives every tick (kn-089de524).

## 3. Voiding the message

`timeline.build_conversation` already receives the events beside the messages, so the
void is DERIVED at render time from that event's `msg_id` — no column, no migration, no
second copy of the fact. Every surface that prints the conversation gets it from the one
function: `jarvis wo show`, the dashboard work-order page, and the conversation the
supervisor judges from.

The marker is a `void` field placed BEFORE `content` in the turn dict, so the CLI's
generic printer renders it above the words it retracts, and `--json` consumers get a
field rather than a mutated message. The message text itself is never rewritten: the
record is what the worker said, and the void is what the OS knows about it.

## 4. A resume that differs from the attempt

`jarvis wo send <id> resume` re-entered the identical turn shape, which is why the loop
did not terminate. `Daemon._deliver` now prepends the OS's own note when the latest turn
is the one that orphaned a job: it names the dead job, says a turn is one-shot, and asks
for the command in the FOREGROUND.

It rides as a MESSAGE of its own with `source = background-orphan`
(`timeline.UNAUTHORED_SOURCES`, so it renders as "jarvis → worker" and never as the
user), delivered in the same turn as the user's words. No extra turn and no extra cache
write — and the rule at `daemon._deliver` that the user's words are never framed is kept,
because this is a separate message rather than a header wrapped around theirs.

Queued once per turn: a delivery that fails and is retried finds the note already on the
record and does not write a second.

## 5. Deliberately not here

`needs_review`'s durable reason, `IDLE_NO_FINISH_BLOCKER`, `ops.ack_attention` and the
`acknowledged` timeline event belong to issue #573 and are untouched. That one fixes an
ABSENT reason; this one fixes a PRESENT falsehood, and the header is not where the
falsehood is printed.

No self-healing nudge either. The OS does not re-dispatch the worker on its own here:
the detector's job is to stop the record lying, and the resume the user already types is
the retry.
