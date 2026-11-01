"""Scoring after each step: per-note pitch (stable part), onset (first 200 ms),
sustain stability (drift, end-of-breath drop, vibrato) and latency calibration.

All times here are on the target clock: sung frame time minus the latency
compensation, then a per-step alignment search of ±150 ms on top."""
import numpy as np

import engine as E

ALIGN = np.arange(-0.15, 0.1501, 0.01)
ONSET_WIN = 0.20
LAND_C, LAND_HOLD = 20.0, 0.05     # landed = within ±20c held for 50 ms


def mid_window(seg):
    d = seg.t1 - seg.t0
    a = min(E.ATTACK, 0.3 * d) if d < 0.7 else max(E.ATTACK, 0.15 * d)
    return seg.t0 + a, seg.t1 - min(0.1, 0.1 * d)


def scoreable(seg):
    return seg.scored and not seg.glide and (seg.t1 - seg.t0) >= 0.15 and seg.m0 <= E.TOP_LIMIT + 1e-9


def note_median(seg, ta, m, base, delta):
    a, b = mid_window(seg)
    sel = (ta - delta >= base + a) & (ta - delta < base + b)
    tot = int(sel.sum())
    v = m[sel]
    v = v[~np.isnan(v)]
    if tot == 0 or len(v) < max(3, 0.4 * tot):
        return None, len(v), tot
    # centre of the note: midpoint of the 10th/90th percentiles, so a vibrato
    # cut at any phase still reads as its centre (a median would not)
    p10, p90 = np.percentile(v, 10), np.percentile(v, 90)
    return float(0.5 * (p10 + p90) - seg.m0) * 100.0, len(v), float(p90 - p10) * 50.0


def align(st, ta, m):
    """Best per-step shift (s) for timing, not for pitch: the shift that keeps
    every note window clean of its neighbours (least spread inside the windows),
    so a note sung off-pitch is never 'fixed' by sliding into the next one."""
    segs = [s for s in st.segs if scoreable(s)]
    if not segs or not len(ta):
        return 0.0
    best = (1e18, 0.0)
    for d in ALIGN:
        cost = 0.0
        for s in segs:
            a, b = mid_window(s)
            sel = (ta - d >= st.sing0 + a) & (ta - d < st.sing0 + b)
            v = m[sel]
            v = v[~np.isnan(v)]
            if sel.sum() == 0 or len(v) < max(3, 0.4 * sel.sum()):
                cost += 300.0
            else:
                cost += min(300.0, float(np.mean(np.abs(v - np.median(v)))) * 100.0)
        cost += abs(d) * 40  # prefer the calibrated timing on ties
        if cost < best[0]:
            best = (cost, float(d))
    return best[1]


def onset(seg, prev, ta, m, base, delta, settle=0.0, wobble=0.0):
    """First 200 ms of a note: where it started and how long until it landed on
    the pitch you then held (settle = that note's error, so a slide is told
    apart from singing the whole note sharp or flat)."""
    t0 = base + seg.t0 + delta
    lo, hi = t0 - 0.15, t0 + 0.5
    idx = np.nonzero((ta >= lo) & (ta <= hi) & ~np.isnan(m))[0]
    if not len(idx):
        return None
    start = None
    for i in idx:
        if prev is None or prev.glide:
            if i + 2 < len(m) and not np.isnan(m[i + 2]):
                start = i
                break
        elif abs(m[i] - seg.m0) < abs(m[i] - prev.m0):
            start = i
            break
    if start is None:
        return None
    t_on = ta[start]
    w = np.nonzero((ta >= t_on) & (ta < t_on + ONSET_WIN))[0]
    e = (m[w] - seg.m0) * 100.0 - settle
    ok = ~np.isnan(e)
    if ok.sum() < 5:
        return None
    tw, e = ta[w][ok], e[ok]
    hold = max(3, int(LAND_HOLD / (E.HOP / E.SR)))
    land_c = max(LAND_C, wobble + 5.0)       # a wide vibrato is not a slide
    land = None
    for i in range(len(e) - hold + 1):
        if np.all(np.abs(e[i:i + hold]) <= land_c):
            land = i
            break
    slide_ms = None if land is None else (tw[land] - t_on) * 1000.0
    return {"t_on": round(float(t_on), 3), "start_c": round(float(np.median(e[:3])), 1),
            "slide_ms": None if slide_ms is None else round(slide_ms, 0),
            "landed": slide_ms is not None and slide_ms <= 60.0, "land_c": round(land_c, 1),
            "first_err": round(float(np.median(e)) + settle, 1)}


def moving(x, k):
    k = max(1, int(k))
    c = np.convolve(x, np.ones(k) / k, mode="same")
    n = np.convolve(np.ones_like(x), np.ones(k) / k, mode="same")
    return c / n


