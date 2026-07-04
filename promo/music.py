"""The Jarvis promo soundtrack — deterministic, stdlib only.

Deep house at 124 BPM, tuned by measurement against a reference promo the user
likes: round four-on-the-floor kick (~100ms decay), offbeat sub bass, dark EP
stabs, tiny rim/shaker percussion, and layers that keep stacking (pluck arp at
22s, 16th sparkle at 38s) so energy builds instead of plateauing. Master is
tanh-driven for a hot, dense level (≈ −12 dB mean). Full spec: brand/BRAND.md.

    uv run python promo/music.py   # → promo/out/track.wav (60s, 44.1kHz stereo)
"""

from __future__ import annotations

import math
import random
import sys
import wave
from array import array
from pathlib import Path

SR = 44100
DUR = 73.0
N = int(SR * DUR)
OUT = Path(__file__).parent / "out" / "track.wav"

# story beats (keep in sync with render.py timeline)
GROOVE_IN = 9.0      # concept scene: "Create work order" pressed → beat drops
FULL_IN = 19.0       # fleet scene: stabs + percussion
ARP_IN = 27.0        # first gameplay shot: pluck arp
SPARK_IN = 47.0      # neo scene: 16th sparkle — the build
BREAK_AT = 62.0      # all-quiet payoff: rhythm section out
OUTRO_AT = 67.0


def hz(name: str) -> float:
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4,
             "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    return 440.0 * 2 ** ((names[name[:-1]] + (int(name[-1]) - 4) * 12) / 12)


def add_tone(buf, start, dur, freq, amp, attack=0.01, release=0.08,
             partials=((1, 1.0),), detune=0.0, pan=0.0, decay=None):
    """Additive tone. Linear attack/release; optional exponential decay tau."""
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    w = 2 * math.pi * freq * (1 + detune)
    gl = min(1.0, 1.0 - pan)
    gr = min(1.0, 1.0 + pan)
    for i in range(i0, i1):
        t = i / SR - start
        env = min(1.0, t / attack) if attack > 0 else 1.0
        if decay:
            env *= math.exp(-t / decay)
        tail = dur - t
        if tail < release:
            env *= max(0.0, tail / release)
        s = 0.0
        for mult, pamp in partials:
            s += pamp * math.sin(w * mult * (i / SR))
        v = amp * env * s
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def add_kick(buf, start, amp=0.17, f_hi=120.0, f_lo=46.0, p_dec=0.030,
             a_dec=0.042, click=0.18):
    """Round pitch-dropping sine kick with a small attack click."""
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + 0.14) * SR))
    ph = 0.0
    for i in range(i0, i1):
        t = i / SR - start
        f = f_lo + (f_hi - f_lo) * math.exp(-t / p_dec)
        ph += 2 * math.pi * f / SR
        v = amp * math.exp(-t / a_dec) * math.sin(ph)
        if click and t < 0.004:
            v += amp * click * (1 - t / 0.004) * math.sin(2 * math.pi * 2200 * t)
        buf[2 * i] += v
        buf[2 * i + 1] += v


def add_noise(buf, start, dur, amp, lp=6000.0, hp=600.0, tau=None,
              pan=0.0, seed=0):
    """Band-limited noise hit (rim/shaker/hat building block)."""
    rng = random.Random(seed)
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
        env = math.exp(-t / tau) if tau else 1.0
        env *= min(1.0, (dur - t) / 0.008)
        v = amp * env * (lp1 - hp1)
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def sidechain(buf, t0, t1, period, depth=0.22):
    """Duck everything right after each beat in [t0, t1)."""
    for i in range(max(0, int(t0 * SR)), min(N, int(t1 * SR))):
        ph = ((i / SR - t0) % period) / period
        g = (1 - depth) + depth * min(1.0, ph / 0.30)
        buf[2 * i] *= g
        buf[2 * i + 1] *= g


