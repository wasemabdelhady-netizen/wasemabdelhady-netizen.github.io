"""MixChecker engine: loudness (ITU-R BS.1770 / EBU R128), spectrum, stereo and
dynamics for a mix and its references, plus a Matchering quick master.

    python engine.py mix.wav ref1.wav [ref2.wav ref3.wav]
"""
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))  # resampy shim (see vendor/resampy)

import numpy as np
from scipy.signal import butter, lfilter, resample_poly, sosfilt, welch

SR = 48000
ROOT = os.path.dirname(HERE)
OUTPUT_DIR = os.path.join(ROOT, "output")
IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0

THIRD_OCT = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
             1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000]
BANDS = [  # key, Arabic name, lo, hi
    ("sub", "Sub", 20, 60),
    ("low", "Low", 60, 250),
    ("lowmid", "Low-mid", 250, 800),
    ("mid", "Mid", 800, 2500),
    ("highmid", "High-mid", 2500, 6000),
    ("presence", "Presence", 6000, 10000),
    ("air", "Air", 10000, 20000),
]
PLATFORMS = [  # name, target LUFS, official?, turns quiet tracks up?
    ("Spotify", -14.0, True, "up_to_peak"),
    ("YouTube", -14.0, True, "down_only"),
    ("Apple Music", -16.0, True, "both"),
    ("Anghami", -14.0, False, "unknown"),
]


# ----------------------------------------------------------------------------- io

def ffmpeg_bin():
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    local = os.path.join(ROOT, "ffmpeg", exe)
    return local if os.path.isfile(local) else exe


def run_ffmpeg(args):
    return subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin"] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)


