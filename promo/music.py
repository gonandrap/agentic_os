"""Deterministic productivity track for the Jarvis promo — stdlib only.

Brand music direction (brand/BRAND.md): 104 BPM, warm pad on a I–V–vi–IV family,
soft sine plucks in 8ths, round sub-bass, brushed tick on 2 & 4. Arrangement
follows the video: sparse intro → layers stack while the OS works → thin out for
the resolution beat → sustained chord under the outro.

    uv run python promo/music.py   # → promo/out/track.wav (60s, 44.1kHz stereo)
"""

from __future__ import annotations

import math
import wave
from array import array
from pathlib import Path

SR = 44100
BPM = 104
BEAT = 60.0 / BPM            # 0.577s
BAR = BEAT * 4               # 2.308s
DUR = 60.0
N = int(SR * DUR)

OUT = Path(__file__).parent / "out" / "track.wav"

# note frequencies (equal temperament, A4=440)
def hz(name: str) -> float:
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4,
             "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    key, octave = name[:-1], int(name[-1])
    return 440.0 * 2 ** ((names[key] + (octave - 4) * 12) / 12)


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
             partials=((1, 1.0),), detune=0.0):
    """Additive tone with linear attack / exponential-ish release, both channels."""
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    w = 2 * math.pi * freq * (1 + detune)
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
        buf[2 * i] += v
        buf[2 * i + 1] += v


def build() -> array:
    buf = array("d", bytes(8 * 2 * N))  # float64 accumulate, stereo interleaved

    # -- pad: two detuned voices per chord note, slow attack (enters at 0) -------
    t = 0.0
    while t < DUR:
        _, notes = chord_at(t)
        seg = min(CHORD_LEN, DUR - t)
        for n in notes:
            f = hz(n)
            for det in (-0.0012, 0.0012):
                add_tone(buf, t, seg + 0.05, f, amp=0.040, attack=0.9, release=0.9,
                         partials=((1, 1.0), (2, 0.28), (3, 0.07)), detune=det)
        t += CHORD_LEN

    # -- arpeggio: 8th-note plucks over chord tones (enters bar 5 ≈ 9.2s) --------
    arp_start, arp_end = 4 * BAR, 52.0
    step = BEAT / 2
    k = 0
    t = arp_start
    while t < min(arp_end, DUR):
        _, notes = chord_at(t)
        order = [0, 2, 1, 3, 2, 3, 1, 2]
        f = hz(notes[order[k % 8]]) * 2  # one octave up
        add_tone(buf, t, 0.34, f, amp=0.055, attack=0.004, release=0.30,
                 partials=((1, 1.0), (2, 0.12)))
        k += 1
        t += step

    # -- bass: root each half-note (enters bar 9 ≈ 18.5s, rests after 52s) -------
    t = 8 * BAR
    while t < min(52.0, DUR):
        _, notes = chord_at(t)
        f = hz(notes[0]) / 2
        add_tone(buf, t, BEAT * 1.7, f, amp=0.085, attack=0.012, release=0.35,
                 partials=((1, 1.0),))
        t += 2 * BEAT

    # -- brushed tick on beats 2 & 4 (deterministic 'noise', enters 18.5s) -------
    def tick(start, amp=0.05):
        i0, i1 = int(start * SR), min(N, int((start + 0.035) * SR))
        for i in range(i0, i1):
            tt = i / SR - start
            env = max(0.0, 1 - tt / 0.035)
            # deterministic pseudo-noise: sum of odd sines, hp-ish
            s = (math.sin(2 * math.pi * 3800 * tt) + 0.7 * math.sin(2 * math.pi * 5170 * tt)
                 + 0.4 * math.sin(2 * math.pi * 6660 * tt))
            v = amp * env * env * s
            buf[2 * i] += v * 0.8
            buf[2 * i + 1] += v  # a touch wider on the right
    t = 8 * BAR + BEAT
    while t < min(52.0, DUR):
        tick(t)
        t += 2 * BEAT

    # -- gentle sidechain breath against the beat (whole mix, subtle) ------------
    for i in range(int(8 * BAR * SR), int(min(52.0, DUR) * SR)):
        ph = ((i / SR) % BEAT) / BEAT
        g = 0.88 + 0.12 * min(1.0, ph / 0.35)
        buf[2 * i] *= g
        buf[2 * i + 1] *= g

    # -- master fade in/out -------------------------------------------------------
    fade_in, fade_out = 0.8, 3.5
    for i in range(N):
        t = i / SR
        g = min(1.0, t / fade_in) * min(1.0, max(0.0, (DUR - t) / fade_out))
        buf[2 * i] *= g
        buf[2 * i + 1] *= g
    return buf


def write(buf: array) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    peak = max(1e-9, max(abs(v) for v in buf))
    scale = 0.85 * 32767 / peak
    pcm = array("h", (int(v * scale) for v in buf))
    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


if __name__ == "__main__":
    write(build())
    print(f"track → {OUT}")
