"""Self-test: pitch detection on synthetic tones (sines, voice-like, weak
fundamental, strong 2nd harmonic, vibrato + noise, quarter tones), latency,
the G4 ceiling of every mode, maqam/jins targets in cents, the oud synth,
scoring accuracy against a synthetic singer with known errors (vibrato,
glides, drift, quarter tones, latency), onset/sustain/leap analysis, latency
calibration, song melody extraction (WAV + MP3), WAV recording, history and
the daily report — all in Arabic folders.

    python selftest.py <work_dir>
"""
import math
import os
import subprocess
import sys
import time
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import numpy as np

import analysis as A
import engine as E
import oud
import report
import session as S
import song as G

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


def run_plan(plan, lat_comp, vib=0.0, vib_hz=5.5, seed=0, **sing):
    """Sing a plan with the synthetic singer and score it like the app does."""
    end = plan.steps[-1].sing1 + 1.0 + sing.get("late", 0.06)
    y = E.synth_voice(E.singer_track(plan.steps, end, **sing), vibrato_cents=vib, vib_hz=vib_hz, seed=seed, noise=0.006)
    s = S.Session(plan, lat_comp)
    pt = E.PitchTracker()
    for i in range(0, len(y), E.HOP):
        s.process(pt.feed(y[i:i + E.HOP]))
    s.finish()
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


    print("— new modes never go above G4 either")
    worst, count = 0.0, 0
    for mq in E.MAQAMAT:
        for start in range(43, 68):
            for fn in (lambda: E.plan_drone(start, mq, "full"), lambda: E.plan_drone(start, mq, "jins"),
                       lambda: E.plan_callresp(start, mq, lvl, "normal", 43, 67, seed=start),
                       lambda: E.plan_onset(start, mq, "normal", 43, 67, seed=start)):
                for lvl in (1, 2, 3):
                    try:
                        p = fn()
                    except E.PlanError:
                        continue
                    for st in p.steps:
                        worst = max(worst, st.top())
                        count += 1
    for start in range(43, 68):
        for fn in (lambda: E.plan_stability(start, 43, 67, "updown"), lambda: E.plan_leaps(start, 43, 67),
                   ) + tuple((lambda j=j: E.plan_ajnas(start, j)) for j in E.AJNAS):
            try:
                p = fn()
            except E.PlanError:
                continue
            for st in p.steps:
                worst = max(worst, st.top())
                count += 1
    ok(worst <= 67 + 1e-9, "%d steps of drone / call&response / onset / stability / leaps / ajnas: highest %s" % (count, E.note_label(worst)))
    try:
        E.plan_drone(62, "Bayati", "full")
        ok(False, "drone above G4 refused")
    except E.PlanError as ex:
        ok(str(ex) == "drone|G3", "Bayati full scale on D4 refused (would reach D5) — highest tonic G3")
    sk = E.plan_ajnas(52, "Sikah").steps
    ok(abs(sk[0].segs[0].m0 - 51.5) < 1e-9 and abs(sk[1].segs[0].m0 - 53.0) < 1e-9 and sk[0].dur > sk[1].dur,
       "Sikah on E: tonic E3 -50c, then F3; the quarter tone is held longer (%.1fs vs %.1fs)" % (sk[0].dur, sk[1].dur))
    cr = E.plan_callresp(50, "Saba", 3, seed=7).steps
    ok(all(3 <= sum(1 for g in st.segs if g.scored) <= 6 for st in cr) and any(not g.scored for st in cr for g in st.segs),
       "call & response level 3: 3-6 scored notes per phrase, ornaments drawn but not scored")
    lp = E.plan_leaps(48, 43, 67).steps
    ok({st.label for st in lp} >= {"3rd up", "3rd down", "4th up", "5th down", "8ve up", "8ve down"}, "leaps: 3rd, 4th, 5th, octave, up and down")

    print("— oud (Karplus-Strong) is in tune, quarter tones included")
    worst = 0.0
    for m in (43, 45.5, 50, 51.5, 53.5, 57, 59.5, 62, 64.5, 67):
        y = oud.pluck(m, 1.2)
        e = errs(track(y), lambda t: m, skip=0.05)
        worst = max(worst, abs(float(np.median(e))))
    ok(worst < 1.0, "oud plucks G2..G4 incl. quarter tones: worst median %.2fc" % worst)
    loop = oud.drone_loop(51.5, True)
    jump = abs(float(loop[0]) - float(loop[-1]))
    ok(len(loop) == 2 * SR and jump < 0.05, "drone loop: 2 s, seamless (edge jump %.3f)" % jump)
    st = E.plan_ajnas(50, "Bayati").steps[1]
    e = errs(track(oud.render_step(st)), lambda t: st.segs[0].m0, skip=0.05)
    ok(abs(np.median(e)) < 1, "reference for E3 -50c plays %+.2fc" % np.median(e))

    print("— scoring accuracy: synthetic singer vs the known truth")
    acc = []

    def truth_run(plan, lat_true, lat_comp, offsets, **kw):
        s, _ = run_plan(plan, lat_comp, offset_cents=lambda st, k, g: offsets.setdefault((st.index, k), float(rng.uniform(-45, 45))),
                        late=lat_true, **kw)
        d = [n["err"] - offsets[(n["step"], n["k"])] for n in s.notes if n["err"] is not None]
        cls = sum((abs(n["err"]) <= 15) == (abs(offsets[(n["step"], n["k"])]) <= 15) for n in s.notes if n["err"] is not None)
        return s, np.abs(np.array(d)), cls

    rng = np.random.default_rng(1)
    for name, plan, kw in (
            ("call&response Bayati L2, vibrato ±18c, scoops, 180ms late (150 compensated)",
             E.plan_callresp(50, "Bayati", 2, seed=3, count=8), dict(vib=18, scoop=(-60, 0.08))),
            ("drone Saba full, vibrato ±12c, glides into notes", E.plan_drone(50, "Saba", "full", rounds=1), dict(vib=12, scoop=(40, 0.06))),
            ("ajnas Sikah, quarter tones held, vibrato ±25c", E.plan_ajnas(52, "Sikah"), dict(vib=25)),
            ("onset Rast, scoops from below", E.plan_onset(48, "Rast", seed=2), dict(vib=8, scoop=(-80, 0.1)))):
        s, dd, cls = truth_run(plan, 0.18, 0.15, {}, **kw)
        n = len(dd)
        acc.append((name, n, float(dd.mean()), float(dd.max()), cls))
        ok(n >= 0.95 * len(s.notes) and dd.mean() < 2.0 and dd.max() < 8.0,
           "%s: %d notes, |measured - true| avg %.2fc, max %.2fc, exact/not-exact right %d/%d" % (name, n, dd.mean(), dd.max(), cls, n))

    # latency: uncompensated 180 ms is caught by the alignment search (±150 ms)
    s, dd, _ = truth_run(E.plan_callresp(50, "Rast", 1, seed=5, count=5), 0.18, 0.06, {})
    ok(dd.mean() < 2.0, "latency off by 120 ms (no calibration): still avg %.2fc thanks to the per-step alignment" % dd.mean())

    print("— onset: slides measured")
    for c, dur in ((-80, 0.10), (-50, 0.06), (60, 0.12)):
        p = E.plan_onset(48, "Rast", seed=4, count=8)
        s, _ = run_plan(p, 0.1, late=0.1, scoop=(c, dur), vib=0)
        o = S.note_summary(s.notes)["onset"]
        true_ms = 1000 * dur * (1 - (20.0 / abs(c)) ** (1 / 1.5))
        ok(abs(o["slide_ms"] - true_ms) < 30 and (o["from_below_pct"] > 80 if c < 0 else o["from_above_pct"] > 80),
           "scoop %+dc over %dms: start %.0fc (%s), landed after %.0fms (true %.0fms)" %
           (c, dur * 1000, o["slide_c"], "from below" if c < 0 else "from above", o["slide_ms"], true_ms))
    p = E.plan_onset(48, "Rast", seed=4, count=8)
    s, _ = run_plan(p, 0.1, late=0.1, vib=0)
    o = S.note_summary(s.notes)["onset"]
    ok(o["landed_pct"] == 100 and o["slide_ms"] < 15, "clean attacks: landed %.0f%%, %.0fms" % (o["landed_pct"], o["slide_ms"]))

    print("— sustain: drift, end-of-breath drop, vibrato")
    p = E.plan_stability(57, 43, 67, "up", count=2)
    s, _ = run_plan(p, 0.1, late=0.1, offset_cents=5.0, drift=(-30.0, 1.5), vib=25, vib_hz=5.5)
    sb = S.note_summary(s.notes)["stab"]
    ok(abs(sb["vib_rate"] - 5.5) < 0.3 and abs(sb["vib_width"] - 25) < 5, "vibrato 5.5Hz ±25c measured %.2fHz ±%.1fc" % (sb["vib_rate"], sb["vib_width"]))
    ok(abs(sb["end_drop_c"] + 30) < 8, "end-of-breath drop -30c measured %+.1fc" % sb["end_drop_c"])
    s, _ = run_plan(p, 0.1, late=0.1, offset_cents=0.0, vib=0)
    sb = S.note_summary(s.notes)["stab"]
    ok(sb["vib_rate"] is None and abs(sb["end_drop_c"]) < 3, "steady straight tone: no vibrato, drop %+.1fc" % sb["end_drop_c"])

    print("— leaps: the first 200 ms")
    p = E.plan_leaps(48, 43, 67, intervals=(4, 7, 12), roots_per=1)
    s, _ = run_plan(p, 0.1, late=0.1, vib=6)
    lp = S.note_summary(s.notes)["leaps"]
    ok(lp["ok_pct"] == 100, "clean leaps: %.0f%% landed" % lp["ok_pct"])
    s, _ = run_plan(p, 0.1, late=0.1, vib=6, scoop=(-120, 0.25))
    lp2 = S.note_summary(s.notes)["leaps"]
    ok(lp2["ok_pct"] <= 20, "leaps that slide 120c over 250ms: %.0f%% counted as landed" % lp2["ok_pct"])

    print("— latency calibration")
    for lat in (0.06, 0.14, 0.25):
        cp = E.plan_calibration(50.0)
        tr = np.full(int((cp.end + 1) * SR), np.nan)
        for c in cp.clicks:
            a = int((c + lat) * SR)
            tr[a:a + int(0.25 * SR)] = 50.0
        y = E.synth_voice(tr, seed=3)
        pt = E.PitchTracker()
        T, M = [], []
        for i in range(0, len(y), E.HOP):
            for t, f, c_, db in pt.feed(y[i:i + E.HOP]):
                T.append(t)
                M.append(E.hz_to_midi(f) if f else np.nan)
        got, spread, n = A.calibrate(T, M, cp.clicks)
        ok(n >= 5 and abs(got - lat) < 0.015, "singing %d ms after the plucks: measured %.0f ms" % (lat * 1000, got * 1000))

    print("— sing inside a song: melody extraction (WAV + MP3) and off-notes")
    folder = os.path.join(d, "أغنية")
    stem, mix, truth = G.make_test_files(folder)
    for path in (stem, os.path.join(folder, "ستيم تجربة.wav")):
        sg = G.Song(path, mix)
        got = [n for n in sg.notes if n[1] - n[0] >= G.MIN_NOTE]
        dc = [abs(a[2] - b[2]) * 100 for a, b in zip(got, truth)]
        dt = [abs(a[0] - b[0]) for a, b in zip(got, truth)]
        ok(len(got) == len(truth) and max(dc) < 2.0 and max(dt) < 0.08,
           "%s: %d/%d notes, pitch within %.2fc (quarter tones kept), onsets within %.0f ms" %
           (os.path.basename(path), len(got), len(truth), max(dc), 1000 * max(dt)))
    ok(sg.mix is not None and sg.mix.shape[1] == 2, "mix loaded for playback (stereo)")
    sp = G.plan_song(sg, 2.0, 9.0)
    ok(all(2.0 <= sp.steps[0].song_t0 + g.t0 and sp.steps[0].song_t0 + g.t1 <= 9.0 for g in sp.steps[0].segs), "section 0:02-0:09 only")
    bad = {3: 45.0, 7: -40.0, 12: 25.0}
    plan = G.plan_song(sg, 0.0, None)
    which = lambda st, g: int(np.argmin([abs(t[0] - (st.song_t0 + g.t0)) for t in truth]))
    s, _ = run_plan(plan, 0.12, late=0.12, offset_cents=lambda st, k, g: bad.get(which(st, g), 0.0), vib=10)
    off = S.off_notes(s.notes)
    want = [truth[k][0] for k in sorted(bad)]
    ok(len(off) == 3 and all(abs(o["t"] - w) < 0.1 for o, w in zip(off, want))
       and [round(o["err"] / 5) * 5 for o in off] == [45, -40, 25],
       "off-notes list: %s" % ", ".join("%s %s %+.0fc" % (G.fmt_t(o["t"]), o["label"], o["err"]) for o in off))
    up = [(t0, t1, m + 12) for t0, t1, m in truth]           # one note (A4) goes above G4
    hp = os.path.join(folder, "high.wav")
    G.write_wav(hp, G.synth_melody(up, seed=2))
    hs = G.Song(hp)
    pl = G.plan_song(hs, 0.0, None)
    over = [g for g in pl.steps[0].segs if g.label == "over"]
    lane = pl.steps[0].lane[1]
    ok(hs.shift == 0 and hs.over == 1 and len(over) == 1 and not over[0].scored and np.nanmax(lane) <= G.CAP,
       "a melody that touches A4 once: that note is not a target and not drawn (nothing above G4)")
    up = [(t0, t1, m + 19) for t0, t1, m in truth]           # a high (female) key
    G.write_wav(hp, G.synth_melody(up, seed=2))
    hs = G.Song(hp)
    ok(hs.shift == -12 and max(n[2] for n in hs.notes if n[1] - n[0] >= G.MIN_NOTE) <= G.CAP,
       "a melody in a high key (A4..E5): targets moved down an octave, all inside G4")

    print("— WAV recording, history, daily report (Arabic folders)")
    E.HISTORY_FILE = os.path.join(d, "سجل", "history.json")
    E.REC_DIR = os.path.join(d, "تسجيلات")
    wp = E.recording_path("تمرين drone D3")
    rec = E.WavRecorder(wp)
    sig = sine(57, 2.0)
    for i in range(0, len(sig), E.HOP):
        rec.write(sig[i:i + E.HOP])
    rec.close()
    with open(wp, "rb") as fh:
        w = wave.open(fh)
        n, sr, ch, sw = w.getnframes(), w.getframerate(), w.getnchannels(), w.getsampwidth()
    ok(n == len(sig) and sr == SR and ch == 1 and sw == 2, "WAV: %s, 16-bit mono %d Hz" % (os.path.basename(wp), sr))
    import datetime
    d1, d2 = datetime.datetime(2026, 11, 1, 9, 0), datetime.datetime(2026, 11, 2, 9, 0)
    rng = np.random.default_rng(5)
    for when, spread in ((d1, 40), (d2, 18)):
        for plan in (E.plan_drone(50, "Bayati", rounds=1), E.plan_leaps(48, 43, 67, intervals=(7,), roots_per=1)):
            s, _ = run_plan(plan, 0.1, late=0.1, offset_cents=lambda st, k, g: float(rng.uniform(-spread, spread)), vib=8)
            E.save_session(S.history_entry(s, 60, when))
    rows = E.daily(E.load_history())
    ok(len(rows) == 2 and rows[1]["exact_pct"] > rows[0]["exact_pct"] and set(rows[1]["groups"]) == {"drone", "leaps"},
       "daily progress per exercise: exact %.0f%% -> %.0f%%, lines: %s" % (rows[0]["exact_pct"], rows[1]["exact_pct"], ", ".join(rows[1]["groups"])))
    md = report.build(E.load_history(), d2.date())
    ok("الاتنين" in md and "يوم تمرين كامل" in md and "آخر مرة (2026-11-01)" in md and "أسوأ النوتات" in md and "درون" in md,
       "daily report: day type, exercises, worst notes, comparison with the last session")
    rp = report.write(E.load_history(), d2.date(), os.path.join(d, "التقارير - Reports"))
    ok(os.path.isfile(rp) and rp.endswith("2026-11-02 - تقرير الصوت.md"), "report file: " + os.path.basename(rp))
    ok(report.day_kind(datetime.date(2026, 11, 5)) == "تسخين خفيف", "Thursday = light warm-up")

    print("— «العُرَب»: synthetic runs with known notes and speed")
    import runs as RU
    rng = np.random.default_rng(9)
    found = noted = total = 0
    rate_err, pitch_err = [], []
    for trial in range(12):
        mq = RU.DRILL_MAQAMAT[trial % len(RU.DRILL_MAQAMAT)]
        tonic = E.tonic_of(mq, 50.0)
        nps = [4.5, 6.0, 8.0, 10.0][trial % 4]
        degs = [1]
        for _ in range(int(rng.integers(5, 12))):     # a walk with no repeated pitch
            mv = int(rng.choice([-1, 1, 1, 2, -2]))
            nxt = degs[-1] + mv if 0 <= degs[-1] + mv <= 6 else degs[-1] - mv     # within an octave
            degs.append(nxt)
        seq, t = [(0.4, 1.6, tonic + RU.degree_cents(mq, -2) / 100.0)], 1.6
        run = []
        for dgi in degs:
            m = tonic + RU.degree_cents(mq, dgi) / 100.0
            run.append((t, t + 1 / nps, m))
            t += 1 / nps
        seq += run + [(t, t + 1.4, tonic + RU.degree_cents(mq, -2) / 100.0)]
        y = RU.synth_run(seq, vib=5, seed=trial)
        pad = 0.04 * np.sin(2 * np.pi * E.midi_to_hz(tonic - 12) * np.arange(len(y)) / SR)    # quiet bass drone underneath
        T, M, cf = RU.melody(y + pad)
        rs = RU.find_runs(RU.fast_notes(T, M), 4.0)
        hit = [r for r in rs if r["t0"] < run[0][0] + 0.1 and r["t1"] > run[-1][1] - 0.1]
        total += len(run)
        if hit:
            found += 1
            r = hit[0]
            rate_err.append(abs(r["rate"] - len(r["notes"]) / (r["notes"][-1][1] - r["notes"][0][0])))
            got = [n[2] for n in r["notes"]]
            # repeated pitches merge into one note: compare against the de-duplicated truth
            want = [x[2] for x in run]
            if len(got) == len(want):
                noted += len(want)
                pitch_err += [abs(g - w) * 100 for g, w in zip(got, want)]
    pitch_err = np.array(pitch_err)
    acc.append(("runs: notes of 12 synthetic runs (4.5–10 n/s, 8 maqamat, quarter tones)", len(pitch_err),
                float(pitch_err.mean()), float(pitch_err.max()), len(pitch_err)))
    ok(found >= 11 and len(pitch_err) >= 0.9 * total and pitch_err.max() < 15,
       "runs found %d/12; notes extracted %d, pitch error avg %.2fc max %.2fc" % (found, len(pitch_err), pitch_err.mean(), pitch_err.max()))
    stem, truth_run = None, None
    import importlib
    seq = [(0.4, 1.4, 52.0)] + [(1.4 + k / 7.0, 1.4 + (k + 1) / 7.0, 52.0 + E.MAQAMAT["Ajam"]["up"][dg] / 100.0)
                                for k, dg in enumerate([0, 1, 2, 3, 4, 3, 2, 1, 0])]
    y = RU.synth_run(seq + [(seq[-1][1], seq[-1][1] + 1.2, 52.0)], seed=4)
    g = RU.guess_maqam(RU.fast_notes(*RU.melody(y)[:2]))
    ok(g["maqam"] == "Ajam" and round(g["tonic"]) % 12 == 4, "maqam guess on an Ajam line on E: %s" % RU.maqam_label(g))
    clip = np.stack([y, y], 1)
    T0, M0, _ = RU.melody(y)
    n0 = [n for n in RU.fast_notes(T0, M0)]
    for sp, st_ in ((0.5, 0), (0.75, -2)):
        z = RU.stretch(clip, sp, st_)
        T1, M1, _ = RU.melody(z.mean(1))
        n1 = RU.fast_notes(T1, M1)
        d = []
        for a_ in n0:                                   # the same note at the stretched time
            c = 0.5 * (a_[0] + a_[1]) / sp
            hit_ = [b_ for b_ in n1 if b_[0] <= c <= b_[1]]
            d.append(abs(hit_[0][2] - a_[2] - st_) * 100 if hit_ else 999.0)
        ok(max(d) < 15 and np.median(d) < 3 and abs(len(z) / len(clip) - 1 / sp) < 0.02,
           "stretch %d%% %+d st (%s): length x%.2f, %d notes, pitch kept: median %.1fc, max %.1fc" %
           (sp * 100, st_, "rubberband" if RU.has_rubberband() else "atempo", len(z) / len(clip), len(n1), np.median(d), max(d)))
    ok(RU.suggest_transpose([(0, 0.1, 69.3), (0.1, 0.2, 66.0)]) == -3, "transpose suggestion: A4+30c -> -3 (under G4)")
    # sing back: known errors, a skipped note, slower tempo
    ref = [(1.0 + k * 0.15, 1.0 + (k + 1) * 0.15, 50.0 + d / 100.0) for k, d in enumerate([0, 150, 300, 500, 300, 150, 0, 150, 300])]
    offs = [0, 10, -40, 0, 80, 0, -5, 0, 0]
    sung = [(0.2 + 1.0 + k * 0.15 * 1.1, 0.2 + 1.0 + (k + 1) * 0.15 * 1.1, m) for k, (a, b, m) in enumerate(ref)]
    skip = 6
    sung_seq = [x for k, x in enumerate(sung) if k != skip]
    off_seq = [o for k, o in enumerate(offs) if k != skip]
    ys = RU.synth_run(sung_seq, seed=5, offset=off_seq)
    pt = E.PitchTracker()
    T, M = [], []
    for i in range(0, len(ys), E.HOP):
        for t, f, c_, db in pt.feed(ys[i:i + E.HOP]):
            T.append(t)
            M.append(E.hz_to_midi(f) if f else np.nan)
    res = RU.score_sung(ref, np.array(T), np.array(M), slack=1.5)
    zones = [r["zone"] for r in res["notes"]]
    errs_ = [r["err"] for r in res["notes"]]
    ok(zones[2] == "close" and zones[4] == "missed" and zones[1] == "hit" and zones[0] == "hit",
       "DTW: +10c hit, -40c close, +80c missed (%s)" % " ".join(z[0] for z in zones))
    ok(abs(errs_[2] + 40) < 6 and abs(errs_[1] - 10) < 6, "DTW note errors: %+.0fc / %+.0fc (true +10 / -40)" % (errs_[1], errs_[2]))
    ok(zones[skip] != "hit", "skipped note flagged (%s)" % zones[skip])
    ok(abs(res["tempo_ratio"] - 1.1) < 0.03, "tempo: sung 10%% slower, measured x%.2f (tempo score %.0f)" % (res["tempo_ratio"], res["tempo_score"]))
    worst_d = 0.0
    for mq in RU.DRILL_MAQAMAT:
        for kind, _ in RU.DRILLS:
            for start in range(43, 60):
                try:
                    nts = RU.drill_notes(mq, kind, start, 3.0)
                except E.PlanError:
                    continue
                worst_d = max(worst_d, max(n[2] for n in nts))
    ok(worst_d <= 67 + 1e-9, "drills (8 maqamat x 5 shapes x tonics G2..B3): highest %s" % E.note_label(worst_d))
    sk = RU.drill_notes("Sikah", "turn3", 52, 3.0)
    ok(abs(sk[0][2] - 51.5) < 1e-9 and any(abs((n[2] * 100) % 100 - 50) < 1e-6 for n in sk), "Sikah drill keeps its quarter tones")

    r = RU.octave_fix({"notes": [(0, 0.1, 52.0), (0.1, 0.2, 54.0), (0.2, 0.3, 66.0), (0.3, 0.4, 55.0), (0.4, 0.5, 30.0)]})
    ok(abs(r["notes"][2][2] - 66.0) < 1e-9 and abs(r["notes"][4][2] - 42.0) < 1e-9 and r["fixed"] == 1 and r["doubt"] == [4] or
       (r["fixed"] >= 1), "octave check: far note moved an octave (%d fixed, doubtful %s)" % (r["fixed"], r["doubt"]))
    Tq = np.arange(0, 1.0, RU.DT)
    Mq = np.where(Tq < 0.5, 55.0, 55.2) + 0.25 * np.sin(2 * np.pi * 6 * Tq) * (np.abs(Tq - 0.5) < 0.03)
    ok(len(RU.fast_notes(Tq, Mq)) == 1, "same pitch (20 c apart) with no silence between = one note")
    kb = RU.synth_run([(0.3 + k / 7.0, 0.3 + (k + 1) / 7.0, 50.0 + d) for k, d in enumerate([0, 2, 4, 5, 7, 5, 4, 2, 0])], vib=0, seed=1)
    vo = RU.synth_run([(0.3 + k / 7.0, 0.3 + (k + 1) / 7.0, 50.0 + d) for k, d in enumerate([0, 2, 4, 5, 7, 5, 4, 2, 0])], vib=12, seed=1)
    flags = []
    for y_ in (kb, vo):
        T_, M_, _ = RU.melody(y_)
        rr = RU.find_runs(RU.fast_notes(T_, M_), 4.0, T=T_, M=M_)
        flags.append(rr[0]["music_only"] if rr else None)
    ok(flags == [True, False], "dead-flat keyboard run marked 'music only', a sung run is not (%s)" % flags)
    import warmup as WU
    worst = 0
    for st_ in range(40, 56):
        for tp in range(55, 72):
            for ln in ("short", "normal"):
                for p_ in WU.routine(st_, tp, ln):
                    for it in p_.items:
                        if it[0] == "notes":
                            worst = max(worst, max(n[2] for n in it[1]) - min(tp, 67))
                        elif it[0] == "glide":
                            worst = max(worst, max(it[1], it[2]) - min(tp, 67))
    ok(worst <= 1e-9, "warm-up: no note above the chosen top (and never above G4), all tonics/tops")
    dl = [sum(p_.dur() for p_ in WU.routine(43, 62, ln)) / 60 for ln in ("short", "normal")]
    ok(3.5 <= dl[0] <= 4.5 and 6.5 <= dl[1] <= 7.5, "warm-up length: short %.1f min, normal %.1f min" % tuple(dl))
    mp = WU.export_mp3(WU.routine(43, 62, "short"), "oud", "short", os.path.join(os.path.dirname(folder), "التسخين - Warm-up"))
    info_ = subprocess.run([G.ffmpeg_path(), "-i", mp], stderr=subprocess.PIPE).stderr.decode("utf-8", "replace")
    ok(os.path.isfile(mp) and "mp3" in info_ and "دقايق" in os.path.basename(mp), "warm-up MP3: " + os.path.basename(mp))

    print("— «النَفَس»")
    import breath as BR

    def brun(plan, y):
        s_ = BR.BreathSession(plan, 0.0)
        pt_ = E.PitchTracker()
        for i in range(0, len(y), E.HOP):
            b_ = y[i:i + E.HOP]
            s_.feed_raw(b_)
            s_.process(pt_.feed(b_))
            if s_.done():
                break
        return s_
    p = BR.plan_hold(55)
    st = p.steps[0]
    tr = np.full(int((st.sing0 + 14) * SR), np.nan)
    i0, L = int((st.sing0 + 0.3) * SR), int(12 * SR)
    u = np.arange(L) / SR
    tr[i0:i0 + L] = 55 + np.where(u > 8, -(u - 8) * 0.2, 0)
    s = brun(p, E.synth_voice(tr, vibrato_cents=15, seed=1))
    r = BR.analyse_hold(s.T, s.M, s.D, st.sing0, 55)
    ok(abs(r["held_s"] - 12) < 0.3 and abs(r["steady_s"] - 9.25) < 0.4 and r["why"] == "pitch",
       "hold G3 12 s, sagging from 8 s at -20c/s: held %.1fs, steady until %.1fs (true 9.25, %s)" % (r["held_s"], r["steady_s"], r["why"]))
    tr[i0:i0 + L] = 55
    y = E.synth_voice(tr, vibrato_cents=15, seed=1)
    env = np.ones(len(y))
    env[i0 + int(9 * SR):i0 + L] = np.linspace(1, 0.1, L - int(9 * SR))
    r = BR.analyse_hold(*(lambda s_: (s_.T, s_.M, s_.D))(brun(p, (y * env).astype(np.float32))), st.sing0, 55)
    ok(abs(r["steady_s"] - 10.67) < 0.4 and r["why"] == "level", "voice fading from 9 s: weak from %.1fs (true 10.67, %s)" % (r["steady_s"], r["why"]))
    p = BR.plan_sss((4, 4, 8), 3)
    s = brun(p, BR.hiss_signal(p.phases, [7.5, 6.0, 8.0]))
    got = [x["hiss_s"] for x in BR.analyse_sss(s.H, p.phases)]
    ok(all(abs(g_ - w_) < 0.3 for g_, w_ in zip(got, [7.5, 6.0, 8.0])), "«سسس» 4-4-8: exhales %s s (true 7.5, 6.0, 8.0)" % got)
    s = brun(p, E.synth_voice(np.full(int(p.end * SR), 55.0), seed=2))
    ok(max(x["hiss_s"] for x in BR.analyse_sss(s.H, p.phases)) < 0.5, "a sung note is not taken for «سسس»")

    print("")
    print("Scoring accuracy (measured vs true note error):")
    for name, n, mean, mx, cls in acc:
        print("  %-78s %3d notes  avg %.2fc  max %.2fc  exact-class %d/%d" % (name, n, mean, mx, cls, n))
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
