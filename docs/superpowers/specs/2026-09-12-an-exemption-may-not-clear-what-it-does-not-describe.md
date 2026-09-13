# An exemption may not clear what it does not describe

Status: implemented (wo-551f5e8c). Supersedes nothing; tightens `gate_rules.py`.

## 1. The incident

On 2026-09-12 the release gate — the one whose summary says *this reaches the live
production fleet* — could be walked. Probed live with `jarvis gate explain`:

```
echo hi<newline>./scripts/shipit.sh                             NOT GATED
jarvis wo finish wo-12345678 --summary "x" && gh pr merge 210    NOT GATED
ls .claude/skills/shipit && ./scripts/shipit.sh                  NOT GATED
```

Five learned exemptions were walkable, across two gate kinds. All five were authored by
Neo, validated by `validate_pattern`, canary-tested on admission, and listed as sound by
`jarvis gate rules`.

## 2. The cause is not the newline

The work order found the first of them and named the newline: a negated character class
that excludes `;`, `&`, `|`, backtick and `$` but not `\n`, in a pattern anchored with
`$` rather than `\Z`. Python's `re` matches `\n` inside a negated class, and without
`re.MULTILINE` the trailing `$` still permits it, so the class swallows every following
line.

That is true of three of the five. The other two had no end anchor at all —
`gr-f121cbe4` described `jarvis wo finish wo-XXXXXXXX` and stopped, and `gr-e4127741`
ended on `(\s|$)`. A plain `&&` walked both, no newline involved.

So the defect is one level up. **A learned exemption clears the WHOLE command on the
strength of a claim about PART of it.** The newline is the most alarming way to exploit
that, not the thing itself.

## 3. The two floors

`Rule.clears`, on the `regex` arm only:

- the match must **cover the whole command** (`fullmatch`, after stripping) — a pattern
  describing a prefix says nothing about what was chained after it;
- it never clears a command **containing a newline** — a regex exemption is learned from
  a single-line command and cannot say which line of a script it is about.

These hold for a rule already stored, which matters: all five were in the table, and
nothing re-validates a rule after admission.

Signature exemptions are deliberately untouched. A heredoc *is* multi-line, and clearing
that shape is what the structural path exists for; it re-derives the shape at clearance
time and already refuses executable position and any chain containing an executor.

## 4. The same two claims, at learn time

`validate_pattern` rejects a pattern failing either property, and `propose_exemption`
refuses to learn *any* regex from a multi-line command — it falls through to the
structural signature, which is the path that case always wanted.

Both places, on purpose. The floor in §3 stops an unsound rule doing harm; this stops it
entering the base at all, where a human reads it in `jarvis gate rules` and believes it.

The newline test is **probed, not parsed**: the pattern is run against the command with a
gated command appended below it. Reading a reviewer's regex for the defect means
reimplementing the regex engine, and the engine is right here.

## 5. Why the canary report was green over an open gate

`jarvis gate rules` ends with `✓ every command that must gate still gates`. That was
false, and not because the check was wrong: every canary was a **single line**, so the
set could not express the shape `gr-391ba702` cleared. A green report was evidence about
single-line commands only.

Each gate kind now has a multi-line canary — a reader, a newline, the canonical gated
command. `test_the_multi_line_canary_report_fails_on_the_rule_that_walked_the_gate` pins
that the set is falsifiable: with the historical rule reinstalled and the §3 floor
removed, the release canary goes red.

## 6. What this costs, and what it does not fix

Learned exemptions get narrower, so some read-only commands re-gate and cost a Neo
review. That is the direction the work order asked for and the user confirmed.

It also pushes the wrong way on `bl-9f2714ad`, which is the same generator seen from the
false-negative side: exemptions over-fit the one string they were dismissed from.
`fullmatch` makes that stricter, not looser. The two wants doing together, and the fix
there is the one that backlog item already names — derive from the *shape*, so the
exemption describes a family and the whole-command rule costs nothing.

Not addressed here: the recogniser `('release', r'shipit')` still cannot tell the release
script from the file that tests it. Also `bl-9f2714ad`.
