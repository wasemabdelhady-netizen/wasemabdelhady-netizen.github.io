"""«غنّي جوه أغنيتك»: decode a vocal stem (WAV/MP3 via the bundled ffmpeg),
extract the reference melody (quarter tones kept), and build a plan where the
section plays while you sing and every note is scored."""
import math
import os
import shutil
import subprocess
import sys
import wave

import numpy as np

import engine as E

MIN_NOTE = 0.12        # shorter pieces are ornaments: drawn, not scored
SPLIT_C = 60.0         # a new note when the pitch moves this far for 3 frames
CAP = E.TOP_LIMIT + 0.15   # G4 as sung (±15c) still counts as G4


def ffmpeg_path():
    p = os.path.join(E.ROOT, "ffmpeg", "ffmpeg.exe")
    return p if os.path.isfile(p) else shutil.which("ffmpeg")


def decode(path, sr=E.SR):
    """-> float32 (n, 2). ffmpeg for anything; plain PCM WAV works without it."""
    ff = ffmpeg_path()
    if ff:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000   # CREATE_NO_WINDOW under pythonw
        r = subprocess.run([ff, "-v", "error", "-i", path, "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, **kw)
        if r.returncode != 0 or not r.stdout:
            raise IOError(r.stderr.decode("utf-8", "replace")[-400:] or "ffmpeg failed")
        return np.frombuffer(r.stdout, "<f4").reshape(-1, 2).copy()
    with open(path, "rb") as fh:
        w = wave.open(fh)
        ch, sw, fr, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    elif sw == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3)
        x = ((b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16)) << 8 >> 8) / 8388608.0
        x = x.astype(np.float32)
    else:
        raise IOError("WAV %d-bit needs ffmpeg" % (8 * sw))
    x = x.reshape(-1, ch)
    x = np.repeat(x, 2, 1) if ch == 1 else x[:, :2]
    if fr != sr:
        t = np.arange(int(len(x) * sr / fr)) * fr / sr
        x = np.stack([np.interp(t, np.arange(len(x)), x[:, c]) for c in range(2)], 1).astype(np.float32)
    return x


def contour(mono, sr=E.SR, progress=None):
    """Offline pitch track of the stem: (times, midi with NaN)."""
    x = np.asarray(mono, np.float64)
    hop = E.HOP
    nfr = len(x) // hop
    rms = np.sqrt(np.mean(x[:nfr * hop].reshape(nfr, hop) ** 2, 1) + 1e-12)
    ref_db = 20 * math.log10(np.percentile(rms, 95) + 1e-9)
    pt = E.PitchTracker(threshold=0.12, gate_db=max(-60.0, ref_db - 30.0))
    T, M = [], []
    step = hop * 400
    for i in range(0, len(x), step):
        for t, f0, c, db in pt.feed(x[i:i + step]):
            T.append(t)
            M.append(E.hz_to_midi(f0) if f0 else np.nan)
        if progress:
            progress(min(1.0, (i + step) / len(x)))
    T, M = np.array(T), np.array(M)
    # drop voiced blips shorter than 40 ms
    v = ~np.isnan(M)
    i = 0
    while i < len(v):
        if v[i]:
            j = i
            while j < len(v) and v[j]:
                j += 1
            if (j - i) * hop / sr < 0.04:
                M[i:j] = np.nan
            i = j
        else:
            i += 1
    return T, M


def segment(T, M):
    """Contour -> notes [(t0, t1, midi)] keeping quarter tones (median of each note)."""
    notes = []
    dt = E.HOP / E.SR
    i, n = 0, len(M)
    while i < n:
        if np.isnan(M[i]):
            i += 1
            continue
        j = i
        while j < n and not np.isnan(M[j]):
            j += 1
        run = M[i:j]
        start = 0
        k = 0
        while k < len(run):
            ref = np.median(run[max(start, k - 8):k]) if k > start else run[k]
            if k > start + 2 and all(abs(run[q] - ref) * 100 > SPLIT_C for q in range(k, min(k + 3, len(run)))):
                notes.append((T[i + start], T[i + k - 1] + dt, run[start:k]))
                start = k
            k += 1
        notes.append((T[i + start], T[i + len(run) - 1] + dt, run[start:]))
        i = j
    out = []
    for t0, t1, v in notes:
        # centre of the steady middle (skip 20% at each end: slides in/out)
        a, b = int(len(v) * 0.2), max(int(len(v) * 0.8), int(len(v) * 0.2) + 1)
        core = v[a:b]
        c = 0.5 * (np.percentile(core, 10) + np.percentile(core, 90))
        out.append((float(t0), float(t1), float(c)))
    return out


class Song:
    def __init__(self, stem_path, mix_path=None, progress=None):
        self.stem_path, self.mix_path = stem_path, mix_path
        self.stem = decode(stem_path)
        self.mix = decode(mix_path) if mix_path else None
        self.dur = len(self.stem) / E.SR
        T, M = contour(self.stem.mean(1), progress=progress)
        notes = segment(T, M)
        mids = [m for t0, t1, m in notes if t1 - t0 >= MIN_NOTE]
        # sung an octave (or two) above my range: move the targets down
        self.shift = 0
        while mids and np.mean(np.array(mids) + self.shift > E.TOP_LIMIT + 0.3) > 0.10 and self.shift > -24:
            self.shift -= 12
        self.T, self.M = T, M + self.shift
        self.notes = [(t0, t1, m + self.shift) for t0, t1, m in notes]
        self.over = sum(1 for t0, t1, m in self.notes if m > CAP and t1 - t0 >= MIN_NOTE)

    def audio(self, which="stem"):
        return self.mix if (which == "mix" and self.mix is not None) else self.stem