def stability(seg, ta, m, base, delta):
    """Long note: drift over the note, the drop at the end of the breath, vibrato."""
    a, b = base + seg.t0 + delta, base + seg.t1 + delta + 0.2
    sel = (ta >= a) & (ta <= b) & ~np.isnan(m)
    if sel.sum() < 200:
        return None
    t = ta[sel]
    c = (m[sel] - seg.m0) * 100.0
    t_on, t_off = t[0] + 0.4, t[-1]
    keep = t >= t_on
    t, c = t[keep], c[keep]
    if len(t) < 150 or t_off - t_on < 2.5:
        return None
    # resample to a regular 172 Hz grid (gaps interpolated)
    dt = E.HOP / E.SR
    g = np.arange(t[0], t[-1], dt)
    cg = np.interp(g, t, c)
    k = int(round(0.36 / dt))                         # ~2 vibrato periods
    smooth = moving(cg, k)
    resid = cg - smooth
    core = slice(k, len(cg) - k) if len(cg) > 3 * k else slice(0, len(cg))
    r = resid[core]
    r = r - r.mean()
    spec = np.abs(np.fft.rfft(r * np.hanning(len(r)), 8 * len(r)))
    fr = np.fft.rfftfreq(8 * len(r), dt)
    band = (fr >= 3.0) & (fr <= 9.0)
    rate = float(fr[band][np.argmax(spec[band])]) if band.any() else None
    width = float(np.sqrt(2) * np.std(r))
    has_vib = width >= 8.0 and rate is not None and spec[band].max() > 3 * np.median(spec[(fr > 0.5) & (fr < 20)])
    tt = g - g[0]
    first = smooth[tt <= 1.0]
    last = smooth[tt >= tt[-1] - 0.4]           # where the breath ends
    middle = smooth[(tt > 1.0) & (tt < tt[-1] - 1.5)]
    slope = float(np.polyfit(tt[(tt > 0.3) & (tt < tt[-1] - 1.5)], smooth[(tt > 0.3) & (tt < tt[-1] - 1.5)], 1)[0]) \
        if middle.size > 20 else 0.0
    return {"mean_c": round(float(np.median(smooth)), 1),
            "drift_c": round(float(np.median(last) - np.median(first)), 1),
            "end_drop_c": round(float(np.median(last) - (np.median(middle) if middle.size else np.median(first))), 1),
            "slope_c_s": round(slope, 1),
            "vib_rate": round(rate, 2) if has_vib else None,
            "vib_width": round(width, 1) if has_vib else 0.0,
            "held_s": round(float(t_off - (t_on - 0.4)), 1)}


def score_step(st, ta, m, mode):
    """ta (target-clock times, latency removed) and m (midi or NaN) around the step."""
    delta = align(st, ta, m)
    out = []
    prev = None
    for k, s in enumerate(st.segs):
        if not scoreable(s):
            if s.scored or s.label != "orn":
                prev = s
            continue
        e, nv, wob = note_median(s, ta, m, st.sing0, delta)
        r = {"step": st.index, "k": k, "t": round(st.sing0 + s.t0, 2), "song_t": round(st.song_t0 + s.t0, 2),
             "target": round(s.m0, 3), "label": E.note_label(s.m0), "deg": s.label, "err": None if e is None else round(e, 1),
             "missed": e is None, "delta_ms": round(delta * 1000)}
        r["exact"] = e is not None and abs(e) <= E.GREEN
        r["close"] = e is not None and abs(e) <= E.YELLOW
        if mode in ("onset", "leaps", "callresp", "ajnas", "basic", "maqam", "drone", "song", "stability"):
            after_orn = k > 0 and st.segs[k - 1].label == "orn"   # the ornament IS the approach
            p = None if (prev is None or (s.t0 - prev.t1) > 0.05) else prev
            o = None if after_orn else onset(s, p, ta, m, st.sing0, delta, settle=e or 0.0,
                                               wobble=wob if e is not None else 0.0)
            if o:
                r["onset"] = o
        if mode == "leaps" and k == len(st.segs) - 1:
            o = r.get("onset")
            r["leap"] = st.label
            r["leap_ok"] = bool(o and abs(o["first_err"]) <= 25.0)
        if (s.t1 - s.t0) >= 4.0:
            sb = stability(s, ta, m, st.sing0, delta)
            if sb:
                r["stab"] = sb
        out.append(r)
        prev = s
    return out


def calibrate(t_raw, m, clicks):
    """Latency from singing short notes with the plucks: median of
    (sung onset - pluck time), plucks 3.. only. Returns (lat_s, spread_s, n)."""
    t_raw = np.asarray(t_raw)
    v = ~np.isnan(np.asarray(m, float))
    starts = []
    gap = 0
    for i in range(len(v)):
        if not v[i]:
            gap += 1
            continue
        if gap >= 15 and i + 4 < len(v) and v[i:i + 5].all():   # ≥ 90 ms of silence before
            starts.append(t_raw[i])
        gap = 0
    offs = []
    for c in clicks[2:]:
        cand = [s - c for s in starts if -0.05 <= s - c <= 0.45]
        if cand:
            offs.append(min(cand, key=abs))
    if len(offs) < 4:
        return None, None, len(offs)
    offs = np.array(offs)
    med = float(np.median(offs))
    return med, float(np.median(np.abs(offs - med))), len(offs)
