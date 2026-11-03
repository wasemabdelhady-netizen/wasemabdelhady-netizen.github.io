"""«العُرَب» — vocal runs: predominant-melody tracking in a mixed recording,
run detection, note extraction (quarter tones kept), maqam/jins guess,
pitch-preserving stretch/transpose (ffmpeg rubberband), DTW scoring of a
sung-back run, and generated progressive drills."""
import math
import subprocess
import sys

import numpy as np

import engine as E
import song as G

DT = E.HOP / E.SR                       # 5.8 ms per frame
BIN_C = 10.0                            # salience grid: 10 cents
F_LO = 55.0
NBINS = int(1200 * math.log2(1100.0 / F_LO) / BIN_C)
HARM_W = 0.8 ** np.arange(8)

# ---------------------------------------------------------------- names
AR_NAMES = ["دو", "دو#", "ري", "مي♭", "مي", "فا", "فا#", "صول", "لا♭", "لا", "سي♭", "سي"]


def names(m):
    """(Western, Arabic). Quarter tones: half-flat of a natural note (E♭½3 / مي♭½),
    or half-sharp of the natural below when the upper note has an accidental
    (F#-50c = F#½ / فا#½)."""
    n = int(math.floor(m + 0.5))
    c = (m - n) * 100
    w, a = E.NAMES[n % 12].replace("b", "♭") + "%d" % (n // 12 - 1), AR_NAMES[n % 12]
    if c < -25:
        if len(E.NAMES[n % 12]) == 1:
            w, a = E.NAMES[n % 12] + "♭½%d" % (n // 12 - 1), AR_NAMES[n % 12] + "♭½"
        else:
            k = n - 1
            w, a = E.NAMES[k % 12] + "#½%d" % (k // 12 - 1), AR_NAMES[k % 12] + "#½"
    return w, a


# ------------------------------------------------- predominant melody
def _peaks(mag, freqs, k=30):
    """Top spectral peaks of one frame: (freq, weight) with parabolic interpolation."""
    m = mag
    p = np.nonzero((m[1:-1] > m[:-2]) & (m[1:-1] >= m[2:]))[0] + 1
    if not len(p):
        return np.zeros(0), np.zeros(0)
    p = p[np.argsort(m[p])[-k:]]
    a, b, c = np.log(m[p - 1] + 1e-12), np.log(m[p] + 1e-12), np.log(m[p + 1] + 1e-12)
    den = a - 2 * b + c
    off = np.where(np.abs(den) > 1e-12, 0.5 * (a - c) / den, 0.0)
    f = (p + np.clip(off, -0.5, 0.5)) * (freqs[1] - freqs[0])
    db = 20 * np.log10(m[p] / (m.max() + 1e-12) + 1e-12)
    w = np.clip((db + 40.0) / 40.0, 0, 1)
    return f, w


def melody(x, sr=E.SR, progress=None):
    """Dominant pitched line of a (possibly accompanied) recording.
    Harmonic-summation salience on a 10-cent grid + Viterbi continuity inside
    voiced stretches, refined from the harmonic peaks. Returns T, M (NaN =
    unvoiced) and a per-frame confidence 0..1 (chosen salience / frame max)."""
    x = np.asarray(x, np.float64)
    N, hop = E.FRAME, E.HOP
    nfr = max(0, (len(x) - N) // hop)
    win = np.hanning(N)
    freqs = np.fft.rfftfreq(N, 1.0 / sr)
    S = np.zeros((nfr, NBINS), np.float32)
    PK = []
    rms = np.zeros(nfr)
    for i in range(nfr):
        fr = x[i * hop:i * hop + N]
        rms[i] = np.sqrt(np.mean(fr * fr) + 1e-12)
        f, w = _peaks(np.abs(np.fft.rfft(fr * win)), freqs)
        PK.append((f, w))
        for h in range(8):
            fb = f / (h + 1)
            ok = (fb >= F_LO) & (fb < 1100.0)
            if not ok.any():
                continue
            b = 1200 * np.log2(fb[ok] / F_LO) / BIN_C
            for dd, kw in ((-1, 0.5), (0, 1.0), (1, 0.5)):
                bi = np.round(b).astype(int) + dd
                good = (bi >= 0) & (bi < NBINS)
                np.add.at(S[i], bi[good], (w[ok] * HARM_W[h] * kw)[good])
        if progress and i % 2000 == 0:
            progress(0.8 * i / max(1, nfr))
    # vocal-range prior: slight preference for 90–700 Hz
    fb = F_LO * 2 ** (np.arange(NBINS) * BIN_C / 1200)
    S *= np.where((fb > 90) & (fb < 700), 1.0, 0.8).astype(np.float32)
    smax = S.max(1)
    gate = 20 * np.log10(rms + 1e-12) > 20 * np.log10(np.percentile(rms, 95) + 1e-12) - 30
    voiced = gate & (smax > 0.25 * np.percentile(smax[gate], 90) if gate.any() else gate)
    path = np.full(nfr, -1)
    lam = 0.02                                       # salience cost per 10 cents of jump
    i = 0
    while i < nfr:
        if not voiced[i]:
            i += 1
            continue
        j = i
        while j < nfr and voiced[j]:
            j += 1
        seg = S[i:j] / (smax[i:j, None] + 1e-9)
        D = 40                                       # up to 400 c per frame
        score = seg[0].copy()
        back = np.zeros((j - i, NBINS), np.int16)
        offs = np.arange(-D, D + 1)
        pen = lam * np.abs(offs)
        for k in range(1, j - i):
            padded = np.concatenate((np.full(D, -1e9), score, np.full(D, -1e9)))
            cand = np.stack([padded[D + o:D + o + NBINS] for o in offs]) - pen[:, None]
            arg = np.argmax(cand, 0)
            score = cand[arg, np.arange(NBINS)] + seg[k]
            back[k] = offs[arg]
        b = int(np.argmax(score))
        for k in range(j - i - 1, -1, -1):
            path[i + k] = b
            b += int(back[k][b])
        i = j
        if progress:
            progress(0.8 + 0.2 * i / max(1, nfr))
    T = (np.arange(nfr) * hop + N / 2) / sr
    M = np.full(nfr, np.nan)
    conf = np.zeros(nfr)
    for i in np.nonzero(path >= 0)[0]:
        f0 = F_LO * 2 ** (path[i] * BIN_C / 1200)
        f, w = PK[i]
        est, ws = [], []
        for h in range(1, 9):
            if len(f):
                c = 1200 * np.log2(np.maximum(f, 1e-6) / (h * f0))
                k = np.nonzero(np.abs(c) < 35)[0]
                if len(k):
                    kk = k[np.argmax(w[k])]
                    est.append(f[kk] / h)
                    ws.append(w[kk] * HARM_W[h - 1])
        if est:
            M[i] = E.hz_to_midi(float(np.average(est, weights=ws)))
        else:
            M[i] = E.hz_to_midi(f0)
        conf[i] = S[i, path[i]] / (smax[i] + 1e-9)
    # drop blips < 40 ms
    v = ~np.isnan(M)
    k = 0
    while k < nfr:
        if v[k]:
            e = k
            while e < nfr and v[e]:
                e += 1
            if (e - k) * DT < 0.04:
                M[k:e] = np.nan
            k = e
        else:
            k += 1
    return T, M, conf


def confidence(M, conf):
    v = ~np.isnan(M)
    return {"voiced_pct": round(100 * float(v.mean()), 1) if len(M) else 0.0,
            "conf": round(float(np.median(conf[v])), 2) if v.any() else 0.0}


# ------------------------------------------------------------ notes
def fast_notes(T, M, stable_c=15.0, min_len=0.04, merge_c=40.0):
    """Plateaus of the pitch line (slope under ~15 c per frame) → notes
    [(t0, t1, midi)]. Works for 10 notes/s runs and for vibrato notes alike."""
    m = M.copy()
    ok = ~np.isnan(m)
    d = np.full(len(m), np.inf)
    d[1:] = np.abs(np.diff(m)) * 100
    st = ok & np.concatenate(([False], ok[:-1])) & (d < stable_c)
    out = []
    i, n = 0, len(m)
    while i < n:
        if not st[i]:
            i += 1
            continue
        j = i
        while j < n and st[j]:
            j += 1
        # a slow glide (or a stretched one) can stay under the slope limit:
        # split where the pitch walks > 45 c away from the running note for 3 frames
        a0 = i - 1
        k = a0 + 3
        while k < j:
            ref = np.median(m[a0:k])
            if k + 2 < j and all(abs(m[q] - ref) > 0.45 for q in range(k, k + 3)):
                if (k - a0) * DT >= min_len:
                    v = m[a0:k]
                    out.append([T[a0], T[k - 1] + DT, float(0.5 * (np.percentile(v, 10) + np.percentile(v, 90)))])
                a0 = k
                k += 3
                continue
            k += 1
        if (j - a0) * DT >= min_len:
            v = m[a0:j]
            c = 0.5 * (np.percentile(v, 10) + np.percentile(v, 90))
            out.append([T[a0], T[j - 1] + DT, float(c)])
        i = j
    merged = []
    for t0, t1, c in out:
        # same pitch (< 40 c) with no silence in between = one note (a wide vibrato or a slide splits held notes)
        if merged and abs(c - merged[-1][2]) * 100 < merge_c and voiced_between(T, M, merged[-1][1], t0):
            p = merged[-1]
            w0, w1 = p[1] - p[0], t1 - t0
            p[2] = (p[2] * w0 + c * w1) / (w0 + w1)
            p[1] = t1
        else:
            merged.append([t0, t1, c])
    return [tuple(x) for x in merged]


def voiced_between(T, M, a, b):
    i, j = np.searchsorted(T, a), np.searchsorted(T, b)
    return not np.isnan(M[i:j]).any()


def octave_fix(r, far=9.0):
    """Notes more than `far` semitones from the run's median: moved an octave when
    that brings them closer, otherwise marked doubtful (indexes in r["doubt"])."""
    ms = np.array([n[2] for n in r["notes"]])
    mu = float(np.median(ms))
    notes, doubt, fixed = [], [], 0
    for k, (a, b, m) in enumerate(r["notes"]):
        if abs(m - mu) > far:
            c = m - 12 if m > mu else m + 12
            if abs(c - mu) < abs(m - mu):
                m = c
                fixed += 1
            if abs(m - mu) > far:
                doubt.append(k)
        notes.append((a, b, m))
    r.update(notes=notes, doubt=doubt, fixed=fixed)
    return r


def vocal_like(T, M, r, min_jitter=1.0):
    """A sung line wobbles (jitter, vibrato); a keyboard holds a dead-flat pitch.
    Median frame-to-frame movement inside the run's notes, in cents."""
    vals = []
    for a, b, m in r["notes"]:
        i, j = np.searchsorted(T, a + 0.01), np.searchsorted(T, b - 0.01)
        v = M[i:j] * 100
        v = v[~np.isnan(v)]
        if len(v) > 4:
            vals += list(np.abs(np.diff(v)))
    j = float(np.median(vals)) if vals else 0.0
    r["jitter"] = round(j, 2)
    r["music_only"] = j < min_jitter
    return r


def find_runs(notes, min_rate=4.0, min_notes=3, max_gap=0.10, T=None, M=None):
    """Chains of short notes: each note shorter than ~1.6/min_rate s, gaps under
    100 ms, at least min_notes, overall rate (notes / span) ≥ min_rate."""
    max_note = max(0.22, 1.6 / min_rate)
    runs, cur = [], []

    def close():
        if len(cur) >= min_notes:
            span = cur[-1][1] - cur[0][0]
            rate = len(cur) / span
            if rate >= min_rate:
                r = octave_fix({"t0": round(cur[0][0], 3), "t1": round(cur[-1][1], 3), "notes": list(cur),
                                "rate": round(rate, 2), "name": ""})
                runs.append(vocal_like(T, M, r) if T is not None else r)

    for nt in notes:
        short = (nt[1] - nt[0]) <= max_note
        if cur and (not short or nt[0] - cur[-1][1] > max_gap):
            close()
            cur = []
        if short:
            cur.append(nt)
    close()
    return runs


def run_from(notes, t0, t1, name="", T=None, M=None):
    sel = [n for n in notes if n[0] >= t0 - 1e-6 and n[1] <= t1 + 1e-6]
    if not sel:
        return None
    span = max(1e-3, sel[-1][1] - sel[0][0])
    r = octave_fix({"t0": round(t0, 3), "t1": round(t1, 3), "notes": sel, "rate": round(len(sel) / span, 2), "name": name})
    return vocal_like(T, M, r) if T is not None else r


# --------------------------------------------------- maqam / jins guess
GHAMMAZ = {"Bayati": 3, "Rast": 4, "Hijaz": 3, "Saba": 2, "Nahawand": 4, "Kurd": 3, "Ajam": 4, "Sikah": 2}
DRILL_MAQAMAT = list(GHAMMAZ)
AR_LETTER = {"C": "دو", "D": "ري", "E": "مي", "F": "فا", "G": "صول", "A": "لا", "B": "سي"}


def guess_maqam(notes, candidates=None, prefer=None):
    """Best (maqam, tonic midi, mean distance c) for duration-weighted notes.
    Tonic candidates: every note's pitch rounded to 50 c; the final and the
    most-held pitch class get a bonus (tarab phrases resolve on the tonic)."""
    if not notes:
        return None
    candidates = candidates or DRILL_MAQAMAT
    pcs = np.array([(n[2] * 100) % 1200 for n in notes])
    w = np.array([n[1] - n[0] for n in notes])
    w = w / w.sum()
    hist = {}
    if prefer is not None:
        hist.setdefault(round(prefer / 50) * 50 % 1200, 0.0)
    for p, ww in zip(np.round(pcs / 50) * 50 % 1200, w):
        hist[p] = hist.get(p, 0) + ww
    held = [n for n in notes if n[1] - n[0] >= 0.4] or list(notes)
    final = round((held[-1][2] * 100) % 1200 / 50) * 50 % 1200   # last held note: where phrases rest
    best = None
    for tonic in sorted(hist):
        for mq in candidates:
            deg = np.array(E.MAQAMAT[mq]["up"][:7] + ([E.MAQAMAT[mq]["down"][1]] if "down" in E.MAQAMAT[mq] else []))
            rel = (pcs - tonic) % 1200
            dist = np.min(np.abs(((rel[:, None] - deg[None, :]) + 600) % 1200 - 600), 1)
            cost = float(np.sum(w * np.minimum(dist, 100)))
            cost -= 6.0 * hist[tonic] + (8.0 if tonic == final else 0.0)
            if prefer is not None and abs(((tonic - prefer) + 600) % 1200 - 600) < 1:
                cost -= 10.0                      # a run inside a song: keep the song's tonic
            if best is None or cost < best[0]:
                best = (cost, mq, tonic, float(np.sum(w * dist)))
    _, mq, tonic, dist = best
    # tonic as a midi number near the notes' register
    base = min(n[2] for n in notes)
    t = math.floor(base) - 12
    while (t * 100) % 1200 != tonic and t < base + 12:
        t += 0.5
    return {"maqam": mq, "tonic": t, "dist": round(dist, 1)}


def maqam_label(g):
    if not g:
        return "—"
    w, a = names(g["tonic"])
    return "%s على %s" % (E.MAQAMAT[g["maqam"]]["ar"], a)


# ----------------------------------------------------- stretch / transpose
def suggest_transpose(notes, top=E.TOP_LIMIT):
    """Semitones (≤ 0) so the run's highest note sits at or under G4."""
    hi = max(n[2] for n in notes)
    return min(0, int(math.floor(top + 0.15 - hi)))


def stretch(audio, speed=1.0, semis=0.0, sr=E.SR):
    """Pitch-preserving time stretch + transpose via ffmpeg (rubberband, or
    atempo/asetrate when this ffmpeg has no rubberband). audio: (n, 2) float32."""
    if abs(speed - 1) < 1e-3 and abs(semis) < 1e-3:
        return audio
    ff = G.ffmpeg_path()
    if not ff:
        raise IOError("ffmpeg missing")
    p = 2 ** (semis / 12.0)
    if has_rubberband():
        af = "rubberband=tempo=%.4f:pitch=%.6f:transients=smooth:formant=preserved" % (speed, p)
    else:
        chain = []
        t = speed / p if p != 1 else speed
        if p != 1:
            chain += ["asetrate=%d" % int(sr * p), "aresample=%d" % sr]
        while t < 0.5:
            chain.append("atempo=0.5")
            t /= 0.5
        chain.append("atempo=%.4f" % t)
        af = ",".join(chain)
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    r = subprocess.run([ff, "-v", "error", "-f", "f32le", "-ac", "2", "-ar", str(sr), "-i", "-", "-af", af,
                        "-f", "f32le", "-ac", "2", "-ar", str(sr), "-"],
                       input=np.ascontiguousarray(audio, np.float32).tobytes(), stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, **kw)
    if r.returncode != 0:
        raise IOError(r.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(r.stdout, "<f4").reshape(-1, 2).copy()


_RB = None


def has_rubberband():
    global _RB
    if _RB is None:
        ff = G.ffmpeg_path()
        try:
            kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
            out = subprocess.run([ff, "-hide_banner", "-filters"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw).stdout
            _RB = b" rubberband " in out
        except Exception:
            _RB = False
    return _RB


# ------------------------------------------------------------- DTW score
HIT_C, CLOSE_C, LATE_S = 25.0, 50.0, 0.08


def score_sung(ref_notes, sT, sM, slack=0.6):
    """Align the sung line to the reference notes with DTW (free start/end on
    the sung side) and score every note. ref_notes: [(t0, t1, midi)] on the
    same clock as sT. Returns per-note results + summary + the aligned path."""
    dt = 2 * DT
    r0, r1 = ref_notes[0][0], ref_notes[-1][1]
    rt = np.arange(r0, r1, dt)
    rm = np.full(len(rt), np.nan)
    rid = np.full(len(rt), -1)
    for k, (a, b, m) in enumerate(ref_notes):
        sel = (rt >= a) & (rt < b)
        rm[sel], rid[sel] = m, k
    # gaps between notes: hold the next note's pitch (transitions)
    for i in range(len(rt)):
        if rid[i] < 0:
            nxt = [k for k, n in enumerate(ref_notes) if n[0] > rt[i]]
            k = nxt[0] if nxt else len(ref_notes) - 1
            rm[i] = ref_notes[k][2]
    sT, sM = np.asarray(sT), np.asarray(sM, float)
    st = np.arange(r0 - 0.3, r1 + slack, dt)
    sm = np.interp(st, sT, np.nan_to_num(sM, nan=-1000.0)) if len(sT) else np.full(len(st), -1000.0)
    sm = np.where(sm < 0, np.nan, sm)
    R, S = len(rt), len(st)
    C = np.where(np.isnan(sm[None, :]), 1.2, np.minimum(np.abs(rm[:, None] - sm[None, :]), 3.0))
    D = np.full((R, S), np.inf)
    D[0] = C[0]                                  # free start on the sung side
    for i in range(1, R):
        prev = D[i - 1]
        best = np.minimum(prev, np.concatenate(([np.inf], prev[:-1])))   # (i-1, j) or (i-1, j-1)
        row = best + C[i]
        for j in range(1, S):                    # (i, j-1): sung frames held on one ref frame
            v = row[j - 1] + C[i, j] + 0.05
            if v < row[j]:
                row[j] = v
        D[i] = row
    j = int(np.argmin(D[-1]))
    path = [(R - 1, j)]
    i = R - 1
    while i > 0:
        cands = [(D[i - 1, j], i - 1, j)]
        if j > 0:
            cands += [(D[i - 1, j - 1], i - 1, j - 1), (D[i, j - 1] + 1e9 * 0, i, j - 1)]
        _, i, j = min(cands)
        path.append((i, j))
    path.reverse()
    per = {k: [] for k in range(len(ref_notes))}
    first = {}
    for i, j in path:
        k = rid[i]
        if k >= 0:
            per[k].append(sm[j])
            if k not in first and not np.isnan(sm[j]) and abs(sm[j] - rm[i]) < 0.6:
                first[k] = st[j]          # first sung frame that is actually on this note
    res = []
    for k, (a, b, m) in enumerate(ref_notes):
        v = np.array([x for x in per[k] if not np.isnan(x)])
        if len(v) < max(2, 0.3 * len(per[k])):
            res.append({"k": k, "target": m, "err": None, "zone": "missed", "onset": first.get(k)})
            continue
        c = 0.5 * (np.percentile(v, 10) + np.percentile(v, 90))
        e = (c - m) * 100
        zone = "hit" if abs(e) <= HIT_C else ("close" if abs(e) <= CLOSE_C else "missed")
        res.append({"k": k, "target": m, "err": round(float(e), 1), "zone": zone, "onset": first.get(k)})
    # onsets for tempo/lateness: the sung notes themselves (plateaus), matched to
    # the reference in order (edit-distance alignment on pitch)
    sn = fast_notes(np.asarray(sT), np.asarray(sM, float)) if len(sT) else []
    sn = [n for n in sn if r0 - 0.5 <= n[0] <= r1 + slack]
    pairs = match_notes([n[2] for n in ref_notes], [n[2] for n in sn])
    for r in res:
        r["onset"] = None
    for a_, b_ in pairs:
        res[a_]["onset"] = sn[b_][0]
    on = [(ref_notes[r["k"]][0], r["onset"]) for r in res if r["zone"] != "missed" and r["onset"] is not None]
    ratio = None
    if len(on) >= 3 and on[-1][0] > on[0][0]:
        x, y = np.array(on).T                       # robust line through (ref onset, sung onset)
        ratio, icpt = np.polyfit(x, y, 1)
        resid = y - (ratio * x + icpt)
        keep = np.abs(resid) <= max(0.06, 2.5 * np.median(np.abs(resid)))
        if keep.sum() >= 3:
            ratio, icpt = np.polyfit(x[keep], y[keep], 1)
        for r in res:
            if r["zone"] != "missed" and r["onset"] is not None:
                exp = icpt + ratio * ref_notes[r["k"]][0]
                r["late"] = bool(r["onset"] - exp > LATE_S)
    n = len(res)
    hits = sum(r["zone"] == "hit" for r in res)
    close = sum(r["zone"] == "close" for r in res)
    pitch = 100.0 * (hits + 0.5 * close) / n
    tempo = None if ratio is None or ratio <= 0 else max(0.0, 100.0 - 150.0 * abs(math.log(ratio)))
    total = pitch if tempo is None else 0.8 * pitch + 0.2 * tempo
    return {"notes": res, "hits": hits, "close": close, "missed": n - hits - close,
            "late": sum(bool(r.get("late")) for r in res), "tempo_ratio": None if ratio is None else round(ratio, 3),
            "tempo_score": None if tempo is None else round(tempo, 1), "pitch_score": round(pitch, 1),
            "score": round(total, 1), "path": [(float(rt[i]), float(st[j])) for i, j in path[::3]]}


def match_notes(ref, sung, gap=0.8):
    """Order-preserving alignment of two pitch sequences (semitones); returns
    matched index pairs whose pitches are within 0.6 semitone."""
    R, S = len(ref), len(sung)
    D = np.zeros((R + 1, S + 1))
    D[:, 0] = np.arange(R + 1) * gap
    D[0, :] = np.arange(S + 1) * gap
    for i in range(1, R + 1):
        for j in range(1, S + 1):
            D[i, j] = min(D[i - 1, j - 1] + min(abs(ref[i - 1] - sung[j - 1]), 2.0), D[i - 1, j] + gap, D[i, j - 1] + gap)
    i, j, out = R, S, []
    while i > 0 and j > 0:
        if abs(D[i, j] - (D[i - 1, j - 1] + min(abs(ref[i - 1] - sung[j - 1]), 2.0))) < 1e-9:
            if abs(ref[i - 1] - sung[j - 1]) < 0.6:
                out.append((i - 1, j - 1))
            i, j = i - 1, j - 1
        elif abs(D[i, j] - (D[i - 1, j] + gap)) < 1e-9:
            i -= 1
        else:
            j -= 1
    return out[::-1]


# ------------------------------------------------------------------ drills
DRILLS = [("turn3", "لفّة 3 نوتات"), ("turn4", "لفّة 4 نوتات"), ("turn5", "لفّة 5 نوتات"),
          ("scale", "سلّم طالع ونازل"), ("laffa", "اللفّة حوالين القرار والغمّاز")]


def drill_degrees(maqam, kind):
    up = E.MAQAMAT[maqam]["up"]
    g = GHAMMAZ[maqam]
    if kind == "scale":
        return list(range(0, 8)) + list(range(6, -1, -1))
    if kind == "laffa":
        return [0, 1, 0, -1, 0, 1, 2] + [g, g + 1, g, g - 1, g] + list(range(g - 1, -1, -1))
    shape = {"turn3": [0, 1, 0], "turn4": [0, 1, 0, -1], "turn5": [0, 1, 0, -1, 0]}[kind]
    out = []
    for i in range(0, g + 1):
        out += [i + s for s in shape]
    return out + [0]


def degree_cents(maqam, i):
    up = E.MAQAMAT[maqam]["up"]
    if i < 0:
        return up[7 + i] - 1200 if i >= -7 else up[0] - 1200
    if i > 7:
        return up[i - 7] + 1200
    if "down" in E.MAQAMAT[maqam] and i == 6:
        return up[6]
    return up[i]


def drill_notes(maqam, kind, start, nps):
    """[(t0, t1, midi)] at nps notes per second; last note held. Raises if above G4."""
    tonic = E.tonic_of(maqam, start)
    cs = [degree_cents(maqam, i) for i in drill_degrees(maqam, kind)]
    E.check_top(tonic, max(cs), E.TOP_LIMIT, "drill")
    d = 1.0 / nps
    out, t = [], 0.0
    for k, c in enumerate(cs):
        L = d * (3 if k == len(cs) - 1 else 1)
        out.append((t, t + L, tonic + c / 100.0))
        t += L
    return out


def synth_run(notes, glide=0.025, vib=0.0, seed=0, sr=E.SR, offset=None):
    """Test helper: sung run with short glides between notes."""
    end = notes[-1][1] + 0.3
    tr = np.full(int(end * sr), np.nan)
    for k, (a, b, m) in enumerate(notes):
        i0, i1 = int(a * sr), int(b * sr)
        vals = np.full(i1 - i0, m + (offset[k] / 100.0 if offset else 0.0))
        if k and abs(notes[k - 1][1] - a) < 1e-6:
            g = min(int(glide * sr), len(vals) // 2)
            prev = notes[k - 1][2] + (offset[k - 1] / 100.0 if offset else 0.0)
            vals[:g] = np.linspace(prev, vals[g] if g < len(vals) else vals[-1], g)
        tr[i0:i1] = vals
    return E.synth_voice(tr, vibrato_cents=vib, seed=seed, noise=0.004)
