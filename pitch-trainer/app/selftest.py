"""Self-test: pitch detection on synthetic tones (sines, voice-like, weak
fundamental, strong 2nd harmonic, vibrato + noise, quarter tones), latency,
the G4 ceiling of every exercise, maqam targets in cents, scoring, the
reference piano, WAV recording and the history — all in an Arabic folder.

    python selftest.py <work_dir>
"""
import math
import os
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import numpy as np

import engine as E

SR = E.SR


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def track(y, block=512):
    pt = E.PitchTracker()
    fr = []
    for i in range(0, len(y), block):
        fr += pt.feed(y[i:i + block])
    return fr


def errs(fr, midi_fn, skip=0.1):
    out = []
    for t, f, c, d in fr:
        if f and t > skip:
            out.append((E.hz_to_midi(f) - midi_fn(t)) * 100)
    return np.array(out)


def sine(m, dur=1.0, amp=0.3):
    t = np.arange(int(dur * SR)) / SR
    return (amp * np.sin(2 * np.pi * E.midi_to_hz(m) * t)).astype(np.float32)


def run_session(steps, info, sing, **voice):
    end = steps[-1].sing1 + 0.5
    s = E.Session(steps, info.get("mode", "exercise"), info)
    y = E.synth_voice(sing(steps, end), **voice)
    pt = E.PitchTracker()
    for i in range(0, len(y), E.HOP):
        s.process(pt.feed(y[i:i + E.HOP]))
    return s, s.score()


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    os.makedirs(d, exist_ok=True)

    print("— pitch detection on synthetic tones")
    notes = [40, 43, 45, 47.5, 48, 50, 51.5, 52.5, 55, 57, 59.5, 60, 62, 63.5, 64, 65, 66.5, 67]
    worst = 0.0
    for m in notes:
        e = errs(track(sine(m)), lambda t: m)
        worst = max(worst, float(np.abs(e).max()))
        ok(len(e) > 150 and abs(np.median(e)) < 1.0, "sine %-9s median %+.2fc  max %.2fc" % (E.note_label(m), np.median(e), np.abs(e).max()))
    ok(worst < 3.0, "sines E2..G4 incl. quarter tones: every frame within 3 cents (worst %.2f)" % worst)

    for label, kw in (("voice-like harmonics", {}), ("weak fundamental", {"weak_fund": True}),
                      ("strong 2nd harmonic", {"harmonics": (0.35, 1.0, 0.7, 0.4, 0.3, 0.2, 0.1)})):
        bad = 0
        allabs = []
        for m in (40, 45, 48, 52, 55, 59, 62, 64, 67):
            y = E.synth_voice(np.full(SR, float(m)), noise=0.01, seed=m, **kw)
            e = errs(track(y), lambda t: m)
            bad += int(np.sum(np.abs(e) > 600))
            allabs += list(np.abs(e[np.abs(e) < 600]))
        ok(bad == 0 and np.median(allabs) < 2.0 and np.percentile(allabs, 99) < 8,
           "%s + noise (-26 dB): no octave errors, median %.2fc, p99 %.2fc" % (label, np.median(allabs), np.percentile(allabs, 99)))

    # vibrato: compare against the instantaneous pitch (window centre)
    m0, vc, vh = 52.0, 35.0, 5.5
    y = E.synth_voice(np.full(2 * SR, m0), vibrato_cents=vc, vib_hz=vh, noise=0.02, seed=3)
    fr = track(y)
    best = min((float(np.median(np.abs(errs(fr, lambda t: m0 + vc / 100 * math.sin(2 * math.pi * vh * (t - lag)), skip=0.2)))), lag)
               for lag in np.arange(0, 0.0201, 0.0025))
    mm = np.array([E.hz_to_midi(f) for t, f, c, db in fr if f and t > 0.2])
    depth = (np.percentile(mm, 97) - np.percentile(mm, 3)) * 50
    ok(best[0] < 3 and best[1] <= 0.015 and depth > 30,
       "vibrato ±35c @5.5Hz with noise: depth %.0fc, median |err| %.1fc at %.0f ms lag" % (depth, best[0], best[1] * 1000))

    fr = track(np.zeros(SR, np.float32)) + track((np.random.default_rng(1).standard_normal(SR) * 0.002).astype(np.float32))
    ok(sum(1 for f in fr if f[1]) == 0, "silence and quiet noise: no pitch (%d frames)" % len(fr))
    fr = track((np.random.default_rng(2).standard_normal(SR) * 0.1).astype(np.float32))
    v = sum(1 for f in fr if f[1]) / len(fr)
    ok(v < 0.05, "loud white noise: %.1f%% frames voiced" % (100 * v))

    for c, z in ((8, "green"), (-14, "green"), (22, "yellow"), (-30, "yellow"), (45, "red"), (-80, "red")):
        e = errs(track(E.synth_voice(np.full(SR, 57 + c / 100.0), seed=5)), lambda t: 57)
        med = float(np.median(e))
        ok(abs(med - c) < 1.0 and E.zone(med) == z, "A3 %+dc measured %+.1fc -> %s" % (c, med, z))

    # latency: silence then A3 — when does the first good pitch come out?
    on = int(0.5 * SR)
    y = np.concatenate([np.zeros(on, np.float32), E.synth_voice(np.full(SR, 57.0), noise=0, seed=1)])
    pt = E.PitchTracker()
    lat = None
    for i in range(0, len(y), E.HOP):
        for t, f, c, db in pt.feed(y[i:i + E.HOP]):
            if lat is None and f and abs(E.hz_to_midi(f) - 57) * 100 < 20 and pt.n > on:
                lat = (pt.n - on) / SR
    ok(lat is not None and lat <= 0.06, "latency onset -> first pitch within 20c: %.0f ms" % (1000 * lat))
    y = (np.random.default_rng(0).standard_normal(SR * 3) * 0.05).astype(np.float32) + sine(50, 3)
    t0 = time.perf_counter()
    n = len(track(y, E.HOP))
    rate = n / (time.perf_counter() - t0)
    ok(rate > 3 * SR / E.HOP, "speed: %.0f pitches/s (real time needs %.0f)" % (rate, SR / E.HOP))

    print("— exercises never go above G4")
    worst = 0
    count = 0
    for kind in E.EXERCISES:
        for start in range(E.LOW_LIMIT, 72):
            for top in (60, 64, 67, 70, 76):
                for dr in ("up", "down", "updown"):
                    for tempo in E.TEMPI:
                        for st in E.build_plan(kind, start, 40, top, dr, tempo):
                            worst = max(worst, st.top())
                            count += 1
    for mq in E.MAQAMAT:
        for span in ("jins", "full"):
            for start in range(E.LOW_LIMIT, 72):
                for dr in ("up", "down", "updown"):
                    for st in E.build_plan("maqam", start, 40, 76, dr, "normal", mq, span):
                        worst = max(worst, st.top())
                        count += 1
    ok(worst <= 67 + 1e-9, "%d generated steps (all exercises, maqamat, starts C2..B4, top up to E5): highest %s" % (count, E.note_label(worst)))
    ok(E.build_plan("scale5", 66, 40, 76, "up") == [], "start above the ceiling -> no steps (the window explains why)")
    p = E.build_plan("scale5", 48, 45, 67, "up")
    ok([s.root for s in p] == list(range(48, 61)) and p[-1].top() == 67, "5-note scale from C3: roots C3..C4 by semitones, stops at G4")
    p = E.build_plan("arpeggio", 50, 45, 67, "updown")
    ok([s.root for s in p][:3] == [50, 51, 52] and p[len(p) // 2].root == 55 and p[-1].root == 50,
       "arpeggio D3 up and down: turns at G3 (G4 on top), back to D3")
    p = E.build_plan("octave", 48, 45, 67, "down")
    ok([s.root for s in p] == [48, 47, 46, 45], "octave jumps down from C3 to the lowest note A2")
    p = E.build_plan("siren", 48, 45, 67, "up")
    ok(any(g.glide for g in p[0].segs) and p[-1].top() == 67, "siren glides a fifth, last one tops at G4")
    ok(all(st.listen1 <= st.sing0 < st.sing1 for st in p), "every step: reference first, then your turn")

    print("— maqam targets in cents (not forced to 12 tones)")
    expect = {"Bayati": 150, "Saba": 150, "Rast": 350, "Hijaz": 100, "Nahawand": 200}
    for mq, c in expect.items():
        st = E.build_plan("maqam", 50, 45, 67, "up", "normal", mq)[0]
        deg = st.segs[1] if mq != "Rast" else st.segs[2]
        ok(abs((deg.m0 - 50) * 100 - c) < 1e-6, "%s on D3: %s = %s" % (mq, deg.label, E.note_label(deg.m0)))
    ok(E.note_label(50 + 1.5) == "E3 -50c" and E.note_label(52.5) == "F3 -50c", "quarter-tone labels read as half-flats: E3 -50c, F3 -50c")
    full = E.maqam_pattern("Nahawand", "full")
    ok(1100 in full and 1000 in full, "Nahawand full: B natural going up, Bb coming down")

    print("— scoring with a synthetic singer")
    p = E.build_plan("scale5", 55, 45, 67, "up", "fast")
    s, sc = run_session(p, {}, lambda st, end: E.singer_track(st, end, offset_cents=10), seed=1)
    ok(abs(sc["avg_cents"] - 10) < 2 and sc["in_tune_pct"] > 97, "+10c singer: avg %.1fc, in tune %.0f%%" % (sc["avg_cents"], sc["in_tune_pct"]))
    ok(sc["highest"].startswith("G4") and sc["lowest"].startswith("G3"), "stable range: %s .. %s" % (sc["lowest"], sc["highest"]))
    _, sc = run_session(p, {}, lambda st, end: E.singer_track(st, end, offset_cents=-25), seed=2)
    ok(sc["in_tune_pct"] < 3 and sc["near_pct"] > 97 and abs(sc["avg_cents"] - 25) < 2,
       "-25c singer: in tune %.0f%%, within ±35 %.0f%%, avg %.1fc" % (sc["in_tune_pct"], sc["near_pct"], sc["avg_cents"]))
    ok(abs(sc["bias_cents"] + 25) < 2, "bias shows flat singing: %+.1fc" % sc["bias_cents"])
    _, sc = run_session(p, {}, lambda st, end: E.singer_track(st, end, offset_cents=0), vibrato_cents=25, seed=3)
    ok(10 < sc["avg_cents"] < 22 and 30 < sc["in_tune_pct"] < 80, "vibrato ±25c: avg %.1fc, in tune %.0f%%" % (sc["avg_cents"], sc["in_tune_pct"]))

    q = E.build_plan("maqam", 50, 45, 67, "up", "fast", "Saba", "jins")[:3]
    _, sc = run_session(q, {"mode": "maqam"}, lambda st, end: E.singer_track(st, end), seed=4)
    ok(sc["avg_cents"] < 3 and sc["in_tune_pct"] > 97, "Saba sung with real quarter tones: avg %.1fc, in tune %.0f%%" % (sc["avg_cents"], sc["in_tune_pct"]))

    def snapped(st, end):
        tr = E.singer_track(st, end)
        return np.where(np.isnan(tr), np.nan, np.round(tr + 1e-6))
    s, sc = run_session(q, {"mode": "maqam"}, snapped, seed=5)
    neutral = [abs(f[3]) for f in s.frames if f[3] is not None and abs((f[2] or 0) % 1 - 0.5) < 1e-6]
    ok(len(neutral) > 50 and abs(np.median(neutral) - 50) < 2, "Saba sung on the piano grid: neutral degrees %.0fc off -> red" % np.median(neutral))

    g = E.build_plan("siren", 48, 45, 67, "up", "normal")[:2]
    _, sc = run_session(g, {}, lambda st, end: E.singer_track(st, end, late=0.18), seed=6)
    ok(sc["avg_cents"] < 12, "siren followed 180 ms late: avg %.1fc (glides allow ±250 ms)" % sc["avg_cents"])

    s = E.Session([], "free")
    pt = E.PitchTracker()
    s.process(pt.feed(E.synth_voice(np.full(SR, 52.3), seed=7)))
    e = [f[3] for f in s.frames if f[3] is not None]
    ok(abs(np.median(e) - 30) < 1.5, "free mode: E3 +30c shown against the nearest note (%+.1fc)" % np.median(e))

    print("— reference piano / tone")
    st = E.build_plan("maqam", 50, 45, 67, "up", "slow", "Bayati")[0]
    for kind in ("piano", "tone"):
        y = E.render_step(st, kind=kind)
        fr = track(y)
        good = 0
        for g in st.segs[:5]:
            e = [(E.hz_to_midi(f) - g.m0) * 100 for t, f, c, db in fr if f and g.t0 + 0.15 < t < g.t1 - 0.1]
            good += int(len(e) > 10 and abs(np.median(e)) < 3)
        ok(good == 5 and np.abs(y).max() < 1, "%s reference plays the Bayati degrees in tune (incl. E3 -50c)" % kind)

    print("— WAV recording and history (Arabic folder)")
    E.HISTORY_FILE = os.path.join(d, "سجل", "history.json")
    E.REC_DIR = os.path.join(d, "تسجيلات")
    wp = E.recording_path("تمرين scale5 C3")
    rec = E.WavRecorder(wp)
    sig = sine(57, 2.0)
    for i in range(0, len(sig), E.HOP):
        rec.write(sig[i:i + E.HOP])
    rec.close()
    with open(wp, "rb") as fh:
        w = wave.open(fh)
        n, sr, ch, sw = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
        back = np.frombuffer(w.readframes(n), "<i2") / 32767.0
    ok(n == len(sig) and sr == SR and ch == 1 and sw == 2, "WAV: %s, %d samples, 16-bit mono %d Hz" % (os.path.basename(wp), n, sr))
    ok(abs(np.median(errs(track(back.astype(np.float32)), lambda t: 57))) < 1, "recorded WAV plays back at the sung pitch")

    import datetime
    s1, _ = run_session(p, {"mode": "exercise", "exercise": "scale5"}, lambda st, end: E.singer_track(st, end, offset_cents=12), seed=8)
    s2, _ = run_session(p, {"mode": "exercise", "exercise": "scale5"}, lambda st, end: E.singer_track(st, end, offset_cents=4), seed=9)
    d1, d2 = datetime.datetime(2026, 10, 20, 9, 0), datetime.datetime(2026, 10, 21, 9, 0)
    E.save_session(E.history_entry(s1, 60, d1))
    E.save_session(E.history_entry(s1, 60, d2))
    h = E.save_session(E.history_entry(s2, 120, d2))
    rows = E.daily(E.load_history())
    ok(len(h) == 3 and [r["date"] for r in rows] == ["2026-10-20", "2026-10-21"], "history saved: 3 sessions, 2 days")
    ok(rows[0]["avg_cents"] > rows[1]["avg_cents"] and rows[1]["sessions"] == 2 and rows[1]["minutes"] == 3.0,
       "daily progress: %.1fc -> %.1fc, day 2 = 2 sessions / 3 min" % (rows[0]["avg_cents"], rows[1]["avg_cents"]))
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