def plan_song(song, t_from=0.0, t_to=None, loops=1, play="stem"):
    t_to = min(song.dur, t_to or song.dur)
    if t_to - t_from < 1.0:
        raise E.PlanError("section|")
    steps = []
    for r in range(loops):
        segs = []
        for t0, t1, m in song.notes:
            if t0 < t_from or t1 > t_to:
                continue
            ok = m <= CAP
            segs.append(E.Seg(t0 - t_from, t1 - t_from, min(m, float(E.TOP_LIMIT)),
                              label="song", scored=ok and (t1 - t0) >= MIN_NOTE))
            if not ok:
                segs[-1].label = "over"
        if not segs:
            raise E.PlanError("section|")
        st = E.Step(r, segs[0].m0, segs, listen=False)
        st.dur = t_to - t_from
        st.song_t0 = t_from
        sel = (song.T >= t_from) & (song.T < t_to)
        lane = song.M[sel]
        lane = np.where(lane > CAP, np.nan, lane)   # nothing above G4 is shown as a target
        st.lane = (song.T[sel] - t_from, lane)
        steps.append(st)
    E.layout(steps, lead=1.0, tail=0.8)
    p = E.Plan("song", steps, {"song": os.path.basename(song.stem_path), "from": round(t_from, 1),
                               "to": round(t_to, 1), "shift": song.shift, "play": play}, song=song)
    p.play = play
    return p


def fmt_t(t):
    return "%d:%04.1f" % (int(t // 60), t - 60 * int(t // 60))


def parse_t(s):
    s = (s or "").strip()
    if not s:
        return None
    if ":" in s:
        m, x = s.split(":", 1)
        return int(m) * 60 + float(x)
    return float(s)


def synth_melody(notes, sr=E.SR, vib=12.0, seed=0, glide=0.05):
    """Test helper: a 'vocal stem' singing (t0, t1, midi) notes with vibrato and
    short glides between them."""
    end = max(t1 for t0, t1, m in notes) + 0.5
    tr = np.full(int(end * sr), np.nan)
    for i, (t0, t1, m) in enumerate(notes):
        a, b = int(t0 * sr), int(t1 * sr)
        vals = np.full(b - a, m)
        if i and abs(notes[i - 1][1] - t0) < 0.01:   # legato: glide from the previous note
            g = int(glide * sr)
            vals[:g] = np.linspace(notes[i - 1][2], m, g)
        tr[a:b] = vals
    return E.synth_voice(tr, vibrato_cents=vib, seed=seed, noise=0.004)


def write_wav(path, y, sr=E.SR):
    y = np.asarray(y, np.float32)
    ch = 1 if y.ndim == 1 else y.shape[1]
    with open(path, "wb") as fh:
        w = wave.open(fh, "wb")
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
        w.close()


TEST_DEGREES = [0, 1.5, 3, 5, 3, 1.5, 0, -2, 0, 1.5, 3, 5, 7, 5, 3, 1.5, 0]
TEST_LENGTHS = [0.6, 0.4, 0.8, 1.2, 0.5, 0.5, 1.0, 0.6, 0.4, 0.7, 0.9, 0.6, 1.0, 0.5, 0.6, 0.5, 1.4]


def test_melody(tonic=50.0, t=0.6):
    """A Bayati line on D3 (quarter tones, legato, two breaths) for tests."""
    notes = []
    for i, (d, L) in enumerate(zip(TEST_DEGREES, TEST_LENGTHS)):
        notes.append((t, t + L, tonic + d))
        t += L
        if i in (3, 7, 12):
            t += 0.45
    return notes


def make_test_files(folder):
    """Synthetic 'vocal stem' as MP3 (through ffmpeg) + a 'mix' WAV with an oud bed."""
    import oud
    os.makedirs(folder, exist_ok=True)
    notes = test_melody()
    y = synth_melody(notes, vib=14, seed=11)
    wav = os.path.join(folder, "ستيم تجربة.wav")
    write_wav(wav, y)
    mp3 = os.path.join(folder, "ستيم تجربة.mp3")
    ff = ffmpeg_path()
    if ff:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        subprocess.run([ff, "-y", "-v", "error", "-i", wav, "-b:a", "192k", mp3], stdin=subprocess.DEVNULL, **kw)
    stem = mp3 if os.path.isfile(mp3) else wav
    bed = np.zeros_like(y)
    for k in range(0, int(len(y) / E.SR), 2):
        p = oud.pluck(50.0, 2.5, amp=0.15)
        a = k * E.SR
        bed[a:a + len(p)] += p[:len(bed) - a]
    mix = os.path.join(folder, "ميكس تجربة.wav")
    write_wav(mix, np.stack([y + bed, y + 0.8 * bed], 1))
    return stem, mix, notes
