# Vendored: the `caveman` skill

`SKILL.md` is a verbatim copy of `skills/caveman/SKILL.md` from
<https://github.com/JuliusBrussee/caveman> at commit `2f49f0e` (2026-08-21). MIT — the
repository is split-licensed (MIT plus BSL-1.1 for the compression engine), and
`skills/` is on the MIT side per its `LICENSING.md`. That licence sits beside this file.

Unlike `i-have-adhd`, this one ships **unmodified**: upstream already declares no
`disable-model-invocation`, and its description already auto-triggers on a request for
token efficiency, so a headless worker can load it without any adaptation. Do not edit it
in place — the point of a verbatim copy is that a later reader can diff it against
upstream. Refresh by copying the upstream file over `SKILL.md` and updating the commit
above.

Why it ships alongside `i-have-adhd` rather than instead of it: caveman's own
`## Boundaries` section excludes everything persisted outside the chat — code, comments,
commits, docs, PR and issue text — so it compresses what a session *says* and says
nothing about what it *writes*. See
`docs/superpowers/specs/2026-08-22-agent-concision.md` SS4.
