"""«صوتك النهارده» — a 30-second voice check: a 5 s «آه» (jitter, shimmer, HNR,
pitch steadiness), maximum phonation time, and a siren for the range (low/high
note). Results are compared with your own last 7 checks, not with norms."""
import datetime
import os
import wave

import numpy as np

import breath as BR
import engine as E

VC_DIR = os.path.join(E.ROOT, "history", "voicecheck")
KINDS = [("pre", "قبل اللايف"), ("post", "بعد اللايف"), ("normal", "عادي")]
DISCLAIMER = "ده مؤشر للمتابعة، مش تشخيص طبي."
# metric: (Arabic label, unit, higher is better?)
METRICS = [("jitter", "Jitter", "%", False), ("shimmer", "Shimmer", "%", False), ("hnr", "HNR", "dB", True),
           ("stab_c", "ثبات الطبقة", "c", False), ("mpt", "أطول نَفَس", "s", True),
           ("lo_midi", "أوطى نوتة", "", False), ("hi_midi", "أعلى نوتة", "", True)]


# --------------------------------------------------------------- measures
def _lowpass(x, fc, sr):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1.0 / sr)
    X[f > fc] = 0
    X[f < 0.5 * fc / 1.8] = 0
    return np.fft.irfft(X, len(x))


def cycles(x, f0, sr=E.SR):
    """Upward zero crossings of the fundamental (signal low-passed at 1.8·f0),
    interpolated → cycle boundaries in samples."""
    y = _lowpass(np.asarray(x, np.float64), 1.8 * f0, sr)
    s = np.nonzero((y[:-1] < 0) & (y[1:] >= 0))[0]
    return s + (-y[s] / (y[s + 1] - y[s] + 1e-18)), y


def jitter_shimmer(x, f0, sr=E.SR):
    """Local jitter % (mean |T_i − T_i+1| / mean T) and local shimmer % (same on
    every cycle's peak-to-peak amplitude). Cycles by waveform matching: each next
    cycle start is where the waveform best repeats (normalised cross-correlation
    in 0.8–1.2 periods, parabolic sub-sample peak) — amplitude changes don't move it."""
    x = np.asarray(x, np.float64)
    zc, _ = cycles(x, f0, sr)
    if len(zc) < 25:
        return None, None
    T0 = sr / f0
    L = int(T0)
    s = float(zc[0])
    Ts, As = [], []
    lags = np.arange(int(0.8 * T0), int(1.2 * T0) + 2)
    while s + lags[-1] + L + 2 < len(x):
        i = int(s)
        ref = x[i:i + L]
        ref = ref - ref.mean()
        nr = np.sqrt(np.dot(ref, ref)) + 1e-12
        seg = np.lib.stride_tricks.sliding_window_view(x[i + lags[0]:i + lags[-1] + L], L)
        seg = seg - seg.mean(1, keepdims=True)
        cc = seg @ ref / (np.sqrt(np.sum(seg * seg, 1)) * nr + 1e-12)
        k = int(np.argmax(cc))
        off = 0.0
        if 0 < k < len(cc) - 1:
            a_, b_, c_ = cc[k - 1], cc[k], cc[k + 1]
            den = a_ - 2 * b_ + c_
            off = 0.5 * (a_ - c_) / den if abs(den) > 1e-12 else 0.0
        T = lags[0] + k + off
        Ts.append(T)
        As.append(np.ptp(x[i:i + int(T)]))
        s = s + T
    Ts, As = np.array(Ts), np.array(As)
    if len(Ts) < 20:
        return None, None
    jit = np.mean(np.abs(np.diff(Ts))) / np.mean(Ts) * 100
    shim = np.mean(np.abs(np.diff(As))) / np.mean(As) * 100
    return float(jit), float(shim)


