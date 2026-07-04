"""Keystroke foley for the Jarvis promo — the brand's signature audio device.

Every command typed on screen is heard. Each key is a *thock*: a short burst of
band-limited noise (a bright 3ms transient, a mid-band body around 1–2 kHz, and
a tiny low bump — the finger landing), not a tone. Space is duller and lower;
return is a deeper, longer clack. All parameters micro-vary per key from a
seeded RNG, so the take is deterministic yet never machine-gun identical.

Click times are derived from the SAME timeline and the SAME typing math the
scenes use (`render.py::timeline`, `scene.js::typeInto`), so audio and pixels
cannot drift.

    uv run python promo/sfx.py   # → promo/out/sfx.wav
"""

from __future__ import annotations

import math
import random
from array import array
from pathlib import Path

from music import DUR, N, SR, write  # same session length / writer

OUT = Path(__file__).parent / "out" / "sfx.wav"


# -- replicate the scenes' typing math (scene.js) ------------------------------

def _seg(t, t0, dur):
    return min(1.0, max(0.0, (t - t0) / dur))


def _ease_out(p):
    return 1 - (1 - p) ** 5


def _increment_times(n_of_t, span, chars):
    """Sample n(t) at 1ms and return the char index → time of each increment."""
    events, last = [], 0
    t = span[0]
    while t <= span[1]:
        n = n_of_t(t)
        while last < n:
            events.append((last, t))   # (char index, time it appears)
            last += 1
        if last >= chars:
            break
        t += 0.001
    return events


def key_events() -> list[tuple[float, str, str]]:
    """(absolute time, kind, char) for every on-screen keystroke.

    kinds: 'key' | 'space' | 'return'
    """
    from render import timeline  # single source of truth for scene offsets

    events: list[tuple[float, str, str]] = []
    offset = 0.0
    for name, dur, params in timeline():
        if name == "title.html":
            # scene.js: round(len * easeOut(seg(t, 0.7, 1.2))) over "JARVIS"
            wm = "JARVIS"
            evs = _increment_times(
                lambda t: round(len(wm) * _ease_out(_seg(t, 0.7, 1.2))),
                (0.6, 2.2), len(wm))
            for idx, t in evs:
                events.append((offset + t, "key", wm[idx]))
        elif name == "showcase.html":
            cmd = params.get("cmd", "")
            if cmd:
                d = min(1.8, len(cmd) * 0.045)
                evs = _increment_times(
                    lambda t, D=d, L=len(cmd): round(L * _seg(t, 1.1, D)),
                    (1.0, 1.2 + d), len(cmd))
                for idx, t in evs:
                    ch = cmd[idx]
                    events.append((offset + t, "space" if ch == " " else "key", ch))
                if evs:
                    events.append((offset + evs[-1][1] + 0.14, "return", "\n"))
        offset += dur
    return events


# -- synthesis: a key press is noise, not a tone ----------------------------------

def _burst(buf, start, dur, amp, lp, hp, tau, pan, rng):
    """Band-limited noise burst: white noise → one-pole LP → one-pole HP → decay."""
    klp = 1 - math.exp(-2 * math.pi * lp / SR)
    khp = 1 - math.exp(-2 * math.pi * hp / SR)
    lp1 = hp1 = 0.0
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    gl = min(1.0, 1.0 - pan)
    gr = min(1.0, 1.0 + pan)
    for i in range(i0, i1):
        t = i / SR - start
        x = rng.uniform(-1, 1)
        lp1 += klp * (x - lp1)
        hp1 += khp * (lp1 - hp1)
        env = math.exp(-t / tau) * min(1.0, (dur - t) / 0.004)
        v = amp * env * (lp1 - hp1)
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def _bump(buf, start, freq, amp, dur=0.016):
    """The finger landing: one tiny damped low sine, both channels."""
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    for i in range(i0, i1):
        t = i / SR - start
        v = amp * math.exp(-t / 0.006) * math.sin(2 * math.pi * freq * t)
        buf[2 * i] += v
        buf[2 * i + 1] += v


def _key(buf, t, rng, kind):
    pan = rng.uniform(-0.35, 0.35)
    v = rng.uniform(0.82, 1.18)          # per-key micro variation
    if kind == "return":
        # deeper, longer clack — the satisfying end of a command
        _burst(buf, t, 0.010, 0.50, lp=6500, hp=1800, tau=0.003, pan=0.0, rng=rng)
        _burst(buf, t, 0.075, 0.62, lp=1300 * v, hp=350, tau=0.020, pan=0.0, rng=rng)
        _bump(buf, t, 115, 0.30, dur=0.024)
    elif kind == "space":
        _burst(buf, t, 0.006, 0.22 * v, lp=5200, hp=1600, tau=0.002, pan=pan, rng=rng)
        _burst(buf, t, 0.042, 0.30 * v, lp=1100 * v, hp=380, tau=0.012, pan=pan, rng=rng)
        _bump(buf, t, 130, 0.13 * v)
    else:
        # bright 3ms transient + mid body ~1–2 kHz + low bump
        _burst(buf, t, 0.005, 0.30 * v, lp=9000, hp=3200, tau=0.0016, pan=pan, rng=rng)
        _burst(buf, t, 0.034, 0.34 * v, lp=2100 * v, hp=750, tau=0.009, pan=pan, rng=rng)
        _bump(buf, t, 170 * v, 0.10 * v)


def build(min_gap: float = 0.034) -> array:
    buf = array("d", bytes(8 * 2 * N))
    last_t = -1.0
    for k, (t, kind, _ch) in enumerate(key_events()):
        if t >= DUR:
            continue
        if kind != "return" and t - last_t < min_gap:
            continue  # cap density: fast typing, not a buzz
        _key(buf, t, random.Random(k * 7919 + 13), kind)
        last_t = t
    return buf


if __name__ == "__main__":
    write(build(), out=OUT, headroom=0.42)   # foley sits under the music
    print(f"sfx → {OUT} ({len(key_events())} keystrokes)")
