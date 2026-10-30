"""Cinematic mode — audio side: footage↔master sync, beats/onsets, vocal loudness.

All times are in seconds. `offset` = master time − footage time
(footage t=0 is master t=offset).
"""
import numpy as np

import autoreel

SYNC_SR = 8000


def _env(x, sr, hop):
    """Onset envelope: positive change of log energy per `hop` seconds."""
    n = int(sr * hop)
    k = len(x) // n
    if k < 4:
        return np.zeros(0, dtype=np.float32)
    f = x[:k * n].reshape(k, n)
    e = np.log10((f * f).mean(axis=1) + 1e-7)
    d = np.maximum(np.diff(e), 0)
    return ((d - d.mean()) / (d.std() + 1e-9)).astype(np.float32)


def _xcorr(a, b):
    """c[k] = sum_t a[t] * b[t + k] for k in -(len(a)-1) .. len(b)-1 -> (lags, values)."""
    n = 1
    while n < len(a) + len(b):
        n *= 2
    c = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    lags = np.concatenate([np.arange(0, len(b)), np.arange(-len(a) + 1, 0)])
    vals = np.concatenate([c[:len(b)], c[n - len(a) + 1:]])
    return lags, vals


def sync_offset(master_path, footage_path, log=None):
    """Find where the footage sits in the master from the camera's own audio.
    Returns (offset, confidence 0..1). Coarse: onset envelopes (10 ms); fine:
    raw waveform correlation around the coarse lag (sub-millisecond)."""
    m = autoreel.decode(master_path, SYNC_SR)
    v = autoreel.decode(footage_path, SYNC_SR)
    if len(v) < SYNC_SR * 3:
        raise RuntimeError("الفيديو مفيهوش صوت كفاية للمزامنة")
    hop = 0.01
    em, ev = _env(m, SYNC_SR, hop), _env(v, SYNC_SR, hop)
    lags, vals = _xcorr(ev, em)
    k = int(np.argmax(vals))
    coarse = lags[k] * hop
    # confidence: peak against the rest of the correlation (excluding ±0.3 s)
    mask = np.abs(lags - lags[k]) > int(0.3 / hop)
    rest = vals[mask]
    z = (vals[k] - rest.mean()) / (rest.std() + 1e-9) if len(rest) else 0.0
    conf = float(np.clip((z - 4.0) / 8.0, 0.0, 1.0))
    # fine: waveforms, ±40 ms around the coarse lag, on the loudest 20 s of the footage
    win = int(20 * SYNC_SR)
    if len(v) > win:
        e = np.convolve(v * v, np.ones(SYNC_SR), "valid")[::SYNC_SR]
        s0 = int(np.argmax(np.convolve(e, np.ones(20), "valid"))) * SYNC_SR
        s0 = min(s0, len(v) - win)
    else:
        s0, win = 0, len(v)
    seg = v[s0:s0 + win]
    pad = int(0.04 * SYNC_SR)
    m0 = int(round((s0 / SYNC_SR + coarse) * SYNC_SR)) - pad
    fine = coarse
    if m0 >= 0 and m0 + win + 2 * pad <= len(m):
        ref = m[m0:m0 + win + 2 * pad]
        seg_n = (seg - seg.mean()) / (seg.std() + 1e-9)
        ref_n = (ref - ref.mean()) / (ref.std() + 1e-9)
        l2, v2 = _xcorr(seg_n, ref_n)
        ok = (l2 >= 0) & (l2 <= 2 * pad)
        if ok.any():
            j = int(np.argmax(np.where(ok, v2, -np.inf)))
            fine = (m0 + l2[j]) / float(SYNC_SR) - s0 / float(SYNC_SR)
            if abs(fine - coarse) > 0.05:
                fine = coarse
    if log:
        log("المزامنة: الفيديو بيبدأ عند %.3f ث من الماستر (ثقة %d%%)" % (fine, conf * 100))
    return float(fine), conf


# ----------------------------------------------------------------------------- beats

BEAT_SR = 22050
BEAT_HOP = 512


