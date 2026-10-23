"""PitchTrainer engine: pitch detection (YIN), exercises, maqam targets, scoring,
reference tones, WAV recording and the practice history. No GUI and no audio
device code here, so selftest.py and the Windows CI can drive all of it.

Pitches are MIDI numbers as floats (60 = C4, 60.5 = C4 + 50 cents), so maqam
quarter tones are just .5 steps and nothing is forced onto the 12-tone grid.
"""
import datetime
import json
import math
import os
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HISTORY_FILE = os.path.join(ROOT, "history", "history.json")
REC_DIR = os.path.join(ROOT, "recordings")

SR = 44100
FRAME = 2048          # 46 ms analysis window (two periods of ~E2)
HOP = 256             # a new pitch every 5.8 ms
FMIN, FMAX = 65.0, 1050.0
TOP_LIMIT = 67        # G4 — nothing above this is ever generated
LOW_LIMIT = 36        # C2

GREEN, YELLOW = 15.0, 35.0   # cents
ATTACK = 0.20                # seconds ignored at the start of every note
GLIDE_SLACK = 0.25           # siren: compare with the target ±0.25 s around now
WRONG_NOTE = 300.0           # |error| above this = a different note (not averaged)

NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


# --------------------------------------------------------------------- notes
def hz_to_midi(f):
    return 69.0 + 12.0 * math.log2(f / 440.0)


def midi_to_hz(m):
    return 440.0 * 2.0 ** ((m - 69.0) / 12.0)


