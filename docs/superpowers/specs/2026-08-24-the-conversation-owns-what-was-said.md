# The conversation owns what was said

*2026-08-24*

Follow-on to `docs/superpowers/specs/2026-08-23-the-work-order-record.md`, reported against
`wo-b1088ed9` on the production dashboard (jarvis-0.7.1, which ships that spec's PR #125).

---

## Problem

Two complaints about the same page, and one cause.

> **1.** In *Conversation*, I can see when Neo answers, but not when Neo is **asked**. I can
> see the ask in the *Timeline* tab, but that's the wrong place — the actual conversation
> has to go to the Conversation tab; Timeline is only to sequence the events.
>
> **2.** Each entry in *Conversation* is listed in *Timeline*. Why? `[Neo, answering for
> the user]` shouldn't be listed in Timeline; Timeline should say "Neo answered" and add
> the link to the answer.

`wo-b1088ed9` shows both at once. Its Conversation tab opened on

> **neo → worker** · via neo · delivered
> `[Neo, answering for the user] A now, B filed as a backlog feature order, C rejected…`

with nothing above it — an answer to a question that appeared nowhere on that tab — while
the Timeline tab carried that same paragraph in full, a click away.

### Why the previous spec did not catch it

§1 of the 2026-08-23 spec states the rule this violates:

> A record says what happened. Where the thing that happened has a record of its own, it
> POINTS at that record rather than reproducing it.

That rule was applied to the **event** half of `build_timeline` — `created`, `assumption`,
`question_asked`, the two `*_answered` kinds — and to nothing else. The function's second
loop, the one that merges `messages`, was left exactly as it was:

```python
for m in messages:
    entries.append({..., "detail": m.get("content") or "", "ref": None})
```

So every message body stayed on the timeline, which is complaint 2 — the *one* class of
entry the anti-duplication pass never touched.

And the same pass created complaint 1. Before it, `question_asked` printed the question in
full, so the ask was at least *somewhere*. §3 replaced that text with a `ref`, correctly —
but on the assumption that the conversation held the ask already. It never has. A worker's
question to Neo is a `wo_events` row (`ops.neo_ask` → `add_event(… "question_asked" …)`);
it is not a message, and the Conversation tab rendered `store.list_messages()` alone. The
ask went from duplicated to absent in one step, and no test could see it: §7's rule says to
assert an entry *against the surface its text was duplicated from*, and for this entry
there was no such surface.

---

## The rule this adds

> **The conversation is where words live. The timeline is where moments live, and it
> points at the words.**

Both readings are of one record and neither is derivable from the other:

| | holds | never holds |
|---|---|---|
| `build_conversation` | every turn, whoever took it — including the ones that are events | lifecycle, dispatch, gates, validation |
| `build_timeline` | every moment, in order, each pointing at its record | the body of anything said |

The first corollary of §1 still binds: a pointer is only a saving if it resolves. So every
conversation turn carries an `anchor` (`msg-<id>`, `q-<id>`), and a message with no id keeps
its text on the timeline rather than pointing at nothing.

---

## 1. `build_conversation(events, messages)`

New, in `timeline.py`, beside the function it completes. Pure — it never opens a store, and
in particular never opens Neo's: `ops.neo_ask` writes the question **text** into the event
payload alongside the id precisely so a record built from the project store alone can show
what was asked.

It merges, in `ts` order:

- every `question_asked` event → a turn from `worker → Neo`, carrying the question and a
  `ref` to `/neo/question/<id>` (where the deliberation is);
- every message → a turn labelled from its own `source`, by `_message_label`, which is now
  used by exactly one caller instead of being restated in the template.

A `question_asked` row with no recorded text is **skipped**: an empty speech bubble is worse
than no bubble, and the timeline still records that a question was asked.

Each turn: `{ts, kind, who, content, anchor, ref, msg_id, source, status, inbound}`.

## 2. The timeline stops reprinting

A message entry becomes what happened, with `detail = ""` and a ref:

| `source` / direction | label |
|---|---|
| `neo` | `Neo answered the worker` |
| `pr-conflict` (`UNAUTHORED_SOURCES`) | `Jarvis messaged the worker` |
| anything else inbound | `You messaged the worker` |
| outbound | `Worker replied` |

`_message_event_label` is a second function rather than a second use of `_message_label`,
because the two surfaces want different grammar: the conversation wants a speaker tag over
the words (`neo → worker`), the timeline wants a sentence about a moment. `Neo → worker`
over an empty detail says less than `Neo answered the worker` does.

`source="neo"` is written in exactly one place — `daemon._neo_drain`, only for the message
carrying an answer — so that label cannot land on anything that is not one.

`question_asked` now renders `detail = ""` **unconditionally**. The id-less fallback §3 kept
is gone: the conversation renders the ask from the same payload with or without an id, so
the fallback had become the duplication it was written to avoid.

## 3. Resolving the refs

`_ref` and `_message_ref` stay surface-neutral — never a URL, never an anchor — because
`jarvis wo show` renders the same entries. The dashboard resolves them:

| ref | link |
|---|---|
| `{"kind": "neo_question", "id": 7}` | `/neo/question/7` |
| `{"kind": "message", "id": 528}` | `#msg-528` |

`#msg-528` is a same-page anchor into a **closed tab**, so it depends entirely on the
`hashchange` handler §6 added for `#pending`. That handler already does the right thing —
find the target, walk `closest('.tabpanel')`, open it — which is the payoff for having built
it generally rather than for one id.

## 4. `jarvis wo show`

`messages` is replaced by `conversation`, not joined by it. It is a strict superset (every
message, plus the questions, plus a `who` label), and a third list of the same words is the
defect this spec exists to remove — the CLI printed `timeline:` and `messages:` back to back,
which is complaint 2 in a terminal.

---

## How this is tested

- `tests/test_timeline.py`: the conversation carries the ask; it interleaves by `ts`; it
  holds nothing that was not said; every turn has an anchor; the timeline's message entries
  carry a label, no detail and a ref. The §7 rule is now satisfiable for the ask — each test
  asserts the text against the surface that owns it, and there is one.
- `tests/test_ui.py`: the two tabs are split on `id="tab-timeline"` and asserted separately,
  so "the words are on this tab and not that one" is a real assertion rather than a
  page-wide substring check.
- `tests_browser`: following `in the conversation →` from the Timeline tab opens the
  Conversation tab at that turn. It is the `hashchange` path and is provable no other way.
  The click's handler runs after the click's own task, so the test waits for the panel
  rather than sampling it.