def onset_strength(x, sr=BEAT_SR, hop=BEAT_HOP, nfft=2048):
    """Spectral flux of log-magnitude (per frame of `hop` samples)."""
    if len(x) < nfft:
        return np.zeros(1, dtype=np.float32)
    win = np.hanning(nfft).astype(np.float32)
    n = 1 + (len(x) - nfft) // hop
    out = np.empty(n, dtype=np.float32)
    prev = None
    step = 256
    for a in range(0, n, step):
        b = min(n, a + step)
        idx = (np.arange(a, b) * hop)[:, None] + np.arange(nfft)[None, :]
        spec = np.abs(np.fft.rfft(x[idx] * win, axis=1)).astype(np.float32)
        spec = np.log1p(1000.0 * spec)
        if prev is None:
            prev = spec[:1]
        both = np.vstack([prev, spec])
        out[a:b] = np.maximum(np.diff(both, axis=0), 0).mean(axis=1)
        prev = spec[-1:]
    out -= np.convolve(out, np.ones(16) / 16, "same")  # local mean removal
    out = np.maximum(out, 0)
    return out / (out.max() + 1e-9)


def tempo(env, fps, lo=70.0, hi=180.0, prior=110.0):
    """BPM from the onset autocorrelation with a log-normal prior around `prior`."""
    env = env - env.mean()
    n = len(env)
    m = 1
    while m < 2 * n:
        m *= 2
    f = np.fft.rfft(env, m)
    ac = np.fft.irfft(f * np.conj(f), m)[:n]
    lags = np.arange(n)
    bpm = 60.0 * fps / np.maximum(lags, 1)
    ok = (bpm >= lo) & (bpm <= hi)
    w = np.exp(-0.5 * (np.log2(bpm / prior) / 0.9) ** 2)
    score = np.where(ok, ac * w, -np.inf)
    k = int(np.argmax(score))
    # parabolic refinement
    if 1 <= k < n - 1:
        a, b, c = ac[k - 1], ac[k], ac[k + 1]
        d = (a - c) / (2 * (a - 2 * b + c) + 1e-12)
        k = k + float(np.clip(d, -0.5, 0.5))
    return 60.0 * fps / k


def track_beats(env, fps, bpm, tightness=100.0):
    """Ellis dynamic-programming beat tracker -> frame indices."""
    period = 60.0 * fps / bpm
    n = len(env)
    score = env.astype(np.float64).copy()
    back = np.full(n, -1, dtype=np.int64)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    offs = np.arange(-hi, -lo + 1)
    pen = -tightness * (np.log(-offs / period)) ** 2
    for i in range(n):
        j = i + offs
        ok = j >= 0
        if not ok.any():
            continue
        cand = score[j[ok]] + pen[ok]
        b = int(np.argmax(cand))
        if cand[b] > 0:
            score[i] = env[i] + cand[b]
            back[i] = j[ok][b]
    # last beat: best score in the final period
    tail = max(0, n - int(period))
    i = tail + int(np.argmax(score[tail:]))
    beats = []
    while i >= 0:
        beats.append(i)
        i = back[i]
    return np.array(beats[::-1], dtype=np.int64)


class Beats:
    """Beat grid of the master: times, bpm, downbeat phase, onset curve."""

    def __init__(self, master_path, log=None):
        x = autoreel.decode(master_path, BEAT_SR)
        self.fps = BEAT_SR / float(BEAT_HOP)
        self.env = onset_strength(x)
        self.bpm = tempo(self.env, self.fps)
        fr = track_beats(self.env, self.fps, self.bpm)
        self.times = fr / self.fps
        self.strength = self.env[np.clip(fr, 0, len(self.env) - 1)] if len(fr) else np.zeros(0)
        # downbeat phase: the bar position whose beats are loudest
        best, self.phase = -1.0, 0
        for p in range(4):
            s = float(self.strength[p::4].mean()) if len(self.strength[p::4]) else 0.0
            if s > best:
                best, self.phase = s, p
        if log:
            log("الإيقاع: %.1f BPM — %d بيت" % (self.bpm, len(self.times)))

    @property
    def period(self):
        return 60.0 / self.bpm

    def in_span(self, s, e):
        """[(time, strength, is_downbeat)] inside [s, e)."""
        out = []
        for i, t in enumerate(self.times):
            if s <= t < e:
                out.append((float(t), float(self.strength[i]), (i - self.phase) % 4 == 0))
        return out

    def onset_at(self, t):
        i = int(t * self.fps)
        return float(self.env[i]) if 0 <= i < len(self.env) else 0.0


# ----------------------------------------------------------------------------- vocals

def vocal_db(audio, sr, s, e):
    """Mean loudness (dB) of a stretch of the vocal track."""
    a, b = int(max(0, s) * sr), int(max(0, e) * sr)
    seg = audio[a:b]
    if len(seg) < sr * 0.1:
        return -80.0
    return float(10 * np.log10((seg * seg).mean() + 1e-9))
