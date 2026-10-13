# A manager's by-design idle is not a standstill

Work order wo-775a1a74. GitHub issue 1010. Neo question 1603 answered the three design
questions (the by-design test narrows candidacy only; the hold rides on
`Verdict.excluded` with a named cause; `kind` is an input to the decision and not a store
read inside `stuck.py`) and they are not re-opened here. The user's standing ruling
kn-1cec46b5 — every fleet-health number is a catalog key, never a module constant — and
Neo 1596 govern section 2.

**Two places where this spec deviates from the work order's brief, both on evidence found
while verifying it, both argued in full below: the shipped default for `exempt_kinds`
(section 2.1) and the home of the outstanding-child sentence (section 5).** Read those two
before implementing.

## The problem

### 1. `kind` is not an input to the stuck decision, so a manager is judged like a worker

`ops.stuck_scan` (`src/jarvis/ops.py:9048`) walks every open order of a project and skips
exactly two classes before judging:

```python
if wo["origin"] in UNGOVERNED_ORIGINS:
    continue
if wo.get("kind") in ("investigation", "investigator") \
        or str(wo.get("parent_id") or "").startswith(
            f'{FO_ID_PREFIXES["investigation"]}-'):
    continue
```

Everything that survives goes to `stuck.assess` (`src/jarvis/stuck.py:66`), whose
signature is `status, seconds_in_status, seconds_since_activity, discounted_seconds,
thresholds, fallback_seconds, activity_statuses, excluded_cause`. **There is no `kind`
parameter.** The decision is time in status (`stuck.py:84`) against
`thresholds.get(status, fallback_seconds)` (`stuck.py:85`), and nothing else.

A `kind='manager'` order (`WO_KINDS`, `src/jarvis/project_store.py:302`) is
**idle-until-messaged by design**, and three sites in the OS say so in those words:

* `Daemon`'s reconciler branch at `src/jarvis/daemon.py:6202-6251` writes `idle` and
  raises **no** attention flag — "A project manager order is idle BY DESIGN: it acts on a
  message and ends its turn, and between messages there is nothing for it to do"
  (`daemon.py:6203-6205`). It clears a stale flag rather than raising one
  (`daemon.py:6262-6263`).
* `Daemon._manager_handoff` (`src/jarvis/daemon.py:1850`) is what wakes it — "a manager is
  idle-until-messaged by design" (`daemon.py:1857-1858`).
* `invariants.status_label` renders the status as `idle — waiting for a message from its
  feature` (`src/jarvis/invariants.py:1805-1806`).

`idle` is **not** in `DEFAULT_FLEET_HEALTH_THRESHOLDS` (`src/jarvis/catalog.py:1046-1054`,
which names `pending`, `dispatching`, `running`, `validating`, `needs_review`,
`waiting_pr_merge`, `waiting_input`), so it falls to
`DEFAULT_FLEET_HEALTH_FALLBACK_MINUTES = 30` (`catalog.py:1060`) — **0.5h**. Arithmetic,
therefore: every feature manager in the fleet crosses the threshold within half an hour of
its first turn and stays over it for its feature's whole life.

**Root cause, stated once: the sweep's subject test is `status`, and `status` cannot
express "nothing is expected to happen here until somebody speaks to it".** `stuck.py`
already has the shape for that distinction — `ACTIVITY_STATUSES` (`stuck.py:28`) exists
because `running` is judged on silence rather than on time in status — but membership is
keyed on the status, and a manager's "silence" is not its own silence: it is its feature's.

### 2. The measured incident

From issue 1010, on manager wo-2db95e7c: idle **76.8h**, `needs_attention` **0**, waiting
correctly on its last live child wo-d2d777dc (`waiting_pr_merge`, PR 943). The sweep made
it a candidate, bought an investigator session for it, and alarm al-f1534d0b escalated to
Neo — two user attention items spent on a feature that was progressing. The `needs_attention
0` is the OS being right at `daemon.py:6202` and the sweep contradicting it from another
module.

`waiting_pr_merge` is in `OPEN_STATUSES` (`project_store.py:64`), so the child was live by
every reading the OS has. Nothing about the record was ambiguous; the sweep simply never
looked at it.

### 3. The brief the sweep wrote was wrong on its face

