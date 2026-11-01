"""PitchTrainer engine: pitch detection (YIN), note helpers, maqam/jins tables,
exercise plans for every mode, synthetic singer, WAV recording and history.
No GUI and no audio device code here, so selftest.py and the Windows CI can
drive all of it.

Pitches are MIDI numbers as floats (60 = C4, 60.5 = C4 + 50 cents), so maqam
quarter tones are just .5 steps and nothing is forced onto the 12-tone grid.
"""
import datetime
import json
import math
import os
import random
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

GREEN, YELLOW = 15.0, 35.0   # cents: exact / close
ATTACK = 0.20                # seconds ignored at the start of every note
GLIDE_SLACK = 0.25           # siren: compare with the target ±0.25 s around now
WRONG_NOTE = 300.0           # |error| above this = a different note (not averaged)

BASELINE_CENTS, BASELINE_EXACT, GOAL_EXACT = 21.7, 32.0, 60.0

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



# ------------------------------------------------------------- maqam / jins
# cents from the tonic, Arabic 24-tone practice values (150 = three-quarter tone).
# Sikah sits on a half-flat: the tonic is the chosen note -50c (E3 -> E3 -50c).
MAQAMAT = {
    "Bayati":   {"ar": "بياتي",  "up": [0, 150, 300, 500, 700, 800, 1000, 1200]},
    "Saba":     {"ar": "صبا",    "up": [0, 150, 300, 400, 700, 800, 1000, 1100]},
    "Rast":     {"ar": "راست",   "up": [0, 200, 350, 500, 700, 900, 1050, 1200]},
    "Hijaz":    {"ar": "حجاز",   "up": [0, 100, 400, 500, 700, 800, 1000, 1200]},
    "Nahawand": {"ar": "نهاوند", "up": [0, 200, 300, 500, 700, 800, 1100, 1200],
                 "down": [1200, 1000, 800, 700, 500, 300, 200, 0]},
    "Sikah":    {"ar": "سيكاه",  "up": [0, 150, 350, 550, 700, 850, 1050, 1200], "offset": -50},
}
AJNAS = {
    "Bayati": {"ar": "بياتي", "cents": [0, 150, 300, 500]},
    "Saba":   {"ar": "صبا",   "cents": [0, 150, 300, 400]},
    "Sikah":  {"ar": "سيكاه", "cents": [0, 150, 350], "offset": -50},
    "Rast":   {"ar": "راست",  "cents": [0, 200, 350, 500, 700]},
}


def tonic_of(name, start):
    return start + MAQAMAT.get(name, AJNAS.get(name, {})).get("offset", 0) / 100.0


def maqam_pattern(name, span="jins"):
    m = MAQAMAT[name]
    up = m["up"] if span == "full" else m["up"][:5]
    if span == "full" and "down" in m:
        down = m["down"][1:]
    else:
        down = list(reversed(up))[1:]
    return up + down


def quarter(c):
    return abs((c % 100) - 50) < 1e-6


# ------------------------------------------------------------------ segments
class Seg:
    __slots__ = ("t0", "t1", "m0", "m1", "label", "scored")

    def __init__(self, t0, t1, m0, m1=None, label="", scored=True):
        self.t0, self.t1, self.m0 = t0, t1, m0
        self.m1 = m0 if m1 is None else m1
        self.label = label
        self.scored = scored

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


def notes_to_segs(rel, root, dur, labels=None, scored=None):
    segs, t = [], 0.0
    for i, (r, d) in enumerate(zip(rel, dur)):
        segs.append(Seg(t, t + d, root + r, label=labels[i] if labels else "",
                        scored=True if scored is None else scored[i]))
        t += d
    return segs


