# Jarvis brand system — "control room at dusk"

The single source of truth for every Jarvis surface: the web UI, the promo videos,
future social content. If a pixel ships anywhere, it derives from this file.
The identity: **a calm operations room after dark** — the fleet works, the room is
quiet, and the only warm light is the thing that needs you.

## Palette

| Token | Hex | Role — never mix roles |
|---|---|---|
| `--bg` | `#0e1420` | the room. Every frame starts and ends on this |
| `--surface` | `#151d2c` | panels, cards |
| `--raised` | `#1b2437` | controls, chips |
| `--line` | `#253049` | hairlines, rails, routes at rest |
| `--ink` | `#d9e0ec` | primary text |
| `--ink-2` | `#93a3ba` | secondary text |
| `--ink-3` | `#5f6f88` | labels, captions |
| `--amber` | `#f2a33c` | **needs-you. Reserved.** Attention, escalations, the caret |
| `--amber-ink` | `#ffd9a0` | text on amber contexts |
| `--cyan` | `#56c8e8` | active flow: running work, data in motion, links |
| `--green` | `#46d69a` | settled-good: completed, all quiet |
| `--red` | `#e85d5d` | settled-bad: failed. Rare by design |

Amber discipline is the brand: **amber appears only when something needs the human.**
A frame full of amber is a lie; a frame with one amber element is a signal.

## Typography

- **Mono carries identity**: wordmark, commands, ids, statuses, labels —
  `ui-monospace, "JetBrains Mono", Menlo, Consolas, monospace`, generous
  letter-spacing (0.14–0.18em) on uppercase labels.
- **Sans carries prose**: `system-ui, -apple-system, "Segoe UI", sans-serif`.
- The wordmark is always `> JARVIS` — mono, bold, `>` in amber, letters in ink.
  The caret may blink (1.1s steps); the letters never animate.

## Motion language (video + future UI motion)

1. **Data is light moving along rails.** Messages, work orders, and answers are
   small glowing pulses (cyan) traveling along `--line` routes. Routes light up
   `--cyan` at 40% while a pulse rides them, then cool back to `--line`.
2. **Agents ignite.** A spawned worker is a node that flickers on like a monitor:
   0→bright overshoot→settle (about 400ms). Stopping is a clean fade, no explosion.
3. **Attention arrives as a strip, not a flash.** Amber slides/fades in as a band;
   it never blinks, shakes, or pulses faster than 2.4s.
4. **Resolution is exhale.** Amber → green transitions take ~600ms ease-out.
   The "all quiet" state is the emotional payoff of every sequence.
5. **Camera moves are slow and singular.** One Ken Burns move per shot
   (scale 1.00→1.06 or a gentle pan), never both fast. No whip cuts; scenes fade
   through `--bg` (0.4s out, 0.4s in).
6. Easing: `cubic-bezier(0.22, 1, 0.36, 1)` (ease-out-quint feel) for arrivals,
   linear for pulses on rails.

## Voice & copy

Lowercase-calm, verbs first, no exclamation marks. The product does the bragging
via real screens. Taglines rotate around the same promise:
"route the work. keep the context. own your attention."
Screen text must be readable muted — social video plays silent by default, so
every beat carries an on-screen caption (mono, `--ink-2`, bottom-left).

## Music direction

**Deep house, ~124 BPM** (matched against the reference promo the user liked):
round four-on-the-floor kick (≈100ms decay), offbeat sub bass, dark EP stabs,
tiny rim/shaker percussion, and layers that keep *stacking* — pluck arp joins
at ~22s, a 16th sparkle at ~38s — so energy builds rather than plateaus.
Mix is dense and hot (≈ −12 dB mean via gentle tanh drive), momentum without
drama: no risers, no drops, no vocal chops. The arrangement follows the story:
intro → groove in while the OS works → rhythm section out on the all-quiet
payoff (the room exhales) → settle home. `music.py::DEFAULT_STYLE = "deephouse"`
is the canonical build; four alternate style builders remain in the file as raw
material. Regenerate; don't swap in licensed music without updating this file.

**Signature audio device — keystroke foley.** Every command typed on screen is
heard. A key press is *noise, not a tone*, and specifically a warm mechanical
*thock*: spectral centroid ~650 Hz, energy concentrated 200–1500 Hz, a small
dark transient and a low finger-knock, decay tight (a few ms) — no high fizz.
Space is duller and lower; return is a deeper, louder clack. Keys ride close to
the music's level (a foreground element, not an easter egg) and stay ≥55ms
apart so each is a distinct press, never a buzz. On-screen typing runs at
~65ms/char to match. `promo/sfx.py` derives the click times from the SAME
timeline that renders the frames, so audio and pixels cannot drift. Any future
content that types text on screen must carry this layer.

## Video grammar (all promotional cuts)

- 1920×1080, 30fps, H.264 + AAC. Master scene timing lives in `promo/render.py`.
- Two shot types, alternated: **cinematic** (HTML motion graphics obeying the
  motion language) and **gameplay** (real product screenshots from a real run —
  never mockups; seed realistic data and photograph the actual UI).
- Every gameplay shot shows the real command that caused it as a terminal chip
  (`$ jarvis wo create …`) — the CLI is the OS, the video says so visually.
- End card: wordmark, tagline, repo URL, ≥ 4s hold.

## Regenerating / extending content

```bash
uv sync --extra dev && uv run playwright install chromium
uv run python promo/render.py          # → promo/out/jarvis-os-60s.mp4
```

`promo/README.md` documents the pipeline (seed fixture → real screenshots →
seek-deterministic HTML scenes → frames → ffmpeg). New videos: copy a scene HTML,
keep `window.seek(t)` deterministic, register it in the timeline in `render.py`,
and stay inside this file's palette, motion, and music rules.
