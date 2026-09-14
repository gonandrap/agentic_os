# Filing a gate request is never itself a gated action

Issue #233. Gate 123 (wo-b304c02a, PR 223, 2026-09-13) held the command that was making
the case for gate 124, and recorded that the worker had gone round the gate.

## 1. What went wrong

Three defects, one shape. A worker writes its justification with `cat > /tmp/why.txt
<<'EOF'`, then files the request from `python - <<'PY'`, because the case runs to several
paragraphs. The `pr_merge` recogniser fires on the action named inside that prose.

**The body belonged to nobody.** A heredoc body sits on its own lines, and a newline is a
list separator, so `list_segments` split every body off into a segment with no argv0.
`read_only_mentions` then asked "can the command holding this literal only read it?" of a
line of English, got no, and gated. The classifier's *stated* reason was wrong too, and
differently: `shape_of` reported the body as owned by the `python` further down the chain,
which never reads it.

**The carve-out sat over a hole.** `scannable` blanks quoted spans when no shell invoker
is present, and python is not one — so every string literal inside a `python -` heredoc
was invisible to every recogniser. The brief's correct case (a filing that gates nothing)
was ungated by accident rather than by anything establishing it was paperwork, and
`python - <<PY / os.system("gh pr merge 223 --squash") / PY` matched nothing at all.

**The record accused a worker of the one thing gates exist to catch.** `gates.file_request`
was handed a single placeholder — "the worker ran the command directly rather than filing
a request" — whatever the blocked command was. On gate 123 the blocked command *was* the
filing.

## 2. Own the heredoc

`owned_spans` is the whole of it: one span per heredoc, from the newline ENDING its opener
line to the end of its terminator line. `_inert` blanks that span including its newlines,
so opener, body and terminator arrive as one segment belonging to the command that reads
them. `_list_inert` is gone — there is one inert form now, because the reason the two
differed was the misattribution.

Two things paid for that ownership, and both are pinned:

- `reads_only` now refuses any command containing an **unterminated** heredoc. Merging the
  body into its owner's segment would otherwise make `cat <<EOF … EOF && gh pr merge 223`
  a single `cat` — a reader — and clear the merge the body swallowed (kn-67364b3a).
- `Shape.exemptible` is False when the owner **hands off** what it was given. A signature
  is `{position, owner}` and nothing else, so a dismissal of `cat > /tmp/s.sh <<EOF` would
  otherwise mint a standing rule reading "a heredoc body owned by `cat` is prose".

## 3. Paperwork survives an interpreter

`files_a_claim(segment)` is the segment-level property: the segment does nothing but
RECORD or READ a claim about a command. Two routes to it — `gate_paperwork` (the chain is
`jarvis gate <verb>`) and `interpreter_paperwork` (an interpreter whose whole program is).

`mentions_only` is `read_only_mentions` widened by that one segment kind, in the same loop
so the safety argument stays in one place: every segment naming the literal must either
only read it or only file a claim about it, and a reader that WRITES what it read puts the
whole chain back under the same test.

The distinction `interpreter_paperwork` has to establish is **argument versus execution**.
`subprocess.run(["jarvis","gate","request", …, "gh pr merge 223"])` passes the merge to a
filing; `os.system("gh pr merge 223")` runs it; nothing a regex can see tells them apart.
So it reads the AST, and answers False for every program it cannot read:

- python only, the program from the heredoc body and nowhere else — `-c`, `-m` and a
  script path are all refused, and so is a redirection;
- one heredoc, terminated, nothing else in the pipeline;
- **the body must reach python as written** — see §3.1, which is what review round 1 found
  missing and is the premise everything below rests on;
- every call either a `subprocess` entry point given an argv LIST whose first three
  elements are literally `jarvis gate <paperwork verb>`, or an inert call. `shell=True`,
  `executable=`, `**kwargs`, `os.system`, `exec`, an `open` with a mode: all refused;
- "inert" is judged on the RECEIVER as well as the name. A method on a value the program
  built (`open(p).read().strip()`) is inert; a module-qualified call must be named in full
  in `_PY_INERT_QUALIFIED`, which has one entry. Matching the attribute alone made
  `pickle.loads(open("/tmp/p").read().encode())` inert — arbitrary code execution scored
  as paperwork. `loads` and `dumps` left with it;
- no name this calls inert may be REBOUND: no `import … as`, no `from … import`, no
  assignment to a name in `_PY_INERT_FUNCS` at all, and — the rule that does the real work
  — **no reference to an executor anywhere**, see §3.2;