def hnr(x, f0, sr=E.SR, win=0.04):
    """Harmonics-to-noise ratio (Boersma autocorrelation method): per 40 ms
    frame, normalised autocorrelation at the period (window-corrected)."""
    n = int(win * sr)
    w = np.hanning(n)
    rw = np.correlate(w, w, "full")[n - 1:]
    lag0 = sr / f0
    vals = []
    for a in range(0, len(x) - n, n // 2):
        fr = x[a:a + n] - np.mean(x[a:a + n])
        fr = fr * w
        r = np.correlate(fr, fr, "full")[n - 1:]
        if r[0] <= 0:
            continue
        r = r / r[0] / np.maximum(rw / rw[0], 1e-9)
        lo, hi = int(lag0 * 0.8), min(int(lag0 * 1.2) + 2, n // 2)
        k = lo + int(np.argmax(r[lo:hi]))
        if 0 < k < len(r) - 1:   # parabolic peak
            a_, b_, c_ = r[k - 1], r[k], r[k + 1]
            den = a_ - 2 * b_ + c_
            rmax = b_ - 0.25 * (a_ - c_) ** 2 / den if abs(den) > 1e-12 else b_
        else:
            rmax = r[k]
        rmax = min(max(rmax, 1e-6), 0.999999)
        vals.append(10 * np.log10(rmax / (1 - rmax)))
    return float(np.median(vals)) if vals else None


def analyse_aah(audio, T, M, t0, sr=E.SR):
    """The 5 s «آه»: its steady middle (0.6 s after the start to 0.3 s before the end)."""
    a, b = BR.voiced_span(T, M, t0)
    if a is None or b - a < 2.0:
        return None
    a, b = a + 0.6, b - 0.3
    T, M = np.asarray(T), np.asarray(M, float)
    sel = (T >= a) & (T <= b) & ~np.isnan(M)
    f0 = float(E.midi_to_hz(np.median(M[sel])))
    x = np.asarray(audio, np.float64)[int(a * sr):int(b * sr)]
    jit, shim = jitter_shimmer(x, f0, sr)
    return {"jitter": None if jit is None else round(jit, 2), "shimmer": None if shim is None else round(shim, 2),
            "hnr": (lambda h: None if h is None else round(h, 1))(hnr(x, f0, sr)),
            "stab_c": round(float(np.std(M[sel] * 100)), 1), "f0": round(f0, 1)}


def analyse_mpt(T, M, t0):
    a, b = BR.voiced_span(T, M, t0, bridge=0.25)
    return None if a is None else round(float(b - a), 1)


def analyse_range(T, M, t0):
    T, M = np.asarray(T), np.asarray(M, float)
    v = M[(T >= t0) & ~np.isnan(M)]
    if len(v) < 50:
        return None, None
    k = 9
    sm = np.array([np.median(v[max(0, i - k):i + k]) for i in range(0, len(v), 3)])
    return float(np.percentile(sm, 2)), float(np.percentile(sm, 98))


# ------------------------------------------------------------ comparison
def baseline(history, n=7, before=None):
    """Mean of each metric over your last n checks (before this one)."""
    hs = [e for e in history if e.get("mode") == "vcheck" and (before is None or (e["date"], e["time"]) < before)][-n:]
    out = {}
    for k, *_ in METRICS:
        v = [e[k] for e in hs if e.get(k) is not None]
        out[k] = (float(np.mean(v)), len(v)) if v else (None, 0)
    return out


def grade(key, value, base):
    """'green' | 'yellow' | 'red' | None — against your own average (worse direction only)."""
    mean, n = base.get(key, (None, 0))
    if value is None or mean is None or n < 2:
        return None
    better_up = dict((k, hb) for k, _, _, hb in METRICS)[key]
    if key in ("lo_midi", "hi_midi"):
        worse = (mean - value) if better_up else (value - mean)        # semitones
        return "green" if worse <= 1 else ("yellow" if worse <= 2 else "red")
    if key == "hnr":
        worse = mean - value                                            # dB
        return "green" if worse <= 1.5 else ("yellow" if worse <= 3 else "red")
    rel = ((mean - value) / mean) if better_up else ((value - mean) / mean)
    return "green" if rel <= 0.10 else ("yellow" if rel <= 0.25 else "red")


def verdict(grades):
    g = [x for x in grades.values() if x]
    if not g:
        return "لسه محتاج فحصين على الأقل عشان أقارنك بنفسك."
    red, yel = g.count("red"), g.count("yellow")
    if red >= 2:
        return "صوتك أتعب من العادي النهارده — خفّف، واشرب مية، ومتزوّدش في الطبقات العالية."
    if red == 1 or yel >= 2:
        return "صوتك أقل شوية من العادي — سخّن كويس قبل ما تغني."
    return "صوتك زي العادي أو أحسن — تمام."


def before_after(history, day):
    pre = [e for e in history if e.get("mode") == "vcheck" and e.get("date") == day and e.get("kind") == "pre"]
    post = [e for e in history if e.get("mode") == "vcheck" and e.get("date") == day and e.get("kind") == "post"]
    if not pre or not post:
        return None
    a, b = pre[-1], post[-1]
    rows = []
    for k, lab, unit, hb in METRICS:
        if a.get(k) is None or b.get(k) is None:
            continue
        d = b[k] - a[k]
        arrow = "↑" if d > 0 else ("↓" if d < 0 else "=")
        good = (d >= 0) == hb if d else True
        rows.append((k, lab, a[k], b[k], arrow, good))
    return rows


def daily(history, key, days=14):
    out = {}
    for e in history:
        if e.get("mode") == "vcheck" and e.get(key) is not None:
            out.setdefault(e["date"], []).append(e[key])
    return [(d, float(np.mean(v))) for d, v in sorted(out.items())][-days:]


def save_wav(path, x, sr=E.SR):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    x = np.clip(np.asarray(x, np.float32), -1, 1)
    with open(path, "wb") as fh:
        w = wave.open(fh, "wb")
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())
        w.close()
    return path


def wav_path(kind, step, when=None):
    when = when or datetime.datetime.now()
    return os.path.join(VC_DIR, "%s - %s - %d.wav" % (when.strftime("%Y-%m-%d %H%M%S"), kind, step))


# ------------------------------------------------------------ test signal
def synth_aah(f0=196.0, dur=5.0, jitter_pct=0.5, shimmer_pct=3.0, hnr_db=20.0, sr=E.SR, seed=0):
    """A sustained vowel built cycle by cycle with random period and amplitude
    perturbations and added white noise. Returns (signal, true jitter %,
    true shimmer %, true HNR dB) — the truths measured on what was generated."""
    rng = np.random.default_rng(seed)
    T0 = sr / f0
    sj = jitter_pct / 100 * T0 * np.sqrt(np.pi) / 2      # E|ΔT| = 2σ/√π for iid normal
    sa = shimmer_pct / 100 * np.sqrt(np.pi) / 2
    Ts, As, out = [], [], []
    n = 0
    while n < dur * sr:
        T = T0 + sj * rng.standard_normal()
        A = 1 + sa * rng.standard_normal()
        k = int(round(T))
        ph = np.arange(k) / T * 2 * np.pi
        cyc = sum((0.75 ** (h - 1)) * np.sin(h * ph) for h in range(1, 11)) * A
        out.append(cyc)
        Ts.append(k)
        As.append(np.ptp(cyc))
        n += k
    y = np.concatenate(out)
    harm_p = np.mean(y ** 2)
    noise = rng.standard_normal(len(y)) * np.sqrt(harm_p / 10 ** (hnr_db / 10))
    Ts, As = np.array(Ts, float), np.array(As)
    tj = np.mean(np.abs(np.diff(Ts))) / np.mean(Ts) * 100
    ts = np.mean(np.abs(np.diff(As))) / np.mean(As) * 100
    y = y + noise
    return (0.2 * y / np.abs(y).max()).astype(np.float32), tj, ts, hnr_db


# ------------------------------------------------------------------ plans
class VCSession(BR.BreathSession):
    """Keeps the raw mic signal (for jitter/shimmer/HNR and the WAV)."""

    def __init__(self, plan, lat=0.0):
        super().__init__(plan, lat)
        self.raw = []

    def feed_raw(self, blk):
        self.raw.append(np.asarray(blk, np.float32).ravel().copy())
        super().feed_raw(blk)

    def audio(self):
        return np.concatenate(self.raw) if self.raw else np.zeros(0, np.float32)

    def done(self):
        if self.mode == "vc2" and self.T:
            st = self.steps[0]
            v = [t for t, m in zip(self.T[-400:], self.M[-400:]) if not np.isnan(m)]
            if v and self.now - v[-1] > 1.2 and v[-1] - self.lat > st.sing0 + 0.8:
                return True
        return super().done()


STEPS = {
    "vc1": "١) «آه» ممدودة 5 ثواني على النوتة — صوت عادي، من غير فايبراتو ومن غير ضغط.",
    "vc2": "٢) خد نَفَس عميق، وقول «آه» على نفس النوتة لأطول وقت مريح — من غير ما تعصر آخر النَفَس.",
    "vc3": "٣) سايرن «آه» من أوطى نوتة مريحة لحد فوق وارجع — متعدّيش G4.",
}


def plan_step(mode, note):
    if mode == "vc1":
        st = E.Step(0, float(note), [E.Seg(0.0, 5.0, float(note), label="aah")], listen_len=1.2)
        lead, end_pad = 3.0, 0.4
    elif mode == "vc2":
        st = E.Step(0, float(note), [E.Seg(0.0, 60.0, float(note), label="mpt")], listen_len=1.2)
        lead, end_pad = 3.0, 0.0
    else:
        st = E.Step(0, 43.0, [E.Seg(0.0, 6.0, 43.0, 67.0), E.Seg(6.0, 12.0, 67.0, 43.0)], listen=False)
        lead, end_pad = 3.0, 0.4
    E.layout([st], lead=lead, gap=0.6)
    p = E.Plan(mode, [st], {"note": E.note_name(note)})
    p.session_cls = VCSession
    p.end = st.sing1 + end_pad
    return p
