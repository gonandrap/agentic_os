# An exemption may not clear what it does not describe

Status: implemented (wo-551f5e8c). Supersedes nothing; tightens `gate_rules.py`.

## 1. The incident

On 2026-09-12 the release gate — the one whose summary says *this reaches the live
production fleet* — could be walked, and so could `pr_merge`. Five learned exemptions
were affected. All five were authored by Neo, validated by `validate_pattern`,
canary-tested on admission, and listed as sound by `jarvis gate rules`.

**On what this repository does and does not carry.** It is public. The five live patterns
and the command each one cleared are *not* in this repo — not here and not in the tests,
which stand in shape-for-shape patterns over commands nobody ever dismissed
(`DEFECT_SHAPES` in `tests/test_gate_rules.py`, and the comment there says why). Nothing
is lost by that: the floors in §3 are indifferent to what a pattern says, so a stand-in
exercises the same code identically, and the mutation check confirms all five stand-ins
go red without the fix.

The real pairs live in private state — the work-order record, the retraction reasons in
`jarvis gate rules`, and `kn-988d1733`. Publishing them is the user's decision and they
have not made it. An earlier commit on this branch did carry them; see the work order for
what was done about that.

Retracted rule *ids* are kept, here and in the tests. An id is an opaque hash of a row in
private state: it carries no pattern and no command, and it is the only handle by which a
reader with access can find the real record.

## 2. The cause is not the newline

The work order found the first of them and named the newline: a negated character class
that excludes `;`, `&`, `|`, backtick and `$` but not `\n`, in a pattern anchored with
`$` rather than `\Z`. Python's `re` matches `\n` inside a negated class, and without
`re.MULTILINE` the trailing `$` still permits it, so the class swallows every following
line.

That is true of three of the five. The other two had no end anchor at all — one
described a harmless `jarvis wo finish` prefix and stopped, the other ended on `(\s|$)`.
A plain `&&` walked both, no newline involved.

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

## 7. An unterminated heredoc body is not prose

Found in review round 1, by writing the negative half of §3's carve-out. Once a regex may
not be learned from a multi-line command, the structural signature is the **only** route
by which one is ever cleared — so it carries the whole weight, and it had two holes:

- a release chained onto the **terminator line** (`EOF && ./scripts/shipit.sh`): the
  delimiter line is no longer a delimiter line, so the body ran to the end of the string
  and swallowed the release;
- a heredoc whose delimiter simply never appears.

Both were **latent, not live**: exploiting either needs a signature exemption at
`heredoc` position, and every signature rule in the production base is at `quoted`
position (checked 2026-09-12). So this one was a hole in the floor rather than an open
gate — which is also why it is safe to describe here.

`heredoc_spans` now reports whether each body was `terminated`, and `shape_of` returns
`CODE` for any match inside one that was not. This is the module's existing convention —
unrecognised syntax fails the test rather than passing it, as `reads_only` already says
of a name carrying a slash.

One case looks identical and is not, so it has its own test rather than an assertion in
the sweep: appending `&& cat <<'EOF' | bash … EOF` to a heredoc reuses the delimiter, and
the appended block's own `EOF` closes the **outer** heredoc. Everything becomes one commit
message. Verified against real bash — the payload never ran — so clearing it is correct,
and a change that makes it gate is over-gating rather than a fix.
