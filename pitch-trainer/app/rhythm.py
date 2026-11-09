"""Metronome and Arabic rhythms: synthesised click / dum / tak, the beat grid for
exercises (quantize + count-in), the click track for a session and seamless
loops for the «الإيقاع» tab. No audio device code here (selftest drives it)."""
import math

import numpy as np

from engine import SR

BPM_MIN, BPM_MAX = 40, 200
COUNT_IN = 4          # beats before the first note when the metronome is on

# one bar, in steps of `div` per beat: D = dum, T = tak, k = soft tak, A/a = click accent/normal
RHYTHMS = [
    ("click44", "ميترونوم 4/4", 1, "Aaaa"),
    ("click24", "ميترونوم 2/4", 1, "Aa"),
    ("click34", "ميترونوم 3/4", 1, "Aaa"),
    ("maqsoum", "مقسوم", 2, "DT.TD.T."),
    ("baladi", "بلدي", 2, "DD.TD.T."),
    ("saidi", "صعيدي", 2, "DT.DD.T."),
    ("malfuf", "ملفوف", 2, "D..T..T."),
    ("wahda", "وحدة", 2, "D..T.kTk"),
    ("ayyub", "أيوب", 2, "D.kDT.kk"),
]
RHYTHM = {k: (ar, div, pat) for k, ar, div, pat in RHYTHMS}
UNITS = [("1", "ضربة"), ("2", "ضربتين"), ("0.5", "نص ضربة")]


def clamp_bpm(b):
    try:
        b = float(b)
    except (TypeError, ValueError):
        b = 80.0
    return max(BPM_MIN, min(BPM_MAX, b))


def beat_of(bpm):
    return 60.0 / clamp_bpm(bpm)


# ------------------------------------------------------------------- sounds
def _env(n, ms):
    return np.exp(-np.arange(n) / (ms / 1000.0 * SR))


def click(accent=False, amp=0.5):
    """Woodblock-like click: two short partials, high enough (1.4–2.1 kHz)
    to sit above the voice range the pitch tracker reads."""
    n = int(0.045 * SR)
    t = np.arange(n) / SR
    f = 2100.0 if accent else 1500.0
    y = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(2 * np.pi * f * 2.7 * t)) * _env(n, 9)
    return (y / np.abs(y).max() * amp * (1.0 if accent else 0.7)).astype(np.float64)


def dum(amp=0.6):
    n = int(0.28 * SR)
    t = np.arange(n) / SR
    f = 95.0 * np.exp(-t * 6) + 60.0                 # pitch drop, like a darbuka dum
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.sin(ph) * _env(n, 90)
    y += 0.15 * np.random.default_rng(1).uniform(-1, 1, n) * _env(n, 6)
    return (y / np.abs(y).max() * amp).astype(np.float64)


def tak(amp=0.45, soft=False):
    n = int(0.07 * SR)
    rng = np.random.default_rng(2 if soft else 3)
    y = rng.uniform(-1, 1, n)
    y = y - np.concatenate(([0.0], y[:-1]))          # brighter
    t = np.arange(n) / SR
    y = 0.7 * y + 0.5 * np.sin(2 * np.pi * 880 * t)
    y *= _env(n, 12 if soft else 16)
    return (y / np.abs(y).max() * amp * (0.55 if soft else 1.0)).astype(np.float64)


_SOUNDS = {}


def sound(ch):
    if ch not in _SOUNDS:
        _SOUNDS[ch] = {"A": lambda: click(True), "a": lambda: click(False), "D": dum,
                       "T": tak, "k": lambda: tak(soft=True)}[ch]()
    return _SOUNDS[ch]


def _add(out, y, t):
    a = int(round(t * SR))
    if a >= len(out) or a < 0:
        return
    k = min(len(y), len(out) - a)
    out[a:a + k] += y[:k]


# ------------------------------------------------------------- tab loop
def bar_loop(kind, bpm):
    """One seamless bar of the rhythm at bpm (tails wrap round to the start)."""
    ar, div, pat = RHYTHM.get(kind, RHYTHM["click44"])
    beat = beat_of(bpm)
    step = beat / div
    n = int(round(step * len(pat) * SR))
    out = np.zeros(2 * n)
    for i, ch in enumerate(pat):
        if ch != ".":
            _add(out, sound(ch), i * step)
    loop = out[:n] + out[n:]
    return np.clip(loop, -0.95, 0.95).astype(np.float32)