def decode(path, sr=SR):
    """Stereo float32 (n, 2). Mono files are duplicated to both channels."""
    r = run_ffmpeg(["-i", path, "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("مش قادر أقرا الملف:\n%s\n\n%s" % (path, r.stderr.decode("utf-8", "replace")[-500:]))
    x = np.frombuffer(r.stdout, dtype="<f4").reshape(-1, 2).astype(np.float64)
    if len(x) < sr * 5:
        raise RuntimeError("الملف قصير قوي (أقل من 5 ثواني):\n" + path)
    return x


def db(x, floor=1e-12):
    return 10 * np.log10(np.maximum(x, floor))


# ----------------------------------------------------------------------------- loudness

def _biquad_shelf(G, Q, fc, fs):
    A = 10 ** (G / 40.0)
    w0 = 2 * math.pi * fc / fs
    cw, alpha = math.cos(w0), math.sin(w0) / (2 * Q)
    sa = 2 * math.sqrt(A) * alpha
    b = [A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw), A * ((A + 1) + (A - 1) * cw - sa)]
    a = [(A + 1) - (A - 1) * cw + sa, 2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa]
    return np.array(b) / a[0], np.array(a) / a[0]


def _biquad_hp(Q, fc, fs):
    w0 = 2 * math.pi * fc / fs
    cw, alpha = math.cos(w0), math.sin(w0) / (2 * Q)
    b = [(1 + cw) / 2, -(1 + cw), (1 + cw) / 2]
    a = [1 + alpha, -2 * cw, 1 - alpha]
    return np.array(b) / a[0], np.array(a) / a[0]


def k_weight(x, fs=SR):
    b1, a1 = _biquad_shelf(4.0, 1 / math.sqrt(2), 1500.0, fs)
    b2, a2 = _biquad_hp(0.5, 38.0, fs)
    return lfilter(b2, a2, lfilter(b1, a1, x, axis=0), axis=0)


def _block_power(kw, win, step):
    """Mean square per block, summed over channels (L, R weights = 1)."""
    p = (kw ** 2).sum(axis=1)
    c = np.concatenate([[0.0], np.cumsum(p)])
    starts = np.arange(0, len(p) - win + 1, step)
    return (c[starts + win] - c[starts]) / win, starts


def loudness(x, fs=SR):
    kw = k_weight(x, fs)
    # integrated: 400 ms blocks, 75 % overlap, absolute -70 / relative -10 gates
    pw, _ = _block_power(kw, int(0.4 * fs), int(0.1 * fs))
    lk = -0.691 + db(pw)
    g = pw[lk > -70]
    if not len(g):
        integrated = -70.0
    else:
        rel = -0.691 + db(g.mean()) - 10
        g2 = g[(-0.691 + db(g)) > rel]
        integrated = float(-0.691 + db(g2.mean())) if len(g2) else -70.0
    # short-term: 3 s windows every 100 ms
    ps, st_starts = _block_power(kw, int(3 * fs), int(0.1 * fs))
    st = -0.691 + db(ps)
    st_max = float(st.max()) if len(st) else integrated
    # LRA (EBU Tech 3342): short-term, -70 abs and -20 relative gates, 10th..95th percentile
    s = st[st > -70]
    lra = 0.0
    if len(s):
        rel = -0.691 + db((10 ** ((s + 0.691) / 10)).mean()) - 20
        s = s[s > rel]
        if len(s) > 1:
            lra = float(np.percentile(s, 95) - np.percentile(s, 10))
    # loudness graph: short-term every 1 s
    step = 10
    curve_t = (st_starts[::step] + 1.5 * fs) / fs
    curve = np.maximum(st[::step], -60)
    return integrated, st_max, lra, curve_t, curve


# ITU-R BS.1770-4 Annex 2: 4x oversampling, 48-tap polyphase interpolation filter
_TP_PHASES = np.array([
    [0.0017089843750, 0.0109863281250, -0.0196533203125, 0.0332031250000, -0.0594482421875, 0.1373291015625,
     0.9721679687500, -0.1022949218750, 0.0476074218750, -0.0266113281250, 0.0148925781250, -0.0083007812500],
    [-0.0291748046875, 0.0292968750000, -0.0517578125000, 0.0891113281250, -0.1665039062500, 0.4650878906250,
     0.7797851562500, -0.2003173828125, 0.1015625000000, -0.0582275390625, 0.0330810546875, -0.0189208984375],
    [-0.0189208984375, 0.0330810546875, -0.0582275390625, 0.1015625000000, -0.2003173828125, 0.7797851562500,
     0.4650878906250, -0.1665039062500, 0.0891113281250, -0.0517578125000, 0.0292968750000, -0.0291748046875],
    [-0.0083007812500, 0.0148925781250, -0.0266113281250, 0.0476074218750, -0.1022949218750, 0.9721679687500,
     0.1373291015625, -0.0594482421875, 0.0332031250000, -0.0196533203125, 0.0109863281250, 0.0017089843750],
])


def true_peak(x, fs=SR):
    """True peak in dBTP. The BS.1770 filter is specified for 48 kHz input; other rates are resampled first."""
    if fs != 48000:
        x = resample_poly(x, 48000, fs, axis=0)
    peak = float(np.abs(x).max())
    for h in _TP_PHASES:
        peak = max(peak, float(np.abs(lfilter(h, [1.0], x, axis=0)).max()))
    return 20 * math.log10(max(peak, 1e-9))


# ----------------------------------------------------------------------------- spectrum & stereo

def band_edges(fc):
    return fc / 2 ** (1 / 6.0), fc * 2 ** (1 / 6.0)


def psd(sig, fs=SR):
    f, p = welch(sig, fs=fs, nperseg=8192, noverlap=4096, window="hann", scaling="spectrum")
    return f, p


def band_power(f, p, lo, hi):
    sel = (f >= lo) & (f < hi)
    return float(p[sel].sum()) if sel.any() else 1e-20


class Track:
    def __init__(self, path, label, progress=None):
        self.path, self.label = path, label
        self.name = os.path.splitext(os.path.basename(path))[0]
        say = progress or (lambda m: None)
        say("بقرا «%s»…" % self.name)
        x = decode(path)
        self.duration = len(x) / SR
        say("بقيس العلو في «%s»…" % self.name)
        self.lufs, self.st_max, self.lra, self.curve_t, self.curve = loudness(x)
        self.tp = true_peak(x)
        self.sample_peak = 20 * math.log10(max(np.abs(x).max(), 1e-9))
        rms = math.sqrt(float((x ** 2).mean()))
        self.rms = 20 * math.log10(max(rms, 1e-9))
        self.crest = self.sample_peak - self.rms
        self.plr = self.tp - self.lufs
        say("بحلل الطيف والستيريو في «%s»…" % self.name)
        L, R = x[:, 0], x[:, 1]
        M, S = (L + R) / 2, (L - R) / 2
        f, pm = psd(M)
        _, ps = psd(S)
        self.f = f
        total = pm.sum()
        # 1/3-octave curve as share of the total energy (dB) — same basis as the band table,
        # so tracks of different loudness compare on tonal balance only
        self.third = np.array([db(band_power(f, pm, *band_edges(fc)) / total) for fc in THIRD_OCT])
        self.bands = {k: float(db(band_power(f, pm, lo, hi) / total)) for k, _, lo, hi in BANDS}
        self.side_mid = {k: float(db(band_power(f, ps, lo, hi)) - db(band_power(f, pm, lo, hi)))
                         for k, _, lo, hi in BANDS}
        den = math.sqrt(float((L ** 2).sum() * (R ** 2).sum())) or 1.0
        self.corr = float((L * R).sum() / den)
        sos = butter(4, 120, "lowpass", fs=SR, output="sos")
        lo_l, lo_r = sosfilt(sos, L), sosfilt(sos, R)
        den = math.sqrt(float((lo_l ** 2).sum() * (lo_r ** 2).sum())) or 1.0
        self.low_corr = float((lo_l * lo_r).sum() / den)
        lm, ls = (lo_l + lo_r) / 2, (lo_l - lo_r) / 2
        self.low_side_mid = float(db((ls ** 2).sum()) - db((lm ** 2).sum()))
        self.is_mono = float(np.abs(S).max()) < 1e-4


# ----------------------------------------------------------------------------- comparison

ADVICE = {
    "sub": ("Sub زيادة — ممكن يبوّش على السماعات الكويسة ويسرق headroom فالماستر ميعلاش. "
            "جرّب high-pass على كل حاجة مش محتاجة سب، أو قلّل الـ 808/الكيك تحت 50Hz.",
            "Sub ناقص — الأغنية هتبان خفيفة على سماعات كويسة وفي العربيات. "
            "جرّب تعلّي الكيك أو الـ 808 حوالي 40–60Hz أو ضيف sub layer للباص."),
    "low": ("Low زيادة — تقل وزحمة بين الكيك والباص. جرّب تقلل الباص حوالي 100–200Hz "
            "أو sidechain خفيف من الكيك على الباص.",
            "Low ناقص — المكس رفيع ومن غير جسم. جرّب boost خفيف حوالي 80–150Hz على الباص أو الكيك."),
    "lowmid": ("Low-mid زيادة — عكارة (mud) وصوت مكتوم. جرّب cut هادي 2–3 dB حوالي 250–500Hz "
               "على الجيتار/البادز/البيانو/الفوكال — مش على الماستر كله.",
               "Low-mid ناقص — ناقص دفا وجسم، والفوكال ممكن يبان رفيع. جرّب boost خفيف حوالي 300–500Hz."),
    "mid": ("Mid زيادة — صوت أنفي/كرتوني (honky) ويتعب الودن. دوّر بـ cut ضيق حوالي 800Hz–1.5kHz.",
            "Mid ناقص — المكس مجوّف (scooped) والفوكال والآلات اللحنية ممكن يختفوا على الموبايل. "
            "جرّب boost خفيف حوالي 1–2kHz."),
    "highmid": ("High-mid زيادة — حدة وخشونة بتتعب في الصوت العالي. جرّب de-esser أو cut هادي حوالي "
                "3–5kHz على الفوكال/الجيتار/الهاي هات.",
                "High-mid ناقص — ناقص وضوح وحضور، والكلام مش هيبان. جرّب boost هادي حوالي 3–4kHz على الفوكال."),
    "presence": ("Presence زيادة — سيبلنس (س/ش) ولمعة زيادة. de-esser على الفوكال، أو cut للهاي هات "
                 "والسنير حوالي 6–9kHz.",
                 "Presence ناقص — المكس مكتوم شوية. جرّب shelf خفيف من 6–8kHz على الفوكال أو الماستر."),
    "air": ("Air زيادة — ممكن يبقى حاد أو يبان فيه hiss. قلّل الـ high shelf فوق 10kHz.",
            "Air ناقص — ناقص هوا ولمعة. جرّب high shelf خفيف 1–2 dB من 10–12kHz على الماستر أو الفوكال."),
}
BAND_NAMES = {k: n for k, n, _, _ in BANDS}
BAND_RANGE = {k: (lo, hi) for k, _, lo, hi in BANDS}


def fmt_hz(f):
    return ("%gk" % (f / 1000.0)) if f >= 1000 else ("%g" % f)


def compare(mine, refs, threshold=1.5):
    """Differences vs the average of the references."""
    out = {"refs": refs}
    if not refs:
        return out
    ref_third = np.mean([r.third for r in refs], axis=0)
    out["ref_third"] = ref_third
    out["diff_third"] = mine.third - ref_third
    items = []
    for k, _, lo, hi in BANDS:
        ref_v = float(np.mean([r.bands[k] for r in refs]))
        d = mine.bands[k] - ref_v
        # where inside the band the difference is largest (same sign)
        idx = [i for i, fc in enumerate(THIRD_OCT) if lo <= fc <= hi] or [0]
        dd = out["diff_third"][idx]
        j = idx[int(np.argmax(dd if d > 0 else -dd))]
        # neighbouring 1/3-octave centres, kept inside the band
        a = max(THIRD_OCT[max(j - 1, 0)], lo if lo > 20 else 20)
        b = min(THIRD_OCT[min(j + 1, len(THIRD_OCT) - 1)], hi)
        items.append({"band": k, "name": BAND_NAMES[k], "lo": lo, "hi": hi, "mine": mine.bands[k], "ref": ref_v,
                      "diff": d, "peak_hz": THIRD_OCT[j], "around": "%s–%s Hz" % (fmt_hz(a), fmt_hz(b)),
                      "advice": ADVICE[k][0 if d > 0 else 1], "big": abs(d) >= threshold})
    out["bands"] = items
    out["top"] = sorted([i for i in items if i["big"]], key=lambda i: -abs(i["diff"]))
    out["ref_lufs"] = float(np.mean([r.lufs for r in refs]))
    out["ref_plr"] = float(np.mean([r.plr for r in refs]))
    out["ref_lra"] = float(np.mean([r.lra for r in refs]))
    out["ref_side_mid"] = {k: float(np.mean([r.side_mid[k] for r in refs])) for k, _, _, _ in BANDS}
    return out


def stereo_notes(mine, cmp):
    notes = []
    if mine.is_mono:
        notes.append(("info", "التراك مونو بالكامل — مفيش ستيريو خالص. لو ده مقصود تمام."))
        return notes
    if mine.low_corr < 0.8 or mine.low_side_mid > -12:
        notes.append(("bad", "تحت 120Hz فيه ستيريو كتير (correlation %.2f، side/mid %.1f dB). الباص والكيك "
                             "ممكن يضعفوا على الموبايل والسماعة الواحدة، ويعملوا مشاكل في الماستر. "
                             "خلّي اللي تحت 120Hz مونو (Utility / Bass Mono / mid-side EQ)."
                      % (mine.low_corr, mine.low_side_mid)))
    else:
        notes.append(("ok", "الـ low end مونو كويس تحت 120Hz (correlation %.2f)." % mine.low_corr))
    if mine.corr < 0:
        notes.append(("bad", "الـ correlation سالب (%.2f) — فيه مشكلة phase: المكس هيضعف أو يختفي حاجات لما يتسمع مونو. "
                             "راجع الـ stereo wideners والمايكات المقلوبة." % mine.corr))
    elif mine.corr < 0.3:
        notes.append(("warn", "الـ correlation واطي (%.2f) — المكس عريض قوي. اسمعه مونو واتأكد إن مفيش حاجة بتختفي."
                      % mine.corr))
    if cmp.get("ref_side_mid"):
        for k, n, lo, hi in BANDS:
            d = mine.side_mid[k] - cmp["ref_side_mid"][k]
            if k in ("sub", "low"):
                continue
            if d > 4:
                notes.append(("warn", "%s (%s–%s Hz) أعرض من الريفرنس بـ %.1f dB." % (n, fmt_hz(lo), fmt_hz(hi), d)))
            elif d < -4:
                notes.append(("info", "%s (%s–%s Hz) أضيق من الريفرنس بـ %.1f dB — ممكن يحتاج شوية عرض."
                              % (n, fmt_hz(lo), fmt_hz(hi), -d)))
    return notes


def loudness_notes(mine, cmp):
    rows = []
    for name, target, official, mode in PLATFORMS:
        gain = target - mine.lufs
        if gain < -0.5:
            what = "هيوطّيها %.1f dB — العلو الزيادة مش هيكسّبك حاجة، بس الديناميك اللي ضاع مش هيرجع." % -gain
        elif gain > 0.5:
            what = {"up_to_peak": "ممكن يعلّيها لحد %.1f dB بس لو الـ true peak يسمح (لحد -1 dBTP)." % gain,
                    "down_only": "مش بيعلّي الهادي — هتبان أوطى من غيرها بـ %.1f dB." % gain,
                    "both": "Sound Check ممكن يعلّيها حوالي %.1f dB." % gain,
                    "unknown": "أوطى من الهدف بـ %.1f dB." % gain}[mode]
        else:
            what = "مظبوط على الهدف."
        rows.append({"name": name, "target": target, "official": official, "gain": gain, "what": what})
    warn = []
    if mine.tp > -1.0:
        warn.append("الـ true peak = %.1f dBTP (فوق -1). ممكن يحصل clipping بعد ما المنصات تحوّله لـ AAC/MP3/Opus. "
                    "خلّي الـ ceiling بتاع الليميتر -1 dBTP (أو -2 لو الماستر عالي جداً)." % mine.tp)
    return rows, warn


def analyze(mix, refs, progress=None):
    say = progress or (lambda m: None)
    mine = Track(mix, "mine", say)
    rs = [Track(p, "ref%d" % (i + 1), say) for i, p in enumerate(refs) if p]
    cmp = compare(mine, rs)
    cmp["stereo"] = stereo_notes(mine, cmp)
    cmp["loud_rows"], cmp["tp_warn"] = loudness_notes(mine, cmp)
    return mine, rs, cmp


# ----------------------------------------------------------------------------- quick master

def quick_master(mix, ref, out_dir=None, log=None):
    """Reference-matched master with Matchering. Returns (wav, mp3)."""
    import matchering as mg  # imported lazily: heavy (scipy + statsmodels)
    say = log or (lambda m: None)
    out_dir = out_dir or os.path.dirname(os.path.abspath(mix))
    base = os.path.splitext(os.path.basename(mix))[0]
    rname = os.path.splitext(os.path.basename(ref))[0]
    name = "%s - QUICK MASTER (demo-reels) - ref %s" % (base, rname)
    name = re.sub(r'[\\/:*?"<>|]+', " ", name)[:150]
    wav = os.path.join(out_dir, name + ".wav")
    mp3 = os.path.join(out_dir, name + ".mp3")
    tmp = tempfile.mkdtemp(prefix="mixchecker_")
    try:
        t, r, o = (os.path.join(tmp, n) for n in ("target.wav", "reference.wav", "master.wav"))
        for src, dst in ((mix, t), (ref, r)):  # 44.1 kHz float WAV: what Matchering works in
            res = run_ffmpeg(["-y", "-i", src, "-vn", "-ac", "2", "-ar", "44100", "-c:a", "pcm_f32le", dst])
            if res.returncode != 0:
                raise RuntimeError(res.stderr.decode("utf-8", "replace")[-500:])
        say("Matchering شغال… (ممكن ياخد دقيقة)")
        mg.log(info_handler=lambda m: say("  " + m), warning_handler=lambda m: say("  ⚠ " + m))
        mg.process(target=t, reference=r, results=[mg.Result(o, subtype="FLOAT", use_limiter=True, normalize=True)],
                   config=mg.Config(temp_folder=tmp))
        say("بصدّر WAV 24-bit 48k و MP3 320k…")
        # keep the true peak at or under -1 dBTP after resampling to 48 kHz
        tp = true_peak(decode(o))
        trim = min(0.0, -1.1 - tp)
        if trim < 0:
            say("  الـ true peak كان %.2f dBTP — بنزّله %.2f dB عشان يبقى تحت -1" % (tp, -trim))
        meta = ["-metadata", "comment=QUICK MASTER for demos/reels - MixChecker + Matchering"]
        for path, codec in ((wav, ["-c:a", "pcm_s24le"]), (mp3, ["-c:a", "libmp3lame", "-b:a", "320k"])):
            res = run_ffmpeg(["-y", "-i", o, "-af", "volume=%.3fdB,aresample=48000:resampler=soxr:precision=28" % trim,
                              "-ar", "48000"] + meta + codec + [path])
            if res.returncode != 0:
                res = run_ffmpeg(["-y", "-i", o, "-af", "volume=%.3fdB" % trim, "-ar", "48000"] + meta + codec + [path])
            if res.returncode != 0:
                raise RuntimeError(res.stderr.decode("utf-8", "replace")[-500:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return wav, mp3


def report_path(mix):
    base = os.path.splitext(os.path.basename(mix))[0]
    return os.path.join(os.path.dirname(os.path.abspath(mix)), base + " - MixChecker.html")


def main(argv=None):
    a = argv or sys.argv[1:]
    mine, refs, cmp = analyze(a[0], a[1:], progress=print)
    for t in [mine] + refs:
        print("%-30s I %6.1f  STmax %6.1f  LRA %4.1f  TP %5.1f  PLR %4.1f  crest %4.1f  corr %.2f  low<120 %.2f"
              % (t.name[:30], t.lufs, t.st_max, t.lra, t.tp, t.plr, t.crest, t.corr, t.low_corr))
    for i in cmp.get("top", []):
        print("  %-9s %+5.1f dB  %s" % (i["name"], i["diff"], i["around"]))
    import report
    print(report.write(mine, refs, cmp, report_path(a[0])))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