`stuck_scan` composes each candidate's row with
`"blocker": blocker[0] if blocker else "nothing the record can name"` (`ops.py:9095-9096`),
from `invariants.true_blockers` (`src/jarvis/invariants.py:985`). For wo-2db95e7c
`true_blockers` returned empty, so the sweep printed **"nothing the record can name"** over
a record that names wo-d2d777dc. That string reaches the investigator verbatim through
`stuck.WHY`'s `{blocker}` slot (`stuck.py:40`), so the session was briefed that the record
was silent.

`true_blockers` is empty there for a defensible reason — `idle` derives exactly one blocker
and nothing else (`invariants.py:87-93`) — which is why the repair in section 5 is not where
the brief put it.

### 4. One element of the hardcoded tuple is dead code

`("investigation", "investigator")` at `ops.py:9072`. `WO_KINDS =
("worker", "planner", "manager", "analyst", "investigator")`
(`project_store.py:302`), and `ProjectStore.create_work_order` asserts
`kind in WO_KINDS` (`project_store.py:1946`). `investigation` is a **feature-order** kind
(`ops.py:8898`, `ops.py:8971`) — no `work_orders` row can ever hold it, so that element has
never matched anything. Harmless today, load-bearing for section 2.1.

## The fix

Five changes. Section 1 makes `kind` an input to the decision; section 2 moves both
exemption mechanisms into the catalog; section 3 is the narrowing rule; section 4 names the
hold out loud; section 5 stops the brief lying about the record.

### 1. `stuck.assess` gains `kind`, and the decision stays inside it

```python
def assess(status: str, seconds_in_status: float, seconds_since_activity: float,
           discounted_seconds: float, thresholds: Mapping[str, float],
           fallback_seconds: float,
           activity_statuses: Sequence[str] = ACTIVITY_STATUSES,
           excluded_cause: str = "",
           kind: str = "worker",
           exempt_kinds: Sequence[str] = (),
           by_design_idle: bool = False) -> Verdict:
```

**Why inside `stuck.assess` and not at the call site.** The module's opening docstring is
the reason, verbatim: "nothing here calls a model, opens a store, reads a file or writes a
row", and `health.due`'s rule it quotes — "this function is the whole of the spend decision
and reading state inside it would make that decision untestable without a store"
(`stuck.py:1-14`). A caller that filtered candidates before calling `assess` would move
half the spend decision somewhere with no unit test and no `Verdict` to show for it, which
is exactly how section 1's defect arrived: the two `continue`s at `ops.py:9069-9074` are
already that, and they are the only part of the decision with no `Verdict` behind it.

**`kind` and `by_design_idle` are scalars, never a store.** `exempt_kinds` is a
`Sequence[str]` on the precedent of `activity_statuses` (`stuck.py:69`), which is the same
shape of argument — a policy list the caller resolves and the predicate reads.
`by_design_idle` is a bool the CALLER computes from the store (section 2.2); `stuck.py`
gains no import of `project_store`, `bus` or anything else.

**Precedence inside `assess`, and it matters.** Three exclusions can be true at once:

1. `kind in exempt_kinds` → `HOLD_EXEMPT_KIND`. First, because such an order is not a
   subject at all: whether the account's window is shut is irrelevant to something nobody
   is ever going to judge.
