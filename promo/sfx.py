"""Keystroke foley for the Jarvis promo — the brand's signature audio device.

Every command typed on screen is heard: a synthesized key click per character
(micro-varied pitch/level, deterministic) and a lower return-key *clack* when the
command completes. Click times are derived from the SAME timeline and the SAME
typing math the scenes use (`render.py::timeline`, `scene.js::typeInto`), so audio
and pixels cannot drift.

    uv run python promo/sfx.py   # → promo/out/sfx.wav
"""

from __future__ import annotations

import math
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


# -- synthesis -------------------------------------------------------------------

def _rand(seed: float) -> float:
    """Deterministic pseudo-random in [0, 1) (no random module — reproducible)."""
    return math.sin(seed * 127.1 + 311.7) * 43758.5453 % 1.0


def _click(buf, start, f0, body_f, amp, dur=0.028, pan=0.0):
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    gl, gr = min(1.0, 1 - pan), min(1.0, 1 + pan)
    for i in range(i0, i1):
        t = i / SR - start
        env = math.exp(-t / (dur / 4))
        s = (0.9 * math.sin(2 * math.pi * f0 * t)
             + 0.5 * math.sin(2 * math.pi * f0 * 1.83 * t)
             + 0.7 * math.sin(2 * math.pi * body_f * t) * math.exp(-t / 0.006))
        v = amp * env * s
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def build(min_gap: float = 0.034) -> array:
    buf = array("d", bytes(8 * 2 * N))
    last_t = -1.0
    for k, (t, kind, _ch) in enumerate(key_events()):
        if t >= DUR:
            continue
        if kind != "return" and t - last_t < min_gap:
            continue  # cap density: fast typing, not a buzz
        r1, r2, r3 = _rand(k + 1), _rand(k + 101), _rand(k + 201)
        if kind == "return":
            # the satisfying end-of-command clack: lower, longer, both channels
            _click(buf, t, f0=900 + 150 * r1, body_f=140, amp=0.34, dur=0.055)
        elif kind == "space":
            _click(buf, t, f0=1300 + 250 * r1, body_f=170, amp=0.16 + 0.05 * r2,
                   dur=0.030, pan=(r3 - 0.5) * 0.5)
        else:
            _click(buf, t, f0=1900 + 700 * r1, body_f=230 + 60 * r2,
                   amp=0.20 + 0.08 * r3, dur=0.026, pan=(r1 - 0.5) * 0.5)
        last_t = t
    return buf


if __name__ == "__main__":
    write(build(), out=OUT, headroom=0.5)   # foley sits under the music
    print(f"sfx → {OUT} ({len(key_events())} keystrokes)")
