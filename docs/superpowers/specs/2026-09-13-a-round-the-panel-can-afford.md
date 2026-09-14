# A round the panel can afford, and a seat that remembers

wo-f1ce0f24. Two changes to `validation.py` that fail differently and therefore ship as
two pull requests: **cost and coverage** (§2-§6) and **review quality** (§7-§8).

Supersedes the caching paragraph of `build_seat_system_prompt`'s docstring in
`2026-08-08-validation-panel-design.md`. Nothing else in that design moves: the roster,
the veto table in `arbitrate`, and blindness are untouched by every change below.

## §1 What this is measured against

`jarvis cost wo-a6af01f0`, re-run on 2026-09-13 after the order settled:

| | $ | tokens | calls |
|---|---|---|---|
| total | 57.65 | 56.9M | |
| the worker's own session | 44.83 (78%) | 53.5M | 6 turns |
| validation seats | 7.88 (13.7%) | 1.2M | 19 (4 rounds x 4 seats + 3 chair) |

The panel is an eighth of the order and the worker's rework is three quarters of it. That
ratio is the whole argument for §6: **the cheapest thing the panel can do for the bill is
stop causing rounds**, and it causes them by reviewing a third of the diff.

Every seat in every round of wo-a6af01f0 said so in its own words — "the diff was cut at
github.py, so I read none of the test files and am taking their contents on your
declaration"; "I could not read github.py, ops.py, validation.py or any test file — their
patches were withheld".

### The cache measurement

Five `claude -p --output-format json --tools '' --model sonnet` calls, sequential, one
per seat, against the real CLI on this machine. Packet = PR #206's diff truncated to
60,000 chars.

Today's layout — mandate in the system prompt, packet in `-p`:

```
tester      write  32555   read  10925
security    write  32651   read  10925
architect   write  32555   read  10925
maintainer  write  32663   read  10925
chair       write  32956   read  10925
```

Inverted — packet in the system prompt, mandate in `-p`:

```
tester      write  68357   read       0     <- cold, writes the shared prefix
security    write   4815   read   38867
architect   write   4613   read   38867
maintainer  write   4632   read   38867
chair       write   5014   read   38867
```

Cache read is 0.1x input, cache write 1.25x. Five writes of one prefix become one write
and four reads: **74% off the prefix half of a round**, at every diff size (§6), with no
behaviour intended to change.

## §2 The prefix is the packet, not the mandate

`build_seat_system_prompt` put the per-seat MANDATE in the system prompt and
`build_packet_prompt`'s output in `-p`. Its docstring reasoned about caching and
optimised the wrong axis: byte-stable PER SEAT only pays if the same seat runs twice
inside the 5-minute TTL, which happens between rounds that are minutes or hours apart.
The packet is 3-5x the size of a mandate and is shared by five calls **seconds apart**.
That is the reuse that was on the floor.

So:

- `build_shared_prefix(packet, project, brief)` — the system prompt every seat of a round
  receives, byte-identical: the packet, then the project's standing instructions.
- `build_seat_prompt(seat, …)` — the `-p` prompt: the roster header, then the seat's
  mandate, then (§7) that seat's own previous round.

The roster header (`# Jarvis validation seat: {seat}`) moves with the mandate into `-p`.
It was in the system prompt so that a reader of the record — and the test fake — could
tell a validation `chair` from a Neo `chair`; it still does that from the user prompt,
and it *cannot* stay in the system prompt without unsharing the prefix.

**Anything per-seat belongs in `-p`. Putting it in the system prompt silently undoes this
whole section** and measures as a no-op.

## §3 Priming, or the fan-out pays five writes anyway

`seats.run_blind` submits every seat to a `ThreadPoolExecutor` before reading any result
— that is what makes the round blind. On a cold cache none of the five sees another's
write, so all five write the full prefix and the change measures as nothing.

`decide` therefore calls `seats.prime_cache` and AWAITS it before `run_blind` — one call,
on the seats' own model, with the shared prefix as its system prompt and a one-token user
turn. It costs one extra cache READ of the prefix (0.1x) and about ten seconds; the
alternative, running the first seat serially, costs a full seat's generation (50-100s)
for the same effect.

It lives in `decide` rather than inside `run_blind` because its usage has to be RECORDED,
and `seats.py` takes no store by design — the thread that may query one is the caller's.
It is billed like any other seat, under the label `prime`.

Measured through the shipped path, cold cache, `diff_chars=150000`, four seats:

```
                     per-seat write        per-seat read
  no priming              ~67,600                 3,298     total write 270,178
  primed                   ~4,400                66,428     total write  17,658
```

`tests/test_validation_seats.py` asserts the ordering directly rather than the flag: the
fake records `started_at`/`finished_at` per call, and no seat call may start before the
priming call returned. A primer made concurrently with the seats would satisfy "a priming
call was made" and buy nothing.