def bar_len(kind, bpm):
    ar, div, pat = RHYTHM.get(kind, RHYTHM["click44"])
    return beat_of(bpm) / div * len(pat)


def tap_bpm(taps):
    """BPM from tap times (seconds): median of the last intervals, ignoring a
    pause longer than 2 s (a new tapping run)."""
    taps = list(taps)[-8:]
    iv = [b - a for a, b in zip(taps, taps[1:]) if 0.2 < b - a < 2.0]
    if len(iv) < 2:
        return None
    return round(clamp_bpm(60.0 / float(np.median(iv))))


# ------------------------------------------------------ exercises on the beat
def quantize_plan(plan, bpm, unit=1.0, snap_notes=True):
    """Put a laid-out exercise on the beat grid that starts at session time 0:
    a COUNT_IN-beat lead, every note an exact number of `unit` beats (the
    exercise's usual note becomes one unit; long single holds keep their
    length rounded up to whole beats), gaps/tails in whole beats. Sets
    plan.metro = {"beat", "t0", "end"} for the click track."""
    import engine as E
    beat = beat_of(bpm)
    u = beat * float(unit)
    steps = plan.steps
    if not steps:
        return plan
    if snap_notes:
        for st in steps:
            segs = st.segs
            if not segs:
                continue
            durs = [s.t1 - s.t0 for s in segs]
            if len(segs) == 1:
                scale = 1.0
                grid = beat
            else:
                base = float(np.median(durs))
                scale = u / base if base > 0 else 1.0
                grid = u
            t = 0.0
            for s, d in zip(segs, durs):
                n = max(1, int(round(d * scale / grid))) if len(segs) > 1 else max(1, int(math.ceil(d / grid - 1e-6)))
                s.t0, s.t1 = t, t + n * grid
                t = s.t1
            old_dur, old_ll = st.dur, st.listen_len
            st.dur = segs[-1].t1
            if old_ll >= old_dur - 1e-9:
                st.listen_len = st.dur
            else:   # partial reference (onset, stability, ajnas): keep it on whole beats
                st.listen_len = min(st.dur, max(beat, math.ceil(old_ll * (st.dur / old_dur) / beat - 1e-6) * beat))
        gap = max(1, int(math.ceil(E.GAP / beat - 1e-6))) * beat
        tail = max(1, int(math.ceil(E.TAIL / beat - 1e-6))) * beat
        lead = COUNT_IN * beat
        if plan.drone:
            lead = max(lead, math.ceil(3.0 / beat - 1e-6) * beat)
        E.layout(steps, lead=lead, gap=gap, tail=tail)
        first = 0.0
    else:
        st0 = steps[0]
        at = st0.listen0 if st0.listen else st0.sing0
        dt = COUNT_IN * beat - at
        if dt > 0:
            shift_plan(plan, dt)
            at += dt
        first = at - COUNT_IN * beat
    last = steps[-1].sing1
    end = plan.end if plan.end is not None else last + 0.6
    plan.metro = {"beat": beat, "t0": max(0.0, first), "end": end, "bpm": round(clamp_bpm(bpm))}
    return plan


def shift_plan(plan, dt):
    for st in plan.steps:
        st.listen0 += dt
        st.listen1 += dt
        st.sing0 += dt
        st.sing1 += dt
    if plan.end is not None:
        plan.end += dt


def click_track(metro, beats_per_bar=4):
    """Clicks from metro['t0'] to metro['end'], accent on every bar's first beat."""
    beat, t0, end = metro["beat"], metro["t0"], metro["end"]
    out = np.zeros(int((end + 0.2) * SR))
    t, i = t0, 0
    while t < end:
        _add(out, sound("A" if i % beats_per_bar == 0 else "a"), t)
        t += beat
        i += 1
    return np.clip(out, -0.95, 0.95).astype(np.float32)


def beat_times(metro):
    beat, t0, end = metro["beat"], metro["t0"], metro["end"]
    n = int(math.floor((end - t0) / beat + 1e-9)) + 1
    return [t0 + i * beat for i in range(n) if t0 + i * beat < end]
