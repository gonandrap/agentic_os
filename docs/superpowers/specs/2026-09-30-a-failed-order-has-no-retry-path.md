# A failed order has no retry path

Work order `wo-bfefdc59`, GitHub issue #886 (medium, expedited, filed by hand on Jarvis OS
0.10.40). Scope pinned by Neo q1107 and q1108, quoted below and not relitigated here.

One command, one route, one `waiting_on` branch. Nothing automatic — §2 says why, in the
user's own words.

---

## The problem

**A `failed` work order can only be revived through an undocumented side effect of a
command that describes itself as something else.**

The filing, verbatim from #886: `wo-604b5b99` turn 7 resumed after a usage limit at
09-28 21:00, died at 23:55 without writing a result, and settled `failed`. The user
accepted its 3 pending assumptions at 09-30 05:33. That left the attention reason as
`worker failed — review and retry` — and there was nothing anywhere that retried it.

The evidence, all paths in this worktree:

* **The revive exists and is unnamed.** `ops.send_message`, `src/jarvis/ops.py:1160`. On
  `wo["status"] in ("completed", "failed", "cancelled")` it sets
  `note = f"note: work order is {wo['status']}; the session will be revived"`
  (`ops.py:1165`), queues the message and clears the attention flag. The mechanism is not
  in that function at all: `Daemon._deliver` launches the turn and then, at
  `src/jarvis/daemon.py:3173-3175`, `if wo["status"] != "running": store.set_status(...,
  "running"); store.clear_attention(...)`. A queued message IS the retry, and nothing says
  so.
* **`grep -rn revive src/jarvis/ CLAUDE.md docs/` hits three lines**: `ops.py:1165` (that
  note) and `ops.py:9290`/`9294`, which are `fo resume`. The work-order revive is
  documented nowhere — not in the crib sheet, not in `wo send --help`, whose whole text is
  `"send feedback to the worker handling a work order"` (`src/jarvis/cli.py:684`).
* **`jarvis wo why` cannot name it.** `ops.waiting_on` (`ops.py:1187`) folds `failed` into
  one arm with `completed`/`cancelled`/`waiting_pr_merge`/`needs_review` at `ops.py:1318`
  and answers `the work order is failed — nothing is running to nudge`. True, and it hands
  the reader nothing.
* **So `wo why` offers only a dismissal.** `ops._diagnose_commands` (`ops.py:1970`) has no
  revive to offer, and the one command it would otherwise reach for is refused:
  `force_validation_refusal` (`ops.py:5204`) rejects any status outside
  `FORCEABLE_STATUSES`. The user reads `jarvis wo ack` as the only move — putting the flag
  down on work that was never finished.
* **`jarvis wo fix` is correct and is not the answer.** `failed` is in
  `FIX_NOTHING_TO_CLEAR` (`ops.py:2140`), so `fix` says there is nothing for a remedy to
  clear. That stays true: `remedies.py` excludes `set_status`, `wo done` and `fo resume` on
  purpose, and a retry is the user's own act rather than a remedy.
* **The dashboard has no control.** `src/jarvis/ui/app.py` has no retry route; the nearest
  template is `@app.post("/wo/{name}/{wo_id}/fix")` at `app.py:1782`.

**Root cause, named:** the revival path was built as a by-product of message delivery and
never given a name, a refusal, a record or a surface. The symptom is the missing dashboard
button; the cause is that no function in the tree means "retry this order", so no surface
can offer one and no document can describe one.

Not the root cause, and deliberately left standing: **a turn can die without writing a
result at all.** That is issue #888 / `wo-2ae960c4` (harvest a partial result from a
no-result turn). #888's own description says "the retry turn (#886) is launched with that
harvest as its brief", so the mechanism here is the one it will hang off, and **nothing in
this spec may assume a harvest exists.**

## The fix

`jarvis wo retry <wo-id> [--message "…"]`, a `POST /wo/{name}/{wo_id}/retry` control, and a
`waiting_on` branch that names the command. Manual only.

### 1. What is NOT built, and why

**No automatic retry.** Quoting Neo q1108 in full, because it is the scope boundary:

> Narrowed: do not build the automatic retry. The ruling in kn-3d8fa23a (retry only on
> api_error>=500) stands. A turn that ends with no result is exactly the replay risk that
> ruling excludes, and the user already turned down kill-and-re-deliver. For #886, ship the
> manual path: a retry action on the dashboard, and 'jarvis wo why' naming the retry
> command. Leave the order failed until the user retries.