2. `excluded_cause` (today's usage window / fleet pause / reopen ramp) → unchanged, second.
3. `by_design_idle` → `HOLD_AWAITING_FEATURE`. Last of the three: a fleet pause is a fact
   about the whole fleet and outranks a fact about one order's feature.

All three short-circuit to `stuck=False`, keeping `active_seconds` and
`threshold_seconds` populated as they are today (`stuck.py:87-91`) so the row still shows
the arithmetic.

**The reason sentence becomes per-cause.** Today's single sentence — "held by {cause}, which
clears by itself — nothing is judged while it does" (`stuck.py:90-91`) — is false for both
new causes: an exempt kind never clears, and a manager's hold clears when its feature
speaks, not by itself. So `stuck.py` gains

```python
EXCLUSION_REASON: Mapping[str, str]
```

keyed by cause, with today's sentence as the fallback for `PAUSE_USAGE_LIMIT` and anything
added later, and the two new entries in section 4. One site composes the sentence, as now.

### 2. Two exemption mechanisms, both catalog-resolvable

#### 2.1 `os.fleet_health.exempt_kinds` — a `FleetHealthConfig` field

```python
@dataclass
class FleetHealthConfig:
    ...
    exempt_kinds: tuple[str, ...] = DEFAULT_FLEET_HEALTH_EXEMPT_KINDS
```

`FleetHealthConfig` is at `src/jarvis/catalog.py:1108`; the new constant sits with its
siblings above it (`catalog.py:1040-1106`). It **replaces** the hardcoded tuple at
`ops.py:9072` — a module constant is what kn-1cec46b5 and Neo 1596 forbid, and a kind the
sweep must not judge is the same class of decision as a threshold.

**DEVIATION 1 — the shipped default is `("investigator",)`, not
`("investigation", "investigator")` as the brief says.** Section 4 of the problem is the
evidence: no `work_orders` row can hold `kind='investigation'`
(`project_store.py:1946`, `WO_KINDS` at `project_store.py:302`). Shipping it would force one
of two bad outcomes — either the validator below accepts values outside `WO_KINDS`, which
throws away the whole point of refusing a typo, or the shipped default fails its own
validator and every catalog parse raises. Behaviour is identical with it gone, because it
never matched. If the lead wants literal fidelity to the brief instead, say so and the
validator has to accept `FO_KINDS` too; that is argued against in Rejected alternatives.

**Validation, in `_parse_fleet_health` (`catalog.py:2267`).** Three refusals, each where
the message can name the key — `GateConfig.parse`'s rule, which the function's own docstring
already cites (`catalog.py:2274-2276`):

* not a list of strings → `"{where}.exempt_kinds" must be a list of strings`;
* a kind not in `project_store.WO_KINDS` → refused naming `sorted(WO_KINDS)`, the exact
  shape of the `thresholds` refusal at `catalog.py:2293-2295`. A typo that silently
  exempted nothing is how a manager stays watched after somebody tried to stop it;
* the empty list is **allowed** and means "judge every kind". It is a meaningful policy, not
  a mistake, unlike `os.navigation`'s empty pattern list (`catalog.py:2322-2325`) which
  measures nothing while looking measured.

**A WHOLE-FIELD override, not a per-element merge** — unlike `thresholds`, which inherits
per status (`catalog.py:2271-2272`, `catalog.py:2288-2298`). The asymmetry is deliberate.
`thresholds` is a map of independent numbers, so merging lets a project name one and keep
the fleet's answer for the other eight. A kind list is one policy statement, and a merging
list can only ever GROW: a project that wants its own investigators judged would have no way
to say so, because every removal would be silently undone by the fleet value. One-line
override is the only arrangement in which both directions are expressible.

**The reflective `>= 1` loop at `catalog.py:2308-2312` will raise `TypeError` on a tuple.**
`FLEET_HEALTH_MAP_KEYS` (`catalog.py:1094`) is named for mappings and a tuple is not one, so
add

```python
FLEET_HEALTH_LIST_KEYS = ("exempt_kinds",)
```

and skip it in the same loop beside `FLEET_HEALTH_MAP_KEYS` and `enabled`. Without this,
every catalog parse in the suite dies.

**Not a fleet-only key.** It does not go in `FLEET_HEALTH_FLEET_ONLY_KEYS`
(`catalog.py:1100`): both entries there are refused because no arrangement of per-project
numbers can express them, and "which kinds this project's sweep judges" is per-project by
nature.

**The `parent_id`-prefixed investigation skip at `ops.py:9072-9074` stays exactly as it is.**
It is a test on the subject's FAMILY — an order whose parent is an `inv-` feature row — not
on its kind, it has no cause to name, and changing it is not what issue 1010 is about.

#### 2.2 The manager rule: judged on silence from its feature

A `kind='manager'` order **in status `idle`** is a candidate only when **both** are true:

* its feature has **no outstanding child** — no child that is non-settled and
  non-superseded; and
* **nothing has messaged it** — no queued `wo_messages` row for the manager, and no
  envelope still `queued` to role `manager` on the feature.

A manager in any other status is judged like any other order. The hold is the by-design
IDLE wait and nothing else: one wedged in `running`, or sitting in `waiting_input` /
`needs_review`, is a real stuck class the activity clock catches, and the same
`status == "idle"` gate is what `outstanding_children_note` (section 5) applies, so the
label and the sweep cannot disagree.

**The second half is `_manager_handoff`'s existing reading and must not be redefined.**
`Daemon._manager_handoff` computes exactly it at `src/jarvis/daemon.py:1930-1936`:

```python
if store.queued_messages(manager["id"]):
    return
if any(e["state"] == "queued" and e["to_role"] == "manager"
       for e in store.envelopes(subject_fo_id=fo_id)):
    return
```

with the reason in its docstring — "one with a queued message, or with the rejection
envelope still queued against its feature, has not started" (`daemon.py:1887-1891`). **Two
definitions of "a manager is waiting correctly" is the defect this spec exists to remove, one
module along.** So extract it:

```python
# src/jarvis/bus.py
def nothing_queued_to_manager(store: ProjectStore, fo_id: str,
                              manager_wo_id: str) -> bool:
```

**Why `bus.py`.** It owns envelopes, `queued` state, `post`, `deliver` and
`ADDRESSABLE_ROLES`; the `to_role == "manager"` string is its vocabulary, not the sweep's.
`_manager_handoff` replaces its two guards with the call and keeps its own
`manager["status"] != "idle"` / revive guard, which is a different question (has it finished
reacting) and belongs to the nudge.

**The first half is one new derivation, beside the function that answers its sibling
question:**

```python
# src/jarvis/invariants.py, beside dead_feature_children (invariants.py:607)
def outstanding_feature_children(store: ProjectStore,
                                 fo_id: str) -> list[dict[str, Any]]:
```

non-settled (`status not in TERMINAL_STATUSES`) and `not c["superseded"]`, over
`store.feature_children(fo_id)`. **Why there:** `dead_feature_children` already answers
"which children fail this feature" over the same rows with the same superseded rule, and its
docstring's argument for being ONE function — two readers that must agree exactly — applies
identically here, with `ops.stuck_scan` and `invariants.status_label` as the two readers.
The superseded rule is `Daemon.settle_features`' own, quoted at `daemon.py:1598-1604`: a
superseded child settles its feature neither way, so it is not outstanding either.
`budget.live_children` (`src/jarvis/budget.py:722`) is NOT reused: it excludes
`budget.EXHAUSTED` because an exhausted child cannot spend, and an exhausted child is very
much outstanding to a manager.

**The caller.** In `ops.stuck_scan`, after the existing row filters and before `assess`.
The flag passed is TRUE when the manager is waiting correctly, i.e. when there IS an
outstanding child or something IS queued; spelling it as the positive "is it waiting
correctly" keeps the parameter name honest:

```python
by_design_idle = (wo.get("kind") == "manager" and wo["status"] == "idle"
                  and bool(wo.get("parent_id")) and (
    bool(invariants.outstanding_feature_children(pstore, wo["parent_id"]))
    or not bus.nothing_queued_to_manager(pstore, wo["parent_id"], wo["id"])))
```

**Gated on `kind`, `status` and `parent_id` so no other order pays for the reads** — the rule every
neighbouring branch of `true_blockers` follows ("the read sits behind the status check, so no
other work order pays for it", `invariants.py:1046`). A manager with a NULL `parent_id` is
`by_design_idle=False`: a manager with no feature is stranded by definition, and there is no
feature to be silent.

### 3. A genuinely stranded manager still clears its ordinary threshold

Neo 1603 (1). **The by-design test NARROWS candidacy and never widens it**, and in this
arrangement that is true by construction rather than by care: `by_design_idle` can only set
`excluded` and short-circuit to `stuck=False`. It never sets `stuck=True`, never lowers a
threshold and never changes a clock. A manager with no outstanding child and nothing queued
falls through to the same arithmetic as any other order and is a candidate **only** once
`active >= threshold` — silence AND over-threshold, in that order.

The 30-minute fallback (`catalog.py:1060`) stays the number for `idle`; a project that wants
a stranded manager caught sooner names `idle` in its own `thresholds`, which already works.

**What this does not duplicate.** `_manager_handoff`'s `FEATURE_MANAGER_STALLED` /
`FEATURE_MANAGER_SILENT` flags (`daemon.py:1960-1967`) cover the POST-ROUND case: a judged
round, every live child completed, nothing new since the last nudge. The sweep's stranded-
manager case is the complement — no round in flight, no children left, nobody talking to it —
and it files an investigation rather than a feature flag. Neither is reachable from the
other's precondition.

### 4. The hold rides on `Verdict.excluded`, with a named cause

Neo 1603 (2), the usage-limit hold's shape (`ops.py:9089-9090`,
`worker_session.PAUSE_USAGE_LIMIT`). Two constants in `stuck.py`, beside `ACTIVITY_STATUSES`:

```python
HOLD_EXEMPT_KIND = "exempt_kind"
HOLD_AWAITING_FEATURE = "awaiting_feature"
```

The one-sentence reasons, from `EXCLUSION_REASON` (section 1):

* `HOLD_EXEMPT_KIND` — **"an order of kind {kind} is never judged by this sweep —
  `os.fleet_health.exempt_kinds` names it, so time in status is not a claim about it"**
* `HOLD_AWAITING_FEATURE` — **"a manager is idle until its feature messages it, and this
  feature has not finished with it — nothing is judged while that is true"**

`{kind}` interpolates, which is why `kind` is a parameter of `assess` rather than something
the caller substitutes afterwards: the sentence is part of the verdict.

**Where it is read, unchanged in each case.** `Verdict.reason` already reaches
`ops.stuck_report`'s row (`ops.py:9145`) and `Verdict.excluded` its `"excluded"` key
(`ops.py:9144`); `cmd_stuck` prints `row['reason']` at `src/jarvis/cli.py:2958` and
`ui/templates/stuck.html:26` renders `r.reason` with an `excluded` column at
`stuck.html:49,59`. **No renderer changes. One copy change:**
`src/jarvis/ui/templates/stuck.html:44` currently promises that "a usage window, a fleet
pause or the reopen ramp says so under excluded" — three causes, and there are now five. That
sentence has to name the two new ones or the page is lying about its own column.

`stuck.WHY`'s `{reason}` slot (`stuck.py:36`) needs nothing: an excluded order is never a
candidate, so no investigator ever receives one of these sentences. That is the invariant, not
an omission.

**The behaviour change to state plainly: an exempt kind becomes a VISIBLE excluded row
instead of an invisible skip.** The `continue` at `ops.py:9069-9074` increments nothing and
emits nothing, so an investigator order is absent from `jarvis stuck` with no explanation.
Moving the test into `assess` means `scanned` counts it, `stuck_report` shows it with
`excluded = exempt_kind`, and the sweep still opens nothing for it — `stuck_only=True` drops
it at `ops.py:9091` before `true_blockers` and before the candidate list. The cost is two
indexed reads per exempt order per sweep (`state_durations`, `holds.held`, `ops.py:9076-9083`)
that the `continue` used to save. Accepted: investigator and manager orders are a handful per
project, and "indistinguishable from an order inside its threshold" is precisely the
complaint Neo 1603 (2) settles.

### 5. The brief stops saying "nothing the record can name" over a record that names it

**DEVIATION 2 — this sentence is NOT a `true_blockers` entry, and it must not be.**

The brief asks for a blocker from `invariants.true_blockers` naming the outstanding child.
That is unsafe, and the evidence is one import away:

* `idle` is in `BLOCKED_STATUSES` (`invariants.py:93`), whose comment says `idle` is there
  "for exactly one blocker and derives no other… Being idle asks nothing of anyone — that
  is the whole of issue #264";
* `invariants.check_blocked_work_is_surfaced` — INV-ATTENTION-MISSING, `invariants.py:2490` —
  walks `BLOCKED_STATUSES` and, for any row with a non-empty `true_blockers` and no flag,
  **raises the flag and repairs** (`invariants.py:2506`).

So a `true_blockers` entry for a manager with a live child would put a permanent attention
flag on **every** manager in the fleet, every tick — converting issue 1010's one correct
reading (`needs_attention 0`) into a fleet-wide false positive strictly worse than the
defect being fixed. `true_blockers`' own contract forbids it too: "the reasons this work
order genuinely needs the user" (`invariants.py:987`). An outstanding child needs nobody.

**The precedent for the arrangement used instead is in this very file.** `status_label`'s
plan-hold branch: "Flagless — `true_blockers` gets no branch, so INV-ATTENTION-MISSING stays
quiet without an exception" (`invariants.py:1831-1832`). A descriptive sentence about why
nothing is happening is a LABEL, not an obligation.

**The mechanism.** `invariants.status_label`'s `idle` branch (`invariants.py:1805-1806`)
grows from the fixed `idle — waiting for a message from its feature` to naming the children,
when there are any:

```python
idle — waiting on 1 of 3 children in fo-… (wo-d2d777dc)
```

over `outstanding_feature_children` (section 2.2), falling back to today's exact string when
the list is empty. Gated on `wo["status"] == "idle"` and `wo.get("parent_id")` and
`wo.get("kind") == "manager"`, so only a manager pays the read — the same gating as the
`PR_REPAIR_STATUSES` branch two below it (`invariants.py:1823-1826`).

**And `ops.stuck_scan`'s `blocker` field falls back to it before the empty string**
(`ops.py:9095-9096`): `blocker[0] if blocker else outstanding_children_note(...) or "nothing
the record can name"`, one formatter shared with the label so the row and the listing cannot
drift. In the measured incident this line is now reached only from `stuck_report`
(`stuck_only=False`) — the by-design verdict keeps the manager off the candidate list
entirely — so this is the reporting half of the fix, belt to section 2.2's braces.

**Why the settled-feature branch at `invariants.py:1130-1146` is a DIFFERENT question, and
why its `kind != "manager"` skip stays.** That branch fires when a child's parent feature has
SETTLED under it, and it skips a manager because `_close_feature_manager` settling the manager
with its feature is correct (`invariants.py:1134-1136`, spec §(b) of
`2026-10-07-a-settled-features-live-children-must-have-a-manager-or-a-hold.md`). It is about a
settled parent and an owed decision. This section is about an OPEN parent and a thing nobody
owes. Same two columns read, opposite verdicts, and that is why the new sentence is not a
sibling branch of it.

## Rejected alternatives

1. **Add `"manager"` to `exempt_kinds` and stop.** One line, and it loses the stranded
   manager for ever: a manager whose feature has no children left and nobody talking to it is
   genuinely stuck, and this is the only sweep that would ever notice. It also makes the
   exemption a property of the KIND when it is a property of the SITUATION — and Neo 1603 (1)
   is explicit that the by-design test must narrow candidacy, not delete the subject.
2. **Add `"idle"` to `DEFAULT_FLEET_HEALTH_THRESHOLDS` with a large number.** Moves the false
   positive instead of removing it: at any finite threshold a manager on a long-running
   feature eventually crosses it, and at a large one a genuinely stranded manager waits days.
   Time in status is the wrong measurement, not a badly calibrated one.
3. **Filter managers in `ops.stuck_scan` with a third `continue`.** This is the shape that
   produced the defect. It leaves the `Verdict` with nothing to say, so the hold is
   indistinguishable from absence on `/stuck` (Neo 1603 (2)), and it puts half the spend
   decision outside the one pure function the module docstring says owns it
   (`stuck.py:1-14`).
4. **Read the store inside `stuck.py`.** Rejected by Neo 1603 (3) and by the module's own
   docstring; it would make the spend decision untestable without a database, which is the
   property `health.due` and `stuck.assess` were both built to have.
5. **Keep `exempt_kinds` as a module constant in `ops.py`.** Refused by the user's standing
   ruling kn-1cec46b5 and Neo 1596: every fleet-health number is a catalog key. A kind the
   sweep must not judge is the same class of decision as a threshold, and the precedent
   (`DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS`, `catalog.py:1077-1078`) is a module constant the
   user already turned down once.
6. **Merge `exempt_kinds` per element across fleet and project.** Argued in 2.1: a merging list
   can only grow, so un-exempting a kind becomes inexpressible.
7. **Ship `("investigation", "investigator")` as the default and widen the validator to accept
   feature-order kinds.** The validator's job is to refuse the typo that silently leaves a kind
   watched (`catalog.py:2293-2295`'s rule). A validator accepting names no work order can hold
   accepts `"plannr"`-shaped mistakes too, in exchange for keeping one element that has never
   matched a row.
8. **Put the outstanding-child line in `true_blockers` and exempt the manager inside
   INV-ATTENTION-MISSING.** This is the brief's shape with the flag bug patched. Rejected:
   it adds a kind exemption to the one invariant whose job is catching invisibly stalled work
   ("the more dangerous direction", `invariants.py:2494-2496`), and it pays kn-cffc8905's trap
   a fourth time — `true_blockers` is read by the dashboard banner, the attention list,
   `ops.lower_attention` and INV-ATTENTION-REASON, so an entry nobody owes would have to be
   suppressed in each of them.

## Tests

`tests/test_health_sweep.py`, in the stuck-sweep section that starts at line 1214 with its own
`stuck_catalog` / `stuck_os` fixtures:

1. **A manager with an outstanding child is not a candidate however long it has idled.** Idle
   for 76.8h, one child in `waiting_pr_merge` — the incident's exact shape. Assert
   `verdict.stuck is False`, `verdict.excluded == stuck.HOLD_AWAITING_FEATURE`, and that the
   sweep opened no investigation. Then re-assert at 10x the threshold: the exemption is not a
   grace period.
2. **A manager with no outstanding child and nothing queued IS a candidate once over
   threshold, and not before.** Two assertions on one fixture, either side of the 30-minute
   fallback. This is Neo 1603 (1); without the "and not before" half the narrowing claim is
   untested.
3. **The two queued readings each hold it back on their own.** One case with a queued
   `wo_messages` row, one with an envelope `queued` to role `manager` — no outstanding
   children in either. Both must be excluded. Pair with the assertion that
   `Daemon._manager_handoff` and `ops.stuck_scan` reach the same reading through
   `bus.nothing_queued_to_manager`, so a future edit to one cannot move only one of them.
3b. **A manager not in `idle` is still judged.** `kind='manager'` in `running` and in
   `needs_review`, with an outstanding child and no activity past the threshold: a
   candidate, `excluded == ""`. The hold is the idle wait only (section 2.2).
4. **A superseded child does not hold a manager back**, and **an exhausted child does** — the
   two places `outstanding_feature_children` differs from its neighbours
   (`daemon.py:1598-1604`, `budget.live_children`).
5. **An exempt kind is an excluded ROW, not an absence.** An `investigator` order over
   threshold appears in `ops.stuck_report` with `excluded == stuck.HOLD_EXEMPT_KIND`, its
   reason naming the kind, and the sweep opens nothing for it. Negative control: with
   `exempt_kinds: []` the same order IS a candidate — otherwise the test passes against a
   hardcoded skip.

`tests/test_catalog.py`, in the `fleet_health` block at lines 680-727 (beside
`test_fleet_health_inherits_per_status`):

6. **`exempt_kinds` set fleet-wide reaches the project**, and **set on a project overrides the
   fleet value WHOLE** — fleet `["investigator", "analyst"]`, project `["manager"]`, assert the
   project resolves to exactly `("manager",)`. The contrast with the per-status `thresholds`
   assertion in the neighbouring test is the point.
7. **A kind not in `WO_KINDS` is refused naming `WO_KINDS`** (including `"investigation"`,
   which is the regression guard for deviation 1), **a non-list is refused**, and **the empty
   list parses and means "judge everything"**.
8. **The shipped default parses** — the guard for the `>= 1` reflective loop
   (`catalog.py:2308-2312`). It fails with `TypeError` if `FLEET_HEALTH_LIST_KEYS` is missed,
   which no other test in the suite would explain.

`tests/test_manager_order.py` (it already asserts `true_blockers` against managers at lines
357-402 and 667-683):

9. **The label names the child**: `status_label` for an idle manager with outstanding children
   contains the child's id and the `n of m` count; with none, it is today's exact string
   `idle — waiting for a message from its feature`.
10. **The flagless control, and it is the most important test here.** `true_blockers` for that
    same manager is `[]`, and a `check_blocked_work_is_surfaced` pass leaves
    `needs_attention == 0`. This is deviation 2's whole argument, pinned.
11. **The sweep's brief names the child**: `ops.stuck_report`'s row for that manager has a
    `blocker` that is not `"nothing the record can name"` — issue 1010's third complaint,
    asserted against the string it printed.

`tests/test_settled_feature_hold.py` keeps its existing assertions unchanged; test 10 is the
proof that the new derivation did not disturb them.

## Not covered

* **The alarm path.** al-f1534d0b escalated to Neo off this manager's turn. `jarvis inspect`'s
  thresholds (`inspect.alarm_turn_minutes` and siblings) are a separate judgement with its own
  catalog keys, and whether a manager's long-lived turn should raise one is a different
  question from whether it is stuck. If the alarm re-fires on a manager after this ships, that
  is a second order, not a gap in this one.
* **Any other `kind`.** `planner` and `analyst` are untouched: both run a turn and settle, so
  time in status is a true claim about them.
* **`Daemon._manager_handoff`'s own flags.** Section 3 states why they do not overlap; neither
  template changes.
* **The 30-minute fallback for `idle`.** Left at its shipped value. Section 3 says how a
  project changes it, and nothing here argues it is the wrong number for a stranded manager.
* **Backfill.** Nothing is written by this change; every verdict is re-derived each sweep, so
  an order excluded from the next tick onwards needs no migration. A stale attention flag
  already raised against a manager is cleared by the existing re-derivation at
  `daemon.py:6262-6263`.
