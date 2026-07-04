"""Five deterministic soundtrack directions for the Jarvis promo — stdlib only.

Exploration round: the user picks a direction, the winner becomes DEFAULT_STYLE
and brand/BRAND.md's music direction gets rewritten to match it.

    uv run python promo/music.py                     # DEFAULT_STYLE → out/track.wav
    uv run python promo/music.py lofi out/t.wav      # one style → explicit path

Styles
  lofi       88 BPM boom-bap: dusty swung drums, warm EP 7th chords, vinyl crackle
  synthwave 100 BPM retrowave: pulsing 16th synth bass, big detuned pads, gated snare
  acoustic  100 BPM indie: plucked-string arpeggios (Karplus-Strong), claps, shaker
  deephouse 112 BPM deep house: round four-on-floor, offbeat sub bass, EP stabs
  keynote    92 BPM piano + pulse: product-launch optimism, strings, soft heartbeat

Every style honors the same story beats (in sync with render.py's timeline):
intro → GROOVE_IN (fleet) → FULL_IN (first gameplay) → BREAK_AT (all-quiet payoff,
rhythm section drops out) → OUTRO_AT (settle home) → fade.
"""

from __future__ import annotations

import math
import random
import sys
import wave
from array import array
from pathlib import Path

SR = 44100
DUR = 60.0
N = int(SR * DUR)
OUT = Path(__file__).parent / "out" / "track.wav"

DEFAULT_STYLE = "deephouse"

# story beats (keep in sync with render.py timeline)
GROOVE_IN = 6.0
FULL_IN = 14.0
BREAK_AT = 49.0
OUTRO_AT = 54.0


def hz(name: str) -> float:
    names = {"C": -9, "C#": -8, "D": -7, "D#": -6, "E": -5, "F": -4,
             "F#": -3, "G": -2, "G#": -1, "A": 0, "A#": 1, "B": 2}
    return 440.0 * 2 ** ((names[name[:-1]] + (int(name[-1]) - 4) * 12) / 12)


# ---------------------------------------------------------------- instruments

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


def add_pluck(buf, start, freq, amp, dur=1.4, damp=0.996, soft=0, pan=0.0, seed=1):
    """Karplus-Strong plucked string. soft>0 mellows the attack."""
    rng = random.Random(seed)
    period = max(2, int(SR / freq))
    line = [rng.uniform(-1, 1) for _ in range(period)]
    for _ in range(soft):                       # moving-average the burst
        line = [(line[j] + line[j - 1]) / 2 for j in range(period)]
    i0 = max(0, int(start * SR))
    i1 = min(N, int((start + dur) * SR))
    gl = min(1.0, 1.0 - pan)
    gr = min(1.0, 1.0 + pan)
    idx = 0
    for i in range(i0, i1):
        v = line[idx]
        line[idx] = damp * 0.5 * (v + line[(idx + 1) % period])
        idx = (idx + 1) % period
        t = i / SR - start
        g = min(1.0, (dur - t) / 0.05)          # tail fade, no click at cutoff
        s = amp * v * g
        buf[2 * i] += s * gl
        buf[2 * i + 1] += s * gr


def add_kick(buf, start, amp=0.16, f_hi=150.0, f_lo=54.0, p_dec=0.03,
             a_dec=0.05, click=0.35):
    """Pitch-dropping sine kick with an optional attack click."""
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
    """Band-limited noise hit (snare/hat/shaker/clap building block)."""
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


def add_clap(buf, start, amp=0.07, seed=0):
    for j, (dt, g) in enumerate(((0.0, 0.8), (0.011, 1.0), (0.023, 0.7))):
        add_noise(buf, start + dt, 0.09, amp * g, lp=3200, hp=900,
                  tau=0.028, pan=(j - 1) * 0.2, seed=seed + j)


def add_thump(buf, start, freq=185.0, amp=0.05, dur=0.03):
    add_tone(buf, start, dur, freq, amp, attack=0.001, release=0.01, decay=0.012)


def sidechain(buf, t0, t1, period, depth=0.18):
    """Duck everything right after each beat in [t0, t1)."""
    for i in range(max(0, int(t0 * SR)), min(N, int(t1 * SR))):
        ph = ((i / SR - t0) % period) / period
        g = (1 - depth) + depth * min(1.0, ph / 0.30)
        buf[2 * i] *= g
        buf[2 * i + 1] *= g


