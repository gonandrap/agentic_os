# Jarvis promo pipeline

Reproducible promotional video: real product screenshots + brand-locked motion
graphics + a generated soundtrack. Look & feel rules live in `brand/BRAND.md` —
read that first; every future piece of content derives from it.

```bash
uv sync --extra dev && uv run playwright install chromium   # once
uv run python promo/render.py                               # → promo/out/jarvis-os-promo.mp4
uv run python promo/render.py --skip-screens --skip-frames  # audio-only iteration (reuses frames)
```

`promo/out/` is gitignored — artifacts regenerate from source on demand.

## How it works

| Stage | File | What it does |
|---|---|---|
| 1. Gameplay | `capture_screens.py` | Boots the REAL OS over a seeded fixture fleet (fake `claude` supervisor for determinism), serves the real dashboard, photographs it at 2× (dashboard busy/quiet, work-order detail, neo tab, backlog) |
| 2. Cinematics | `scenes/*.html` | Motion-graphics pages. Each exposes `window.seek(t)` — **fully deterministic per t**, no wall-clock animation — and obeys the brand motion language (pulses on rails, ignition, amber discipline, edge fades) |
| 3. Frames | `render.py` | Walks the timeline, screenshots 30 fps per scene into `out/frames/` |
| 4. Music | `music.py` | Stdlib-only synth: 124 BPM deep house per BRAND.md, mapped to the story beats — groove in at 6s, layers stack at 14/22/38s, rhythm out on the all-quiet payoff at 49s. The only audio layer (no keystroke foley — see BRAND.md) |
| 5. Assembly | `render.py` | ffmpeg: frames + track → H.264/AAC 1920×1080 |

## The 73s timeline (edit in `render.py::timeline`)

The first third is the pitch — what Jarvis *is* and where you sit — before any
feature: you create a work order, Jarvis routes it, a Claude session starts
working. Then the feature run.

| t | scene | beat |
|---|---|---|
| 0–6 | `title.html` | wordmark types in; promise line |
| 6–13 | `concept.html` | THE PITCH: you fill the new-work-order form → it rides to Jarvis → routed to the webapp claude session, which ignites |
| 13–19 | `worker.html` | terminal: the claude session picks the WO up — reads, edits, tests pass |
| 19–27 | `fleet.html` | jarvisd dispatches pulses; workers ignite per project |
| 27–34 | `showcase` dashboard_busy | real dashboard; `jarvis wo create` chip |
| 34–41 | `spawn.html` | work order → worktree; hooks/assume/finish audit trail |
| 41–47 | `showcase` wo_detail | real WO page; `jarvis wo send` feedback chip |
| 47–55 | `neo.html` | question queue drains FIFO; one amber escalation to you |
| 55–62 | `showcase` neo_tab | real review UI; corrections teach Neo |
| 62–67 | `showcase` dashboard_quiet | the payoff: ● all quiet |
| 67–73 | `outro.html` | wordmark, tagline, repo URL |

## Making the next video

1. Reuse `showcase.html` for any new gameplay beat — it takes `img`, `cmd`,
   `caption`, `zoom`, `pan`, `dur` as query params.
2. New cinematic: copy a scene, keep everything driven by `seek(t)`, stick to
   `base.css` tokens and the brand motion rules.
3. Add rows to `timeline()`; durations are free-form (the track is 60s — extend
   `music.py::DUR` or regenerate to length).
4. New screenshots: extend `capture_screens.py::seed` — seed realistic state,
   photograph the real UI. Never mock the product.
