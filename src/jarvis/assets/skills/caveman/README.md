# Vendored and adapted: the `caveman` skill

`SKILL.md` derives from `skills/caveman/SKILL.md` in
<https://github.com/JuliusBrussee/caveman> at commit `2f49f0e` (2026-08-21). MIT — the
repository is split-licensed (MIT plus BSL-1.1 for the compression engine), and
`skills/` is on the MIT side per its `LICENSING.md`. That licence sits beside this file.

**It is no longer a verbatim copy.** It was one until 2026-09-19. The change and the
reasoning are `docs/superpowers/specs/2026-09-19-concision-enforced.md`; the short
version is that the verbatim copy was invoked **zero times in 142 worker sessions**, and
would have compressed nothing even if it had loaded.

## The diff against upstream, so a reader can still check it

1. **`## Boundaries` replaced.** Upstream returns to normal prose for anything
   "persisted outside chat" — code, comments, commits, docs, issue/PR text, memory
   files. Every surface a Jarvis worker writes is on that list, including the
   work-order record, because the record is written through a CLI call. Under upstream's
   rule the skill excluded itself from its entire job here. The replacement compresses
   all generated output and keeps an explicit uncompressed list for the correctness
   rails (exact errors, numbers, units, negations, security warnings, irreversible-action
   confirmations). Spec SS4 and SS4.2.
2. **Description rewritten.** Upstream triggers on the user saying "caveman mode" or
   asking for token efficiency. A headless `-p` worker has no user and never says
   either, so the trigger described a conversation that cannot happen. It now names the
   Jarvis surfaces and says the style is always on.
3. **Level pinned to `full`.** Upstream lets the user switch level with `/caveman <level>`.
   A worker cannot type a slash command, and the choice is the OS's anyway: `ultra`
   strips conjunctions, which work-order prose full of multi-step sequences cannot
   afford. The intensity table is kept as documentation of the upstream skill.
4. **Off switch removed** for the same reason — the style belongs to the OS, not to the
   session.

Everything else — the compression rules, Auto-Clarity, the examples, the wenyan
modes — is upstream's text untouched. Refreshing from upstream means re-applying the
four changes above, not overwriting the file.

## Why it ships alongside `i-have-adhd`

They do different jobs and both are needed. caveman decides how many words a sentence
costs. `i-have-adhd` decides what order the sentences go in, what is cut entirely, and
that the first line is the answer. Compression without shaping gives a short pile;
shaping without compression is what the September measurements found.

Neither is relied on to load itself. `concision.house_style` carries the operative rules
of both into every turn through the `SessionStart` hook — that is the mechanism; these
files are the source of truth it is written from, and the full reference a worker opens
when it wants the examples.