A primer that fails is a price, not an outage: `prime_cache` never raises, and the seats
behind it then run exactly as they did before it existed.

## §4 The seats were never tool-free, and `--tools ''` is not why

`--tools ''` strips Claude Code's built-in tools. It does **not** strip MCP servers. The
system-prompt snapshot for a real seat-shaped call on this machine (`claude -p --tools ''
--system-prompt-snapshot on`) carries 13 MCP tool definitions, 31,871 chars of them:

```
mcp__claude_ai_Google_Drive__{copy_file,create_file,download_file_content,
  get_file_metadata,get_file_permissions,list_recent_files,read_file_content,
  search_files,share_file,trash_file,update_file}
mcp__plugin_context7_context7__{query-docs,resolve-library-id}
```

Asked to name its tools, such a call lists all thirteen. So `validation.py`'s header —
"THE SEATS JUDGE THE PACKET AND ONLY THE PACKET … `tools=''`" — has been false for as
long as the daemon's user has had an MCP server configured, and what a seat could reach
depended on exactly the thing that header says it must not: the user's global
configuration. A seat judging a diff could `share_file` it.

`claude_cli.run_headless_result` now passes `--strict-mcp-config` **whenever `tools ==
""`**. The blast radius is exactly the callers that already asked for no tools (the
validation seats and the dashboard digest); Neo and Neo's panel pass `tools=None` and are
untouched. It is worth ~8,000 tokens of prefix per round as well, but the reason it ships
is the invariant.

## §5 The 128 KiB argv ceiling, which §6 would otherwise hit head-on

`MAX_ARG_STRLEN` is 32 pages — 131,072 bytes — **per argument**, regardless of the 2 MB
`ARG_MAX` total. Measured:

```
argv --append-system-prompt    OSError [Errno 7] Argument list too long   (300,792 bytes)
--append-system-prompt-file    write 126698  read 10925
```

Today's packet rides in `-p` at ~60k chars, so the fleet has been running at under half
the ceiling by luck rather than by design: a PR with a long body and a long file list
could already have exceeded it, and every seat call would have died with an `OSError` the
panel reports as an outage.

`claude_cli` therefore writes any system prompt over `SYSTEM_PROMPT_ARGV_LIMIT` (64 KiB,
half the ceiling) to a temporary file and passes `--append-system-prompt-file`. Small
callers keep the argv door, so a CLI without the flag cannot break Neo or the digest.

## §6 `diff_chars` = 150,000

Prefix cost by diff size, measured through the file door, and what a round's prefix costs
under each layout (input-equivalent tokens: write 1.25x, read 0.1x):

| diff_chars | prefix tokens | today: 5 writes | shared: 1 write + 4 reads |
|---|---|---|---|
| 0 (overhead alone) | 18,818 | 117,612 | 31,050 |
| 60,000 | 41,822 | 261,388 | 69,006 |
| 150,000 | 76,347 | 477,169 | 125,973 |
| 300,000 | 137,621 | 860,131 | 227,075 |

Diff text runs at a steady 0.384 tokens/char. **A round at 150,000 chars costs less than
half what a round at 60,000 costs today** (125,973 against 261,388) and sees two and a
half times as much.

300,000 is affordable too — it still undercuts today's 60,000 — and is rejected on
context, not on price: 137,621 prefix tokens plus the packet's non-diff sections, the
mandate, the seat's own prior round (§7) and its thinking leaves too little of a 200k
window, and a seat that overflows abstains, which is the one failure this change must not
buy. 150,000 leaves ~60k of headroom.

150,000 is also the number the record asks for: PR #206, the change every seat complained
about, is 150,380 chars.

The truncation machinery stays exactly as it is, and the file list stays untruncated. A
cap that can never be hit is a cap that rots — this one is hit by real pull requests in
this repository.

## §7 A seat carries its own previous round

`decide` built every seat prompt from `build_packet_prompt(packet)` and nothing else. No
seat ever saw what it had asked for in the round before. Only the chair saw opinions, and
only its own round's; the SUBMITTER received the chair's asks, and the reviewers who wrote
them had amnesia.

wo-a6af01f0, round 2: the tester wrote "you did not name a test that asserts it" with no
awareness that it had raised that ask itself in round 1, and the architect and maintainer
both restated their round-1 findings from scratch. A seat could not verify that what it
demanded was delivered — it could only re-derive the finding, from a diff that was
truncated anyway.

Each seat's `-p` prompt now carries **its own** previous-round reply — verdict, reason,
asks — under a heading that says whose it is and that this round's job includes saying,
ask by ask, whether it was met.

**Its own, and no other seat's.** Blindness is the property the whole panel rests on, and
a seat that could read another's prior round would be reading a round-delayed copy of the
opinion it is not allowed to see. `tests/test_validation_seats.py` asserts both halves in
the same test: a round-2 prompt contains that seat's round-1 asks, and contains no other
seat's text. Either alone is satisfied by a prompt carrying no memory at all.