- the grammar itself is an allow-list (`_PY_NODES`). A loop, a function, a `with` or a
  comprehension is a program this cannot read, and a program it cannot read is not one it
  may clear.

Widening any of that is a change to a security boundary, not a convenience.

### 3.1 The body the AST reads must be the body python runs

The shell rewrites an UNQUOTED heredoc body before python sees a line of it, so
`python - <<PY` carrying `"$(gh pr merge 223 --squash)"` had already merged by the time
the first version read a program that merely quoted a string. Verified against real bash
rather than from memory (kn-67364b3a): with `<<PY`, `$W` expanded and `$(echo IT-RAN)`
ran; with `<<'PY'` both arrived verbatim; a body carrying none of `` $ ` \ `` was passed
through untouched; `<<-` stripped leading tabs.

So the body may be read as a program on either of two grounds: the delimiter is QUOTED, or
the body contains none of `_EXPANDS` — `` $ ``, a backtick, a backslash. `<<-` is refused
outright.

The second ground is not a softening of the first, and the review asked for the first
alone. Issue #233's reduced repro is written `python - <<PY`: refusing an unquoted
delimiter outright would re-gate the exact command this work order exists to clear, for a
body the shell demonstrably does not touch. `_EXPANDS` states the property that actually
matters; the delimiter is a proxy for it.

`files_a_claim` also applies the raw-string `_SUBSTITUTION` guard itself, repeating what
both branches already do. It is the predicate `_mentions_only` consults BEFORE
`reads_only`, so a missing guard there clears a segment outright rather than passing it to
something stricter.

### 3.2 The rule is about references, not calls

Guarding the SHAPE of an assignment's right-hand side stops `print = os.system` and
nothing else. `print = [os.system][0]` and `print = dict(s=os.system)["s"]` bind the same
callable through a container — every node is in the grammar, the container scores inert,
and the call site then reads a name it trusts while running a merge (review round 2).

So what is refused is the REFERENCE. Which names are modules comes from the program's own
`import` list, and a module reference is allowed in exactly two places: as the callee of a
call `_py_call` accepts, and as a name in `_PY_INERT_QUALIFIED`. Anywhere else — inside a
list, a `dict()` keyword, a subscript, a call argument — it refuses. A bare module name
outside an allowed attribute refuses too, which closes `m = [os][0]`. Allowed references
are held by node identity, so one `os.system` being in a checked position says nothing
about another.

The rule has to stop at MODULE references, and that is the negative half: `r.returncode`
is a value read off a local, not a way to run anything, and refusing it would refuse the
production filing shape this exists to clear.

A reference that is never syntactically called is still a reference something else can
call. That is the general form, and kn-a3db2914 — written after round 1, one round before
round 2 found this — already stated it: wherever a check trusts a NAME, enumerate every
way that name can come to mean something else.

### Why the gate had to widen first

Per Neo's ruling on question 281: a heredoc body owned by an interpreter is now scanned
WHOLE — `scannable` does not blank quotes inside one, because the shell never sees them
and a string literal in a python program is part of the program. Without that, the
carve-out above would sit over a hole rather than over a gate, and its central sentence —
"the only thing that would run this literal is `jarvis gate request`" — would be
untestable for every python body, none of which reached a recogniser at all.

The cost is accepted: a heredoc that merely quotes a gated literal inside python now
gates. kn-96791d60 already tells workers those bodies are programs and to use the Write
tool instead.

## 4. Never record an accusation the classifier cannot support

`gates.no_case_justification(command, prior_id)` replaces the single placeholder with what
is actually established:

| The command | What the record says |
|---|---|
| files a gate request somewhere in the chain | it FILES a request, so the OS has **not** established that the worker ran the action rather than asking |
| merely names `jarvis gate` | the OS cannot tell which it was, and has established neither |
| neither, but a request for this kind is already on the work order | points at that request, where the case will be |
| neither | the original text — and here it is true |

Note what the first row does not say. A chain may both file a request and run the action
(`gh pr merge … && jarvis gate request …`), so the exculpation stops at "not established":
a record that clears a worker it cannot vouch for is the same defect pointing the other
way. `is_no_case` keys the readers on a prefix, so a case still REPLACES a placeholder
rather than landing under it (issue 185).

## 5. Not a dismissal case

`gate explain` on the failing command ended "a dismissal of this could NOT be generalised
— its own command executes via python". That was correct, and it is why this false
positive could not be worked down the way the ordinary ones are. No dismissal rule for
this shape was added.
