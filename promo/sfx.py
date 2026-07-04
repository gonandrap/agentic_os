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
                d = min(2.6, len(cmd) * 0.065)
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


# -- synthesis: a key press is a STRUCK OBJECT, not a noise burst -----------------
#
# Modal synthesis: the keycap/plate/case ring at a few discrete damped
# frequencies. Each press = a 1ms noise exciter + a bank of exponentially
# damped sine modes (higher modes die faster), plus the mechanical signature
# of a real press: the switch click at t and the bottom-out ~4ms later.

def _strike(buf, start, modes, amp, pan, rng, click=0.5, click_lp=5000.0):
    """One hit: short noise exciter + damped modal ring-down."""
    dur = 0.060
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    gl = min(1.0, 1.0 - pan)
    gr = min(1.0, 1.0 + pan)
    # random phase per mode makes each press ring slightly differently
    ph = [rng.uniform(0, 2 * math.pi) for _ in modes]
    klp = 1 - math.exp(-2 * math.pi * click_lp / SR)
    lp1 = 0.0
    for i in range(i0, i1):
        t = i / SR - start
        s = 0.0
        for (f, a, tau), p0 in zip(modes, ph):
            s += a * math.exp(-t / tau) * math.sin(2 * math.pi * f * t + p0)
        if t < 0.0012:                       # the exciter: 1ms of dull noise
            lp1 += klp * (rng.uniform(-1, 1) - lp1)
            s += click * lp1 * (1 - t / 0.0012)
        v = amp * s * min(1.0, (dur - t) / 0.004)
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def _key(buf, t, rng, kind):
    """A mechanical-keyboard press: switch click + bottom-out thock 3–6ms later.
    Mode layout (case hollow ~110Hz, cap/plate 350–900Hz, thin fast highs)
    keeps the centroid in the reference's 600–800Hz pocket with no high fizz."""
    pan = rng.uniform(-0.25, 0.25)
    v = rng.uniform(0.90, 1.10)          # per-key micro variation
    modes = [
        (rng.uniform(96, 128),        0.22, 0.009),   # case hollow
        (rng.uniform(340, 440) * v,   0.85, 0.0060),  # cap body — the thock
        (rng.uniform(700, 920) * v,   1.00, 0.0042),
        (rng.uniform(1500, 1950) * v, 0.55, 0.0024),
        (rng.uniform(2700, 3500) * v, 0.22, 0.0013),  # brief "tick", dies fast
    ]
    if kind == "return":
        # bigger key, deeper board contact, hits harder
        modes = [(f * 0.78, a, tau * 1.35) for f, a, tau in modes]
        _strike(buf, t, modes, 0.62, 0.0, rng, click=0.40, click_lp=3200)
        _strike(buf, t + 0.0045, modes, 0.95, 0.0, rng, click=0.25, click_lp=2200)
    elif kind == "space":
        # long stabilized bar: lowest, most hollow key on the board
        modes = [(f * 0.68, a, tau * 1.25) for f, a, tau in modes]
        _strike(buf, t, modes, 0.34 * v, pan, rng, click=0.35, click_lp=2800)
        _strike(buf, t + 0.0050, modes, 0.55 * v, pan, rng, click=0.20, click_lp=2000)
    else:
        down = rng.uniform(0.003, 0.006)     # travel time to bottom-out
        _strike(buf, t, modes, 0.30 * v, pan, rng, click=0.55, click_lp=4200)
        _strike(buf, t + down, modes, 0.60 * v, pan, rng, click=0.30, click_lp=2600)


def build(min_gap: float = 0.055) -> array:
    buf = array("d", bytes(8 * 2 * N))
    last_t = -1.0
    for k, (t, kind, _ch) in enumerate(key_events()):
        if t >= DUR:
            continue
        if kind != "return" and t - last_t < min_gap:
            continue  # each key stays a distinct thock, never a buzz
        _key(buf, t, random.Random(k * 7919 + 13), kind)
        last_t = t
    return buf


if __name__ == "__main__":
    # foley rides close to the music's level — in the reference the keys are
    # a foreground element, not an easter egg
    write(build(), out=OUT, headroom=0.70)
    print(f"sfx → {OUT} ({len(key_events())} keystrokes)")