class Step:
    """One unit of the exercise: (optional) reference, then your turn.
    Times are absolute session times (target clock) after layout()."""

    def __init__(self, index, root, segs, listen=True, listen_len=None, label=""):
        self.index, self.root, self.segs = index, root, segs
        self.dur = segs[-1].t1 if segs else 0.0
        self.listen = listen
        self.listen_len = self.dur if listen_len is None else min(listen_len, self.dur)
        self.label = label
        self.lane = None            # song mode: (times rel. to sing0, midi) reference contour
        self.song_t0 = 0.0          # song mode: where this step starts in the song
        self.listen0 = self.listen1 = self.sing0 = self.sing1 = 0.0

    def top(self):
        return max((s.top() for s in self.segs), default=0.0)

    def listen_segs(self):
        return [s for s in self.segs if s.t0 < self.listen_len - 1e-9]


class Plan:
    def __init__(self, mode, steps, info, drone=None, song=None, clicks=None):
        self.mode, self.steps, self.info = mode, steps, info
        self.drone = drone          # {"tonic": midi, "fifth": bool}
        self.song = song            # song.Song (audio for playback)
        self.clicks = clicks        # calibration: pluck times
        self.end = None


LEAD_IN = 0.6
GAP = 0.45   # silence between the reference and your turn
TAIL = 0.7   # extra time after your turn before the next reference


def layout(steps, lead=LEAD_IN, gap=GAP, tail=TAIL):
    t = lead
    for st in steps:
        if st.listen:
            st.listen0, st.listen1 = t, t + st.listen_len
            st.sing0 = st.listen1 + gap
        else:
            st.listen0 = st.listen1 = st.sing0 = t
        st.sing1 = st.sing0 + st.dur
        t = st.sing1 + tail
    for st in steps:
        assert st.top() <= TOP_LIMIT + 1e-9, "note above G4"
    return steps


# -------------------------------------------------------------- basic + maqam
EXERCISES = {
    "siren":    {"ar": "سايرن / جلايد"},
    "scale5":   {"ar": "سكيل 5 نوتات"},
    "arpeggio": {"ar": "أربيجيو"},
    "sustain":  {"ar": "نوتة طويلة"},
    "octave":   {"ar": "نطّة أوكتاف"},
}
TEMPI = {"slow": 1.0, "normal": 0.7, "fast": 0.5}


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
    elif kind == "hold8":
        rel, dur = [0], [8.0]
    elif kind == "octave":
        rel = [0, 12, 0]
        dur = [q * 1.5, q * 2, q * 2]
    else:
        raise ValueError(kind)
    return notes_to_segs(rel, root, dur)


def span_of(kind, maqam=None, span="jins"):
    return max(s.top() for s in pattern(kind, 0.0, "normal", maqam, span))


def roots(kind, start, bottom, top, direction, maqam=None, span="jins", span_c=None):
    """Step roots: semitone moves from start, never letting a note go above
    min(top, G4) or a root go below bottom."""
    top = min(top, TOP_LIMIT)
    sp = span_of(kind, maqam, span) if span_c is None else span_c
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


def build_plan(kind, start, bottom, top, direction, tempo="normal", maqam=None, span="jins"):
    """Basic exercises and the maqam scale mode (kind == 'maqam')."""
    off = tonic_of(maqam, 0) if kind == "maqam" else 0.0
    steps = []
    for i, r in enumerate(roots(kind, start, bottom, top - off, direction, maqam, span)):
        steps.append(Step(i, r + off, pattern(kind, float(r) + off, tempo, maqam, span)))
    return layout(steps)


def max_start(kind, top, maqam=None, span="jins"):
    return int(math.floor(min(top, TOP_LIMIT) - span_of(kind, maqam, span) + 1e-9))


# ------------------------------------------------------------- the new modes
MODES = [
    ("basic", "تمارين أساسية"), ("maqam", "مقامات (سلّم)"), ("drone", "درون"),
    ("callresp", "ردّ الجملة"), ("song", "غنّي جوه أغنيتك"), ("onset", "دقة الدخول"),
    ("stability", "ثبات النوتة"), ("leaps", "القفزات"), ("ajnas", "الأجناس"), ("free", "حر"),
]
MODE_AR = dict(MODES)
# history chart: one line per exercise group (basic + maqam scales share a line)
GROUPS = [("basic", "أساسي"), ("drone", "درون"), ("callresp", "ردّ الجملة"), ("song", "أغنية"),
          ("onset", "الدخول"), ("stability", "الثبات"), ("leaps", "القفزات"), ("ajnas", "الأجناس")]