So `Daemon.retry_paused_turns`, `worker_session.turn_pause`, `TurnPause.resumable`,
`transient_failure` and the `api_error_status >= 500` predicate are **untouched**. A
no-result turn settles `failed` exactly as it does today, and stays `failed` until a person
acts.

And q1107, which decides the shape:

> Confirmed: go with (b). Build 'jarvis wo retry <id> [--message]' in ops, have
> _diagnose_commands offer it so wo why names it, and add a POST control on the work order
> page. Make wo retry the documented name for wo send's revive. It is the user's own
> explicit act, and the standing wo fix exclusions forbid set_status, which a revive remedy
> would do.

### 2. `ops.retry` — the signature, and where the revival logic lives

```python
def retry(wo_id: str, message: str | None = None, project_name: str | None = None,
          relay: bool = False) -> dict[str, Any]
```

**It DELEGATES to `send_message`; there is no extracted shared helper, and that is a
decision rather than laziness.** The revival is not code in either function — it is the
queue row plus `Daemon._deliver`'s `set_status` at `daemon.py:3173-3175`. What
`send_message` owns is four statements: `queue_message`, the `message_queued` event, the
`clear_attention`, and the note. Pulling those into a third symbol would give two callers a
chance to drift on the one field that must never drift, `authored_by` — and
`project_store.queue_message`'s docstring (`project_store.py:3872-3876`) states the rule
that `ops.send_message` is *the only* caller that ever passes `MESSAGE_AUTHOR_USER`. A
second queueing path would make that sentence false. So:

```
retry()  ->  retry_refusal(wo)            # raise OpsError, §4
         ->  the text, §3
         ->  send_message(wo_id, text, source="retry",
                          project_name=project_name, relay=<§3>)
         ->  store.add_event(wo_id, "retry_requested", {...})   # §8
         ->  its own payload, overriding send_message's note
```

`retry` re-reads nothing: `find_work_order` is called once inside `send_message`, and
`retry` needs its own read first for the refusal. Two reads of one row on a user-typed
command is not a cost worth a new plumbing seam.

`send_message` KEEPS ITS CURRENT BEHAVIOUR ON EVERY STATUS, including `completed` and
`cancelled`. "The documented name for `wo send`'s revive" is deliberately **not** a full
scope match — see §4.

Return payload: `{"project", "wo_id", "msg_id", "status": "failed", "authored": bool,
"note": RETRY_QUEUED, "assumptions": <int>}`.

### 3. A retry with no `--message`

A relaunch needs something on the queue: `Daemon.deliver_messages` is the only thing that
opens a turn on a settled order. So a message-less retry queues an OS-authored literal.

```python
RETRY_NOTE = (
    "The OS is relaunching this work order because the user asked for it. Its last turn "
    "ended without a result, so the OS recorded it as failed — nothing about the work was "
    "judged wrong. Say where you got to, then carry on from there; do not start again. "
    "Finish with `jarvis wo finish` when the work is done."
)
```

**Verbatim, and it must not read as the user speaking.** Two enforcement points, both
already in the tree:

* `source="retry"`, never `"jarvis"`/`"ui"`/`"direct"`, so the conversation panel renders
  `· via retry` (`work_order.html:471`) and the timeline's `_message_label` can tell it
  apart.
* `authored_by` stays `""`. `retry` calls `send_message(..., relay=False)` **whenever
  `message is None`**, whatever its own `relay` argument said, so `ops.user_authorship`
  (`ops.py:1130`) returns `""` by its first condition. This is load-bearing and not
  cosmetic: `ProjectStore.user_messages` (`project_store.py:3967`) is the narrow reader
  that feeds the user's own words into a privileged-action request, and it matches
  `authored_by` exactly. An OS literal stamped `user` would let a worker quote the OS back
  to the gate panel as the user authorising something.

With `--message`, the text is the user's, and `relay` is passed straight through — the CLI
passes `relay=True` exactly as `cli.py:3026` does for `send`, the route passes `relay=True`
as `app.py:1682` does, and `user_authorship`'s `JARVIS_WO_ID` check still strips the stamp
from a worker running the same command. `--message` REPLACES the literal rather than
appending to it: two paragraphs where the second re-explains what the first already said is
the framing `Daemon._deliver` refuses to add around a user's words ("Anything framing them
… is text the worker can mistake for an instruction from the user").

### 4. Which statuses `retry` accepts

**`failed` only**, following `ops.resume_feature_order` (`ops.py:9260`), whose docstring
argues its own narrowness: "`cancelled` was the user's own decision and reversing it is a
different act with different consequences for the children they stopped; `completed` has
nothing to resume." The same reasoning transfers unchanged, plus: an order that DELIVERED
has `jarvis validation force` and `jarvis wo review`, and an open one has a worker to talk
to.

