"""Synthetic test song with known ground truth: D minor, 96 BPM, chords per beat,
a vibrato 'vocal' stem, and a Bayati-style E half-flat in bars 13–16."""
import math
import wave

import numpy as np

SR = 44100
BPM = 96.0
BEAT = 60.0 / BPM
LEAD = 0.4
BARS = 24
# chord per beat for an 8-bar cycle: (root pc, quality)
CYCLE = ([(2, "min")] * 4 + [(10, "maj")] * 4 + [(5, "maj")] * 4 + [(0, "maj")] * 4 +
         [(2, "min")] * 4 + [(10, "maj")] * 4 + [(7, "min")] * 2 + [(9, "maj")] * 2 + [(2, "min")] * 4)
# melody per bar: list of (beats, midi) — D minor around D4..D5
MEL = [
    [(1, 62), (1, 64), (1, 65), (1, 67)], [(2, 65), (1, 62), (1, 62)], [(1, 69), (1, 67), (2, 65)], [(2, 64), (2, 67)],
    [(1, 69), (1, 70), (1, 69), (1, 67)], [(2, 65), (2, 62)], [(1, 67), (1, 65), (1, 64), (1, 61)], [(4, 62)],
]
QT_BARS = range(12, 16)  # 0-based bars 13..16 sing E (64) 50 cents flat


def ground_truth():
    chords, notes = [], []
    for b in range(BARS):
        for q in range(4):
            chords.append((LEAD + (b * 4 + q) * BEAT, CYCLE[(b % 8) * 4 + q]))
        t = LEAD + b * 4 * BEAT
        for beats, m in MEL[b % 8]:
            cents = -50 if (b in QT_BARS and m == 64) else 0
            notes.append({"start": t, "end": t + beats * BEAT, "midi": m, "cents": cents, "bar": b + 1})
            t += beats * BEAT
    return chords, notes


def tone(f, dur, harm, att=0.01, rel=0.1, decay=0.0):
    t = np.arange(int(dur * SR)) / SR
    y = sum(a * np.sin(2 * np.pi * f * (k + 1) * t) for k, a in enumerate(harm))
    env = np.minimum(1, t / att) * np.minimum(1, np.maximum(0, (dur - t) / rel)) * np.exp(-decay * t)
    return y * env


def synth():
    rng = np.random.default_rng(3)
    chords, notes = ground_truth()
    n = int((LEAD + BARS * 4 * BEAT + 1.5) * SR)
    inst = np.zeros(n)
    voc = np.zeros(n)

    def add(buf, t, sig, g):
        i = int(t * SR)
        k = min(len(sig), len(buf) - i)
        buf[i:i + k] += g * sig[:k]
    for i, (t, (r, q)) in enumerate(chords):
        third = 3 if q == "min" else 4
        if i % 2 == 0:  # chord stabs on beats 1 and 3, held two beats
            for iv in (0, third, 7, 12):
                add(inst, t, tone(440 * 2 ** ((48 + r + iv - 69) / 12), 2 * BEAT, (1, .5, .3, .15), decay=1.2), .09)
        add(inst, t, tone(440 * 2 ** ((36 + r - 69) / 12), BEAT * .95, (1, .4, .2)), .25)
    kt = np.arange(int(.25 * SR)) / SR
    kick = np.sin(2 * np.pi * (50 * kt + 70 * (1 - np.exp(-kt * 30)) / 30)) * np.exp(-kt * 11)
    snare = rng.standard_normal(int(.15 * SR)) * np.exp(-np.arange(int(.15 * SR)) / SR * 25)
    for i, (t, _) in enumerate(chords):
        add(inst, t, kick, .7 if i % 4 == 0 else .45 if i % 2 == 0 else 0)
        if i % 2 == 1:
            add(inst, t, snare, .25)
    # vocal: legato with short portamento, vibrato, repeated notes re-articulated
    prev = None
    for nt in notes:
        dur = nt["end"] - nt["start"]
        t = np.arange(int(dur * SR)) / SR
        target = nt["midi"] + nt["cents"] / 100
        start = prev if prev is not None and abs(prev - target) < 7 else target
        glide = np.minimum(1, t / 0.05)
        pitch = start + (target - start) * glide + 0.2 * np.sin(2 * np.pi * 5.5 * t) * np.minimum(1, t / 0.25)
        f = 440 * 2 ** ((pitch - 69) / 12)
        ph = 2 * np.pi * np.cumsum(f) / SR
        y = sum(a * np.sin((k + 1) * ph) for k, a in enumerate((1, .6, .4, .25, .15, .08)))
        env = np.minimum(1, t / 0.04) * np.minimum(1, np.maximum(0, (dur - t) / 0.06))
        add(voc, nt["start"], y * env, .22)
        prev = target
    mix = inst + voc
    g = 0.9 / np.abs(mix).max()
    return mix * g, voc * g, chords, notes


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with open(path, "wb") as fh:
        w = wave.open(fh, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
        w.close()
