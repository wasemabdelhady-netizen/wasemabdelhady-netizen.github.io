"""«النَفَس» — breath work: hold a note as long as you can (where it starts to
drop or shake), guided «سسس» exhales (hiss detected by the mic: duration and
steadiness), and one phrase repeated on one breath."""
import datetime

import numpy as np

import engine as E
import session as S

HISS_WIN, HISS_HOP = 1024, 512
PATTERNS = [("4-4-8", (4, 4, 8)), ("4-7-8", (4, 7, 8)), ("4-0-6", (4, 0, 6)), ("4-0-10", (4, 0, 10)), ("5-0-15", (5, 0, 15))]
SAFETY = "لو دخت أو حسّيت بدوخة وقّف فوراً. متضغطش على نفسك — النَفَس بيكبر بالتكرار مش بالعافية."


class BreathSession(S.Session):
    """Session + per-frame loudness + a hiss detector fed with the raw mic blocks.
    Hold/phrase end by themselves 1.2 s after the voice stops."""

    def __init__(self, plan, lat=0.0):
        super().__init__(plan, lat)
        self.D = []                     # dB per pitch frame
        self.H = []                     # (t, rms_db, hf_ratio) per 11.6 ms
        self._buf = np.zeros(0, np.float32)
        self._n = 0
        self._win = np.hanning(HISS_WIN)
        self._fr = np.fft.rfftfreq(HISS_WIN, 1.0 / E.SR)

    def process(self, frames):
        for f in frames:
            self.D.append(f[3])
        super().process(frames)

    def feed_raw(self, blk):
        self._buf = np.concatenate((self._buf, np.asarray(blk, np.float32).ravel()))
        while len(self._buf) >= HISS_WIN:
            x = self._buf[:HISS_WIN].astype(np.float64)
            sp = np.abs(np.fft.rfft(x * self._win)) ** 2
            tot = sp.sum() + 1e-18
            hf = sp[self._fr >= 3000].sum() / tot
            db = 10 * np.log10(np.mean(x * x) + 1e-12)
            self.H.append(((self._n + HISS_WIN / 2) / E.SR - self.lat, db, hf))
            self._buf = self._buf[HISS_HOP:]
            self._n += HISS_HOP

    def done(self):
        if self.mode in ("hold", "phrase") and self.T:
            st = self.steps[0]
            if self.now_a > st.sing0 + 1.0:
                v = [t for t, m in zip(self.T[-400:], self.M[-400:]) if not np.isnan(m)]
                if v and self.now - v[-1] > 1.2 and v[-1] - self.lat > st.sing0 + 0.8:
                    return True
        return super().done()


# ------------------------------------------------------------------ plans
def plan_hold(note):
    st = E.Step(0, float(note), [E.Seg(0.0, 90.0, float(note), label="hold")], listen_len=1.5)
    E.layout([st], lead=0.6, gap=0.5)
    p = E.Plan("hold", [st], {"note": E.note_name(note)})
    p.session_cls = BreathSession
    p.end = st.sing1
    return p


def plan_sss(pattern=(4, 4, 8), rounds=3):
    """Phases on the target clock: (kind, t0, t1, round) for inhale / hold / exhale."""
    inh, hold, exh = pattern
    t = 1.0
    phases = []
    for r in range(rounds):
        for kind, d in (("in", inh), ("hold", hold), ("out", exh)):
            if d:
                phases.append((kind, t, t + d, r))
                t += d
        t += 2.0          # recover
    p = E.Plan("sss", [], {"pattern": "%d-%d-%d" % pattern})
    p.phases = phases
    p.session_cls = BreathSession
    p.end = t + 0.5
    return p


def plan_phrase(notes, target):
    """notes: [(t0, t1, midi)] of one phrase. Listen once, then repeat it on one breath."""
    d = notes[-1][1]
    reps = max(target + 3, 8)
    segs = []
    for r in range(reps):
        segs += [E.Seg(r * d + a, r * d + b, m, label="phrase") for a, b, m in notes]
    st = E.Step(0, notes[0][2], segs, listen_len=d)
    E.layout([st], lead=0.6, gap=0.6)
    p = E.Plan("phrase", [st], {"phrase_s": round(d, 2), "target": target})
    p.session_cls = BreathSession
    p.end = st.sing1
    p.phrase_s = d
    return p


# --------------------------------------------------------------- analysis
def voiced_span(T, M, t_from, bridge=0.25):
    """Longest continuous voiced stretch after t_from (gaps under `bridge` s joined)."""
    T, M = np.asarray(T), np.asarray(M, float)
    v = ~np.isnan(M) & (T >= t_from)
    best, cur = (None, None), None
    last = None
    for t, ok in zip(T, v):
        if ok:
            if cur is None or t - last > bridge:
                cur = [t, t]
            cur[1] = t
            last = t
            if best[0] is None or cur[1] - cur[0] > best[1] - best[0]:
                best = (cur[0], cur[1])
    return best