`retry_refusal(wo) -> str | None`, pure over the work-order row, is the one home of the
rule (§5). Two refusals, in this order:

1. Not `failed`:

   > `{wo_id}` is {status}, not failed — `wo retry` relaunches an order whose worker died
   > without delivering. Carry on a {status} one with `jarvis wo send {wo_id} "…"`.

2. `failed` with no `session_id`:

   > `{wo_id}` failed before it ever opened a conversation, so there is no session to
   > relaunch — a message queued here would sit undelivered (`worker_session.delivery_hold`
   > holds it: "it has no session to resume"). Nothing here can be retried; file the work
   > again.

   Reachable, which is why it is a refusal and not an assertion: `failed` is written with no
   session by `ProjectStore.release_dispatch_claim` at `project_store.py:2296`
   (`dispatch_attempts` spent) and by `daemon.py:4611` ("worker turn never started"). On
   those two, `wo send` today queues a message that `delivery_hold`
   (`worker_session.py:300`) holds for ever and `invariants.stuck_message` later reports as
   stuck. `retry` refuses instead of reproducing that.

   **RE-DISPATCHING an order that never opened a conversation is out of scope.** It is a
   `set_status(pending)`, not a revive, and it is a different act with a different record.
   Named here so nobody adds it quietly.

**The `wo send` resolution, stated explicitly.** `send_message` keeps reviving `completed`,
`failed` and `cancelled` with its existing note. `retry` is the NARROWER, NAMED act:
`failed` only, with a refusal, a timeline event and a surface. Nothing is taken away from
`wo send` — its note gains a pointer (§9) and that is all. Two commands where one reaches
three statuses and the other one is not a contradiction: `wo send` says "here is something
to read", `wo retry` says "go again", and only the second is a thing the user can be
OFFERED off a diagnosis.

### 5. Pending assumptions, and the attention flag

**`retry` does NOT refuse on pending assumptions.** `wo ack` and `wo done` both do, and both
for the same reason, in `ack_attention`'s own words (`ops.py:7172`): "burying one silently
drops work the user asked for". A retry buries nothing — the assumptions stay pending, stay
on `jarvis wo review`, and `invariants.true_blockers` keeps raising them. Refusing would
lock the issue's own case (#886: three assumptions, then a retry) behind an unrelated
decision. The payload carries the count and the command, so the user is told, not blocked.

**Attention is cleared exactly as `send_message` clears it (`ops.py:1179-1180`), and the
spec says out loud that this is cosmetic for at most one tick.** The flag is re-derived
every reconcile tick by `invariants.true_blockers`, which appends `worker failed — review
and retry` for any governed `failed` order (`invariants.py:835`), and INV-ATTENTION-REASON
rewrites any reason that derivation does not produce. The flag goes down FOR REAL when the
turn goes out, in `_deliver` (`daemon.py:3173-3175`), and comes back if the delivery fails —
which is the honest behaviour, not a bug to paper over.