**THE LAST ROUND IT SPOKE IN, not the numerically preceding one.** A round can close
`failed` as a transport outage with no opinions in it, and a seat whose memory went blank
because the round before it timed out is the amnesia this section exists to end.
`previous_opinion` walks back to the most recent round in which this seat actually
replied, and the heading names that round number so the seat knows how old its own words
are.

An unparseable prior reply is shown RAW rather than dropped: a seat told nothing cannot
tell "I said nothing last round" from "my reply did not survive", and those want opposite
weight on what the submitter has since changed.

The chair gets the same treatment — it is a seat, and its prior `outcome`/`reason` is its
own.

It rides in `-p` beside the mandate, never in the shared prefix (§2), and it is read on
`decide`'s own thread with every other prompt: `run_blind` takes no store precisely so a
seat on a pool thread cannot reach one.

## §8 Later rounds and the delta

Round n re-sends the whole packet. A human reviewer would be shown "changes since your
last review".

**NOT SHIPPED**, and the arithmetic is why rather than the risk alone.

The only thing a delta saves is the prefix, and §2 already took 74% of it. At
`diff_chars=150000` a later round's prefix is 76,347 tokens: paid cold, once per round,
that is 95,434 input-equivalent tokens — under 2% of what wo-a6af01f0 cost in total, and a
twentieth of what one round of the worker's own rework costs. A delta that cut the diff to
a tenth would save perhaps 85,000 input-equivalent tokens per later round.

Against that: a delta-only round cannot see a regression outside the delta, while
believing it has reviewed the change. That is the same failure mode as the truncation §6
exists to end — the panel reading less than it thinks it has — bought back for a saving
two orders of magnitude below the one the work order is about.

§7 delivers the half of this the record actually asked for. What the seats complained of
in wo-a6af01f0 was not re-reading the diff; it was not knowing what they had already
demanded, and a seat that carries its own asks can say "this ask is met" without the
packet shrinking at all.

Reopen §8 only with a measurement showing a later round's prefix is the expensive thing,
and only with the fallback-to-full-packet path this note declines to build speculatively.

## §9 What a later change will be tempted to undo

**Model tiering un-shares the prefix.** A different model is a different cache. If seats
are ever put on different models they must be GROUPED by model and primed per group, or
§2 and §3 both evaporate silently — the tests stay green, the bill goes back up. Noted at
`validation.seat_model`, which is where the decision would be made.

**The packet is submitter-authored, and it now sits in the system prompt.** The security
seat raised this in round 2 of wo-a6af01f0 and it was never addressed: PR title and body
are written by the thing being judged, and rendering them as packet narration makes them a
channel into the judge's instructions. The model has no hard system/user privilege
boundary, so the label was always the real mitigation — it matters more now. The shared
prefix opens with a line saying the whole document is EVIDENCE, and the PR title and body
are additionally marked as the submitter's own prose. Anything added to the packet later
is inside that frame and must stay inside it.

**Prose cannot be verified by the suite** (kn-fe226ab1). Moving the mandates out of the
system prompt is a prompt-prose change: every structural test stays green while seat
behaviour shifts underneath. `evals/llm/test_validation_judgment.py` (`JARVIS_EVALS_LLM=1`)
is the only instrument that can see it, and a change to §2, §4 or §7 that does not report
it has measured half of what it did.

**AND IT DOES NOT COVER §7.** Every case in that eval runs ONE round, so the memory block
is never rendered into a graded prompt: its 11/11 says adding an optional section did not
disturb round-1 behaviour, and says nothing about whether a seat that has its own asks
uses them. The A/B that would answer it — arm WITHOUT built by cutting
`render_prior_opinion`'s block by marker, two rounds per case over
`untested-new-function` then `new-function-with-its-own-test` — is filed as `bl-86b70464`
with the reason the outcome cannot be the signal.

What it reported for this change, `model=sonnet`, before = `git archive HEAD` in a clean
tree, after = this branch: **11/11 both arms**. One after-run scored `must-pass` 2/3 and a
second scored 3/3 with no code change between them — that battery is three cases behind a
100% floor, so a single run cannot separate a behavioural shift from sampling noise at
n=3, and a future change here should read two runs before believing either direction.

**THE EVAL'S FORCED OUTAGE IS KEYED ON THE SEAT HEADER AND ALMOST FAILED SILENTLY.**
`_seat_of` read the header off the system prompt; once the packet moved there it named no
seat, `fail_seat` stopped matching, and the degradation scenario graded a healthy panel
while reporting a degraded one. It failed loudly here only because the eval also asserts
the seat really was down — the pairing it was written with. Any later change to where a seat's
identity travels must move that meter with it.
