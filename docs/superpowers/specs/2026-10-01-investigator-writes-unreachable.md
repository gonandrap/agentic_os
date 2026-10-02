# An investigator's four permitted writes are unreachable

Issue #905, work order wo-2a615408.

## The problem

Three of the four writes an investigation order is CONTRACTUALLY given are refused in
practice. The allow list is right; reaching it is broken.

`INVESTIGATOR_JARVIS_MUTATIONS` (`src/jarvis/hooks.py:842`) carries exactly the four the
dispatch prompt promises:

```python
INVESTIGATOR_JARVIS_MUTATIONS = frozenset({
    ("wo", "ask"), ("wo", "assume"), ("learn", "add"), ("investigate", "verdict"),
})
```

`_investigator_may_run` (`src/jarvis/hooks.py:1335`) can only consult that set through
`jarvis_verbs(command)` (`src/jarvis/hooks.py:48`) — `verbs = jarvis_verbs(command)` at
`hooks.py:1339`, and the whole jarvis branch is inside `if verbs:`.

`jarvis_verbs` opens with a scan of the RAW command text (`hooks.py:61`):

```python
    if _SHELL_DANGEROUS.search(command):
        return ()
```

where `_SHELL_DANGEROUS = re.compile(r"[|;\`$<>]")` (`hooks.py:27`). It also splits
segments on raw text: `for segment in command.split("&&")` (`hooks.py:64`).

All four permitted writes take a free-prose QUOTED argument — a question for Neo, an
assumption, a learning, and `--from-file` aside, the verdict. Prose contains `;`, `|`,
`$`, `<` and `>` (every `->`). So the metacharacter scan fires on characters the shell
never interprets as structure, `jarvis_verbs` returns `()`, the jarvis branch is skipped,
the fallback segment path refuses (`hooks.py:1353` denies on `>` or `<` anyway), and the
investigator gets the denial at `hooks.py:1291` — which then NAMES the writes it just
refused: "Your only writes are `jarvis wo ask`, `jarvis wo assume`, `jarvis learn add`
and `jarvis investigate verdict …`".

### Evidence

Investigation inv-1c3f7c8a. Its investigator wo-6be2ab21 ran `jarvis wo ask` twice and
was refused twice. Transcript:
`/home/gonzalo/.claude/projects/-home-gonzalo-workspace-agentic-os--claude-worktrees-wo-6be2ab21/5206f315-da4b-4788-b5f9-d27a4e9f466f.jsonl`.

Attempt 1 (line 247), argument double-quoted, the offending character a `>` inside it:

> `('jarvis validation show wo-33e1d0b4' -> 'no validation has run on this work order')`

Attempt 2 (line 254), the offending character a `;`:

> `ops.validation_applies returns False and no validation round can ever open; automerge then holds for ever`

Both were answered with the "An investigation READS" denial (transcript lines 248, 255).
The investigator then abandoned Neo and wrote its verdict on a guess. Both arguments were
inert: a single shell word, nothing after it.

### Why the suite is green

The fixtures are sanitised. `tests/test_investigation_orders.py:120-124` puts the four
writes in `READS` with short, metacharacter-free arguments:

```python
    "jarvis wo ask wo-inv001 'is the hold re-derived?'",
```

A real question does not look like that. That is why this shipped.

### Root cause, named

Deciding shell structure by scanning RAW text. The symptom is "three writes are
unreachable"; the cause is that `jarvis_verbs` has no notion of quoting, while every
command it must clear carries prose in quotes. `_mask_shell_text` (`hooks.py:480`)
already exists for exactly this problem in the backgrounding half of the module and was
not used here.

### Out of scope, recorded not fixed

The same investigator was also refused six evidence READS of the form
`jarvis wo show wo-x 2>&1 | head -120`. Those are genuine pipelines — real structure, not
quoted prose — so the refusal is correct under the current rule. Whether an investigator
should be allowed to pipe a read into `head` is a separate decision about widening reads.
Not this order's fix; do not touch it here.

## The fix

Judge STRUCTURE, not raw text, inside `jarvis_verbs` only — and do it ASYMMETRICALLY by
quote kind.

### Where it lives, and why there

`jarvis_verbs` (`src/jarvis/hooks.py:48`) has exactly one caller,
`_investigator_may_run` (`hooks.py:1339`), so the blast radius is one kind.
`is_jarvis_command_chain` (`hooks.py:30`) stays BYTE-FOR-BYTE UNCHANGED: it is the
auto-allow for every other kind (`hooks.py:1769`, `hooks.py:1979`), and §2.6 of
`docs/superpowers/specs/2026-09-27-investigation-orders.md` is the standing ruling that
narrowing or widening it changes every kind's behaviour. The two functions keep their
shared `shlex` parse, so they still cannot disagree about what a segment IS — only about
which characters count as structure. Update `jarvis_verbs`' docstring (`hooks.py:49-60`),
which currently says "Same `_SHELL_DANGEROUS` and `shlex` parse": after this change that
sentence is false.

### The mechanism

Two masks, one per character class.

1. `|`, `;`, `<`, `>` and the positions of the `&&` split: judge on quote-masked text
   from `_mask_shell_text` (`hooks.py:480`), which blanks single- and double-quoted spans
   and comments while PRESERVING POSITIONS. These five are literal inside EITHER quote
   kind, so masking both is correct for them.
2. `$` and a backtick: judge on text with ONLY single-quoted spans masked. The shell
   INTERPOLATES inside double quotes, so
   `jarvis wo ask wo-1 "$(sed -i s/a/b/ src/x.py)"` is a jarvis command on its face and a
   write when it runs. Masking double quotes for these two would hand the investigator
   arbitrary command execution through the one branch that exists to let it talk to Neo.

