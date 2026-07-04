# ASSUMPTIONS — Jarvis OS build

Every decision made autonomously while building the OS. Review each; mark ✅ accepted /
❌ change (with a note) and I'll adjust. Ordered by impact.

## A. Architecture

1. **The per-project "orchestrator background agent" is a deterministic daemon, not a
   Claude agent.** Your spec said each project bootstraps a background *agent* that
   waits for work and spawns workers. Since that role needs zero intelligence (poll DB,
   spawn, track), implementing it as an LLM session would burn tokens idling and be less
   reliable. jarvisd runs one poller per project and behaves exactly as specified
   (waits for work orders, spawns worker agents, never does the work itself). Workers
   ARE real Claude agents. If you want a literal Claude orchestrator per project, the
   dispatch layer is abstracted so it can be swapped.

2. **Workers are native `claude --bg` background sessions** (not SDK processes), so they
   show up in your agents view, are supervised by Claude's own daemon, survive jarvisd
   restarts, and you can open/chat with them natively. Naming convention
   `[WO <id>] <title>` marks framework-managed sessions in the agents view; unmanaged bg
   sessions found in a project are auto-registered as `adhoc` shadow work orders so the
   UI shows them with a warning badge.

3. **Feedback routing** (`jarvis wo send`, UI): messages queue in the project DB and,
   once the worker's session is idle, jarvisd delivers them by dispatching a NEW
   background agent that resumes the worker's conversation
   (`claude --bg --resume <session-id> "<msg>"`). Answer to your question #9: **yes —
   verified live**: full context carries over (fork semantics under a fresh session
   id, which the SessionStart hook rebinds to the work order), the feedback turn shows
   up in the agents view like any worker, and the reply is captured back into the DB.
   Constraints discovered: plain `--resume -p` refuses sessions owned by a live bg
   agent (kept only as fallback, preceded by `claude stop`), and mid-turn injection
   isn't supported — messages wait until the worker goes idle. For interrupting a
   worker *while it runs*, use the native agents view (third interaction path you
   listed).

   **Known cost consideration (revisit if it hurts):** every feedback delivery starts
   a new API turn that resends the worker's full conversation. Prompt-cache reuse is
   prefix-based on content (not session id), so the fork per se doesn't break it, but
   the Anthropic cache TTL is ~5 minutes — a worker idle longer than that reprocesses
   all its context tokens on delivery, uncached. Acceptable for MVP (deliveries are
   occasional and workers are short-lived); if work orders grow long-context and
   feedback becomes chatty, revisit — options include delivering within the cache
   window when possible, a ManagedBackend that keeps the worker process alive with
   streaming stdin (no re-read), or summarize-and-restart instead of resume.