def new_buf() -> array:
    return array("d", bytes(8 * 2 * N))


def master(buf, fade_in=0.4, fade_out=3.0):
    for i in range(N):
        t = i / SR
        g = min(1.0, t / fade_in) * min(1.0, max(0.0, (DUR - t) / fade_out))
        buf[2 * i] *= g
        buf[2 * i + 1] *= g
    return buf


# -------------------------------------------------------------------- styles

def build_lofi() -> array:
    """88 BPM boom-bap: swung dusty drums, warm EP 7ths, vinyl crackle."""
    buf = new_buf()
    B = 60 / 88
    bar = 4 * B
    CH = [["C3", "E3", "G3", "B3"], ["A2", "C3", "E3", "G3"],
          ["F2", "A2", "C3", "E3"], ["G2", "B2", "D3", "F3"]]

    # EP chords: full hit on 1, softer on 3, tiny roll between voices
    t, k = 0.0, 0
    while t < OUTRO_AT:
        notes = CH[(k // 2) % 4]
        for off, g in ((0.0, 1.0), (2 * B, 0.5)):
            for j, n in enumerate(notes[1:]):
                add_tone(buf, t + off + 0.014 * j, 2.4, hz(n), 0.042 * g,
                         attack=0.006, decay=0.85, release=0.4,
                         partials=((1, 1.0), (2, 0.30), (3, 0.08)),
                         detune=0.0012, pan=-0.3 + 0.3 * j)
        k += 1
        t += bar

    # round bass: root on 1, approach note on the swung 3-and
    t, k = GROOVE_IN, 0
    while t < BREAK_AT:
        root = hz(CH[(k // 2) % 4][0]) / 2
        add_tone(buf, t, 1.4 * B, root, 0.105, attack=0.008, release=0.12,
                 partials=((1, 1.0), (2, 0.25)))
        add_tone(buf, t + 3.6 * B, 0.5 * B, root * 1.5, 0.06, attack=0.008,
                 release=0.08, partials=((1, 1.0), (2, 0.25)))
        k += 1
        t += bar

    # boom-bap kit: kick 1 & 3.6 (lazy), snare 2 & 4, swung 8th hats
    t, k = GROOVE_IN, 0
    while t < BREAK_AT:
        add_kick(buf, t, 0.15, f_hi=120, a_dec=0.06, click=0.2)
        add_kick(buf, t + 2.6 * B, 0.12, f_hi=120, a_dec=0.05, click=0.2)
        for sn in (B, 3 * B):
            add_noise(buf, t + sn, 0.12, 0.085, lp=2600, hp=700, tau=0.030,
                      seed=k * 7 + int(sn * 10))
            add_thump(buf, t + sn, 190, 0.04)
        k += 1
        t += bar
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        swing = 0.12 * B if k % 2 else 0.0
        add_noise(buf, t + swing, 0.035, 0.020 if k % 2 else 0.028,
                  lp=11000, hp=6500, tau=0.010, pan=0.3, seed=100 + k)
        k += 1
        t += B / 2

    # vinyl crackle bed, whole runtime
    rng = random.Random(7)
    tick = 0.0
    while tick < DUR:
        tick += rng.uniform(0.02, 0.16)
        add_noise(buf, tick, 0.004, rng.uniform(0.008, 0.03),
                  lp=4200, hp=300, tau=0.001, pan=rng.uniform(-0.5, 0.5),
                  seed=int(tick * 997))

    # outro: last Cmaj7 rings out
    for j, n in enumerate(CH[0]):
        add_tone(buf, OUTRO_AT + 0.05 * j, DUR - OUTRO_AT, hz(n), 0.035,
                 attack=0.01, decay=2.2, release=1.0,
                 partials=((1, 1.0), (2, 0.25)), pan=-0.2 + 0.15 * j)
    return master(buf)


def build_synthwave() -> array:
    """100 BPM retrowave: pulsing 16th bass, wide detuned pads, gated snare."""
    buf = new_buf()
    B = 60 / 100
    bar = 4 * B
    CH = [["A2", "C3", "E3", "A3"], ["F2", "A2", "C3", "F3"],
          ["C3", "E3", "G3", "C4"], ["G2", "B2", "D3", "G3"]]

    # wide pads: 3 upper voices x 2 detunes, saw-ish partials
    t, k = 0.0, 0
    while t < DUR:
        notes = CH[(k // 2) % 4]
        seg = min(2 * bar, DUR - t)
        for n in notes[1:]:
            for det, pan in ((-0.0022, -0.4), (0.0022, 0.4)):
                add_tone(buf, t, seg + 0.1, hz(n), 0.021, attack=0.5,
                         release=0.8, partials=((1, 1.0), (2, 0.50), (3, 0.33), (4, 0.25)),
                         detune=det, pan=pan)
        k += 2
        t += 2 * bar

    # THE synthwave engine: gated 16th-note bass on the root
    t, k = GROOVE_IN, 0
    acc = [1.0, 0.5, 0.68, 0.5]
    while t < BREAK_AT:
        root = hz(CH[(int(t // (2 * bar))) % 4][0]) / 2
        add_tone(buf, t, 0.13, root, 0.095 * acc[k % 4], attack=0.004,
                 release=0.05, partials=((1, 1.0), (2, 0.45), (3, 0.22)))
        k += 1
        t += B / 4

    # kick 1 & 3, big gated snare 2 & 4
    t = GROOVE_IN
    while t < BREAK_AT:
        add_kick(buf, t, 0.16, f_hi=140, a_dec=0.055)
        add_kick(buf, t + 2 * B, 0.16, f_hi=140, a_dec=0.055)
        t += bar
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        for sn in (B, 3 * B):
            add_noise(buf, t + sn, 0.26, 0.10, lp=3800, hp=500, tau=0.075,
                      seed=k * 13 + int(sn * 10))
            add_thump(buf, t + sn, 175, 0.05)
        k += 1
        t += bar

    # neon arp with an echo, 8ths from FULL_IN
    t, k = FULL_IN, 0
    order = [0, 2, 1, 3, 2, 3, 1, 2]
    while t < BREAK_AT:
        notes = CH[(int(t // (2 * bar))) % 4]
        f = hz(notes[order[k % 8]]) * 2
        add_tone(buf, t, 0.22, f, 0.034, attack=0.003, release=0.18,
                 partials=((1, 1.0), (2, 0.35), (3, 0.12)),
                 pan=0.3 if k % 2 else -0.3)
        add_tone(buf, t + 0.75 * B, 0.18, f, 0.013, attack=0.003, release=0.15,
                 partials=((1, 1.0), (2, 0.35)), pan=-0.45 if k % 2 else 0.45)
        k += 1
        t += B / 2

    # outro: home on C, slow arp up
    for j, n in enumerate(["C3", "E3", "G3", "C4", "E4"]):
        add_tone(buf, OUTRO_AT + j * 0.4, DUR - OUTRO_AT - j * 0.4, hz(n), 0.026,
                 attack=0.05, release=1.5, partials=((1, 1.0), (2, 0.4), (3, 0.2)),
                 pan=-0.3 + 0.15 * j)
    sidechain(buf, GROOVE_IN, BREAK_AT, bar / 2, depth=0.16)
    return master(buf)


def build_acoustic() -> array:
    """100 BPM indie: Karplus-Strong guitars, claps, shaker, felt-good."""
    buf = new_buf()
    B = 60 / 100
    bar = 4 * B
    CH = [["C3", "E3", "G3", "C4"], ["G2", "B2", "D3", "G3"],
          ["A2", "C3", "E3", "A3"], ["F2", "A2", "C3", "F3"]]

    # fingerpicked guitar: 8th-note pattern, one chord per bar (2-bar pairs)
    t, k = 0.0, 0
    pat = [0, 2, 1, 3, 2, 1, 3, 2]
    while t < OUTRO_AT + 2:
        notes = CH[(int(t // (2 * bar))) % 4]
        f = hz(notes[pat[k % 8]]) * 2
        add_pluck(buf, t, f, 0.16, dur=1.2, damp=0.9955, soft=1,
                  pan=-0.25, seed=k + 1)
        k += 1
        t += B / 2

    # second guitar answers with a bar-start broken chord (from FULL_IN)
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        notes = CH[(int(t // (2 * bar))) % 4]
        for j, n in enumerate(notes):
            add_pluck(buf, t + 0.02 * j, hz(n) * 2, 0.10, dur=1.6,
                      damp=0.9965, soft=2, pan=0.35, seed=500 + k * 4 + j)
        k += 1
        t += bar

    # upright-ish bass: root on 1, fifth on 3
    t, k = GROOVE_IN, 0
    while t < BREAK_AT:
        notes = CH[(int(t // (2 * bar))) % 4]
        add_pluck(buf, t, hz(notes[0]), 0.22, dur=1.4, damp=0.9985, soft=4, seed=900 + k)
        add_pluck(buf, t + 2 * B, hz(notes[2]) / 2, 0.18, dur=1.2, damp=0.9985,
                  soft=4, seed=950 + k)
        k += 1
        t += bar

    # shaker 8ths (accent offbeats), kick 1&3 soft, claps 2&4
    t, k = GROOVE_IN, 0
    while t < BREAK_AT:
        add_noise(buf, t, 0.045, 0.022 if k % 2 else 0.014, lp=12000, hp=5500,
                  tau=0.013, pan=0.25, seed=300 + k)
        k += 1
        t += B / 2
    t = FULL_IN
    while t < BREAK_AT:
        add_kick(buf, t, 0.095, f_hi=110, a_dec=0.045, click=0.1)
        add_kick(buf, t + 2 * B, 0.085, f_hi=110, a_dec=0.045, click=0.1)
        add_clap(buf, t + B, 0.065, seed=int(t * 31))
        add_clap(buf, t + 3 * B, 0.065, seed=int(t * 37))
        t += bar

    # glockenspiel sparkle on the 2-bar turns
    t, k = FULL_IN, 0
    mel = ["E5", "G5", "A5", "G5"]
    while t < BREAK_AT:
        add_tone(buf, t, 1.2, hz(mel[k % 4]), 0.030, attack=0.002, decay=0.5,
                 release=0.4, partials=((1, 1.0), (3, 0.12)), pan=0.2)
        k += 1
        t += 2 * bar

    # outro: last C chord strummed slow
    for j, n in enumerate(["C3", "G3", "C4", "E4", "G4"]):
        add_pluck(buf, OUTRO_AT + 0.09 * j, hz(n), 0.15, dur=DUR - OUTRO_AT,
                  damp=0.9985, soft=2, pan=-0.2 + 0.1 * j, seed=1200 + j)
    return master(buf)


def build_deephouse() -> array:
    """124 BPM deep house, tuned against the reference promo the user likes:
    round four-on-the-floor (~97ms decay), offbeat sub, dark EP stabs, and
    layers that keep stacking — arp at 22s, sparkle at 38s — so energy builds
    instead of plateauing. Master is tanh-driven for a hotter, denser level."""
    buf = new_buf()
    B = 60 / 124
    bar = 4 * B
    ARP_IN, SPARK_IN = 22.0, 38.0
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
        add_kick(buf, t, 0.17, f_hi=120, f_lo=46, p_dec=0.030, a_dec=0.042, click=0.18)
        t += B

    # offbeat sub bass
    t, k = GROOVE_IN, 0
    while t < BREAK_AT:
        root = hz(STAB[(int(t // (2 * bar))) % 4][0]) / 2
        add_tone(buf, t + B / 2, 0.16, root, 0.115, attack=0.006, release=0.06,
                 partials=((1, 1.0), (2, 0.15)))
        k += 1
        t += B

    # dark EP stabs on the and-of-2 and the 4
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        notes = STAB[(int(t // (2 * bar))) % 4]
        for off in (1.5 * B, 3 * B):
            for j, n in enumerate(notes[1:]):
                add_tone(buf, t + off + 0.004 * j, 0.20, hz(n), 0.030,
                         attack=0.004, decay=0.10, release=0.08,
                         partials=((1, 1.0), (2, 0.35), (3, 0.10)),
                         detune=0.0010, pan=-0.25 + 0.25 * j)
        k += 1
        t += bar

    # layer 2 at 22s: pluck arp 8ths — motion on top of the groove
    t, k = ARP_IN, 0
    order = [0, 2, 1, 3, 2, 3, 1, 2]
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
    sidechain(buf, GROOVE_IN, BREAK_AT, B, depth=0.22)
    master(buf)
    # gentle tanh drive: raises RMS a few dB (the reference sits at −12dB mean,
    # denser than a clean peak-normalized mix); write() re-normalizes the peak
    peak = max(1e-9, max(abs(v) for v in buf))
    for i in range(2 * N):
        buf[i] = math.tanh(2.0 * buf[i] / peak)
    return buf


def build_keynote() -> array:
    """92 BPM piano + pulse: product-launch optimism, strings, soft heartbeat."""
    buf = new_buf()
    B = 60 / 92
    bar = 4 * B
    CH = [["C3", "E3", "G3", "C4"], ["G2", "B2", "D3", "G3"],
          ["A2", "C3", "E3", "A3"], ["F2", "A2", "C3", "F3"]]
    PIANO = ((1, 1.0), (2, 0.45), (3, 0.22), (4, 0.10), (5, 0.05))

    def piano(t, note, amp, dur=2.4, pan=0.0):
        add_tone(buf, t, dur, hz(note), amp, attack=0.002, decay=1.0,
                 release=0.5, partials=PIANO, detune=0.0006, pan=pan)

    # left hand: root + fifth each bar, always present
    t, k = 0.0, 0
    while t < OUTRO_AT:
        notes = CH[(k // 2) % 4]
        piano(t, notes[0], 0.055, pan=-0.15)
        piano(t + 2 * B, notes[2], 0.038, pan=-0.15)
        k += 1
        t += bar

    # right hand motif: rises through the chord, lands on the color note
    t, k = 0.0, 0
    steps = [(0.0, 1), (B, 2), (2 * B, 3), (3 * B, 2), (3.5 * B, 1)]
    while t < OUTRO_AT:
        notes = CH[(k // 2) % 4]
        for off, idx in steps:
            up = "".join([notes[idx][:-1], str(int(notes[idx][-1]) + 1)])
            piano(t + off, up, 0.050 if off == 0 else 0.036, pan=0.15)
        k += 1
        t += bar

    # soft pulse: high 8th blips — the "OS ticking" texture
    t, k = 0.0, 0
    while t < BREAK_AT + 3:
        root = CH[(int(t // (2 * bar))) % 4][0]
        f = hz(root) * 4
        add_tone(buf, t, 0.10, f, 0.011, attack=0.004, release=0.07,
                 partials=((1, 1.0),), pan=0.4 if k % 2 else -0.4)
        k += 1
        t += B / 2

    # strings swell in for the working section
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        notes = CH[(int(t // (2 * bar))) % 4]
        seg = min(2 * bar, BREAK_AT - t)
        for n in notes[1:]:
            for det, pan in ((-0.0015, -0.4), (0.0015, 0.4)):
                add_tone(buf, t, seg + 0.3, hz(n), 0.016, attack=1.1,
                         release=1.0, partials=((1, 1.0), (2, 0.30), (3, 0.10)),
                         detune=det, pan=pan)
        k += 2
        t += 2 * bar

    # heartbeat kick 1 & 3 + brushed hat 8ths + soft clap on 4 of bar 2
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        add_kick(buf, t, 0.10, f_hi=100, f_lo=50, a_dec=0.05, click=0.0)
        add_kick(buf, t + 2 * B, 0.08, f_hi=100, f_lo=50, a_dec=0.045, click=0.0)
        if k % 2:
            add_clap(buf, t + 3 * B, 0.040, seed=int(t * 17))
        k += 1
        t += bar
    t, k = FULL_IN, 0
    while t < BREAK_AT:
        add_noise(buf, t + B / 2, 0.04, 0.012, lp=9500, hp=5500, tau=0.012,
                  pan=0.3, seed=600 + k)
        k += 1
        t += B

    # outro: final Cmaj arpeggio, ringing
    for j, n in enumerate(["C3", "G3", "C4", "E4", "G4", "C5"]):
        piano(OUTRO_AT + 0.22 * j, n, 0.045, dur=DUR - OUTRO_AT - 0.22 * j,
              pan=-0.25 + 0.1 * j)
    return master(buf)


STYLES = {
    "lofi": build_lofi,
    "synthwave": build_synthwave,
    "acoustic": build_acoustic,
    "deephouse": build_deephouse,
    "keynote": build_keynote,
}


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
    style = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STYLE
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    if style not in STYLES:
        sys.exit(f"unknown style {style!r} — pick from {', '.join(STYLES)}")
    write(STYLES[style](), out=out)
    print(f"track [{style}] → {out}")