def group_of(mode):
    return {"maqam": "basic", "exercise": "basic"}.get(mode, mode)


DRONE_NOTE = {"slow": 3.0, "normal": 2.2, "fast": 1.6}
LEAPS = [(4, "3rd"), (5, "4th"), (7, "5th"), (12, "8ve")]
LEVELS = [(1, "متدرّجة"), (2, "قفزات صغيرة"), (3, "بزخارف")]


class PlanError(ValueError):
    pass


def check_top(tonic, span_c, top, what):
    top = min(top, TOP_LIMIT)
    if tonic + span_c / 100.0 > top + 1e-9:
        hi = math.floor(top - span_c / 100.0 + 1e-9)
        raise PlanError("%s|%s" % (what, note_name(hi)))


def plan_drone(start, maqam, span="jins", tempo="normal", top=TOP_LIMIT, fifth=True, rounds=2):
    tonic = tonic_of(maqam, start)
    cs = maqam_pattern(maqam, span)
    check_top(tonic, max(cs), top, "drone")
    q = DRONE_NOTE[tempo]
    steps = []
    for i in range(rounds):
        dur = [q] * len(cs)
        dur[-1] = q * 1.5
        steps.append(Step(i, tonic, notes_to_segs([c / 100.0 for c in cs], tonic, dur,
                                                  ["%d c" % c for c in cs]), listen=False))
    layout(steps, lead=3.0, tail=1.2)
    return Plan("drone", steps, {"maqam": maqam, "span": span}, drone={"tonic": tonic, "fifth": fifth})


def scale_degrees(maqam, tonic, bottom, top):
    """(cents, midi) of the maqam's degrees from a fourth below the tonic up to the
    octave, inside [bottom, min(top, G4)]."""
    up = MAQAMAT[maqam]["up"]
    cs = [c - 1200 for c in up[4:7]] + up
    top = min(top, TOP_LIMIT)
    return [(c, tonic + c / 100.0) for c in cs if bottom - 1e-9 <= tonic + c / 100.0 <= top + 1e-9]


def phrase(rng, degs, level, q):
    """One call-and-response phrase: indexes into degs -> segs."""
    n = rng.randint(3, 6)
    zero = [i for i, (c, m) in enumerate(degs) if c == 0]
    i = zero[0] if zero and rng.random() < 0.5 else rng.randrange(len(degs))
    idx = [i]
    for _ in range(n - 1):
        moves = [-1, 1] if level == 1 else [-3, -2, -1, 1, 2, 3]
        weights = None if level == 1 else [1, 2, 4, 4, 2, 1]
        for _try in range(20):
            mv = rng.choices(moves, weights)[0]
            if 0 <= idx[-1] + mv < len(degs):
                idx.append(idx[-1] + mv)
                break
        else:
            idx.append(idx[-1])
    segs, t = [], 0.0
    for k, j in enumerate(idx):
        c, m = degs[j]
        d = q * (2.0 if k == n - 1 else 1.0)
        if level == 3 and k > 0 and rng.random() < 0.6:
            up = degs[min(j + 1, len(degs) - 1)][1]
            lo = degs[max(j - 1, 0)][1]
            if rng.random() < 0.5:   # appoggiatura from the upper neighbour
                orn = [(up, 0.13)]
            else:                    # turn: upper, main, lower, then the note
                orn = [(up, 0.09), (m, 0.09), (lo, 0.09)]
            for om, od in orn:
                segs.append(Seg(t, t + od, om, label="orn", scored=False))
                t += od
        segs.append(Seg(t, t + d, m, label="%d c" % c))
        t += d
    return segs


