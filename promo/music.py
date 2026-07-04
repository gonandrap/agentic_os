"""Deterministic energetic-productivity track for the Jarvis promo — stdlib only.

Brand music direction (brand/BRAND.md): 122 BPM, soft four-on-the-floor kick,
driving eighth-note bass with accent syncopation, bright pluck arpeggios, brushed
off-beat hats, warm pad underneath. Arrangement follows the story: pulsing intro →
groove locks in while the OS works → breakdown on the all-quiet payoff → light outro.

    uv run python promo/music.py   # → promo/out/track.wav (60s, 44.1kHz stereo)
"""

from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

SR = 44100
BPM = 122
BEAT = 60.0 / BPM            # 0.4918s
BAR = BEAT * 4               # 1.967s
DUR = 60.0
N = int(SR * DUR)

OUT = Path(__file__).parent / "out" / "track.wav"

# story beats (keep in sync with render.py timeline)
GROOVE_IN = 6.0      # fleet scene: kick + bass enter
FULL_IN = 14.0       # first gameplay shot: hats + brighter layers
BREAK_AT = 49.0      # all-quiet payoff: kick/bass/hats drop out
OUTRO_AT = 54.0


def hz(name: str) -> float:
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4,
             "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    return 440.0 * 2 ** ((names[name[:-1]] + (int(name[-1]) - 4) * 12) / 12)


# progression: C – G – Am – F, two bars per chord (I–V–vi–IV)
CHORDS = [
    ("C",  ["C3", "E3", "G3", "C4"]),
    ("G",  ["G2", "B2", "D3", "G3"]),
    ("Am", ["A2", "C3", "E3", "A3"]),
    ("F",  ["F2", "A2", "C3", "F3"]),
]
CHORD_LEN = 2 * BAR


