"""Reference sounds: an oud-like plucked string (Karplus-Strong, any pitch in
cents, so quarter tones are exact), the old piano / soft tone, the drone loop
and the calibration clicks."""
import math

import numpy as np

from engine import SR, midi_to_hz

OS = 2  # render the string at 2x and resample to the exact pitch


def pluck(m, dur, sr=SR, amp=0.4, t60=None, bright=0.5, seed=None):
    """One plucked note. The string loop runs per period block (vectorised);
    the integer delay at 2x rate is then time-warped onto the exact pitch."""
    f = midi_to_hz(m)
    sro = sr * OS
    D = sro / f
    N = max(2, int(round(D - 0.5)))
    f_r = sro / (N + 0.5)                      # pitch the integer loop really makes
    n_out = int(dur * sr)
    need = int(math.ceil(n_out * OS * f / f_r)) + 2 * N + 4
    t60 = t60 or (2.6 if f < 200 else 1.8)
    g = 10 ** (-3.0 / (t60 * f_r))             # loss per round trip
    rng = np.random.default_rng(seed if seed is not None else int(m * 100) % 9973)
    ex = rng.uniform(-1, 1, N)
    k = max(1, int(3 + 6 * (1 - bright)))      # softer plectrum = smoother burst
    ex = np.convolve(ex, np.ones(k) / k, mode="same")
    p = int(0.17 * N)                          # pluck near the bridge
    ex = ex - np.roll(ex, p)
    y = np.zeros(need)
    y[:N] = ex
    blocks = int(math.ceil(need / N))
    for b in range(1, blocks):
        a = b * N
        e = min(need, a + N)
        prev = y[a - N:e - N]
        prev1 = y[a - N - 1:e - N - 1] if a - N - 1 >= 0 else np.concatenate(([0.0], y[a - N:e - N - 1]))
        y[a:e] = g * 0.5 * (prev + prev1)
    # time-warp onto the exact frequency, back at sr
    pos = np.arange(n_out) * OS * (f / f_r)
    out = np.interp(pos, np.arange(need), y)
    # a touch of body: gentle low-mid emphasis
    body = np.convolve(out, np.exp(-np.arange(200) / 40.0) / 40.0, mode="full")[:n_out]
    out = 0.8 * out + 0.6 * body
    att = np.minimum(1.0, np.arange(n_out) / (0.002 * sr))
    rel = min(n_out, int(0.04 * sr))
    out *= att
    if rel:
        out[-rel:] *= np.linspace(1, 0, rel)
    return (out / (np.abs(out).max() + 1e-12) * amp).astype(np.float64)


def tone(m, dur, sr=SR, kind="piano", amp=0.35):
    n = int(dur * sr)
    t = np.arange(n) / sr
    f = midi_to_hz(m)
    if kind == "piano":
        y = np.zeros(n)
        B = 0.0004  # slight inharmonicity
        for k in range(1, 9):
            fk = f * k * math.sqrt(1 + B * k * k)
            if fk > sr / 2.2:
                break
            y += (0.9 ** k) / k ** 0.6 * np.exp(-t * (1.2 + 0.7 * k)) * np.sin(2 * np.pi * fk * t + k)
        y *= np.minimum(1.0, t / 0.004)
    else:  # soft tone
        y = np.sin(2 * np.pi * f * t) + 0.25 * np.sin(4 * np.pi * f * t) + 0.08 * np.sin(6 * np.pi * f * t)
        y *= np.minimum(1.0, t / 0.03)
    rel = min(n, int(0.05 * sr))
    if rel:
        y[-rel:] *= np.linspace(1, 0, rel)
    return y / (np.abs(y).max() + 1e-12) * amp


def glide_tone(m0, m1, dur, sr=SR, amp=0.3):
    n = int(dur * sr)
    u = np.arange(n) / max(1, n - 1)
    m = m0 + (m1 - m0) * (0.5 - 0.5 * np.cos(np.pi * u))
    ph = 2 * np.pi * np.cumsum(440.0 * 2 ** ((m - 69) / 12)) / sr
    y = np.sin(ph) + 0.3 * np.sin(2 * ph) + 0.1 * np.sin(3 * ph)
    e = np.minimum(1.0, np.minimum(np.arange(n), n - np.arange(n)) / (0.02 * sr))
    return y * e / 1.4 * amp


def note(m, dur, kind="oud", sr=SR):
    if kind == "oud":
        y = pluck(m, dur + 0.6, sr)
        if dur > 1.6:   # long notes: soft re-pluck every 1.5 s, like a tremolo-less oud
            t = 1.5
            while t < dur - 0.3:
                q = pluck(m, dur - t + 0.6, sr, amp=0.25, seed=int(t * 10))
                a = int(t * sr)
                y[a:a + len(q)] += q[:len(y) - a]
                t += 1.5
        return y
    if kind == "piano":
        return tone(m, dur + 0.35, sr, "piano")
    return tone(m, dur, sr, "tone")


def render_step(st, sr=SR, kind="oud"):
    """The reference for one step (only its listen part)."""
    L = st.listen_len
    out = np.zeros(int(L * sr) + int(0.7 * sr))
    for s in st.listen_segs():
        a = int(s.t0 * sr)
        d = min(s.t1, L) - s.t0
        if s.glide:
            y = glide_tone(s.m0, s.at(min(s.t1, L)), d, sr)
        else:
            y = note(s.m0, d, kind, sr)
            if not s.scored:
                y = y * 0.8
        out[a:a + len(y)] += y[:len(out) - a]
    return np.clip(out, -0.95, 0.95).astype(np.float32)


def drone_loop(tonic, fifth=True, sr=SR, every=2.0):
    """A seamless 2 s loop: the tonic re-plucked softly every 2 s (plus the fifth),
    with the ring of the previous plucks carried over."""
    n = int(every * sr)
    total = np.zeros(4 * n)
    voices = [(tonic, 0.32)] + ([(tonic + 7, 0.17)] if fifth else [])
    for i in range(4):
        for m, a in voices:
            y = pluck(m, 2 * every, sr, amp=a, t60=3.5, bright=0.2, seed=i * 7 + int(m))
            total[i * n:i * n + len(y)] += y[:4 * n - i * n]
    return np.clip(total[2 * n:3 * n], -0.95, 0.95).astype(np.float32)


def clicks(times, pitch, sr=SR, end=None):
    end = end or times[-1] + 1.0
    out = np.zeros(int(end * sr))
    for t in times:
        y = pluck(pitch, 0.5, sr, amp=0.5, t60=0.6)
        a = int(t * sr)
        out[a:a + len(y)] += y[:len(out) - a]
    return np.clip(out, -0.95, 0.95).astype(np.float32)