def note_name(m):
    n = int(math.floor(m + 0.5))  # quarter tones read as half-flats (E3 -50c)
    return "%s%d" % (NAMES[n % 12], n // 12 - 1)


def parse_note(s):
    s = s.strip()
    for i in (2, 1):
        if s[:i] in NAMES:
            return NAMES.index(s[:i]) + 12 * (int(s[i:]) + 1)
    raise ValueError(s)


def note_label(m):
    """'D3' or 'E3 -50c' for a quarter tone — the target stays in cents."""
    n = int(math.floor(m + 0.5))  # quarter tones read as half-flats (E3 -50c)
    c = (m - n) * 100.0
    if abs(c) < 0.5:
        return note_name(n)
    return "%s %+.0fc" % (note_name(n), c)


def cents(f_sung, target_midi):
    return (hz_to_midi(f_sung) - target_midi) * 100.0


def zone(err):
    a = abs(err)
    return "green" if a <= GREEN else ("yellow" if a <= YELLOW else "red")


def note_choices(lo=LOW_LIMIT, hi=TOP_LIMIT):
    return [note_name(m) for m in range(lo, hi + 1)]


# ------------------------------------------------------------ pitch detection
class PitchTracker:
    """Streaming YIN. feed() takes any block size and returns one
    (time_s, f0_hz or None, confidence, rms_db) per HOP samples."""

    def __init__(self, sr=SR, frame=FRAME, hop=HOP, fmin=FMIN, fmax=FMAX,
                 threshold=0.15, gate_db=-50.0):
        self.sr, self.frame, self.hop = sr, frame, hop
        self.tau_min = max(2, int(sr / fmax))
        self.tau_max = min(frame // 2, int(math.ceil(sr / fmin)))
        self.w = frame - self.tau_max
        self.nfft = 1 << int(math.ceil(math.log2(frame + self.w)))
        self.threshold, self.gate_db = threshold, gate_db
        self.buf = np.zeros(frame, np.float64)
        self.filled = 0
        self.pending = 0
        self.n = 0                      # samples consumed
        self.hist = []                  # last raw f0 for the median-of-3

    def reset(self):
        self.buf[:] = 0
        self.filled = self.pending = self.n = 0
        self.hist = []

    def detect(self, x):
        """f0, confidence for one frame (len == self.frame)."""
        x = x - x.mean()
        W, tmax = self.w, self.tau_max
        X = np.fft.rfft(x, self.nfft)
        Y = np.fft.rfft(x[:W], self.nfft)
        acf = np.fft.irfft(X * np.conj(Y), self.nfft)[:tmax + 1]
        sq = np.concatenate(([0.0], np.cumsum(x * x)))
        e0 = sq[W]
        taus = np.arange(tmax + 1)
        et = sq[taus + W] - sq[taus]
        d = np.maximum(e0 + et - 2.0 * acf, 0.0)
        cm = np.cumsum(d[1:])
        dn = np.ones_like(d)
        dn[1:] = d[1:] * np.arange(1, len(d)) / np.maximum(cm, 1e-12)
        lo, hi = self.tau_min, tmax
        seg = dn[lo:hi + 1]
        below = np.nonzero(seg < self.threshold)[0]
        if len(below):
            t = lo + below[0]
            while t + 1 <= hi and dn[t + 1] < dn[t]:
                t += 1
        else:
            t = lo + int(np.argmin(seg))
            if dn[t] > 0.35:
                return None, 1.0 - float(dn[t])
        # parabolic interpolation
        if lo < t < hi:
            a, b, c = dn[t - 1], dn[t], dn[t + 1]
            den = a - 2 * b + c
            off = 0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0
            off = max(-1.0, min(1.0, off))
        else:
            off = 0.0
        return self.sr / (t + off), 1.0 - float(dn[t])

    def feed(self, block):
        block = np.asarray(block, np.float64).ravel()
        out = []
        pos = 0
        while pos < len(block):
            need = self.hop - self.pending
            take = block[pos:pos + need]
            pos += len(take)
            k = len(take)
            self.buf = np.roll(self.buf, -k)
            self.buf[-k:] = take
            self.filled = min(self.frame, self.filled + k)
            self.pending += k
            self.n += k
            if self.pending < self.hop:
                break
            self.pending = 0
            if self.filled < self.frame:
                continue
            rms = math.sqrt(float(np.mean(self.buf * self.buf))) + 1e-12
            db = 20 * math.log10(rms)
            f0, conf = (None, 0.0)
            if db > self.gate_db:
                f0, conf = self.detect(self.buf)
            self.hist = (self.hist + [f0])[-3:]
            vals = [v for v in self.hist if v]
            if f0 and len(vals) >= 2:      # median of the last three voiced
                f0 = float(np.median(vals))
            t = (self.n - self.frame / 2) / self.sr   # centre of the window
            out.append((t, f0, conf, db))
        return out


# --------------------------------------------------------------------- maqam
# cents from the tonic, Arabic 24-tone practice values (150 = three-quarter tone)
MAQAMAT = {
    "Bayati":   {"ar": "بياتي",  "up": [0, 150, 300, 500, 700, 800, 1000, 1200]},
    "Saba":     {"ar": "صبا",    "up": [0, 150, 300, 400, 700, 800, 1000, 1100]},
    "Rast":     {"ar": "راست",   "up": [0, 200, 350, 500, 700, 900, 1050, 1200]},
    "Hijaz":    {"ar": "حجاز",   "up": [0, 100, 400, 500, 700, 800, 1000, 1200]},
    "Nahawand": {"ar": "نهاوند", "up": [0, 200, 300, 500, 700, 800, 1100, 1200],
                 "down": [1200, 1000, 800, 700, 500, 300, 200, 0]},
}


def maqam_pattern(name, span="jins"):
    m = MAQAMAT[name]
    up = m["up"] if span == "full" else m["up"][:5]
    if span == "full" and "down" in m:
        down = m["down"][1:]
    else:
        down = list(reversed(up))[1:]
    return up + down


# ------------------------------------------------------------------ exercises
EXERCISES = {
    "siren":   {"ar": "سايرن / جلايد",      "en": "Siren"},
    "scale5":  {"ar": "سكيل 5 نوتات",       "en": "5-note scale"},
    "arpeggio": {"ar": "أربيجيو",           "en": "Arpeggio"},
    "sustain": {"ar": "نوتة طويلة",         "en": "Sustain"},
    "octave":  {"ar": "نطّة أوكتاف",        "en": "Octave jump"},
}
TEMPI = {"slow": 1.0, "normal": 0.7, "fast": 0.5}


class Seg:
    __slots__ = ("t0", "t1", "m0", "m1", "label")

    def __init__(self, t0, t1, m0, m1=None, label=""):
        self.t0, self.t1, self.m0 = t0, t1, m0
        self.m1 = m0 if m1 is None else m1
        self.label = label

    @property
    def glide(self):
        return self.m0 != self.m1

    def at(self, t):
        if not self.glide:
            return self.m0
        u = min(1.0, max(0.0, (t - self.t0) / (self.t1 - self.t0)))
        return self.m0 + (self.m1 - self.m0) * (0.5 - 0.5 * math.cos(math.pi * u))

    def top(self):
        return max(self.m0, self.m1)


def notes_to_segs(rel, root, dur, labels=None):
    segs, t = [], 0.0
    for i, (r, d) in enumerate(zip(rel, dur)):
        segs.append(Seg(t, t + d, root + r, label=labels[i] if labels else ""))
        t += d
    return segs


def pattern(kind, root, tempo="normal", maqam=None, span="jins"):
    """Segments of one step, times relative to the step start."""
    q = TEMPI[tempo]
    if kind == "maqam":
        cs = maqam_pattern(maqam, span)
        rel = [c / 100.0 for c in cs]
        labels = ["%d c" % c for c in cs]
        dur = [q] * len(rel)
        dur[len(rel) // 2] = q * 1.6
        dur[-1] = q * 2
        return notes_to_segs(rel, root, dur, labels)
    if kind == "siren":
        g = 2.0 + 1.2 * q
        return [Seg(0.0, 0.4, root), Seg(0.4, 0.4 + g, root, root + 7),
                Seg(0.4 + g, 0.8 + g, root + 7), Seg(0.8 + g, 0.8 + 2 * g, root + 7, root),
                Seg(0.8 + 2 * g, 1.4 + 2 * g, root)]
    if kind == "scale5":
        rel = [0, 2, 4, 5, 7, 5, 4, 2, 0]
        dur = [q] * 8 + [q * 2]
    elif kind == "arpeggio":
        rel = [0, 4, 7, 12, 7, 4, 0]
        dur = [q] * 3 + [q * 1.5] + [q] * 2 + [q * 2]
    elif kind == "sustain":
        rel, dur = [0], [4.0 if tempo != "fast" else 3.0]
    elif kind == "octave":
        rel = [0, 12, 0]
        dur = [q * 1.5, q * 2, q * 2]
    else:
        raise ValueError(kind)
    return notes_to_segs(rel, root, dur)


def span_of(kind, maqam=None, span="jins"):
    return max(s.top() for s in pattern(kind, 0.0, "normal", maqam, span))


def roots(kind, start, bottom, top, direction, maqam=None, span="jins"):
    """Step roots: semitone moves from start, never letting a note go above
    min(top, G4) or a root go below bottom."""
    top = min(top, TOP_LIMIT)
    sp = span_of(kind, maqam, span)
    hi_root = math.floor(top - sp + 1e-9)
    lo_root = max(LOW_LIMIT, bottom)
    if start > hi_root:
        return []
    up = list(range(max(start, lo_root), hi_root + 1))
    down = list(range(min(start, hi_root), lo_root - 1, -1))
    if direction == "up":
        return up
    if direction == "down":
        return down
    return up + list(reversed(up))[1:]  # up then back down to the start


class Step:
    def __init__(self, index, root, segs):
        self.index, self.root, self.segs = index, root, segs
        self.dur = segs[-1].t1
        self.listen0 = self.listen1 = self.sing0 = self.sing1 = 0.0

    def top(self):
        return max(s.top() for s in self.segs)


LEAD_IN = 0.6
GAP = 0.45   # silence between the reference and your turn
TAIL = 0.7   # extra time after the pattern before the next reference


def build_plan(kind, start, bottom, top, direction, tempo="normal", maqam=None, span="jins"):
    steps, t = [], LEAD_IN
    for i, r in enumerate(roots(kind, start, bottom, top, direction, maqam, span)):
        st = Step(i, r, pattern(kind, float(r), tempo, maqam, span))
        st.listen0, st.listen1 = t, t + st.dur
        st.sing0 = st.listen1 + GAP
        st.sing1 = st.sing0 + st.dur
        t = st.sing1 + TAIL
        steps.append(st)
    for st in steps:
        assert st.top() <= TOP_LIMIT + 1e-9, "note above G4"
    return steps


# ------------------------------------------------------------------- session
class Session:
    """Takes tracker frames (session time = input samples / sr), knows which
    phase/target is active and keeps everything the graph and the score need."""

    def __init__(self, steps=None, mode="exercise", info=None):
        self.steps = steps or []
        self.mode = mode            # exercise | maqam | free
        self.info = info or {}
        self.frames = []            # (t, sung_midi|None, target|None, err|None, phase, step_index)
        self.now = 0.0
        self.end = self.steps[-1].sing1 + 0.3 if self.steps else None
        self._si = 0
        self.level_db = -120.0

    def done(self):
        return self.end is not None and self.now >= self.end

    def phase_at(self, t):
        """('lead'|'listen'|'gap'|'sing'|'tail'|'end', step)"""
        if not self.steps:
            return "free", None
        while self._si + 1 < len(self.steps) and t >= self.steps[self._si + 1].listen0:
            self._si += 1
        while self._si > 0 and t < self.steps[self._si].listen0:
            self._si -= 1
        st = self.steps[self._si]
        if t < st.listen0:
            return "lead", st
        if t < st.listen1:
            return "listen", st
        if t < st.sing0:
            return "gap", st
        if t < st.sing1:
            return "sing", st
        if self._si == len(self.steps) - 1:
            return "end", st
        return "tail", st

    @staticmethod
    def seg_at(st, rel):
        for s in st.segs:
            if s.t0 <= rel < s.t1:
                return s
        return None

    def target_at(self, t):
        ph, st = self.phase_at(t)
        if ph == "listen":
            s = self.seg_at(st, t - st.listen0)
            return (s.at(t - st.listen0), s) if s else (None, None)
        if ph == "sing":
            s = self.seg_at(st, t - st.sing0)
            return (s.at(t - st.sing0), s) if s else (None, None)
        return None, None

    def error(self, t, m):
        """cents error against the target sung at time t (None = not scored)."""
        ph, st = self.phase_at(t)
        if ph != "sing":
            return None, None
        rel = t - st.sing0
        s = self.seg_at(st, rel)
        if s is None:
            return None, None
        if s.glide:
            ts = np.linspace(max(s.t0, rel - GLIDE_SLACK), min(s.t1, rel + GLIDE_SLACK), 11)
            cand = np.array([s.at(x) for x in ts])
            e = (m - cand) * 100.0
            return float(e[np.argmin(np.abs(e))]), s.at(rel)
        if rel - s.t0 < ATTACK:
            return None, s.m0
        return (m - s.m0) * 100.0, s.m0

    def process(self, frames):
        for t, f0, conf, db in frames:
            self.now = t
            self.level_db = db
            m = hz_to_midi(f0) if f0 else None
            if self.mode == "free":
                tgt = float(math.floor(m + 0.5)) if m is not None else None
                err = (m - tgt) * 100.0 if m is not None else None
                self.frames.append((t, m, tgt, err, "free", -1))
                continue
            ph, st = self.phase_at(t)
            err, tgt = (None, None)
            if m is not None:
                err, tgt = self.error(t, m)
            if tgt is None:
                tgt, _ = self.target_at(t)
            self.frames.append((t, m, tgt, err, ph, st.index if st else -1))

    # ---- score
    def score(self):
        errs = np.array([f[3] for f in self.frames if f[3] is not None and f[1] is not None])
        voiced = [f for f in self.frames if f[1] is not None]
        res = {"frames": int(len(errs)), "voiced_s": round(len(voiced) * HOP / SR, 1)}
        if len(errs):
            a = np.abs(errs)
            near = a[a <= WRONG_NOTE]
            res["avg_cents"] = round(float(near.mean()), 1) if len(near) else None
            res["in_tune_pct"] = round(100.0 * float(np.mean(a <= GREEN)), 1)
            res["near_pct"] = round(100.0 * float(np.mean(a <= YELLOW)), 1)
            res["wrong_note_pct"] = round(100.0 * float(np.mean(a > WRONG_NOTE)), 1)
            res["bias_cents"] = round(float(np.median(errs[a <= WRONG_NOTE])), 1) if len(near) else None
        else:
            res.update(avg_cents=None, in_tune_pct=None, near_pct=None, wrong_note_pct=None, bias_cents=None)
        hi, lo = stable_range([f[1] for f in self.frames])
        res["highest_midi"], res["lowest_midi"] = hi, lo
        res["highest"] = note_label_rounded(hi)
        res["lowest"] = note_label_rounded(lo)
        return res


def note_label_rounded(m):
    if m is None:
        return "—"
    n = int(math.floor(m + 0.5))  # quarter tones read as half-flats (E3 -50c)
    c = (m - n) * 100
    return note_name(n) if abs(c) < 5 else "%s %+.0fc" % (note_name(n), c)


def stable_range(midis, win=52, max_std=0.15):
    """Highest/lowest note held steady: a 0.3 s window (52 hops) fully voiced
    with a spread under 15 cents. Returns the window medians' max and min."""
    v = np.array([np.nan if m is None else m for m in midis], float)
    if len(v) < win:
        return None, None
    best_hi = best_lo = None
    for i in range(0, len(v) - win + 1, 4):
        w = v[i:i + win]
        if np.isnan(w).any():
            continue
        if float(np.std(w)) <= max_std:
            med = float(np.median(w))
            best_hi = med if best_hi is None else max(best_hi, med)
            best_lo = med if best_lo is None else min(best_lo, med)
    return best_hi, best_lo


# -------------------------------------------------------------- reference audio
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
        att = np.minimum(1.0, t / 0.004)
        y *= att
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


def render_step(st, sr=SR, kind="piano"):
    out = np.zeros(int(st.dur * sr) + int(0.5 * sr))
    for s in st.segs:
        a = int(s.t0 * sr)
        if s.glide:
            y = glide_tone(s.m0, s.m1, s.t1 - s.t0, sr)
        elif kind == "piano":
            y = tone(s.m0, s.t1 - s.t0 + 0.35, sr, "piano")
        else:
            y = tone(s.m0, s.t1 - s.t0, sr, "tone")
        out[a:a + len(y)] += y[:len(out) - a]
    return np.clip(out, -0.95, 0.95).astype(np.float32)


# ---------------------------------------------------------- synthetic singer
def synth_voice(midi_track, sr=SR, vibrato_cents=0.0, vib_hz=5.5, noise=0.003, seed=0,
                harmonics=(1.0, 0.7, 0.5, 0.35, 0.25, 0.15, 0.1, 0.06), weak_fund=False):
    """Harmonic 'voice' following a per-sample MIDI track (NaN = silence)."""
    rng = np.random.default_rng(seed)
    mt = np.asarray(midi_track, float)
    voiced = ~np.isnan(mt)
    m = np.where(voiced, mt, 60.0)
    n = len(m)
    t = np.arange(n) / sr
    m = m + vibrato_cents / 100.0 * np.sin(2 * np.pi * vib_hz * t)
    f = 440.0 * 2 ** ((m - 69) / 12)
    ph = 2 * np.pi * np.cumsum(f) / sr
    hs = list(harmonics)
    if weak_fund:
        hs[0] = 0.15
    y = np.zeros(n)
    for k, a in enumerate(hs, 1):
        y += a * np.where(f * k < sr / 2.2, 1.0, 0.0) * np.sin(k * ph)
    # smooth on/off so silences do not click
    g = voiced.astype(float)
    k = int(0.015 * sr)
    if k > 1:
        g = np.convolve(g, np.ones(k) / k, mode="same")
    y = y * g * 0.25 + noise * rng.standard_normal(n)
    return y.astype(np.float32)


def singer_track(steps, end, sr=SR, offset_cents=0.0, late=0.06):
    """Per-sample MIDI that 'sings' each sing phase (a little late, offset)."""
    n = int(end * sr)
    tr = np.full(n, np.nan)
    for st in steps:
        for s in st.segs:
            a = int((st.sing0 + s.t0 + late) * sr)
            b = min(n, int((st.sing0 + s.t1 + late) * sr) - int(0.03 * sr))
            if b <= a:
                continue
            u = np.arange(b - a) / sr
            if s.glide:
                vals = np.array([s.at(s.t0 + x) for x in u])
            else:
                vals = np.full(b - a, s.m0)
            tr[a:b] = vals + offset_cents / 100.0
    return tr


# ----------------------------------------------------------------- recording
class WavRecorder:
    def __init__(self, path, sr=SR):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.fh = open(path, "wb")       # open(): Arabic paths on every OS
        self.w = wave.open(self.fh, "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(sr)
        self.n = 0

    def write(self, block):
        x = np.clip(np.asarray(block, np.float32).ravel(), -1, 1)
        self.w.writeframes((x * 32767).astype("<i2").tobytes())
        self.n += len(x)

    def close(self):
        try:
            self.w.close()
        finally:
            self.fh.close()


def recording_path(label, when=None):
    when = when or datetime.datetime.now()
    safe = "".join(c for c in label if c not in '\\/:*?"<>|').strip() or "session"
    return os.path.join(REC_DIR, "%s - %s.wav" % (when.strftime("%Y-%m-%d %H%M%S"), safe))


# ------------------------------------------------------------------- history
def load_history(path=None):
    path = path or HISTORY_FILE
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_session(entry, path=None):
    path = path or HISTORY_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    h = load_history(path)
    h.append(entry)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return h


def daily(history):
    """One row per day: weighted avg cents, % in tune, best high/low, minutes."""
    days = {}
    for e in history:
        if not e.get("frames"):
            continue
        d = days.setdefault(e["date"], {"date": e["date"], "w": 0, "c": 0.0, "it": 0.0,
                                        "hi": None, "lo": None, "sessions": 0, "minutes": 0.0})
        w = e["frames"]
        d["sessions"] += 1
        d["minutes"] += e.get("duration_s", 0) / 60.0
        if e.get("avg_cents") is not None:
            d["c"] += e["avg_cents"] * w
            d["it"] += (e.get("in_tune_pct") or 0) * w
            d["w"] += w
        for k, fn in (("hi", max), ("lo", min)):
            v = e.get("highest_midi" if k == "hi" else "lowest_midi")
            if v is not None:
                d[k] = v if d[k] is None else fn(d[k], v)
    rows = []
    for k in sorted(days):
        d = days[k]
        rows.append({"date": k, "avg_cents": round(d["c"] / d["w"], 1) if d["w"] else None,
                     "in_tune_pct": round(d["it"] / d["w"], 1) if d["w"] else None,
                     "highest_midi": d["hi"], "lowest_midi": d["lo"],
                     "sessions": d["sessions"], "minutes": round(d["minutes"], 1)})
    return rows


def history_entry(session, duration_s, when=None, wav=None):
    when = when or datetime.datetime.now()
    sc = session.score()
    e = {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
         "duration_s": round(duration_s, 1), "wav": wav}
    e.update(session.info)
    e.update(sc)
    return e