def chord_at(t: float):
    return CHORDS[int(t // CHORD_LEN) % len(CHORDS)]


def add_tone(buf, start, dur, freq, amp, attack=0.01, release=0.08,
             partials=((1, 1.0),), detune=0.0, pan=0.0):
    """Additive tone, linear attack / linear release tail. pan ∈ [-1, 1]."""
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    w = 2 * math.pi * freq * (1 + detune)
    gl = min(1.0, 1.0 - pan)   # simple constant-ish pan law
    gr = min(1.0, 1.0 + pan)
    for i in range(i0, i1):
        t = i / SR - start
        env = min(1.0, t / attack) if attack > 0 else 1.0
        tail = dur - t
        if tail < release:
            env *= max(0.0, tail / release)
        s = 0.0
        for mult, pamp in partials:
            s += pamp * math.sin(w * mult * (i / SR))
        v = amp * env * s
        buf[2 * i] += v * gl
        buf[2 * i + 1] += v * gr


def add_kick(buf, start, amp=0.16):
    """Soft four-on-the-floor: sine with a fast pitch drop + tiny click."""
    i0 = int(start * SR)
    i1 = min(N, int((start + 0.11) * SR))
    ph = 0.0
    for i in range(i0, i1):
        t = i / SR - start
        f = 55 + 95 * math.exp(-t / 0.028)          # 150 → 55 Hz drop
        ph += 2 * math.pi * f / SR
        env = math.exp(-t / 0.045)
        v = amp * env * math.sin(ph)
        if t < 0.004:                               # attack click
            v += amp * 0.4 * (1 - t / 0.004) * math.sin(2 * math.pi * 2400 * t)
        buf[2 * i] += v
        buf[2 * i + 1] += v


def add_hat(buf, start, amp=0.035, bright=1.0):
    """Brushed hat: stacked non-harmonic sines, very short."""
    i0 = int(start * SR)
    i1 = min(N, int((start + 0.03) * SR))
    for i in range(i0, i1):
        t = i / SR - start
        env = math.exp(-t / 0.009)
        s = (math.sin(2 * math.pi * 4211 * bright * t)
             + 0.8 * math.sin(2 * math.pi * 6067 * bright * t)
             + 0.6 * math.sin(2 * math.pi * 7919 * bright * t))
        v = amp * env * s
        buf[2 * i] += v * 0.75
        buf[2 * i + 1] += v          # a touch wider right
    return


def build() -> array:
    buf = array("d", bytes(8 * 2 * N))

    # -- pad: warm but light, under everything --------------------------------
    t = 0.0
    while t < DUR:
        _, notes = chord_at(t)
        seg_len = min(CHORD_LEN, DUR - t)
        for n in notes[1:]:                       # skip the low root (bass owns it)
            f = hz(n)
            for det, pan in ((-0.0013, -0.35), (0.0013, 0.35)):
                add_tone(buf, t, seg_len + 0.05, f, amp=0.026, attack=0.30,
                         release=0.6, partials=((1, 1.0), (2, 0.22)), detune=det,
                         pan=pan)
        t += CHORD_LEN

    # -- arpeggio: bright 8th plucks from the top, 16th sparkle when full -----
    step = BEAT / 2
    k = 0
    t = 0.0
    while t < min(BREAK_AT + 5.0, DUR):
        _, notes = chord_at(t)
        order = [0, 2, 1, 3, 2, 3, 1, 2]
        f = hz(notes[order[k % 8]]) * 2
        amp = 0.052 if t >= GROOVE_IN else 0.042
        add_tone(buf, t, 0.26, f, amp=amp, attack=0.003, release=0.22,
                 partials=((1, 1.0), (2, 0.20), (3, 0.05)),
                 pan=0.25 if k % 2 else -0.25)
        # 16th echo sparkle an octave up during the full section
        if FULL_IN <= t < BREAK_AT:
            add_tone(buf, t + step / 2, 0.14, f * 2, amp=0.020, attack=0.002,
                     release=0.12, partials=((1, 1.0),), pan=0.45 if k % 2 else -0.45)
        k += 1
        t += step

    # -- bass: driving 8ths on the root with accent syncopation ---------------
    accents = [1.0, 0.55, 0.8, 0.55, 0.95, 0.55, 0.85, 0.65]
    t = GROOVE_IN
    k = 0
    while t < min(BREAK_AT, DUR):
        _, notes = chord_at(t)
        f = hz(notes[0]) / 2
        a = 0.105 * accents[k % 8]
        add_tone(buf, t, 0.21, f, amp=a, attack=0.006, release=0.10,
                 partials=((1, 1.0), (2, 0.30), (3, 0.10)))
        k += 1
        t += BEAT / 2

    # -- kick: four on the floor ------------------------------------------------
    t = GROOVE_IN
    while t < min(BREAK_AT, DUR):
        add_kick(buf, t)
        t += BEAT

    # -- hats: off-beat 8ths + tick on 2 & 4 ------------------------------------
    t = FULL_IN + BEAT / 2
    while t < min(BREAK_AT, DUR):
        add_hat(buf, t)
        t += BEAT
    t = FULL_IN + BEAT
    while t < min(BREAK_AT, DUR):
        add_hat(buf, t, amp=0.05, bright=0.72)    # duller "tick" on 2 & 4
        t += 2 * BEAT

    # -- outro: light arp keeps momentum into the fade --------------------------
    t = OUTRO_AT
    k = 0
    while t < DUR:
        _, notes = chord_at(0)                    # settle home on C
        f = hz(notes[[0, 2, 1, 3][k % 4]]) * 2
        add_tone(buf, t, 0.3, f, amp=0.035, attack=0.004, release=0.26,
                 partials=((1, 1.0), (2, 0.15)), pan=0.2 if k % 2 else -0.2)
        k += 1
        t += BEAT
    for n in CHORDS[0][1]:
        add_tone(buf, OUTRO_AT, DUR - OUTRO_AT, hz(n), amp=0.030, attack=0.5,
                 release=2.0, partials=((1, 1.0), (2, 0.2)))

    # -- sidechain pump against the kick (groove section only) ------------------
    for i in range(int(GROOVE_IN * SR), int(min(BREAK_AT, DUR) * SR)):
        ph = ((i / SR - GROOVE_IN) % BEAT) / BEAT
        g = 0.80 + 0.20 * min(1.0, ph / 0.30)
        buf[2 * i] *= g
        buf[2 * i + 1] *= g

    # -- master fade in/out -------------------------------------------------------
    fade_in, fade_out = 0.4, 3.0
    for i in range(N):
        t = i / SR
        g = min(1.0, t / fade_in) * min(1.0, max(0.0, (DUR - t) / fade_out))
        buf[2 * i] *= g
        buf[2 * i + 1] *= g
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
    write(build())
    print(f"track → {OUT}")