def plan_callresp(start, maqam, level=1, tempo="normal", bottom=43, top=TOP_LIMIT, count=8, seed=None):
    tonic = tonic_of(maqam, start)
    degs = scale_degrees(maqam, tonic, bottom, top)
    if len(degs) < 4:
        raise PlanError("range|")
    rng = random.Random(seed)
    q = {"slow": 0.8, "normal": 0.6, "fast": 0.45}[tempo]
    steps = [Step(i, tonic, phrase(rng, degs, level, q)) for i in range(count)]
    layout(steps, gap=0.6, tail=0.8)
    return Plan("callresp", steps, {"maqam": maqam, "level": level, "seed": seed})


def plan_onset(start, maqam, tempo="normal", bottom=43, top=TOP_LIMIT, count=10, seed=None):
    tonic = tonic_of(maqam, start)
    degs = scale_degrees(maqam, tonic, bottom, top)
    if not degs:
        raise PlanError("range|")
    rng = random.Random(seed)
    d = {"slow": 1.8, "normal": 1.4, "fast": 1.1}[tempo]
    steps = []
    prev = None
    for i in range(count):
        c, m = rng.choice([x for x in degs if x != prev] or degs)
        prev = (c, m)
        steps.append(Step(i, tonic, [Seg(0.0, d, m, label="%d c" % c)], listen_len=min(1.0, d)))
    layout(steps, gap=0.8, tail=0.6)
    return Plan("onset", steps, {"maqam": maqam, "seed": seed})


def plan_stability(start, bottom, top, direction, count=None):
    rs = roots("hold8", start, bottom, top, direction)
    if not rs:
        raise PlanError("start|%s" % note_name(min(top, TOP_LIMIT)))
    if count:
        rs = rs[:count]
    steps = [Step(i, r, pattern("hold8", float(r)), listen_len=1.5) for i, r in enumerate(rs)]
    layout(steps, gap=0.6, tail=1.5)
    return Plan("stability", steps, {})


def plan_leaps(start, bottom, top, tempo="normal", intervals=(4, 5, 7, 12), roots_per=2):
    top = min(top, TOP_LIMIT)
    q = {"slow": 1.6, "normal": 1.2, "fast": 0.9}[tempo]
    steps = []
    for iv in intervals:
        for k in range(roots_per):
            r = start + 2 * k
            if r + iv > top or r < max(bottom, LOW_LIMIT):
                continue
            name = dict(LEAPS).get(iv, "%d" % iv)
            for a, b, dr in ((r, r + iv, "up"), (r + iv, r, "down")):
                segs = [Seg(0.0, q, float(a), label="from"),
                        Seg(q, 2.2 * q, float(b), label="%s %s" % (name, dr))]
                steps.append(Step(len(steps), float(r), segs, label="%s %s" % (name, dr)))
    if not steps:
        raise PlanError("start|%s" % note_name(top - min(intervals)))
    layout(steps, gap=0.5, tail=0.7)
    return Plan("leaps", steps, {"intervals": list(intervals)})


def plan_ajnas(start, jins, tempo="normal", top=TOP_LIMIT):
    j = AJNAS[jins]
    tonic = start + j.get("offset", 0) / 100.0
    cs = j["cents"]
    check_top(tonic, max(cs), top, "ajnas")
    f = {"slow": 1.3, "normal": 1.0, "fast": 0.8}[tempo]
    order = cs + list(reversed(cs))[1:]
    steps = []
    for i, c in enumerate(order):
        d = (4.0 if quarter(round((tonic + c / 100.0) * 100)) else 2.5) * f   # hold every quarter-tone pitch
        steps.append(Step(i, tonic, [Seg(0.0, d, tonic + c / 100.0, label="%d c" % c)], listen_len=1.4))
    layout(steps, gap=0.5, tail=0.6)
    return Plan("ajnas", steps, {"jins": jins})


def plan_calibration(pitch=50.0, n=8, every=0.8):
    clicks = [1.0 + every * i for i in range(n)]
    p = Plan("calib", [], {}, clicks=clicks)
    p.pitch = pitch
    p.end = clicks[-1] + 1.0
    return p


def note_label_rounded(m):
    if m is None:
        return "—"
    n = int(math.floor(m + 0.5))
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


