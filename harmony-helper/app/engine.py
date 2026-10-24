"""HarmonyHelper engine: tempo/beats/bars, key & scale, chord progression,
vocal melody (YIN pitch tracking), diatonic harmony lines and quarter-tone
(maqam) detection.

    python engine.py song.wav [vocal_stem.wav]
"""
import math
import os
import re
import subprocess
import sys

import numpy as np
from scipy.signal import butter, medfilt, sosfiltfilt

VERSION = "1.1"
SR = 22050
HOP = 512            # analysis hop for beats/chroma (~23 ms)
NFFT = 4096
P_HOP = 256          # pitch-tracking hop (~11.6 ms)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(ROOT, "output")
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0

SHARPS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLATS = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
AR_NOTES = {"C": "دو", "D": "ري", "E": "مي", "F": "فا", "G": "صول", "A": "لا", "B": "سي"}
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10]}
SCALE_AR = {"major": "ماجير (Major)", "minor": "مينير (Minor)"}
# Krumhansl–Kessler key profiles
KK = {"major": np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]),
      "minor": np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])}
LINES = [  # key, label, diatonic steps (None = chromatic octave), octave semitones
    ("third_up", "3rd above (تالتة فوق)", 2, 0),
    ("third_down", "3rd below (تالتة تحت)", -2, 0),
    ("sixth_down", "6th below (سادسة تحت)", -5, 0),
    ("octave_up", "Octave up (أوكتاف فوق)", None, 12),
    ("octave_down", "Octave down (أوكتاف تحت)", None, -12),
]
QUARTER_TONE_CENTS = 35


# ----------------------------------------------------------------------------- io

def ffmpeg_bin():
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    local = os.path.join(ROOT, "ffmpeg", exe)
    return local if os.path.isfile(local) else exe


def run_ffmpeg(args):
    return subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin"] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)