def analyse_hold(T, M, D, t_from, target):
    """Seconds held, and the second where pitch or loudness starts to give way:
    the smoothed pitch leaves the first-3-s centre by > 25 c, the level falls
    > 6 dB under its first-3-s level, or the wobble doubles — for ≥ 0.3 s."""
    a, b = voiced_span(T, M, t_from)
    if a is None or b - a < 0.5:
        return None
    T, M, D = np.asarray(T), np.asarray(M, float), np.asarray(D, float)
    sel = (T >= a) & (T <= b)
    t, m, d = T[sel] - a, M[sel], D[sel]
    ok = ~np.isnan(m)
    t, m, d = t[ok], m[ok] * 100, d[ok]
    k = max(1, int(0.3 / (E.HOP / E.SR)))
    sm = np.convolve(m, np.ones(k) / k, mode="same")
    dm = np.convolve(d, np.ones(k) / k, mode="same")
    wob = np.sqrt(np.convolve((m - sm) ** 2, np.ones(k) / k, mode="same"))
    base = t <= min(3.0, 0.4 * t[-1])
    c0, d0, w0 = np.median(sm[base]), np.median(dm[base]), max(4.0, np.median(wob[base]))
    bad = (np.abs(sm - c0) > 25) | (dm < d0 - 6) | (wob > 2 * w0)
    bad[t < 1.0] = False
    bad[t > t[-1] - 0.15] = False
    run = 0
    brk = None
    for i in range(len(t)):
        run = run + 1 if bad[i] else 0
        if run >= k:
            brk = t[i - k + 1]
            break
    why = None
    if brk is not None:
        i = int(np.searchsorted(t, brk))
        j = min(len(t) - 1, i + k)
        why = "pitch" if np.abs(sm[i:j] - c0).max() > 25 else ("level" if dm[i:j].min() < d0 - 6 else "shake")
    return {"held_s": round(float(b - a), 1), "steady_s": round(float(brk if brk is not None else b - a), 1),
            "why": why, "err_c": round(float(c0 - target * 100), 1)}


def is_hiss(db, hf, floor):
    return db > floor + 12 and hf > 0.45


def analyse_sss(H, phases):
    """Per exhale: seconds of steady hiss and its steadiness (dB spread)."""
    H = np.array(H) if H else np.zeros((0, 3))
    floor = np.percentile(H[:, 1], 10) if len(H) else -90.0
    out = []
    for kind, t0, t1, r in phases:
        if kind != "out":
            continue
        sel = (H[:, 0] >= t0 - 0.3) & (H[:, 0] <= t1 + 3.0) if len(H) else np.zeros(0, bool)
        h = H[sel]
        flags = np.array([is_hiss(db, hf, floor) for _, db, hf in h]) if len(h) else np.zeros(0, bool)
        best, cur = 0.0, None
        lvl = []
        seg_best = []
        gap = 0
        for (t, db, hf), f in zip(h, flags):
            if f:
                if cur is None:
                    cur = [t, t, []]
                cur[1] = t
                cur[2].append(db)
                gap = 0
            elif cur is not None:
                gap += 1
                if gap * HISS_HOP / E.SR > 0.2:
                    if cur[1] - cur[0] > best:
                        best, seg_best = cur[1] - cur[0], cur[2]
                    cur = None
        if cur is not None and cur[1] - cur[0] > best:
            best, seg_best = cur[1] - cur[0], cur[2]
        spread = float(np.std(seg_best)) if len(seg_best) > 10 else None
        out.append({"round": r + 1, "target_s": t1 - t0, "hiss_s": round(float(best), 1),
                    "steady": None if spread is None else round(max(0.0, 100.0 - 12.0 * spread), 0)})
    return out


def analyse_phrase(T, M, t_from, phrase_s):
    a, b = voiced_span(T, M, t_from, bridge=0.3)
    if a is None:
        return {"sung_s": 0.0, "reps": 0}
    return {"sung_s": round(float(b - a), 1), "reps": int((b - a + 0.15) // phrase_s)}


# ---------------------------------------------------------------- history
def best(history, mode, key):
    v = [e[key] for e in history if e.get("mode") == mode and e.get(key) is not None]
    return max(v) if v else None


def weekly(history, mode, key, days=7, today=None):
    today = today or datetime.date.today()
    out = []
    for k in range(days - 1, -1, -1):
        d = (today - datetime.timedelta(days=k)).strftime("%Y-%m-%d")
        v = [e[key] for e in history if e.get("mode") == mode and e.get("date") == d and e.get(key) is not None]
        out.append((d, max(v) if v else None))
    return out


def phrase_target(history):
    b = best(history, "phrase", "reps")
    return max(2, (b or 1) + 1)


def suggest_note(history):
    """Comfortable middle: the middle of your stable range from the history (or G3)."""
    hi = [e["highest_midi"] for e in history if e.get("highest_midi")]
    lo = [e["lowest_midi"] for e in history if e.get("lowest_midi")]
    if hi and lo:
        return int(round((np.median(hi) + np.median(lo)) / 2))
    return 55


def hiss_signal(phases, durations, sr=E.SR, seed=0, lat=0.0, level=0.05):
    """Test helper: highpassed noise 'sss' for each exhale (duration list)."""
    end = phases[-1][2] + 3.0
    y = np.zeros(int(end * sr), np.float32)
    rng = np.random.default_rng(seed)
    outs = [p for p in phases if p[0] == "out"]
    for (kind, t0, t1, r), d in zip(outs, durations):
        a = int((t0 + 0.2 + lat) * sr)
        n = int(d * sr)
        w = rng.standard_normal(n + 2)
        hp = (w[2:] - 2 * w[1:-1] + w[:-2]) / 4.0
        env = np.minimum(1, np.minimum(np.arange(n), n - np.arange(n)) / (0.05 * sr))
        y[a:a + n] += (level * hp * env).astype(np.float32)[:len(y) - a]
    return y + (0.0005 * rng.standard_normal(len(y))).astype(np.float32)