def singer_track(steps, end, sr=SR, offset_cents=0.0, late=0.06, scoop=None, drift=None,
                 ornaments=True, gap=0.03):
    """Per-sample MIDI that 'sings' each sing phase.
    offset_cents: number, or f(step, k, seg) -> cents (per-note errors)
    late:   seconds behind the target (latency + reaction)
    scoop:  (cents, seconds): every note starts that far off and slides in
    drift:  (cents, seconds): the last seconds of long notes sag by that much"""
    n = int(end * sr)
    tr = np.full(n, np.nan)
    for st in steps:
        for k, s in enumerate(st.segs):
            if not s.scored and not ornaments:
                continue
            a = int((st.sing0 + s.t0 + late) * sr)
            b = min(n, int((st.sing0 + s.t1 + late) * sr) - int(gap * sr))
            if b <= a:
                continue
            u = np.arange(b - a) / sr
            if s.glide:
                vals = np.array([s.at(s.t0 + x) for x in u])
            else:
                vals = np.full(b - a, s.m0)
            off = offset_cents(st, k, s) if callable(offset_cents) else offset_cents
            vals = vals + off / 100.0
            if scoop and s.scored and not s.glide:
                c, d = scoop
                vals = vals + c / 100.0 * np.clip(1 - u / d, 0, 1) ** 1.5
            if drift and (s.t1 - s.t0) > 3.0:
                c, d = drift
                L = u[-1]
                vals = vals + c / 100.0 * np.clip((u - (L - d)) / d, 0, 1)
            tr[a:b] = vals
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
        json.dump(h, f, ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    os.replace(tmp, path)
    return h


def _agg(entries):
    """Note-weighted exact/close/avg over sessions (sessions without notes skipped)."""
    n = sum(e.get("notes", 0) for e in entries if e.get("exact_pct") is not None)
    if not n:
        return None
    w = lambda k: sum(e[k] * e["notes"] for e in entries if e.get(k) is not None and e.get("notes")) / n
    sung = [e for e in entries if e.get("note_avg_cents") is not None and e.get("notes")]
    ns = sum(e["notes"] for e in sung)
    return {"notes": n, "exact_pct": round(w("exact_pct"), 1), "close_pct": round(w("close_pct"), 1),
            "avg_cents": round(sum(e["note_avg_cents"] * e["notes"] for e in sung) / ns, 1) if ns else None}


def daily(history):
    """One row per day: note-based exact/close/avg (falls back to the old
    time-based numbers for sessions saved before v2), best high/low, minutes,
    and per exercise group."""
    days = {}
    for e in history:
        if not e.get("frames") and not e.get("notes"):
            continue
        days.setdefault(e["date"], []).append(e)
    rows = []
    for k in sorted(days):
        es = days[k]
        hi = [e["highest_midi"] for e in es if e.get("highest_midi") is not None]
        lo = [e["lowest_midi"] for e in es if e.get("lowest_midi") is not None]
        row = {"date": k, "sessions": len(es), "minutes": round(sum(e.get("duration_s", 0) for e in es) / 60.0, 1),
               "highest_midi": max(hi) if hi else None, "lowest_midi": min(lo) if lo else None}
        a = _agg(es)
        if a:
            row.update(a)
        else:   # v1 sessions: frame-weighted time-in-tune
            w = sum(e["frames"] for e in es if e.get("avg_cents") is not None)
            row.update(notes=0, exact_pct=None, close_pct=None,
                       avg_cents=round(sum(e["avg_cents"] * e["frames"] for e in es if e.get("avg_cents") is not None) / w, 1) if w else None)
        tw = sum(e.get("frames", 0) for e in es if e.get("in_tune_pct") is not None)
        row["in_tune_pct"] = round(sum(e["in_tune_pct"] * e["frames"] for e in es if e.get("in_tune_pct") is not None) / tw, 1) if tw else None
        row["groups"] = {}
        for g, _ in GROUPS:
            ga = _agg([e for e in es if group_of(e.get("mode", "basic")) == g])
            if ga:
                row["groups"][g] = ga
        rows.append(row)
    return rows