4. **One central DB + one DB per project.** Work orders/events/messages/assumptions live
   in `<project>/.jarvis/jarvis.db` (per your #3, gitignored). Anything that must be
   unified — notification inbox, backlog, knowledge, project registry — lives centrally
   in `$JARVIS_HOME/os.db` (default `~/.jarvis`). Rationale: your #12–14 explicitly ask
   for unified handling of those.

5. **Jarvis-the-conversation is a Claude session in this repo** (CLAUDE.md persona)
   using the `jarvis` CLI; the CLI/daemon is the actual OS. From your phone you open a
   session in `agentic_os` and you're talking to Jarvis. Proactive pings reach you via
   notification sinks (Telegram MVP) rather than injecting into an idle chat, to avoid
   context pollution — the persona surfaces the inbox at the start of each turn instead.

## B. Technology

6. **Python 3.11+, stdlib-first.** Core CLI has zero runtime deps (argparse + sqlite3);
   the web UI is an optional extra (`pip install jarvis-os[ui]` → FastAPI + uvicorn +
   Jinja2). Packaged with pyproject/uv. Chosen for reproducibility (OSS goal) and
   because your stack is Python-heavy.

7. **SQLite in WAL mode** for both DBs (your suggestion; concurrent daemon/UI/CLI reads
   are fine at this scale).

8. **Web UI is server-rendered FastAPI + htmx** (no node build step) bound to
   127.0.0.1, no auth in MVP. Access from phone = via the Jarvis persona, not the UI.

## C. Behavior & policy

9. **Default worker permission mode is `acceptEdits`** (not `bypassPermissions`) — safe
   default for an OSS project; your catalog can set per-project
   `worker.permission_mode` (e.g., auto/bypass for sandboxed projects). Blocked
   permission prompts surface as `needs_attention`.

10. **Per-project concurrency limit = 2 simultaneous work orders** (catalog-tunable).

11. **Work orders don't auto-merge anything.** Workers work in worktree `wo-<id>`,
    commit, push, open PRs per each repo's conventions; OPERATION.md instructs them.
    Requirement #6 (new worktree per work order) is satisfied via Claude's native
    `--worktree` flag (worktrees land in `<project>/.claude/worktrees/`).

12. **Assumptions workflow:** workers run `jarvis wo assume <id> "text"`, which appends
    to the project's `ASSUMPTIONS.md` *and* records a DB row; any pending assumption
    flips the WO to `needs_review` so both `jarvis status` and the UI dashboard flag it.
    You accept/reject from the UI or `jarvis wo review`.

13. **Backlog is central with cross-item dependencies** (`depends_on`); promoting an
    item with unfinished deps warns and requires `--force`. Workers are instructed to
    file leftovers there rather than leaving "future work" notes in chat.

14. **Knowledge base MVP is plain text rows** (project, topic, tags, content) injected
    into new worker prompts by recency (project-specific + global, top 8). No
    embeddings/retrieval yet — flagged post-MVP as you suggested (#13 "could be post
    MVP").

15. **Settings injection owns `<project>/.claude/settings.json`.** Original is backed up
    once to `settings.json.pre-jarvis`; a `_jarvis` marker detects manual drift (start
    warns; `--force-config` reapplies). `settings.local.json` remains user-owned.
    Project-specific needs (e.g., auto_heycrypto's credential-guard hooks) must be
    declared in the catalog `settings_overrides` — I ported them there in
    `catalogs/gonzalo.json` so nothing is lost.

16. **Notification sinks:** `log` always; `telegram` enabled when env vars
    (`JARVIS_TELEGRAM_TOKEN`, `JARVIS_TELEGRAM_CHAT_ID` by default) are set; `desktop`
    (notify-send) optional. Existing project telegram scripts keep working until each
    project is migrated to call `jarvis notify` (migration guide included).

## C2. Decisions forced by real-world verification (found during live e2e)

24. **Worker settings travel as a file, not via the project's `.claude/settings.json`.**
    Verified live: a `--bg --worktree` session runs in a fresh worktree checkout where
    the (deliberately untracked) injected settings file doesn't exist — so hooks and
    permissions silently didn't load and the worker blocked on its first `jarvis` call.
    Dispatch now writes the merged settings (project settings + per-WO env) to
    `.jarvis/worker-settings/<wo-id>.json` and passes `--settings <file>`. The
    project-level injected settings remain for interactive sessions in the repo.

25. **Injected hooks call jarvis by absolute path** (`shutil.which` at injection time),
    and **workers get PATH + JARVIS_HOME injected**, because the Claude supervisor
    daemon's environment doesn't necessarily include wherever jarvis is installed.

26. **The OS baseline allows `Bash(jarvis *)`** in every managed project — workers
    must be able to execute the contract commands without permission prompts.

27. **Workers may edit freely inside their own worktree, and nowhere else.** Verified
    live: `acceptEdits` still prompted for a background worker's `Write`, which would
    stall every unattended work order. The injected PreToolUse hook auto-allows
    Edit/Write/NotebookEdit only when (a) the session is a Jarvis worker
    (`JARVIS_WO_ID` set) and (b) the target path resolves inside the session's
    worktree. Interactive sessions and out-of-worktree writes still prompt normally.
    The `cd X && jarvis …` pattern is likewise auto-allowed only for pure cd/jarvis
    chains with no other shell constructs.

28. **Worker → OS plumbing confirmed end to end on the real CLI** (8 verification
    rounds with a haiku worker on a fixture repo): dispatch → worktree → hooks fire →
    `jarvis wo assume`/`finish` from inside the worker → needs_review → `jarvis wo
    review` → completed; feedback message delivered into the session and the reply
    captured; blocked workers surface as `waiting_input` + attention + notification;
    dead sessions detected and failed by the reconciler.

28b. **Workspaces must be trusted by Claude Code** — untrusted workspaces silently
    ignore `permissions.allow` (verified live; the CLI error names the fix). `jarvis
    start`/`adopt` now warn per project with the exact remedy (open `claude` there
    once, or set `hasTrustDialogAccepted` in ~/.claude.json). Your existing projects
    are presumably fine; fresh clones need one interactive open.

## D. Migration

17. **I did not modify any of your real projects.** `jarvis adopt` + `catalogs/
    gonzalo.json` are ready, `MIGRATION.md` gives the order (shared_schedule →
    tesis_grado → rest), but running adoption on real repos is left for you (one
    command each) since it writes to their working copies.

18. **vpn-setup needs `git init` first**; adopt detects and instructs rather than
    auto-initializing a repo you may want structured differently. It also has no
    README.md — adopt generates a stub from its INSTALL_STEPS.md headline for you to
    edit.

19. **auto_heycrypto's monitor daemon keeps its own pipeline for now.** Rerouting its
    production alerts through `jarvis notify` is a one-line change in
    `scripts/notify_telegram.sh` documented in MIGRATION.md, deliberately last in the
    rollout (production trading system — you flip it when you trust the OS pipeline).

## D2. UI design choices

29. **Dark-only "control room at dusk" console**: blue-slate dark background, amber
    reserved for needs-you signals, cyan for active, green/red for outcomes; statuses
    always icon + word (never color alone). The signature element is the attention
    strip at the top — amber band listing exactly what needs you, collapsing to a
    one-line green "all quiet" when nothing does.
30. **Zero-JS, no CDNs, no build step**: server-rendered Jinja + `<meta refresh>` on
    the dashboard (15 s). Deliberate for reproducibility/self-containment; htmx or
    websockets can come later without changing routes.

## F. Neo — the OS answerer agent

31. **Neo is a sequence of headless calls with a byte-stable prefix, not a long-lived
    session.** Each answer is one `claude -p` call whose system prompt (persona +
    learnings) is identical across questions; the question rides in the user message
    after it. The queue drains FIFO and back-to-back on one thread, so every call
    after the first hits the Anthropic prompt cache (~5-min TTL) on the shared
    prefix. A single resumed Neo session was rejected: its context would grow with
    every answer, costing more per question over time and eventually needing
    compaction — the stateless design has constant cost and gets cache reuse anyway.

32. **Learnings render append-only (oldest first).** New feedback extends Neo's
    prompt prefix instead of rewriting it, so cached prefix bytes stay valid. Over
    the limit (default 50, catalog-tunable), the newest N win (a one-time prefix
    shift per overflow). Learnings live in Neo's OWN db (`$JARVIS_HOME/neo.db`)
    alongside the question queue and reviews, per your spec.

33. **Question intake is explicit: workers run `jarvis wo ask <id> "…"` and end
    their turn.** The contract tells them to prefer `wo assume` + continue for
    reversible decisions and reserve `wo ask` for real blockers. Permission-prompt
    blocks (Claude's own dialogs) still go to you — Neo can't answer those, only
    real project questions. The answer arrives as the worker's next user turn via
    the existing delivery path, prefixed `[Neo, answering for the user]` so the
    session transcript is honest about who spoke.

34. **`wo ask` parks the work order as waiting_input WITHOUT flagging your
    attention.** Neo exists to absorb these; only its escalations reach the amber
    strip. Unreviewed answers show as a count on the neo tab (and `jarvis status`),
    deliberately quieter than attention items.

35. **Neo escalates rather than guesses** on: production systems / live credentials,
    spending money, deleting or publishing anything, legal/people matters, or a
    preference it has no learning for. Escalations create an inbox item + work-order
    attention; you answer with `jarvis neo answer <qid> "…"` (or the UI form), which
    delivers to the worker through the same path. Unparseable Neo output is treated
    as an escalation — garbage is never delivered to a worker.

36. **Neo's default model is `opus`** (catalog `os.neo.model`), enabled by default.
    Rationale: Neo's answers steer worker-hours; a wrong cheap answer costs more
    than an expensive right one. Calls are short and mostly cached.

37. **The review loop is the training signal.** Every Neo answer sits `unreviewed`
    until you approve or correct it (UI neo tab or `jarvis neo review`). A
    correction (a) records a learning in Neo's DB — injected into all future
    answers — and (b) is forwarded to the worker as guidance when the work order is
    still open. Approvals just confirm. You can also teach Neo directly
    (`jarvis neo learn`). User-authored answers to escalations are auto-approved.

## G. Evals & CI quality gates

38. **Evals are split into two layers.** Deterministic behavioral evals
    (`evals/test_*.py`, 32 scenarios) run the real OS against the fake `claude` CLI
    and gate every merge in CI: status truthfulness (flag exactly what needs you —
    no misses, no false alarms), routing, feedback delivery, safety rails, Neo's
    no-question-lost / escalation-surfacing / token-economics / learning-loop
    guarantees. LLM-graded evals (`evals/llm/`, 14 scenarios) run the REAL model
    against Neo's persona (escalation recall ≥ 7/8, answer willingness ≥ 6/8,
    learning adherence) and the Jarvis persona (route-don't-do battery). The LLM
    layer is opt-in (`JARVIS_EVALS_LLM=1`, default model sonnet via
    `JARVIS_EVALS_MODEL`) because it spends tokens and needs a logged-in Claude
    Code — CI can't have either, so CI gates on the deterministic layer only. Run
    the LLM layer manually before changing any persona text.

39. **Deterministic evals deliberately overlap unit tests** but assert on
    user-visible surfaces (status output, attention items, queued messages, inbox)
    and print a per-category scorecard (`evals/results.json`). They are the
    regression contract for agent behavior; unit tests are the contract for code.

40. **The eval batteries already caught and fixed three real gaps** (kept as
    regression scenarios): failed workers didn't surface in `jarvis status`
    attention; answering an escalated Neo question left the work order stuck in
    the attention list; the Jarvis persona ignored stated user preferences
    (13/14 → fixed CLAUDE.md → verified 14/14 against the real model).

41. **Browser tests** (`tests_browser/`, Playwright + headless Chromium) drive the
    real uvicorn server end to end: dashboard states, attention-strip review flow,
    work-order forms, the full Neo review/escalation cycle, backlog dependency
    errors, inbox ack. They are a separate CI job (`playwright install chromium`
    downloads the browser there).

42. **`main` is protected by a repository ruleset** (`protect-main`, active, no
    bypass actors): merges only through PRs, force-pushes and branch deletion
    blocked, and all five CI checks required (`unit (3.11/3.12/3.13)`, `evals`,
    `browser`). Approving-review count is 0 — requiring 1 approval would deadlock
    a solo maintainer; flip it on when a second person joins.

## H. Promo video & brand system

43. **The brand is the UI's own "control room at dusk", codified.** `brand/BRAND.md`
    freezes palette, typography, motion language (data = light pulses on rails,
    ignition, amber-only-when-needs-you, resolution-as-exhale), voice, and music
    direction. Every future promotional piece derives from it — that's your
    "persist the look and feel + instructions" requirement.

44. **The video is fully generated from source, nothing hand-edited**: cinematic
    scenes are HTML pages with a deterministic `seek(t)` API screenshotted at
    30 fps (Playwright), interleaved with REAL dashboard screenshots from an
    actual seeded OS run (real daemon, real stores, real UI — the fake `claude`
    supervisor stands in for speed/determinism; a screenshot is always the real
    product rendering real DB state, never a mockup). ffmpeg assembles at
    1080p/H.264.

45. **Music is synthesized in-repo (stdlib only)** — 104 BPM warm pad / plucks /
    bass / brushed ticks per the brand's "productivity, not hype" direction —
    because licensed tracks can't ship in an OSS repo and wouldn't be
    reproducible. Swap it for a licensed track at upload time if you prefer;
    BRAND.md notes how.

46. **On-screen captions carry the story** (no voiceover): social feeds default
    to muted playback, and TTS voices undercut the calm-operator brand. Every
    beat is readable silent.

47. **`promo/out/` (video, frames, screenshots, fixture) is gitignored** — large
    binaries don't belong in git history; the pipeline regenerates them
    identically. The rendered master lands at `promo/out/jarvis-os-60s.mp4`.

48. **Soundtrack round 3 is an exploration, not a pick** (feedback: the 104 BPM
    cut felt dramatic; the 122 BPM cut felt same-but-worse and too fast).
    `music.py` now ships five deliberately different directions — lofi 88,
    synthwave 100, acoustic 100, deephouse 112, keynote 92 BPM — rendered as
    five full cuts via `render.py --versions`, all peak-normalized identically
    so loudness doesn't bias the comparison. The winner sets `DEFAULT_STYLE`
    and gets written into BRAND.md; the other builders stay as raw material.

49. **Keystroke foley is noise-based** (feedback: sine clicks didn't sound like
    keys). Each press is a band-limited noise thock (bright transient + mid
    body + low finger bump), micro-varied per key from a seeded RNG. One shared
    `sfx.wav` rides under all five cuts so only the music varies in the A/B.

50. **Rails always render under node boxes and labels now** (`z-index` fix in
    `base.css`, opaque pill behind the Neo queue label) — crossing rail lines
    previously drew over text in the Neo scene ("arrows over letters"). Brand
    rule made explicit: text is never overwritten by motion elements.

51. **Soundtrack round 4: tuned by measurement against the user's reference
    video** (`~/Downloads/agentic-env-promo.mp4`, liked). Signal analysis of
    that file drove every parameter, not taste: 124 BPM four-on-the-floor
    (deephouse retuned from 112 — "a little slow"), kick decay ~97ms, offbeat
    sub, mix density −12 dB mean (added tanh drive), and keystrokes rebuilt as
    warm thocks — measured reference clicks: centroid ~600–800 Hz, energy
    200–1500 Hz, ~1–2ms decay, near-full-scale loudness, ~100ms apart. Ours
    now measures 662 Hz centroid, ≥55ms spacing, foley at 0.70 peak. On-screen
    typing slowed 45→65 ms/char so each visible character is a distinct press.
    If this still misses, the user can pull exact synth params from the session
    that generated the reference.

## E. Scope cuts (MVP)

20. UI has no auth and no websockets (htmx polling refresh).
21. No Windows support yet (Linux/macOS).
22. E2E tests use a fake `claude` shim; a real-CLI smoke test exists behind
    `JARVIS_E2E_REAL=1` (not run in CI to avoid token burn).
23. Cross-project learning *synthesis* (summarizing learnings into curated docs) is
    backlogged; MVP only captures + injects.
