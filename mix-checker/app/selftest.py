"""Self-test: synthesises a 'mix' with known problems (low-mid +4 dB around 315 Hz,
stereo bass, true peak above -1 dBTP) and two clean references, then checks
the analysis against ffmpeg's EBU R128 meter, the report and the quick master.

    python selftest.py <work_dir>
"""
import math
import os
import re
import sys
import tempfile
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import numpy as np
from scipy.signal import butter, lfilter, sosfilt

import engine
import report

SR = 48000


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def peaking(x, f0, gain_db, q=1.0):
    A = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * f0 / SR
    al = math.sin(w0) / (2 * q)
    b = [1 + al * A, -2 * math.cos(w0), 1 - al * A]
    a = [1 + al / A, -2 * math.cos(w0), 1 - al / A]
    return lfilter(np.array(b) / a[0], np.array(a) / a[0], x, axis=0)


def music(seed, dur=60.0, wide_bass=False):
    rng = np.random.default_rng(seed)
    n = int(dur * SR)
    t = np.arange(n) / SR
    # pink-ish noise bed, decorrelated L/R above 300 Hz
    w = rng.standard_normal((n, 2))
    pink = lfilter([0.049922035, -0.095993537, 0.050612699, -0.004408786],
                   [1, -2.494956002, 2.017265875, -0.522189400], w, axis=0)
    hp = butter(2, 300, "highpass", fs=SR, output="sos")
    mono = pink[:, :1].repeat(2, 1)
    bed = sosfilt(butter(2, 300, "lowpass", fs=SR, output="sos"), mono, axis=0) + sosfilt(hp, pink, axis=0)
    bed *= 0.6
    # bass line: root notes every bar (2 s)
    roots = [55.0, 43.65, 65.41, 49.0]
    f = np.array([roots[int(tt // 2) % 4] for tt in t[::480]]).repeat(480)[:n]
    ph = 2 * np.pi * np.cumsum(f) / SR
    bass = (np.sin(ph) + 0.35 * np.sin(2 * ph) + 0.15 * np.sin(3 * ph)) * 0.35
    if wide_bass:  # stereo low end: right channel phase-shifted
        bassR = (np.sin(ph + 1.6) + 0.35 * np.sin(2 * ph + 1.6) + 0.15 * np.sin(3 * ph + 1.6)) * 0.35
        bass = np.stack([bass, bassR], 1)
    else:
        bass = np.stack([bass, bass], 1)
    # kick every 0.5 s
    k = np.zeros(n)
    kt = np.arange(int(0.25 * SR)) / SR
    kick = np.sin(2 * np.pi * (48 * kt + 70 * (1 - np.exp(-kt * 35)) / 35)) * np.exp(-kt * 12)
    for s in range(0, n - len(kick), SR // 2):
        k[s:s + len(kick)] += kick
    # 'vocal': centred harmonic tone with vibrato, phrases
    vf = 220 * 2 ** (np.round(4 * np.sin(2 * np.pi * t / 7)) / 12)
    vph = 2 * np.pi * np.cumsum(vf + 4 * np.sin(2 * np.pi * 5.5 * t)) / SR
    gate = (np.sin(2 * np.pi * t / 3.0) > -0.3).astype(float)
    voc = sum(a * np.sin((i + 1) * vph) for i, a in enumerate([1, 0.6, 0.45, 0.3, 0.2, 0.12, 0.08])) * 0.25 * gate
    # verse / chorus dynamics
    env = np.where((t // 15) % 2 == 1, 1.0, 0.5)
    mix = (bed + bass + 0.8 * k[:, None] + voc[:, None]) * env[:, None]
    return mix


def limit(x, target_peak_db):
    x = np.tanh(x * 1.0) if target_peak_db > -3 else x
    return x / np.abs(x).max() * 10 ** (target_peak_db / 20)


def write_wav(path, x, bits=24):
    x = np.clip(x, -1, 1)
    if bits == 24:
        v = (x * (2 ** 23 - 1)).astype("<i4")
        raw = np.frombuffer(v.tobytes(), np.uint8).reshape(-1, 4)[:, :3].tobytes()
        sw = 3
    else:
        raw = (x * 32767).astype("<i2").tobytes()
        sw = 2
    with open(path, "wb") as fh:  # open(): Arabic paths on every OS
        w = wave.open(fh, "wb")
        w.setnchannels(x.shape[1])
        w.setsampwidth(sw)
        w.setframerate(SR)
        w.writeframes(raw)
        w.close()


def ffmpeg_r128(path):
    r = engine.run_ffmpeg(["-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"])
    s = r.stderr.decode("utf-8", "replace")
    s = s[s.rfind("Summary:"):]
    i = float(re.search(r"I:\s+(-?[\d.]+) LUFS", s).group(1))
    lra = float(re.search(r"LRA:\s+(-?[\d.]+) LU", s).group(1))
    tp = float(re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", s).group(1))
    return i, lra, tp


def info(path):
    return engine.run_ffmpeg(["-i", path]).stderr.decode("utf-8", "replace")


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    os.makedirs(d, exist_ok=True)
    mix_p = os.path.join(d, "مكس تجربة.wav")
    ref1 = os.path.join(d, "ريفرنس 1.wav")
    ref2 = os.path.join(d, "ريفرنس ٢.mp3")
    base = music(1, wide_bass=True)
    mine = peaking(base, 315, 4.5, q=0.9)           # muddy low-mids
    write_wav(mix_p, limit(mine * 0.9, -0.2))          # hot sample peak -> TP above -1
    write_wav(ref1, limit(music(2) * 3.0, -1.2))     # louder, clean
    tmpw = os.path.join(d, "_r2.wav")
    write_wav(tmpw, limit(music(3) * 2.5, -1.5))
    r = engine.run_ffmpeg(["-y", "-i", tmpw, "-c:a", "libmp3lame", "-b:a", "320k", ref2])
    ok(r.returncode == 0, "sample mp3 reference")
    os.remove(tmpw)

    m, refs, cmp = engine.analyze(mix_p, [ref1, ref2], progress=lambda s: print("     " + s))
    for t in [m] + refs:
        fi, flra, ftp = ffmpeg_r128(t.path)
        print("     %-12s I %.2f (ffmpeg %.2f)  LRA %.1f (%.1f)  TP %.2f (%.2f)  STmax %.1f PLR %.1f crest %.1f corr %.2f low %.2f"
              % (t.name, t.lufs, fi, t.lra, flra, t.tp, ftp, t.st_max, t.plr, t.crest, t.corr, t.low_corr))
        ok(abs(t.lufs - fi) < 0.3, "%s: integrated LUFS matches ffmpeg ebur128 (%.2f vs %.2f)" % (t.name, t.lufs, fi))
        ok(abs(t.tp - ftp) < 0.4, "%s: true peak matches ffmpeg (%.2f vs %.2f dBTP)" % (t.name, t.tp, ftp))
        ok(abs(t.lra - flra) < 1.5, "%s: LRA close to ffmpeg (%.1f vs %.1f LU)" % (t.name, t.lra, flra))
        ok(t.st_max >= t.lufs, "%s: short-term max ≥ integrated" % t.name)
        ok(abs(t.plr - (t.tp - t.lufs)) < 1e-6 and t.crest > 0, "%s: PLR and crest factor" % t.name)
    for i in cmp["bands"]:
        print("     %-9s mine %6.1f  ref %6.1f  diff %+5.1f  %s" % (i["name"], i["mine"], i["ref"], i["diff"], i["around"]))
    top = cmp["top"][0]
    ok(top["band"] == "lowmid" and top["diff"] > 2, "biggest difference: low-mid %+.1f dB" % top["diff"])
    lo, hi = [float(v.replace("k", "e3")) for v in top["around"].replace(" Hz", "").split("–")]
    ok(lo <= 315 <= hi, "low-mid peak located around %s (boost was at 315 Hz)" % top["around"])
    ok("عكارة" in top["advice"], "low-mid advice: muddiness + cut")
    ok(m.low_corr < 0.8 and any(k == "bad" and "120Hz" in t for k, t in cmp["stereo"]), "stereo bass flagged (corr %.2f)" % m.low_corr)
    ok(all(r.low_corr > 0.95 for r in refs), "references: low end mono")
    ok(m.tp > -1 and cmp["tp_warn"], "true-peak warning (%.2f dBTP)" % m.tp)
    sp = {r["name"]: r for r in cmp["loud_rows"]}
    ok(abs(sp["Spotify"]["gain"] - (-14 - m.lufs)) < 1e-9 and sp["Apple Music"]["target"] == -16, "platform targets")
    ok(len(m.curve) > 40, "loudness-over-time curve (%d points)" % len(m.curve))

    rp = report.write(m, refs, cmp, engine.report_path(mix_p))
    ok(os.path.dirname(rp) == d and rp.endswith("مكس تجربة - MixChecker.html"), "report saved next to the track")
    h = open(rp, encoding="utf-8").read()
    for key in ('id="spectrum"', 'id="diffchart"', 'id="tips"', 'id="platforms"', 'id="tp-warning"',
                'id="low-mono"', 'id="loudchart"', "Spotify", "Anghami", "Apple Music", "عكارة", 'dir="rtl"', "font/woff2"):
        ok(key in h, "report contains " + key)

    # quick master — with an Arabic temp folder to prove the paths hold
    tempfile.tempdir = os.path.join(d, "مؤقت")
    os.makedirs(tempfile.tempdir, exist_ok=True)
    wav, mp3 = engine.quick_master(mix_p, ref1, log=lambda s: print("     " + s))
    tempfile.tempdir = None
    ok(os.path.isfile(wav) and os.path.isfile(mp3), "quick master exported: " + os.path.basename(wav))
    ok("QUICK MASTER (demo-reels)" in wav, "file name labelled QUICK MASTER (demo-reels)")
    iw, im = info(wav), info(mp3)
    ok("pcm_s24le" in iw and "48000 Hz" in iw, "quick master WAV is 24-bit 48 kHz")
    ok("320 kb/s" in im and "48000 Hz" in im, "quick master MP3 is 320k 48 kHz")
    mi, _, mtp = ffmpeg_r128(wav)
    ri = refs[0].lufs
    print("     master I %.2f  TP %.2f  (mix %.2f, ref %.2f)" % (mi, mtp, m.lufs, ri))
    ok(abs(mi - ri) < abs(m.lufs - ri) and abs(mi - ri) < 2.0, "quick master loudness moved to the reference (%.1f → %.1f, ref %.1f)" % (m.lufs, mi, ri))
    ok(mtp <= -0.9, "quick master true peak ≤ -1 dBTP (%.2f)" % mtp)
    mm = engine.Track(wav, "master")
    lm_before = [i for i in cmp["bands"] if i["band"] == "lowmid"][0]["diff"]
    ref_lm = refs[0].bands["lowmid"]
    ok(abs(mm.bands["lowmid"] - ref_lm) < abs(m.bands["lowmid"] - ref_lm), "quick master low-mid closer to the reference (%+.1f → %+.1f dB)"
       % (m.bands["lowmid"] - ref_lm, mm.bands["lowmid"] - ref_lm))
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