def build() -> array:
    buf = array("d", bytes(8 * 2 * N))
    B = 60 / 124
    bar = 4 * B
    STAB = [["A2", "C3", "E3", "G3"], ["A2", "C3", "E3", "G3"],
            ["F2", "A2", "C3", "E3"], ["G2", "B2", "D3", "F3"]]

    # airy pad, barely there
    t, k = 0.0, 0
    while t < DUR:
        notes = STAB[(k // 2) % 4]
        seg = min(2 * bar, DUR - t)
        for n in notes[1:]:
            for det, pan in ((-0.0014, -0.35), (0.0014, 0.35)):
                add_tone(buf, t, seg + 0.1, hz(n) * 2, 0.010, attack=0.8,
                         release=1.0, partials=((1, 1.0), (2, 0.2)), detune=det, pan=pan)
        k += 2
        t += 2 * bar

    # round four-on-the-floor (reference: -20dB in ~97ms)
    t = GROOVE_IN
    while t < BREAK_AT:
        add_kick(buf, t)
        t += B

    # offbeat sub bass
    t = GROOVE_IN
    while t < BREAK_AT:
        root = hz(STAB[(int(t // (2 * bar))) % 4][0]) / 2
        add_tone(buf, t + B / 2, 0.16, root, 0.115, attack=0.006, release=0.06,
                 partials=((1, 1.0), (2, 0.15)))
        t += B

    # dark EP stabs on the and-of-2 and the 4
    t = FULL_IN
    while t < BREAK_AT:
        notes = STAB[(int(t // (2 * bar))) % 4]
        for off in (1.5 * B, 3 * B):
            for j, n in enumerate(notes[1:]):
                add_tone(buf, t + off + 0.004 * j, 0.20, hz(n), 0.030,
                         attack=0.004, decay=0.10, release=0.08,
                         partials=((1, 1.0), (2, 0.35), (3, 0.10)),
                         detune=0.0010, pan=-0.25 + 0.25 * j)
        t += bar

    # layer 2 at 22s: pluck arp 8ths — motion on top of the groove
    order = [0, 2, 1, 3, 2, 3, 1, 2]
    t, k = ARP_IN, 0
    while t < BREAK_AT:
        notes = STAB[(int(t // (2 * bar))) % 4]
        f = hz(notes[order[k % 8]]) * 2
        add_tone(buf, t, 0.20, f, 0.026, attack=0.003, release=0.16,
                 partials=((1, 1.0), (2, 0.30), (3, 0.08)),
                 pan=0.3 if k % 2 else -0.3)
        k += 1
        t += B / 2

    # layer 3 at 38s: 16th-offset sparkle echo an octave up — the build
    t, k = SPARK_IN, 0
    while t < BREAK_AT:
        notes = STAB[(int(t // (2 * bar))) % 4]
        f = hz(notes[order[k % 8]]) * 4
        add_tone(buf, t + B / 4, 0.12, f, 0.012, attack=0.002, release=0.10,
                 partials=((1, 1.0),), pan=-0.45 if k % 2 else 0.45)
        k += 1
        t += B / 2

    # rim 2 & 4, 16th shaker, open hat at bar turn
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        add_noise(buf, t + B, 0.03, 0.045, lp=2600, hp=1500, tau=0.007, pan=-0.2,
                  seed=40 + k)
        add_noise(buf, t + 3 * B, 0.03, 0.045, lp=2600, hp=1500, tau=0.007, pan=0.2,
                  seed=80 + k)
        add_noise(buf, t + 3.5 * B, 0.10, 0.020, lp=10000, hp=6000, tau=0.045,
                  pan=0.3, seed=120 + k)
        k += 1
        t += bar
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        add_noise(buf, t, 0.03, 0.010 if k % 2 else 0.015, lp=11000, hp=7000,
                  tau=0.008, pan=0.15, seed=200 + k)
        k += 1
        t += B / 4

    # outro: sub settles on A, pad blooms
    add_tone(buf, OUTRO_AT, DUR - OUTRO_AT, hz("A1"), 0.07, attack=0.3,
             release=2.0, partials=((1, 1.0), (2, 0.1)))
    for j, n in enumerate(["A2", "C3", "E3", "A3"]):
        add_tone(buf, OUTRO_AT + 0.1 * j, DUR - OUTRO_AT, hz(n) * 2, 0.018,
                 attack=1.0, release=2.0, partials=((1, 1.0), (2, 0.2)),
                 pan=-0.3 + 0.2 * j)

    sidechain(buf, GROOVE_IN, BREAK_AT, B)

    # master fade in/out
    fade_in, fade_out = 0.4, 3.0
    for i in range(N):
        t = i / SR
        g = min(1.0, t / fade_in) * min(1.0, max(0.0, (DUR - t) / fade_out))
        buf[2 * i] *= g
        buf[2 * i + 1] *= g

    # gentle tanh drive: raises RMS a few dB (the reference sits at −12dB mean,
    # denser than a clean peak-normalized mix); write() re-normalizes the peak
    peak = max(1e-9, max(abs(v) for v in buf))
    for i in range(2 * N):
        buf[i] = math.tanh(2.0 * buf[i] / peak)
    return buf


def write(buf: array, out: Path = OUT, headroom: float = 0.85) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    peak = max(1e-9, max(abs(v) for v in buf))
    scale = headroom * 32767 / peak
    pcm = array("h", (int(v * scale) for v in buf))
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else OUT
    write(build(), out=out)
    print(f"track → {out}")