**`retry` writes no status.** No `set_status`, so no new state and no `retry_queued` status:
`project_store.py:281-285` is the standing rule ("`waiting_pr_merge` earned a status because
nothing derived it; this does not"), and until the daemon launches the turn the order really
is `failed`, which is what q1108 asked for. `RETRY_QUEUED` says so:

```python
RETRY_QUEUED = ("queued as message {msg}; jarvisd launches the turn on its next tick. The "
                "order stays `failed` — and stays flagged — until that turn starts.")
```

### 6. `waiting_on`: `failed` gets its own branch

`ops.waiting_on`, above the shared arm at `ops.py:1318`, which keeps
`completed`/`cancelled`/`waiting_pr_merge`/`needs_review` exactly as they read now:

```python
if wo["status"] == "failed":
    return {"what": "failed", "stalled": False,
            "detail": f"the worker died without delivering — `jarvis wo retry {wo_id}` "
                      f"relaunches it in the same session; nothing is running to nudge"}
```

* **`stalled` stays `False`.** It is the narrow claim that a *message* is a repair, and it
  is what `_diagnose_commands` reads to offer `jarvis wo resume-auto` (`ops.py:2022`). A
  nudge into a dead conversation is not the move; `retry` is, and it is offered by its own
  predicate.
* **The slug stays `"failed"`**, so `FIX_NOTHING_TO_CLEAR` membership (`ops.py:2140`) is
  unchanged and `jarvis wo fix` keeps answering `nothing here for a remedy to clear —
  {detail}` — now with a detail that names the command. The comment at `ops.py:2131-2139`
  requires every slug to be decided into one of those sets; this one is already decided and
  stays there. `FIX_USERS_MOVE` is deliberately NOT joined: `fix`'s job is remedies, and
  `wo why` is where the offer belongs.

### 7. The offer in `_diagnose_commands`, single-homed

`ops._diagnose_commands` (`ops.py:1970`), in a new block placed FIRST — before
`validation force`, before `wo ack` — because on a failed order it is the only command that
moves the work, and the list is read top down:

```python
retry_no = retry_refusal(wo)
if retry_no is None:
    out.append({"command": f"jarvis wo retry {wo_id}",
                "why": "relaunch the worker in its own session from where it died — "
                       "nothing about the work was judged wrong"})
elif wo["status"] == "failed":
    refusals.append(retry_no)
```

The `elif` guard keeps the refusal off every healthy order: a `running` order does not want
a line explaining why it cannot be retried. A `failed` order with no session DOES — that is
the one the user is standing in front of.

**`retry_refusal` is ONE function called by three callers** — `ops.retry`, this offer, and
`retry_state` (§7b). The function's docstring carries the rule this whole block exists to
obey, quoted from `_diagnose_commands` itself (`ops.py:1976-1980`): "Every predicate here
MIRRORS the refusal of the command it offers rather than restating it … a second copy of a
predicate passes every behavioural test and drifts anyway (kn-4ea33fe6)." Same structure as
`force_validation_refusal` and `ack_refusal`.

No renderer change: `cli._print_diagnosis` (`cli.py:3155-3162`) already prints
`commands[]` and `refusals[]` generically.

### 7b. The dashboard control

**Route**, next to `fix_wo` in `src/jarvis/ui/app.py`:

```python
@app.post("/wo/{name}/{wo_id}/retry")
def retry_wo(name: str, wo_id: str, message: str = Form("")):
```

It calls `ops.retry(wo_id, message=message.strip() or None, project_name=name,
relay=True)`. On `ops.OpsError` it redirects `?error=<the sentence>#retry`, as `done_wo`
(`app.py:1743`) and `force_validation` (`app.py:1832`) do. On success it redirects
`?retried={msg_id}#retry`.

**The notice is REBUILT IN `ops` FROM THE ID.** `ops.retry_queued_notice(msg_id) -> str`
returns `RETRY_QUEUED.format(msg=msg_id)`, and `fix_filed_notice`'s docstring
(`ops.py:2590`) is the rule verbatim: "REBUILT FROM THE ID, never carried across the
redirect as text. A note the query string supplies renders as the OS speaking about what
happened to an order, so a crafted link could state a false fact about an ACT." The id
selects the words; it cannot author them. The GET coerces `retried` with `int()` and renders
nothing if it does not parse.

**State for the template**: `ops.retry_state(store, wo) -> dict | None`, modelled on
`force_validation_state` (`ops.py:5555`) including its None convention — **None when the
order is not `failed`**, so no control is rendered at all for a mechanism that does not
apply there, rather than a permanently disabled box on every page. Otherwise
`{"can_retry": bool, "refusal": str | None, "assumptions": int}`, with `refusal` straight
out of `retry_refusal` — i.e. `can_retry` is False only for the no-session case.

**Form**, in `src/jarvis/ui/templates/work_order.html`, its own `<h2 id="retry">` block
above the tabs (an ask hidden behind a tab is an ask that does not happen, §6 of the
work-order-record spec), directly under the attention/`waiting.detail` block at
`work_order.html:196-203`. Modelled line for line on the `/validation/force` control at
`work_order.html:379-412`:

* the OS's sentence about what a retry does and does not do;
* `{% if retry.refusal %}<p class="sub">◌ ...</p>{% endif %}` **directly above** the control
  it governs — "a rule explained three paragraphs from the control it governs is a rule the
  reader joins up for themselves" (`work_order.html:398-399`);
* `<textarea name="message" rows="2">` — **not `required`**, unlike `/validation/force`'s
  reason, because §3's whole point is that a message-less retry is a first-class act. The
  placeholder says so: `optional — what to tell the worker; empty sends the OS's own
  relaunch note`;
* `{{ 'disabled' if not retry.can_retry }}` on the textarea and the button, as at
  `work_order.html:405-406`;
* the `?retried=` success row rendered from `retried_line`, in the `tone-ok` shape of
  `forced_lines` (`work_order.html:382-389`);
* the pending-assumption count as a `sub` line when non-zero, linking `#pending`.

### 8. The record

One new event kind, `retry_requested`, written by `ops.retry` after the delegated
`send_message` (so a crash between them leaves a queued message and no claim about who asked
— the safe order, `resume_feature_order`'s reasoning at `ops.py:9267-9278`):

```python
store.add_event(wo_id, "retry_requested", {"msg_id": msg_id, "authored": bool(message)})
```

* **NOT in `timeline.DEBUG_KINDS`.** `message_queued` is plumbing (`timeline.py:32`); a
  person deciding to relaunch a dead order is the signal.
* **It MUST get a `timeline._describe` branch**, beside `marked_done` — an unlabelled kind
  falls through to the last line of `_describe` and renders as a bare kind plus a JSON blob
  while `event_level` calls it "signal", which is kn-3f133363 and has bitten this function
  repeatedly:

  ```python
  if kind == "retry_requested":
      return ("You retried this order",
              "with your message" if p.get("authored")
              else "the OS's own relaunch note — you sent no message")
  ```

  THE VERB SAYS WHO, the rule `marked_done` ("Marked done by you") and every `autoreview_*`
  label already follow: this event is only ever written by a user-facing surface, and it must
  never read afterwards as something the OS decided to do.

The rest of the record is the existing trail and is not duplicated: `message_queued`,
`delivering`, `message_delivered`, `status`.

### 9. Documentation

1. **`CLAUDE.md` crib sheet**, after `jarvis wo resume-auto`: `jarvis wo retry <id>
   [--message "…"]` — the named way to relaunch a `failed` order in its own session;
   `--message` optional, empty sends the OS's own note; `failed` only; the order stays
   `failed` until the turn starts; NOT automatic — a turn that died with no result is never
   replayed by the OS.
2. **`cli.py:684`, `wo send`'s help** — it does NOT describe the revive today (the whole
   string is `"send feedback to the worker handling a work order"`); the revive is only a
   runtime note. Extend it: `"… — on a failed order this also revives the session; `wo
   retry` is the named form"`.
3. **`ops.py:1165`, the note itself** — keep the sentence verbatim and append the pointer
   when the status is `failed`: `` … the session will be revived (`jarvis wo retry <id>` is
   the named form of this) ``. Do not reword the existing clause; tests and screenshots
   quote it.
4. **`ops.retry`'s docstring** names q1107, q1108 and this file, and states in one line that
   the automatic half is declined.

### 10. Tests

New file `tests/test_wo_retry.py`, one behaviour per test:

1. `retry` on a `failed` order queues one message, writes `retry_requested`, and returns
   `RETRY_QUEUED` naming that message id.
2. A message-less retry queues `RETRY_NOTE` with `authored_by == ""` — asserted through
   `ProjectStore.user_messages`, which must NOT return it.
3. `retry(..., message="...", relay=True)` IS attributed: the same reader returns it.
4. The refusal sentence, verbatim, for `running`, `needs_review`, `waiting_pr_merge`,
   `completed`, `cancelled`, `pending`.
5. The no-session refusal, on an order failed through
   `ProjectStore.release_dispatch_claim`.
6. End to end: `retry`, then `daemon.tick()`, and the order is `running` with the flag down
   — the revive actually happening, not just a row written.
7. A `failed` order with a pending assumption is retried, and the assumption is still
   pending afterwards.

Extended:

* `tests/test_wo_why.py` — `wo why` on a `failed` order offers `jarvis wo retry` and the
  blocker detail names it; the no-session case reports it as a refusal instead; no other
  status offers it.
* `tests/test_wo_fix.py` — `wo fix` on `failed` still answers `nothing here for a remedy to
  clear`, with the new detail.
* `tests/test_resume_auto_diagnosis.py` — `failed` still answers `stalled=False`, so no
  nudge is offered.
* `tests/test_ui.py` (`TestClient`, as `tests/test_ui_cost.py`) — the control renders on a
  `failed` order and not on a `running` one; the refusal renders above a disabled textarea;
  `POST` redirects `?retried=<id>`; and a crafted `?retried=999999` or `?retried=nonsense`
  renders no claim.

### 11. Out of scope

* **The automatic retry** — §1, Neo q1108.
* **Harvesting a partial result** from a no-result turn — issue #888 / `wo-2ae960c4`. `retry`
  must work with nothing harvested, which is why §3's note asks the worker where it got to
  rather than telling it.
* **Re-dispatching an order that failed before opening a conversation** — §4, refusal 2. A
  `set_status(pending)`, not a revive.
* **Reviving `completed` or `cancelled` under the new name** — §4. `wo send` still does it;
  `wo retry` does not.
* **A `retry_queued` status, or any status write in `retry`** — §5.
* **Any change to `remedies.py`, its registry or its allow-list.** A retry is the user's own
  act; `set_status` is on that registry's exclusion list, and q1107 names that as the reason
  retry is a command of its own.