def decode(path, sr=SR, channels=1):
    r = run_ffmpeg(["-i", path, "-vn", "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "-"])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("مش قادر أقرا الملف:\n%s\n\n%s" % (path, r.stderr.decode("utf-8", "replace")[-500:]))
    x = np.frombuffer(r.stdout, dtype="<f4").reshape(-1, channels).astype(np.float64)
    return x[:, 0] if channels == 1 else x


def song_name(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.sub(r"(?i)[ _\-]*(master(ed)?|final|mix|v\d+)$", "", base).strip(" _-")
    return base or "song"


def fmt_time(t):
    return "%d:%05.2f" % (int(t // 60), t % 60)


# ----------------------------------------------------------------------------- spectrum, beats

def stft_mag(x, nfft=NFFT, hop=HOP):
    win = np.hanning(nfft)
    pad = np.concatenate([np.zeros(nfft // 2), x, np.zeros(nfft // 2)])
    n = 1 + (len(pad) - nfft) // hop
    out = np.empty((n, nfft // 2 + 1), np.float32)
    for a in range(0, n, 1024):
        b = min(n, a + 1024)
        idx = np.arange(nfft)[None, :] + hop * np.arange(a, b)[:, None]
        out[a:b] = np.abs(np.fft.rfft(pad[idx] * win, axis=1))
    return out


def onset_envelope(mag):
    logm = np.log1p(100 * mag)
    flux = np.maximum(0, np.diff(logm, axis=0)).mean(axis=1)
    flux = np.concatenate([[0], flux])
    flux -= np.convolve(flux, np.ones(16) / 16, mode="same")
    flux = np.maximum(flux, 0)
    return flux / (flux.std() + 1e-9)


def estimate_tempo(onset, fps):
    o = onset - onset.mean()
    n = len(o)
    ac = np.fft.irfft(np.abs(np.fft.rfft(o, 2 * n)) ** 2)[:n]
    best, best_bpm = -1, 120.0
    for bpm in np.arange(60, 190.01, 0.25):
        lag = 60.0 * fps / bpm
        s = 0.0
        for m, w in ((1, 1.0), (2, 0.5), (4, 0.25)):
            L = lag * m
            i = int(L)
            if i + 1 >= n:
                continue
            f = L - i
            s += w * ((1 - f) * ac[i] + f * ac[i + 1])
        s *= math.exp(-0.5 * (math.log2(bpm / 110.0) / 0.6) ** 2)
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
    tail = score[max(0, n - int(period * 2)):]
    t = n - len(tail) + int(np.argmax(tail))
    beats = []
    while t >= 0:
        beats.append(t)
        t = back[t]
    beats = np.array(beats[::-1])
    thr = 0.05 * np.median(onset[beats]) if len(beats) else 0
    return beats[onset[beats] > thr] if len(beats) > 8 else beats


def chroma_frames(mag, freqs, lo=55, hi=2000, tuning_cents=0.0):
    sel = (freqs >= lo) & (freqs < hi)
    f = freqs[sel]
    ref = 440.0 * 2 ** (tuning_cents / 1200.0)
    pc = np.round(12 * np.log2(f / ref) + 9).astype(int) % 12  # 0 = C
    m = mag[:, sel] ** 2
    c = np.zeros((mag.shape[0], 12))
    for k in range(12):
        c[:, k] = m[:, pc == k].sum(axis=1)
    return c


# ----------------------------------------------------------------------------- pitch tracking (YIN)

def yin(x, sr=SR, fmin=70.0, fmax=1000.0, frame=2048, hop=P_HOP, thr=0.15):
    """Returns (times, f0 Hz or 0, aperiodicity, rms dB)."""
    sos = butter(4, 60, "highpass", fs=sr, output="sos")
    x = sosfiltfilt(sos, x)
    W = frame // 2
    tmin, tmax = int(sr / fmax), int(sr / fmin)
    pad = np.concatenate([np.zeros(W), x, np.zeros(frame)])
    n = 1 + (len(x)) // hop
    f0 = np.zeros(n)
    ap = np.ones(n)
    rms = np.zeros(n)
    taus = np.arange(tmax + 2)
    for a in range(0, n, 1500):
        b = min(n, a + 1500)
        idx = np.arange(frame)[None, :] + hop * np.arange(a, b)[:, None]
        fr = pad[idx]
        head = fr[:, :W]
        rms[a:b] = 10 * np.log10((head ** 2).mean(axis=1) + 1e-12)
        L = 2 * frame
        acf = np.fft.irfft(np.conj(np.fft.rfft(head, L)) * np.fft.rfft(fr, L), L)[:, :tmax + 2]
        c = np.concatenate([np.zeros((b - a, 1)), np.cumsum(fr ** 2, axis=1)], axis=1)
        e0 = c[:, W][:, None]
        et = c[:, taus + W] - c[:, taus]
        d = e0 + et - 2 * acf
        d[:, 0] = 0
        cm = np.cumsum(d[:, 1:], axis=1)
        dn = np.ones_like(d)
        dn[:, 1:] = d[:, 1:] * np.arange(1, d.shape[1]) / np.maximum(cm, 1e-12)
        for i in range(b - a):
            row = dn[i]
            below = np.nonzero(row[tmin:tmax] < thr)[0]
            if len(below):
                t = tmin + below[0]
                while t + 1 < tmax and row[t + 1] < row[t]:
                    t += 1
            else:
                t = tmin + int(np.argmin(row[tmin:tmax]))
            ap[a + i] = row[t]
            if 1 <= t < len(row) - 1:  # parabolic interpolation
                y0, y1, y2 = row[t - 1], row[t], row[t + 1]
                den = y0 - 2 * y1 + y2
                tt = t + (0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0)
            else:
                tt = t
            f0[a + i] = sr / tt
    times = np.maximum(np.arange(n) * hop - W / 2, 0) / sr  # centre of the analysed window
    return times, f0, ap, rms


def voiced_pitch(times, f0, ap, rms):
    """Voicing decision + octave-jump cleanup. Returns MIDI float per frame (nan = unvoiced)."""
    ref = np.percentile(rms, 98)
    voiced = (ap < 0.3) & (rms > ref - 35) & (rms > -60)
    midi = np.where(voiced, 69 + 12 * np.log2(np.maximum(f0, 1) / 440.0), np.nan)
    # median smoothing inside voiced runs
    out = midi.copy()
    v = ~np.isnan(midi)
    i = 0
    n = len(midi)
    while i < n:
        if not v[i]:
            i += 1
            continue
        j = i
        while j < n and v[j]:
            j += 1
        seg = midi[i:j]
        if len(seg) >= 5:
            med = medfilt(seg, 5)
            # octave errors: snap frames that sit ~12 semitones off the local median
            big = medfilt(seg, min(len(seg) // 2 * 2 - 1, 31)) if len(seg) > 6 else med
            diff = seg - big
            seg = np.where(np.abs(np.abs(diff) - 12) < 1.5, seg - 12 * np.sign(diff), seg)
            out[i:j] = medfilt(seg, 5)
        else:
            out[i:j] = np.nan  # too short to be a sung note
        i = j
    return out


def estimate_tuning(midi):
    """Global tuning offset in cents (circular mean of the deviations)."""
    m = midi[~np.isnan(midi)]
    if len(m) < 20:
        return 0.0
    ang = 2 * np.pi * (m - np.round(m))
    c = math.atan2(np.sin(ang).mean(), np.cos(ang).mean())
    return 100 * c / (2 * np.pi)


def segment_notes(times, midi, rms, tuning, min_dur=0.09):
    """Split the pitch curve into notes. Returns list of dicts (start, end, pitch, cents)."""
    hop = P_HOP / float(SR)
    m = midi - tuning / 100.0
    notes = []
    n = len(m)
    artic = set()  # note starts that came from an energy dip (a re-sung note)
    # re-articulated notes: an energy dip of ≥ 6 dB against both sides (within ~70 ms)
    sm = np.convolve(rms, np.ones(3) / 3, mode="same")
    dips = set()
    w = 6
    for k in range(w, n - w):
        if np.isnan(m[k]) or sm[k] > sm[k - 1] or sm[k] > sm[k + 1]:
            continue
        if sm[k - w:k].max() - sm[k] >= 6 and sm[k + 1:k + w + 1].max() - sm[k] >= 6:
            dips.add(k)
    i = 0
    while i < n:
        if np.isnan(m[i]):
            i += 1
            continue
        j = i
        cur = [m[i]]
        center = m[i]
        start = i
        while j + 1 < n and not np.isnan(m[j + 1]):
            nxt = m[j + 1]
            # new note when the pitch settles ≥ 0.6 semitone away for ~50 ms
            look = m[j + 1:j + 6]
            look = look[~np.isnan(look)]
            if len(look) >= 3 and abs(np.median(look) - np.median(cur[-8:])) > 0.6:
                notes.append((start, j, np.array(cur)))
                start, cur = j + 1, []
            elif j + 1 in dips and j > start + 3:
                notes.append((start, j, np.array(cur)))
                start, cur = j + 1, []
                artic.add(start)
            cur.append(nxt)
            j += 1
        notes.append((start, j, np.array(cur)))
        i = j + 1
    out = []
    for s, e, vals in notes:
        dur = (e - s + 1) * hop
        if dur < min_dur or len(vals) == 0:
            continue
        core = vals[len(vals) // 5: max(len(vals) // 5 + 1, len(vals) - len(vals) // 5)]
        p = float(np.median(core))
        q = int(round(p))
        out.append({"start": float(times[s]), "end": float(times[e] + hop), "midi": q,
                    "cents": float(100 * (p - q)), "exact": p, "artic": s in artic})
    # merge tiny gaps between equal notes (vibrato dropouts)
    merged = []
    for nt in out:
        if merged and nt["midi"] == merged[-1]["midi"] and nt["start"] - merged[-1]["end"] < 0.03 and not nt["artic"]:
            merged[-1]["end"] = nt["end"]
        else:
            merged.append(nt)
    return merged


# ----------------------------------------------------------------------------- harmony

def scale_pcs(tonic, scale):
    return sorted((tonic + s) % 12 for s in SCALES[scale])


def harmony_line(notes, tonic, scale, steps, octave):
    if steps is None:
        return [dict(nt, midi=nt["midi"] + octave) for nt in notes]
    pcs = set(scale_pcs(tonic, scale))
    ladder_nat = [m for m in range(0, 128) if m % 12 in pcs]
    lead = (tonic + 11) % 12
    # minor key + raised 7th in the melody (C# in D minor): harmonise on harmonic minor
    harm_pcs = (pcs - {(tonic + 10) % 12}) | {lead}
    ladder_harm = [m for m in range(0, 128) if m % 12 in harm_pcs]
    out = []
    for nt in notes:
        m = nt["midi"]
        ladder = ladder_harm if scale == "minor" and m % 12 == lead else ladder_nat
        # nearest scale tone (ties go down), keep any chromatic offset
        idx = min(range(len(ladder)), key=lambda i: (abs(ladder[i] - m), ladder[i] > m))
        off = m - ladder[idx]
        j = min(max(idx + steps, 0), len(ladder) - 1)
        out.append(dict(nt, midi=ladder[j] + off))
    return out


def use_flats(tonic, scale):
    major_tonic = tonic if scale == "major" else (tonic + 3) % 12
    return major_tonic in (5, 10, 3, 8, 1, 6)  # F Bb Eb Ab Db Gb


def note_name(pc, flats):
    return (FLATS if flats else SHARPS)[pc % 12]


def chord_name(root, quality, flats):
    return note_name(root, flats) + ("m" if quality == "min" else "")


def key_name(tonic, scale, flats=None):
    flats = use_flats(tonic, scale) if flats is None else flats
    return note_name(tonic, flats) + (" major" if scale == "major" else " minor")


def key_name_ar(tonic, scale):
    n = note_name(tonic, use_flats(tonic, scale))
    base = AR_NOTES[n[0]] + (" دييز" if "#" in n else " بيمول" if "b" in n[1:] else "")
    return "%s %s" % (base, "ماجير" if scale == "major" else "مينير")


# ----------------------------------------------------------------------------- analysis

class Song:
    def __init__(self, path, stem=None, progress=None):
        self.path, self.stem = path, stem or None
        self.name = song_name(path)
        say = progress or (lambda m: None)
        say("بقرا الأغنية…")
        mix = decode(path)
        self.duration = len(mix) / SR
        if self.duration < 8:
            raise RuntimeError("الأغنية قصيرة قوي")
        voc = None
        if self.stem:
            say("بقرا الـ vocal stem…")
            voc = decode(self.stem)
            if len(voc) < len(mix):
                voc = np.pad(voc, (0, len(mix) - len(voc)))
            voc = voc[:len(mix)]
        # instrumental = mix − stem when the stem lines up with the mix (same project)
        inst = mix
        if voc is not None:
            n = min(len(mix), len(voc))
            c = float(np.dot(mix[:n], voc[:n]) / (np.linalg.norm(mix[:n]) * np.linalg.norm(voc[:n]) + 1e-12))
            if c > 0.3:
                g = float(np.dot(mix[:n], voc[:n]) / (np.dot(voc[:n], voc[:n]) + 1e-12))
                inst = mix - g * voc
        self.stem_subtracted = inst is not mix

        say("بدوّر على الـ BPM والمازورة…")
        mag = stft_mag(mix)
        self.fps = SR / float(HOP)
        freqs = np.fft.rfftfreq(NFFT, 1.0 / SR)
        # beats from a short window (1024 / 256): a long window smears attacks ~50 ms early
        ofps = SR / 256.0
        o_short = onset_envelope(stft_mag(mix, 1024, 256))
        bpm = estimate_tempo(o_short, ofps)
        beats = track_beats(o_short, ofps, bpm)
        bt = beats / ofps
        if len(bt) >= 16:
            # straight-line fit over all beats: sub-millisecond period precision on a steady song
            ibi = float(np.median((bt[8:] - bt[:-8]) / 8.0))
            k = np.round((bt - bt[0]) / ibi)
            slope = float(np.polyfit(k, bt, 1)[0])
            bpm = 60.0 / slope if abs(slope - ibi) < 0.02 * ibi else 60.0 / ibi
        # songs made in a DAW sit on whole BPMs: snap when it's that close
        self.bpm = float(round(bpm)) if abs(bpm - round(bpm)) < 0.15 else round(bpm, 2)
        if len(bt) < 8:  # no clear pulse: fall back to the tempo grid
            bt = np.arange(0, self.duration, 60.0 / self.bpm)
        else:  # extend the grid back to the start (pickups, soft intros)
            ibi = 60.0 / bpm
            pre = np.arange(bt[0] - ibi, -0.5 * ibi, -ibi)[::-1]
            bt = np.concatenate([np.maximum(pre[pre > -0.25 * ibi], 0), bt])
        self.beats = bt
        onset = np.interp(np.arange(mag.shape[0]) / self.fps, np.arange(len(o_short)) / ofps, o_short)

        # melody first: its tuning feeds the chroma
        self.notes, self.tuning, self.pitch = [], 0.0, None
        if voc is not None:
            say("بتتبّع اللحن من الـ stem…")
            src = voc
        else:
            say("مفيش stem — بحاول ألقط اللحن من الأغنية كلها (تقريبي)…")
            src = sosfiltfilt(butter(4, [150, 1500], "bandpass", fs=SR, output="sos"), mix)
        times, f0, ap, rms = yin(src)
        midi = voiced_pitch(times, f0, ap, rms)
        self.tuning = estimate_tuning(midi)
        self.notes = segment_notes(times, midi, rms, self.tuning)
        self.pitch = (times, midi)
        self.melody_from_stem = voc is not None

        say("بدوّر على المقام/السلم والكوردات…")
        imag = stft_mag(inst) if inst is not mix else mag
        ch = chroma_frames(imag, freqs, 55, 2000, self.tuning)
        bass = chroma_frames(imag, freqs, 40, 250, self.tuning)
        self.find_key(ch)
        self.find_bars(onset, imag, freqs, ch)
        self.find_chords(ch, bass)
        self.find_quarter_tones()
        say("خلص ✔")

    # -- key ------------------------------------------------------------------
    def find_key(self, ch):
        prof = np.log1p(ch / (ch.sum(axis=1, keepdims=True) + 1e-12) * 50).sum(axis=0)
        if self.notes:  # the melody's pitch classes, weighted by duration
            mel = np.zeros(12)
            for nt in self.notes:
                mel[nt["midi"] % 12] += nt["end"] - nt["start"]
            prof = prof / (prof.sum() + 1e-12) + 0.5 * mel / (mel.sum() + 1e-12)
        scores = []
        for scale, kk in KK.items():
            for t in range(12):
                scores.append((float(np.corrcoef(prof, np.roll(kk, t))[0, 1]), t, scale))
        scores.sort(reverse=True)
        self.key_scores = scores
        _, self.tonic, self.scale = scores[0]
        self.flats = use_flats(self.tonic, self.scale)

    def set_key(self, tonic, scale):
        self.tonic, self.scale = tonic, scale
        self.flats = use_flats(tonic, scale)
        for c in self.chords:
            c["name"] = "N" if c["root"] is None else chord_name(c["root"], c["quality"], self.flats)

    # -- bars ---------------------------------------------------------------
    def find_bars(self, onset, mag, freqs, ch):
        bt = self.beats
        fr = np.clip((bt * self.fps).astype(int), 0, len(onset) - 1)
        low = (mag[:, (freqs >= 30) & (freqs < 150)] ** 2).sum(axis=1)
        low = low / (low.max() + 1e-12)
        chn = ch / (ch.sum(axis=1, keepdims=True) + 1e-12)
        best, ph = -1, 0
        for p in range(4):
            idx = fr[p::4]
            s = low[idx].mean() + 0.5 * onset[idx].mean() / (onset[fr].mean() + 1e-9)
            prev = np.clip(idx - int(self.fps * 0.3), 0, len(chn) - 1)
            nxt = np.clip(idx + int(self.fps * 0.3), 0, len(chn) - 1)
            s += 2.0 * np.abs(chn[nxt] - chn[prev]).sum(axis=1).mean()
            if s > best:
                best, ph = s, p
        self.downbeat_phase = ph
        self.first_downbeat = float(bt[ph]) if len(bt) > ph else 0.0

    def bar_of_beat(self, i):
        """(bar number, beat in bar) — bar 1 starts at the first detected downbeat."""
        k = i - self.downbeat_phase
        return k // 4 + 1, k % 4 + 1

    # -- chords --------------------------------------------------------------
    def find_chords(self, ch, bass):
        bt = self.beats
        edges = np.concatenate([bt, [min(self.duration, bt[-1] + 60.0 / self.bpm)]])
        fr = np.clip((edges * self.fps).astype(int), 0, len(ch) - 1)
        nb = len(bt)
        X = np.array([ch[fr[i]:max(fr[i + 1], fr[i] + 1)].mean(axis=0) for i in range(nb)])
        Bs = np.array([bass[fr[i]:max(fr[i + 1], fr[i] + 1)].mean(axis=0) for i in range(nb)])
        energy = X.sum(axis=1)
        Xn = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-12)
        Bn = Bs / (Bs.sum(axis=1, keepdims=True) + 1e-12)
        labels = []  # (root, quality)
        T = []
        for r in range(12):
            for q, iv in (("maj", (0, 4, 7)), ("min", (0, 3, 7))):
                v = np.zeros(12)
                for k, w in zip(iv, (1.0, 0.8, 0.9)):
                    v[(r + k) % 12] = w
                T.append(v / np.linalg.norm(v))
                labels.append((r, q))
        T = np.array(T)
        pcs = set(scale_pcs(self.tonic, self.scale))
        dom = (self.tonic + 7) % 12  # harmonic-minor V (e.g. A in D minor) is in-key too
        diatonic = np.array([1.0 if ((r in pcs and (r + (4 if q == "maj" else 3)) % 12 in pcs and (r + 7) % 12 in pcs)
                                     or (self.scale == "minor" and r == dom and q == "maj"))
                             else 0.0 for r, q in labels])
        S = Xn @ T.T + 0.35 * Bn[:, [r for r, _ in labels]] + 0.03 * diatonic
        quiet = energy < 0.02 * np.percentile(energy, 90)
        # Viterbi over beats: changing chord costs a little, staying is free
        K = len(labels)
        switch = 0.25
        cost = -S
        acc = cost[0].copy()
        back = np.zeros((nb, K), dtype=int)
        for i in range(1, nb):
            best_prev = int(np.argmin(acc))
            stay = acc
            move = acc[best_prev] + switch
            back[i] = np.where(stay <= move, np.arange(K), best_prev)
            acc = np.minimum(stay, move) + cost[i]
        path = [int(np.argmin(acc))]
        for i in range(nb - 1, 0, -1):
            path.append(back[i][path[-1]])
        path.reverse()
        per_beat = [None if quiet[i] else labels[path[i]] for i in range(nb)]
        self.beat_chords = per_beat
        segs = []
        for i, lab in enumerate(per_beat):
            if segs and segs[-1]["label"] == lab:
                segs[-1]["end"] = float(edges[i + 1])
                segs[-1]["beats"] += 1
            else:
                segs.append({"label": lab, "start": float(edges[i]), "end": float(edges[i + 1]), "beat": i, "beats": 1})
        self.chords = []
        for s in segs:
            root, q = s["label"] if s["label"] else (None, None)
            bar, beat = self.bar_of_beat(s["beat"])
            self.chords.append({"start": s["start"], "end": s["end"], "root": root, "quality": q,
                                "name": "N" if root is None else chord_name(root, q, self.flats),
                                "bar": bar, "beat_in_bar": beat, "beats": s["beats"]})

    # -- maqam / quarter tones ---------------------------------------------------
    def find_quarter_tones(self):
        flagged = [nt for nt in self.notes if abs(nt["cents"]) > QUARTER_TONE_CENTS and nt["end"] - nt["start"] >= 0.15]
        sections = []
        for nt in flagged:
            if sections and nt["start"] - sections[-1]["end"] < 4.0:
                sections[-1]["end"] = nt["end"]
                sections[-1]["notes"].append(nt)
            else:
                sections.append({"start": nt["start"], "end": nt["end"], "notes": [nt]})
        for s in sections:
            s["bar_from"] = self.bar_at(s["start"])
            s["bar_to"] = self.bar_at(s["end"])
            s["pitches"] = sorted({self.qt_name(nt) for nt in s["notes"]})
        self.quarter_sections = sections

    def qt_name(self, nt):
        """A pitch between two semitones: half-flat of the natural above, else half-sharp of the one below."""
        p = nt["exact"]
        lo = int(math.floor(p))
        up = SHARPS[(lo + 1) % 12]
        if "#" not in up:
            return "%s half-flat (%s نص بيمول)" % (up, AR_NOTES[up])
        down = SHARPS[lo % 12]
        return "%s half-sharp (%s نص دييز)" % (down, AR_NOTES[down[0]])

    def bar_at(self, t):
        i = int(np.searchsorted(self.beats, t, side="right")) - 1
        return self.bar_of_beat(max(i, 0))[0]

    def in_quarter_section(self, t0, t1):
        return any(s["start"] - 0.5 <= t1 and t0 <= s["end"] + 0.5 for s in self.quarter_sections)

    def harmony(self, key):
        steps, octave = {k: (s, o) for k, _, s, o in LINES}[key]
        return harmony_line(self.notes, self.tonic, self.scale, steps, octave)

    # -- DAW position: project tempo = bpm, audio placed at bar 1 ------------------
    def daw_pos(self, t):
        beats = t * self.bpm / 60.0
        return "%d.%d" % (int(beats // 4) + 1, int(beats % 4) + 1)


def chart_rows(song):
    """One row per bar: bar, time, DAW position, 4 beat cells ('.' = same chord), quarter-tone flag."""
    bars = {}
    for i in range(len(song.beats)):
        bar, beat = song.bar_of_beat(i)
        lab = song.beat_chords[i]
        bars.setdefault(bar, [None] * 4)[beat - 1] = ("N" if lab is None else chord_name(lab[0], lab[1], song.flats), i)
    rows, prev = [], None
    for bar in sorted(bars):
        cells = []
        prev = None  # every bar starts with its chord name, like a printed chart
        for slot in bars[bar]:
            if slot is None:
                cells.append("")
                continue
            cells.append("." if slot[0] == prev else slot[0])
            prev = slot[0]
        first = next(sl for sl in bars[bar] if sl)
        t = float(song.beats[first[1]])
        rows.append({"bar": bar, "time": t, "daw": song.daw_pos(t), "cells": cells,
                     "qt": song.in_quarter_section(t, t + 4 * 60 / song.bpm)})
    return rows


def chart_text(song):
    """Chord chart: one line per bar, 4 beat slots ('.' = same chord)."""
    lines = ["%s — %s" % (song.name, key_name(song.tonic, song.scale)),
             "BPM %g · 4/4 · بار 1 = أول ضربة قوية عند %s" % (song.bpm, fmt_time(song.first_downbeat)),
             "عمود DAW = مكان البار لو حطيت الأغنية على 1.1.1 وتمبو المشروع %g" % song.bpm, ""]
    for r in chart_rows(song):
        lines.append("بار %3d  %8s  DAW %-7s | %s |%s" % (r["bar"], fmt_time(r["time"]), r["daw"],
                                                        " ".join("%-5s" % c for c in r["cells"]), " ~ربع تون" if r["qt"] else ""))
    return "\n".join(lines) + "\n"


def main():
    a = sys.argv[1:]
    s = Song(a[0], a[1] if len(a) > 1 else None, progress=print)
    print("BPM", s.bpm, "key", key_name(s.tonic, s.scale), "tuning %+.0f cents" % s.tuning, "notes", len(s.notes))
    print(chart_text(s))
    for q in s.quarter_sections:
        print("quarter tones %s–%s bars %d–%d %s" % (fmt_time(q["start"]), fmt_time(q["end"]), q["bar_from"], q["bar_to"], q["pitches"]))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
