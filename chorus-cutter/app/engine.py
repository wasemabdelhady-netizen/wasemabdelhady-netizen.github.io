"""ChorusCutter engine: find the best bar-aligned clip of a song for reels.

    python engine.py master.wav [--stem vocals.wav] [--len 20] [--export 1]
"""
import math
import os
import re
import subprocess
import sys
import tempfile

import numpy as np

SR = 22050          # analysis rate
HOP = 512           # ~23 ms
NFFT = 2048
LENGTHS = (10, 15, 20, 30)
FADE_IN = 0.03
FADE_OUT = 1.5
WEIGHTS = {"loud": 0.25, "vocal": 0.30, "repeat": 0.35, "entry": 0.10}

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "output")
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0


# ----------------------------------------------------------------------------- ffmpeg

def ffmpeg_bin():
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    local = os.path.join(ROOT, "ffmpeg", exe)
    return local if os.path.isfile(local) else exe


def run_ffmpeg(args):
    return subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin"] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)


def decode(path, sr=SR, channels=1):
    """Whole file as float32 array (frames, channels)."""
    r = run_ffmpeg(["-i", path, "-vn", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("مش قادر أقرا الملف:\n%s\n\n%s"
                           % (path, r.stderr.decode("utf-8", "replace")[-500:]))
    return np.frombuffer(r.stdout, dtype="<f4").reshape(-1, channels).astype(np.float32)


# ----------------------------------------------------------------------------- features

def stft_mag(x):
    win = np.hanning(NFFT).astype(np.float32)
    pad = np.concatenate([np.zeros(NFFT // 2, np.float32), x, np.zeros(NFFT // 2, np.float32)])
    n = 1 + (len(pad) - NFFT) // HOP
    out = np.empty((n, NFFT // 2 + 1), np.float32)
    step = 2048
    for a in range(0, n, step):  # chunked to keep memory low
        b = min(n, a + step)
        idx = np.arange(NFFT)[None, :] + HOP * np.arange(a, b)[:, None]
        out[a:b] = np.abs(np.fft.rfft(pad[idx] * win, axis=1))
    return out


def band(freqs, lo, hi):
    return (freqs >= lo) & (freqs < hi)


def onset_envelope(mag):
    logm = np.log1p(100 * mag)
    flux = np.maximum(0, np.diff(logm, axis=0)).mean(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")  # remove slow trend
    flux = np.maximum(flux, 0)
    return flux / (flux.std() + 1e-9)


def estimate_tempo(onset, fps):
    """BPM from onset autocorrelation with a soft prior around 110 BPM."""
    o = onset - onset.mean()
    n = len(o)
    ac = np.fft.irfft(np.abs(np.fft.rfft(o, 2 * n)) ** 2)[:n]
    best, best_bpm = -1, 120.0
    for bpm in np.arange(60, 190.01, 0.25):
        lag = 60.0 * fps / bpm
        # comb: the lag and its multiples
        s = 0.0
        for m, w in ((1, 1.0), (2, 0.5), (4, 0.25)):
            L = lag * m
            i = int(L)
            if i + 1 >= n:
                continue
            f = L - i
            s += w * ((1 - f) * ac[i] + f * ac[i + 1])
        prior = math.exp(-0.5 * (math.log2(bpm / 110.0) / 0.6) ** 2)
        s *= prior
        if s > best:
            best, best_bpm = s, bpm
    return float(best_bpm)


def track_beats(onset, fps, bpm, tightness=100.0):
    """Ellis dynamic-programming beat tracker. Returns beat frame indices."""
    period = 60.0 * fps / bpm
    n = len(onset)
    score = onset.astype(np.float64).copy()
    back = -np.ones(n, dtype=np.int64)
    lo, hi = int(round(period / 2)), int(round(period * 2))
    lags = np.arange(lo, hi + 1)
    pen = -tightness * np.log(lags / period) ** 2
    for t in range(hi, n):
        prev = t - lags
        cand = score[prev] + pen
        k = int(np.argmax(cand))
        score[t] = onset[t] + cand[k]
        back[t] = prev[k]
    # end on a strong beat near the end
    tail = score[max(0, n - int(period * 2)):]
    t = n - len(tail) + int(np.argmax(tail))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1])
    # drop leading beats in silence
    thr = 0.05 * np.median(onset[beats]) if len(beats) else 0
    return beats[onset[beats] > thr] if len(beats) > 8 else beats


def chroma(mag, freqs):
    sel = band(freqs, 55, 4200)
    f = freqs[sel]
    pc = np.round(12 * np.log2(f / 440.0)).astype(int) % 12
    m = mag[:, sel] ** 2
    c = np.zeros((mag.shape[0], 12), np.float32)
    for k in range(12):
        c[:, k] = m[:, pc == k].sum(axis=1)
    c = np.log1p(1000 * c / (c.sum(axis=1, keepdims=True).max() + 1e-12))
    return c


def rank01(v):
    """Values -> 0..1 by robust min/max (5th..95th percentile)."""
    v = np.asarray(v, float)
    if len(v) == 0:
        return v
    lo, hi = np.percentile(v, 5), np.percentile(v, 95)
    if hi - lo < 1e-9:
        return np.full_like(v, 0.5)
    return np.clip((v - lo) / (hi - lo), 0, 1)


# ----------------------------------------------------------------------------- analysis

class Analysis:
    def __init__(self, master, stem=None, progress=None, log=None):
        self.master, self.stem = master, stem or None
        self.progress = progress or (lambda f, m="": None)
        self.log = log or (lambda m: print(m, flush=True))
        self.run()

    def run(self):
        self.progress(0.05, "بقرا الأغنية…")
        st = decode(self.master, SR, 2)
        self.duration = len(st) / SR
        if self.duration < 12:
            raise RuntimeError("الأغنية قصيرة قوي (%.1f ث)" % self.duration)
        mono = st.mean(axis=1)
        side = 0.5 * (st[:, 0] - st[:, 1])
        self.progress(0.2, "بحلل الصوت…")
        mag = stft_mag(mono)
        mag_s = stft_mag(side)
        freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
        self.fps = SR / float(HOP)
        nfr = mag.shape[0]
        self.frame_t = np.arange(nfr) / self.fps

        self.progress(0.4, "بدوّر على الـ BPM…")
        onset = onset_envelope(mag)
        self.bpm = estimate_tempo(onset, self.fps)
        beats = track_beats(onset, self.fps, self.bpm)
        if len(beats) >= 16:  # median 8-beat span: finer than one hop, robust to stray beats
            bt = beats / self.fps
            self.bpm = 60.0 / float(np.median((bt[8:] - bt[:-8]) / 8.0))
        self.beat_frames = beats
        self.beats = beats / self.fps

        # loudness per frame (dB), low end weighted down a bit
        w = np.ones_like(freqs)
        w[freqs < 100] = 0.3
        energy = ((mag * w) ** 2).sum(axis=1) / NFFT
        self.loud_db = 10 * np.log10(energy + 1e-10)

        # vocal presence per frame
        if self.stem:
            self.progress(0.55, "بقرا الـ vocal stem…")
            v = decode(self.stem, SR, 1)[:, 0]
            vm = stft_mag(v)
            n = min(nfr, vm.shape[0])
            ve = 10 * np.log10((vm[:n] ** 2).sum(axis=1) / NFFT + 1e-10)
            ve = np.pad(ve, (0, nfr - n), constant_values=ve.min())
            ref = np.percentile(ve, 95)
            self.vocal = np.clip((ve - (ref - 30)) / 18.0, 0, 1)
            self.vocal_source = "stem"
        else:
            # estimate: centred energy in the voice band, relative to the mix
            vb = band(freqs, 250, 3500)
            mid_e = (mag[:, vb] ** 2).sum(axis=1)
            side_e = (mag_s[:, vb] ** 2).sum(axis=1)
            tot = (mag ** 2).sum(axis=1) + 1e-10
            centre = np.clip(1 - 2 * side_e / (mid_e + 1e-10), 0, 1)
            ratio = mid_e / tot
            # harmonic (peaky) spectrum in the voice band
            flat = np.exp(np.log(mag[:, vb] + 1e-7).mean(axis=1)) / (mag[:, vb].mean(axis=1) + 1e-7)
            raw = centre * ratio * (1 - flat)
            raw = np.convolve(raw, np.ones(9) / 9, mode="same")
            self.vocal = rank01(raw)
            self.vocal_source = "estimate"

        self.progress(0.7, "بدوّر على المازورة والكورَس…")
        self.chroma_f = chroma(mag, freqs)
        self.bars = self.find_bars(onset, mag, freqs)
        self.bar_dur = float(np.median(np.diff(self.bars))) if len(self.bars) > 2 else 2.0
        self.bar_features()
        self.progress(0.95, "")
        self.log("BPM ≈ %.1f — المازورة %.2f ث — %d مازورة — الفوكال: %s"
                 % (self.bpm, self.bar_dur, len(self.bars) - 1,
                    "من الـ stem" if self.vocal_source == "stem" else "تقدير من الماستر"))

    def find_bars(self, onset, mag, freqs):
        beats = self.beat_frames
        if len(beats) < 12:
            # no clear pulse: 2 s grid
            return np.arange(0, self.duration, 2.0)
        low = (mag[:, band(freqs, 30, 150)] ** 2).sum(axis=1)
        low = low / (low.max() + 1e-12)
        # downbeat: phase (of 4) with strongest kick energy and harmonic change
        ch = self.chroma_f
        best, ph = -1, 0
        for p in range(4):
            idx = beats[p::4]
            s = low[idx].mean() + 0.5 * onset[idx].mean() / (onset[beats].mean() + 1e-9)
            # chord changes tend to land on bar lines
            prev = np.clip(idx - int(self.fps * 0.25), 0, len(ch) - 1)
            nxt = np.clip(idx + int(self.fps * 0.25), 0, len(ch) - 1)
            s += 0.5 * np.abs(ch[nxt] - ch[prev]).mean()
            if s > best:
                best, ph = s, p
        bars = self.beats[ph::4]
        # extend the grid backwards to the song start so early clips are possible
        if len(bars) > 2:
            bd = np.median(np.diff(bars))
            pre = np.arange(bars[0] - bd, -bd * 0.5, -bd)[::-1]
            pre = pre[pre >= -0.05]
            bars = np.concatenate([np.maximum(pre, 0), bars])
            post = np.arange(bars[-1] + bd, self.duration + 1e-6, bd)
            bars = np.concatenate([bars, post])
        return bars

    def bar_features(self):
        b = self.bars
        nb = len(b) - 1
        fr = np.clip((b * self.fps).astype(int), 0, len(self.frame_t))
        self.bar_loud = np.array([self.loud_db[fr[i]:max(fr[i + 1], fr[i] + 1)].mean()
                                  for i in range(nb)])
        self.bar_vocal = np.array([self.vocal[fr[i]:max(fr[i + 1], fr[i] + 1)].mean()
                                   for i in range(nb)])
        # beat-synchronous chroma: 4 vectors per bar (one per quarter of the bar)
        C = []
        for i in range(nb):
            seg = []
            for q in range(4):
                a = fr[i] + (fr[i + 1] - fr[i]) * q // 4
                z = fr[i] + (fr[i + 1] - fr[i]) * (q + 1) // 4
                v = self.chroma_f[a:max(z, a + 1)].mean(axis=0)
                seg.append(v)
            C.append(np.concatenate(seg))
        C = np.array(C)
        C = C - C.mean(axis=1, keepdims=True)
        C /= np.linalg.norm(C, axis=1, keepdims=True) + 1e-9
        self.ssm = C @ C.T  # bar x bar similarity (-1..1)

    # -- candidates -------------------------------------------------------------
    def candidates(self, length=20, top=5):
        nb = len(self.bars) - 1
        k = max(1, int(round(length / self.bar_dur)))
        k = min(k, nb)
        S = self.ssm
        rows = []
        for i in range(0, nb - k + 1):
            # repetition: best matching other occurrence (diagonal stripe), no overlap
            rep = 0.0
            for j in range(0, nb - k + 1):
                if abs(j - i) < k:
                    continue
                d = np.mean([S[i + m, j + m] for m in range(k)])
                rep = max(rep, d)
            loud = self.bar_loud[i:i + k].mean()
            voc = self.bar_vocal[i:i + k].mean()
            # entry: vocal in the first two bars, and a jump in energy at the start
            first = self.bar_vocal[i:i + min(2, k)].mean()
            jump = self.bar_loud[i] - self.bar_loud[i - 1] if i > 0 else 0.0
            rows.append([i, k, loud, voc, rep, first, jump])
        if not rows:
            return []
        R = np.array(rows, float)
        loud_n = rank01(R[:, 2])
        voc_n = np.clip(R[:, 3], 0, 1)
        rep_n = np.clip((R[:, 4] - 0.5) / 0.45, 0, 1)
        entry_n = 0.6 * np.clip(R[:, 5], 0, 1) + 0.4 * rank01(R[:, 6])
        score = (WEIGHTS["loud"] * loud_n + WEIGHTS["vocal"] * voc_n
                 + WEIGHTS["repeat"] * rep_n + WEIGHTS["entry"] * entry_n)
        order = np.argsort(-score)
        chosen = []
        for o in order:
            i = int(R[o, 0])
            if any(abs(i - c["bar"]) < k * 0.5 for c in chosen):
                continue  # overlaps a better one by more than half
            start = float(self.bars[i])
            end = float(self.bars[i + k])
            chosen.append({"bar": i, "bars": k, "start": start, "end": end,
                           "length": end - start, "score": float(score[o]) * 100,
                           "loud": float(loud_n[o]) * 100, "vocal": float(voc_n[o]) * 100,
                           "repeat": float(rep_n[o]) * 100})
            if len(chosen) == top:
                break
        return chosen


# ----------------------------------------------------------------------------- export

def fmt_time(t, sep=":"):
    m = int(t // 60)
    s = t - 60 * m
    s_txt = ("%04.1f" % s) if abs(s - round(s)) > 0.05 else ("%02d" % round(s))
    if s_txt == "60" or s_txt == "60.0":
        m, s_txt = m + 1, "00"
    return "%d%s%s" % (m, sep, s_txt)


def fmt_file_time(t):
    """1m23s / 1m23.5s — Windows file names can't hold ':'."""
    m = int(t // 60)
    s = round(t - 60 * m, 1)
    if s >= 60:
        m, s = m + 1, 0.0
    return "%dm%ss" % (m, ("%02d" % s) if s == int(s) else ("%04.1f" % s))


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip().strip(".")
    return s[:80] or "song"


def song_name_from(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"(?i)[ _\-]*(master(ed)?|final|mix|v\d+)$", "", base).strip(" _-")
    return base or "song"


def clip_filter(start, length):
    fo = max(0.0, length - FADE_OUT)
    return ("atrim=start=%.4f:duration=%.4f,asetpts=PTS-STARTPTS,"
            "afade=t=in:st=0:d=%.3f,afade=t=out:st=%.4f:d=%.3f"
            % (start, length, FADE_IN, fo, FADE_OUT))


def export(src, start, length, song, out_dir=None, suffix=""):
    """WAV 24-bit 48k + MP3 320k. Returns the two paths."""
    out_dir = out_dir or OUTPUT_DIR
    os.makedirs(out_dir, exist_ok=True)
    name = "%s - %s (%ds)%s" % (safe_name(song), fmt_file_time(start), int(round(length)), suffix)
    wav = os.path.join(out_dir, name + ".wav")
    mp3 = os.path.join(out_dir, name + ".mp3")
    af = clip_filter(start, length) + ",aresample=48000:resampler=soxr:precision=28"
    for path, codec in ((wav, ["-c:a", "pcm_s24le"]),
                        (mp3, ["-c:a", "libmp3lame", "-b:a", "320k"])):
        r = run_ffmpeg(["-y", "-i", src, "-vn", "-map_metadata", "-1", "-af", af,
                        "-ar", "48000"] + codec + [path])
        if r.returncode != 0:
            # soxr may be missing in some builds — fall back to the default resampler
            r = run_ffmpeg(["-y", "-i", src, "-vn", "-map_metadata", "-1",
                            "-af", clip_filter(start, length), "-ar", "48000"] + codec + [path])
        if r.returncode != 0:
            raise RuntimeError("ffmpeg وقع في التصدير:\n" + r.stderr.decode("utf-8", "replace")[-600:])
    return wav, mp3


def preview(src, start, length):
    """16-bit WAV in temp for playback."""
    fd, path = tempfile.mkstemp(prefix="chorus_", suffix=".wav")
    os.close(fd)
    r = run_ffmpeg(["-y", "-i", src, "-vn", "-af", clip_filter(start, length),
                    "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", path])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-400:])
    return path


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("master")
    ap.add_argument("--stem", default=None)
    ap.add_argument("--len", type=int, default=20)
    ap.add_argument("--export", type=int, default=0, help="export candidate N (1-5)")
    a = ap.parse_args(argv)
    an = Analysis(a.master, a.stem)
    cands = an.candidates(a.len)
    for n, c in enumerate(cands, 1):
        print("%d. %s → %s  (%.1fs, %d bars)  score %.0f  [loud %.0f · vocal %.0f · repeat %.0f]"
              % (n, fmt_time(c["start"]), fmt_time(c["end"]), c["length"], c["bars"],
                 c["score"], c["loud"], c["vocal"], c["repeat"]))
    if a.export:
        c = cands[a.export - 1]
        print(export(a.master, c["start"], c["length"], song_name_from(a.master)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