**This asymmetry is the security-carrying half of the change and must have its own
test.** Half-applied — both classes on `_mask_shell_text` — the fix is a hole, and the
hole passes every test about prose.

### The new mask

A sibling of `_mask_shell_text`, next to it at `hooks.py:480`, reusing a single-quote-only
variant of `_QUOTED_SPAN` (`hooks.py:476`, currently `r"'[^']*'|\"[^\"]*\""`): the same
substitution with the double-quoted alternative dropped. It must blank with spaces of
equal length, exactly as `_mask_shell_text` does, because POSITIONS ARE LOAD-BEARING:
the `&&` split is taken on the masked text and the resulting offsets index back into the
ORIGINAL `command` to get each segment's real text for `shlex.split`. A mask that changed
lengths would slice segments at the wrong byte.

Segment splitting: take the `&&` offsets from the fully masked text (class 1), then slice
`command` — never `command.split("&&")` on raw text, which is the second half of the same
bug (`jarvis learn add 'a && b'` splits into two bogus segments today).

Everything after the metacharacter decision stays as it is: `shlex.split` per segment,
`cd` passthrough, the `(verb, sub)` pair, `()` on a parse failure or a non-jarvis
segment. The empty-tuple contract does not change, so `_investigator_may_run`'s fallback
path, `INVESTIGATOR_JARVIS_DENIED`, `INVESTIGATOR_JARVIS_ARG_VERBS` and the deny text all
stay untouched.

### Rejected alternatives

- **Widen `_SHELL_DANGEROUS` / fix `is_jarvis_command_chain` instead.** Changes the
  auto-allow for workers, planners, managers and analysts in one edit. §2.6 forbids it,
  and the blast radius is the entire fleet's permission surface for a bug that affects
  one kind.
- **Mask both quote kinds for all six characters (symmetric, simpler).** Opens
  `"$(…)"` command substitution inside a permitted write. Rejected on security, not
  taste.
- **`shlex.split` the whole command and look for metacharacters in the resulting
  words.** `shlex` strips quotes, so a real unquoted `;` and a quoted one become
  indistinguishable after the parse. It cannot tell the two evidence commands from
  `jarvis wo ask wo-1 'q' ; git commit -am x`.
- **Special-case the four write verbs: skip the metacharacter scan when the verb pair is
  in `INVESTIGATOR_JARVIS_MUTATIONS`.** The scan is what keeps `jarvis wo ask wo-1 'q' &&
  git push` from clearing on its first segment. Dropping it for exactly the verbs an
  investigator is most likely to chain something onto is backwards.
- **Fix only the prompt (tell the investigator to avoid punctuation).** The contract
  already names these writes as unconditional; a rule the model must remember on every
  question is the shape of defect this hook exists to make impossible, and the measured
  outcome of asking nicely is wo-6be2ab21's guessed verdict.

## Tests

All in `tests/test_investigation_orders.py`.

1. **Regression, the real commands.** The four writes with REALISTIC prose arguments
   carrying each of `;`, `|`, `$`, `>`, `<` and a backtick inside SINGLE quotes are
   ALLOWED through BOTH `hooks.investigator_bash_decision` and `hooks.preflight_decision`
   (the second is not redundant — `test_the_denial_is_reachable_through_preflight_not_
   just_directly` at `tests/test_investigation_orders.py:161` exists because ordering in
   `preflight_decision` is where this kind's rule can be bypassed). Use wo-6be2ab21's two
   actual commands, verbatim-shaped, as the fixtures: the `-> 'no validation has run on
   this work order'` one and the `; automerge then holds` one.
2. **The asymmetry.** `$(…)` and a backtick inside a DOUBLE-quoted argument are still
   DENIED — e.g. `jarvis wo ask wo-1 "$(sed -i s/a/b/ src/x.py)"` and the backtick
   equivalent. Its own test, named for the reason, not folded into the deny table.
3. **Real structure still refused.** `jarvis wo ask wo-1 'q' ; git commit -am x` and
   `jarvis wo show wo-1 > f` are DENIED, and `jarvis bug report …`, `jarvis issues start
   790` and `jarvis wo finish …` (already in `MUTATIONS`,
   `tests/test_investigation_orders.py:105-107`) do not become reachable — including with
   a prose argument carrying a metacharacter, which is the new path to them.
4. **Pin the hook's allow list to the CONTRACT's list** (what #905 asks for). DERIVE the
   four write commands from the investigator prompt `dispatch.py` builds
   (`src/jarvis/dispatch.py:1021-1025`, the "Writes — EXACTLY four, and nothing else is
   permitted:" block) rather than retyping them, so a contract that grows a fifth write
   FAILS this test instead of silently losing the write. Reach the prompt text the way
   `tests/test_investigation_orders.py:794` and `:819` already do: the module-level
   `_prompt(store, "investigator", project_spec)` helper
   (`tests/test_investigation_orders.py:756`, which ends in
   `dispatch.build_worker_prompt(...)`) with the `store` and `project_spec` fixtures.
   Parse the bullet lines under that heading for their `jarvis <verb> <sub>` and assert
   BOTH directions: every write the prompt names clears `investigator_bash_decision`, and
   the count is exactly four.
5. **Negative control.** `hooks.is_jarvis_command_chain` is unchanged — keep and extend
   `tests/test_investigation_orders.py:190`'s
   `assert hooks.is_jarvis_command_chain("cd /tmp && jarvis bug report x")`, and add that
   it still returns False for a prose-quoted command, which is the behaviour this fix
   deliberately does NOT give it — and `investigator_bash_decision(..., _env("worker"))`
   returns `None` for every new fixture above.
