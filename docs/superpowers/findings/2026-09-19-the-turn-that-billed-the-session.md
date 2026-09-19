# The turn that billed the session

**wo-0a9ba9b3**, issue #470. Every multi-turn bill the OS has produced since claude CLI
**2.1.277** landed (2026-09-18 15:27) counts turn 1 once per turn that follows it, because
`modelUsage` and `total_cost_usd` in a result envelope stopped being about the turn and
started being about the resumed session.

Measured over **all 814 result JSONs** under `<project>/.jarvis/turns/` on the dev machine
and the **127 multi-turn orders** among them whose session transcript is still indexed by
`usage.index_sessions`. Ground truth throughout is the deduped transcript total, which
Claude Code writes and Jarvis does not.

---

## Finding 1 — the running total, and the day it started

`modelUsage` is monotone within a session, its per-turn deltas equal each envelope's
top-level `usage` object, and the LAST file's figure equals the whole transcript. The sum
equals nothing. wo-966987af, straight off disk:

| turn | `modelUsage` | delta | top-level `usage` | `total_cost_usd` |
|---:|---:|---:|---:|---:|
| 1 | 2.02M | 2.02M | 2.02M | $2.28 |
| 2 | 41.14M | 39.12M | 39.12M | $25.60 |
| 3 | 45.64M | 4.50M | 4.12M | $28.88 |
| 4 | 46.43M | 0.79M | 0.79M | $29.87 |
| 5 | 49.39M | 2.96M | 2.96M | $32.02 |

Recorded: 184.6M / $118.65. Real: 49.4M / $32.02. The transcript says 49.0M.

**It is a CLI change, not a property of the format.** Splitting the corpus at the minute
2.1.277 was installed:

| | files | `modelUsage` == `usage` |
|---|---:|---:|
| before 2026-09-18 15:27 | 773 | 722 (93%) |
| after | 41 | 9 (22%, and all of them first turns or processes that started before the upgrade) |

The 7% residue before the cut is **subagent spend** (`modelUsage` counts a turn's
subagents; `usage` does not) plus a recurring ~0.38M side-model call that writes no
assistant message. It is not a fraction of the turn.

## Finding 2 — the version-2 docstring argued from the right evidence backwards

`claude_cli.derive_turn_usage`'s note observed `usage` running at "33-60% of
`modelUsage`" over 186 live files and concluded `usage` undercounts. That ratio is what a
per-turn delta looks like beside a cumulative total. Its two corroborations are artefacts
of the same thing: `modelUsage` agrees with the transcript **on the last turn** (a running
total's final value is the total), and `costUSD` sums to `total_cost_usd` **within one
file** because both are cumulative. Corrected in place, because an argument left standing
re-derives its conclusion.

## Finding 3 — the obvious fix is a second, quieter defect

Subtracting unconditionally — "a value lower than its predecessor is a reset" — fixes the
recent orders and destroys the historical ones, whose envelopes never accumulated:

Each rule run over the same 127 orders, as a ratio of the deduped transcript total:

| rule | median | mean | max | orders over 1.05 |
|---|---:|---:|---:|---:|
| sum the envelopes (what shipped) | 1.000 | 1.096 | 4.63 | 7 |
| blind diff, reset on a drop | 0.853 | 0.827 | 1.01 | 0 |
| classify, then diff (this change) | **1.000** | 0.992 | 1.01 | 0 |

The blind rule's median is the damage: it is below 1.0 on nearly every order, and every
one of those is a bill that would be re-derived downwards for ever. The under-counts left
in the third row are orders whose result JSONs were pruned years-of-releases ago; they
read identically under all three rules, and the bill puts them on their own line.

Measured with the shipped `derive_turn_usage`, not a sketch of it. The seven inflated
orders: 4.63x, 3.77x, 3.25x, 3.04x, 3.01x, 1.27x, 1.25x → all 1.00-1.01x. Dollars over
the 127: **$4,094 → $3,571**.

**The classifier** (`claude_cli._continues_previous`) calls an envelope a continuation of
the previous turn only when all three hold: same `session_id`; `modelUsage` monotone in
every token class; and the implied delta at least as large as the envelope's own `usage`,
which is the lead agent's spend for that turn alone and therefore a floor. Where an
envelope reports no `usage` at all — an API error that returned one of all zeroes —
monotonicity decides alone, and the delta is charged to the turn that first reports it
rather than dropped: the session transcript contains that spend and there is no other
turn to give it to.

## Finding 4 — what the two contradictions on the page should have cost

An inflated bill disagreed with itself in two places and shipped anyway: a per-call table
summing to 49.0M under a 184.6M headline, and an agent view attributing all 184.6M to the
lead agent while reporting zero subagents. `reconcile` checked that the calls did not
EXCEED their turn and never that the turn was accounted for. It now also asks the other
direction — a turn against its own API calls plus its own subagents, with a 10% allowance
for side-model calls no transcript carries — and asks it only of a conversation whose
transcript accounts for every recorded turn, because a part-pruned transcript makes a turn
look large for a reason that is not a bug.

## What was applied

* `USAGE_SCHEMA_VERSION` 3: each turn's share, with the raw cumulative reading kept in
  `reported` so the next turn can be derived without re-reading the file.
* `wo_turns.cost_usd` is repaired with the tokens. It is what `budget.spent` sums, so an
  order was stopping at roughly a third of its ceiling.
* `bill.PAYLOAD_VERSION` 5, and `_upgrade_seal` learned that a correction is not an
  ageing: a sealed bill may shrink when every turn in the fresh reading is current, and
  says what it used to hold (`accuracy.corrected_from`).
* Fleet-wide, over the 206 orders with turn files: **$5,160 recorded against $3,894
  real** before the fix — $1,266 phantom, 1.33x.
