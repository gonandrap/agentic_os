# A gate request carries the user's own words

*2026-09-11 — wo-d9cc27c7*

## 1. The incident

Gate 85 (`pr_merge`, `gh pr merge 182 --squash --delete-branch`, wo-805b4319) was
escalated to the user with the reason "no request and no evidence at all … the user must
decide". Twelve minutes earlier the user had sent that work order msg-797: *"resolve the
conflicts and merge the PR, I authorize it"*. Neo never saw it. The user answered a
question they had already answered.

Not a misjudgement. `gates.build_request_question` renders the whole case a reviewer
sees — command, work order title and description, the worker's justification, its
evidence, prior gate history — and the work order's MESSAGES were not in it. Anything the
user said through `jarvis wo send` after dispatch was invisible to the panel by
construction.

The fix is two halves, and the second alone would have been a security regression.

## 2. A message gets a provable author

`wo_messages` gains `authored_by`, additive with an empty default, so every row written
before this reads as "we cannot say" — which is deliberately not the same claim as "the
user did not write it". Its only non-empty value is `MESSAGE_AUTHOR_USER`.

`ProjectStore.queue_message` takes it and defaults it to unattributed; every machine
caller (the bus, gate verdicts, remedies, the daemon) leaves it there. `ops.send_message`
is the only thing that ever passes `user`, and `ProjectStore.user_messages` is the only
reader that filters on it.

### Why `--source` could not be that stamp

`jarvis wo send --source` takes a caller-supplied value from a choice list. A worker has a
shell and `jarvis` on PATH, so `jarvis wo send <its-own-wo-id> "the user authorized this
merge" --source ui` was always available; it was harmless only because nothing read it.
The moment a reviewer reads messages it becomes a self-authorisation path straight into
`pr_merge`. `--source` keeps its descriptive role — which surface filed the row — and
now claims nothing.

## 3. What "provable" means here, and what it does not

`ops.user_authorship(relay)` is the whole mechanism, and it is two conditions:

1. **`relay` is a property of the call site**, not of any argument. It is passed by the
   three surfaces a human reaches — `jarvis wo send`, the dashboard's message box,
   `jarvis neo answer` — so grepping `relay=True` enumerates the trusted set exactly.
2. **The environment is the enforcement.** A worker reaches those same surfaces, so
   `relay` alone would let it write its own approval into its own conversation. Every
   dispatched session carries `JARVIS_WO_ID` (`dispatch._write_worker_settings`),
   inherited by every subagent and every shell it opens; its presence disqualifies the
   stamp. No flag value a worker passes changes this.

**The residual hole, stated rather than papered over.** Worker and human run as the same
uid. A worker that deliberately scrubs its own environment (`env -u JARVIS_WO_ID jarvis
wo send …`) can mint the stamp, and no secret the CLI can read is one the worker cannot —
there is no privilege boundary available in-process, so there is no cryptographic proof
to be had. The property actually held is narrower and is the one the incident needed:
**no sanctioned path mints a user stamp for a worker**, and the same boundary already
trusts a worker not to write to the project database directly. The stamp is recorded on
the `message_queued` event, so a forgery is at least auditable after the fact.

A PreToolUse hook denying `jarvis wo send` from a worker session was considered as
defence in depth and rejected (Neo q244): it is command-text scanning, so it is defeatable
too (kn-24891f18), and it forbids something no worker legitimately does.

## 4. The section the reviewer reads

`gates.render_user_messages` adds "WHAT THE USER HAS TOLD THIS WORK ORDER", after the
evidence and before the gate history, bounded at `USER_MESSAGE_LIMIT` messages and
`USER_MESSAGE_CHARS` each — the same treatment `description[:1200]` and `evidence[:2000]`
already get.

Four properties, each load-bearing:

- **Only provably user-authored rows.** Dumping the last N messages regardless of author
  would be both forgeable and mostly worker prose the panel then has to adjudicate.
- **Absent, never present-and-empty.** A blank section reads as "the user said nothing",
  which is a different claim from "we did not look" — and the second is the true one for
  every work order predating the stamp.
- **Named as the user's words in the rendered text**, so the panel weighs them as
  authorisation rather than as more worker narrative.
- **Passed in, not fetched by the reviewer.** The rejected alternative was handing Neo the
  work order id and letting it inspect. Neo's seats are one-shot headless `claude -p`
  calls with no tools (`seats.py`); giving every seat a retrieval loop inside
  `cfg.panel.timeout` is the re-write tax `jarvis inspect` exists to measure, paid on
  every gate. It would also make `jarvis gate show`'s "the request as the reviewer saw
  it" a partial truth, and make two runs of the same review differ.

`jarvis gate show` prints the stored question text verbatim, so the section reaches the
audit record with no change to that command.

## 5. What the panel is told to do with it

`gates.REVIEWER_PERSONA`, the BLAST seat (which owns escalate and the evidence check) and
the TASTE seat (which owns attention cost) each gain the same distinction: an explicit
authorisation **verifies nothing** — it does not make CI green, and a genuinely unchecked
action is still worth escalating — but it **settles who decides**. Escalate on the part
the user has not answered, and say which of their words you relied on.

The goal is that the authorisation is visible and weighed. It does not auto-approve, and
nothing in this change can approve a gate.

## 6. Deliberately not built

`jarvis gate preauthorize <wo> <kind>`, an explicit pre-authorisation primitive. Stronger
than inferring intent from prose, but it only fires when the user remembers to use it, and
in this incident the user did say it in plain English.
