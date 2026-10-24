"""Self-test for the auto mode (no speech model needed: the ASR output is mocked).

    python selftest_auto.py <work_dir> [--real-asr tiny]

Checks lyric normalization + alignment (repeated chorus), section splitting,
face-crop math, video sync / shot picking, and renders reels end to end from a
generated song + music video with Arabic file names. With --real-asr it also
downloads a faster-whisper model and runs it once (proves the bundle loads it).
"""
import json
import os
import random
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import numpy as np

import autoreel as A
import engine

V1 = ["قلبي اختارك من أول نظرة", "والليل بيحكي عن عيونك لوحده", "مش هقدر أبعد عنك تاني"]
CH = ["يا حبيبي قولّي إيه *الحكاية*", "ليه بتسيبني في نص الطريق"]
V2 = ["كل الطرق بتوديني ليك", "وأنا ماشي ورا قلبي العنيد"]
TEXTS = V1 + CH + V2 + CH + CH
PAUSE_BEFORE = {3: 5.0, 5: 5.0, 7: 5.0}   # instrumental breaks (seconds)
FIRST = 4.0


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def ground_truth():
    """[(line, [(word, start, end), ...])] and the song length."""
    t, gt = FIRST, []
    for i, line in enumerate(TEXTS):
        t += PAUSE_BEFORE.get(i, 0.0)
        ws = []
        for w in line.split():
            d = 0.12 * A.letters(w) + 0.12
            ws.append((w.replace("*", ""), t, t + d))
            t += d + 0.08
        gt.append(ws)
        t += 0.7
    return gt, t + 6.0


def mock_asr(gt, seed=7, drop=0.12, skip_lines=()):
    """What Whisper might return: junk at the start, Egyptian spellings, tashkeel,
    dropped words, slightly off times."""
    rnd = random.Random(seed)
    out = [["موسيقى", 0.2, 1.4, 0.3], ["ترجمة", 1.5, 2.2, 0.2]]
    for li, ws in enumerate(gt):
        if li in skip_lines:
            continue
        for w, a, b in ws:
            if rnd.random() < drop:
                continue
            w2 = w.replace("ق", "أ") if rnd.random() < 0.5 else w
            if rnd.random() < 0.25:
                w2 = w2[:1] + "َ" + w2[1:]
            out.append([w2, a + rnd.uniform(-0.06, 0.06), b + rnd.uniform(-0.06, 0.06), 0.8])
    return out


# ----------------------------------------------------------------------------- unit tests

def test_normalize():
    ok(A.normalize("إِنْتَ") == A.normalize("انت"), "normalize: tashkeel + hamza alef")
    ok(A.normalize("حكايةٌ،") == "حكايه", "normalize: ta marbuta + punctuation")
    ok(A.normalize("على") == A.normalize("علي"), "normalize: alef maqsura")
    ok(A.normalize("*عيونك*!") == "عيونك", "normalize: gold stars")
    ok(A.normalize("٢٠٢٦") == "2026", "normalize: Arabic digits")
    ok(A.loose("قلبي") == A.loose("ألبي"), "loose: Egyptian ق = ء")


def test_align():
    gt, total = ground_truth()
    lines = A.align(TEXTS, mock_asr(gt), total)
    ok(len(lines) == len(TEXTS), "align: one timing per line")
    worst = max(abs(l.start - g[0][1]) for l, g in zip(lines, gt))
    ok(worst < 0.5, "align: every line within 0.5 s (worst %.2f)" % worst)
    ch_starts = [lines[i].start for i in (3, 7, 9)]
    ok(all(abs(a - gt[i][0][1]) < 0.5 for a, i in zip(ch_starts, (3, 7, 9))),
       "align: repeated chorus mapped to each occurrence in order %s" % ["%.1f" % x for x in ch_starts])
    ok(all(l.words and len(l.words) == len(l.text.split()) for l in lines), "align: word times for every word")
    ok(all(lines[i].start < lines[i + 1].start for i in range(len(lines) - 1)), "align: lines in order")
    # a chorus Whisper skipped entirely still lands between its neighbours, flagged for review
    lines2 = A.align(TEXTS, mock_asr(gt, skip_lines=(7, 8)), total)
    ok(lines2[6].end <= lines2[7].start < lines2[8].start <= lines2[9].start,
       "align: skipped chorus kept in order between its neighbours")
    ok(lines2[7].conf == 0 and lines2[8].conf == 0, "align: skipped lines flagged (confidence 0)")
    return lines, total


def test_timed_roundtrip(lines, d):
    p = os.path.join(d, "توقيت.txt")
    A.save_timed(lines, p, "تجربة")
    back = A.load_timed(p, TEXTS)
    ok(back is not None and len(back) == len(lines), "timed file: reads back")
    ok(max(abs(a.start - b.start) for a, b in zip(lines, back)) < 0.011, "timed file: same start times")
    entries = engine.parse_lyrics(engine.read_text(p))
    ok(all(t is not None for t, _ in entries), "timed file: manual mode parses every line with a time")


def test_sections(lines, total):
    secs = A.make_sections(lines, total)
    names = [s["name"] for s in secs]
    ok(names == ["كوبليه 1", "الكورس 1", "كوبليه 2", "الكورس الأخير"], "sections: %s" % " | ".join(names))
    for s in secs:
        a, b = A.reel_span(lines, s["i0"], s["i1"], total)
        ok(A.MIN_REEL - 0.01 <= b - a <= A.MAX_REEL + 0.01, "sections: %s is %.1f s" % (s["name"], b - a))
        ok(abs(a - (lines[s["i0"]].start - A.LEAD)) < 0.01, "sections: %s starts %.1f s before its first line" % (s["name"], A.LEAD))
    ok([s["checked"] for s in secs] == [True, True, False, True], "sections: default = choruses + first verse")
    ok(secs[3]["i0"] == 7 and secs[3]["i1"] == 10, "sections: the back-to-back last choruses form one reel")
    # a long verse is split into parts of <= 30 s at line boundaries
    words = "شمس قمر بحر نهر ورد ليل نجم غيم مطر ريح جبل رمل نخل موج طير باب".split()
    long_lines = [A.Line(" ".join(words[i:i + 3]) + " " + words[-1 - i], 2 + i * 4.0, 2 + i * 4.0 + 3.2)
                  for i in range(14)]
    ls = A.make_sections(long_lines, 70.0)
    ok(len(ls) >= 2 and all(A.reel_span(long_lines, s["i0"], s["i1"], 70)[1] - A.reel_span(long_lines, s["i0"], s["i1"], 70)[0] <= 30.01 for s in ls),
       "sections: 56 s verse -> %d parts (%s)" % (len(ls), " | ".join(s["name"] for s in ls)))


def eval_expr(expr, t):
    e = expr.replace("\\,", ",").replace("if(", "IF(").replace("lt(", "LT(")
    return eval(e, {"IF": lambda c, a, b: a if c else b, "LT": lambda a, b: a < b, "t": t})


def test_face_math():
    ok(A.crop_geometry(1920, 1080) == (606, 1080, True), "crop: 16:9 -> 606x1080 window")
    ok(A.crop_geometry(1080, 1920) == (1080, 1920, False), "crop: 9:16 stays")
    ok(A.crop_geometry(1080, 1080)[2] and A.crop_geometry(1080, 1080)[0] == 606, "crop: square")
    ok(A.crop_x(0.5, 1920, 606) == 657.0, "crop: centred face -> x 657")
    ok(A.crop_x(0.02, 1920, 606) == 0.0 and A.crop_x(0.99, 1920, 606) == 1314.0, "crop: kept inside the frame")
    track = [None, None, 0.3, 0.31, 0.95, 0.3, 0.32, None, None, 0.7, 0.7, 0.7, 0.7, 0.7, 0.7]
    sm = A.smooth_track(track, 3)
    ok(len(sm) == len(track) and abs(sm[0] - 0.3) < 0.05, "track: gaps at the start hold the first face")
    ok(sm[4] < 0.5, "track: one-frame jump (false face) ignored")
    ok(np.all(np.abs(np.diff(sm)) < 0.12), "track: smooth (max step %.3f)" % np.max(np.abs(np.diff(sm))))
    ok(np.allclose(A.smooth_track([None] * 6, 3), 0.5), "track: no face -> centre")
    times = np.arange(len(sm)) / 3.0
    xs = np.array([A.crop_x(c, 1920, 606) for c in sm])
    expr = A.x_expr(times, xs)
    errs = [abs(eval_expr(expr, t) - np.interp(t, times, xs)) for t in np.arange(0, times[-1], 0.1)]
    ok(max(errs) < 60, "crop expression follows the track (max %.0f px between keyframes)" % max(errs))
    ok(A.x_expr([0, 1, 2], [400, 400, 400]) == "400", "crop expression: still face -> constant")


# ----------------------------------------------------------------------------- media

def run(args, what):
    r = engine.run_ffmpeg(args)
    ok(r.returncode == 0, what + ("" if r.returncode == 0 else "\n" + r.stderr.decode("utf-8", "replace")[-600:]))


def make_song(gt, total, path):
    """Each word = a short sung-ish tone (pitch per line); pink noise underneath."""
    parts = []
    for li, ws in enumerate(gt):
        f = 196 + 22 * (li % 5)
        for w, a, b in ws:
            parts.append("between(t\\,%.2f\\,%.2f)*sin(2*PI*%d*t)" % (a, b, f))
    voice = "0.3*(%s)" % "+".join(parts)
    # aevalsrc expression is long but fine; beat on the instrumental too
    run(["-y", "-f", "lavfi", "-i", "aevalsrc=%s:s=44100:d=%.2f" % (voice, total),
         "-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.03:d=%.2f" % total,
         "-filter_complex", "[0][1]amix=inputs=2:normalize=0", "-c:a", "pcm_s16le", path],
        "test song (%.0f s)" % total)


def make_video(song, total, path, intro=3.0):
    """16:9 letterboxed 'music video': black intro, then moving picture with the song."""
    dur = total + intro + 2
    run(["-y", "-f", "lavfi", "-i", "testsrc2=s=1280x540:r=25:d=%.2f" % dur,
         "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25:d=%.2f" % dur,
         "-i", song,
         "-filter_complex",
         "[0]hue=s=0.6,boxblur=2[p];[1][p]overlay=0:90,"
         "fade=t=in:st=%.2f:d=0.3,format=yuv420p[v];"
         "[2]adelay=%d|%d,apad[a]" % (intro, int(intro * 1000), int(intro * 1000)),
         "-map", "[v]", "-map", "[a]", "-t", "%.2f" % dur,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-c:a", "aac", "-b:a", "128k", path],
        "test music video (black intro %.0f s, letterbox)" % intro)


def make_broll(path, dur=60):
    """Unrelated footage without sound: black start, black hole in the middle."""
    run(["-y", "-f", "lavfi", "-i", "mandelbrot=s=960x540:r=25", "-t", "%d" % dur,
         "-vf", "fade=t=in:st=0:d=0.1:c=black,"
                "geq=lum='if(between(T\\,0\\,4)+between(T\\,28\\,34)\\,16\\,lum(X\\,Y))':cb='cb(X\\,Y)':cr='cr(X\\,Y)',"
                "format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", path], "test b-roll (no audio)")


def check_reel(mp4, jpg, expect, tag, d):
    dur, has_v, has_a = engine.probe(mp4)
    ok(has_v and has_a, "%s: video + audio" % tag)
    ok(abs(dur - expect) < 0.25, "%s: duration %.2f (expected %.2f)" % (tag, dur, expect))
    info = engine.run_ffmpeg(["-i", mp4]).stderr.decode("utf-8", "replace")
    ok("h264" in info and "1080x1920" in info, "%s: h264 1080x1920" % tag)
    ok(os.path.isfile(jpg), "%s: thumbnail" % tag)
    for t in (1.0, expect / 2, expect - 0.5):
        png = os.path.join(d, "%s_%04.1f.png" % (tag, t))
        r = engine.run_ffmpeg(["-y", "-ss", "%.2f" % t, "-i", mp4, "-frames:v", "1", png])
        ok(r.returncode == 0 and os.path.isfile(png), "%s: frame at %.1f s" % (tag, t))


def test_end_to_end(d, gt, total):
    song = os.path.join(d, "أغنية تجربة.wav")
    video = os.path.join(d, "الكليب الرسمي.mp4")
    lyrics = os.path.join(d, "كلمات الأغنية.txt")
    make_song(gt, total, song)
    make_video(song, total, video, intro=3.0)
    with open(lyrics, "w", encoding="utf-8") as f:
        f.write("\n".join(TEXTS) + "\n")
    out = os.path.join(d, "output")
    calls = []

    def asr(path, texts):
        calls.append(path)
        return mock_asr(gt)
    log = lambda m: print("     " + m, flush=True)
    pr = A.Project(song, video, lyrics, song="أغنية تجربة", out_root=out, log=log, asr=asr)
    secs = pr.analyze()
    ok(calls == [song], "e2e: ASR ran once on the song")
    ok(os.path.isfile(pr.timed_path) and pr.timed_path.endswith("أغنية تجربة - timed.txt"),
       "e2e: saved '%s' next to the lyrics" % os.path.basename(pr.timed_path))
    ok(pr.offset is not None and abs(pr.offset - 3.0) < 0.15, "e2e: video synced by its soundtrack (offset %s)" % pr.offset)
    vi = pr.vinfo
    ok(abs(vi.ah - 540) <= 16 and vi.aw == 1280, "e2e: letterbox found (picture %dx%d)" % (vi.aw, vi.ah))
    ok(not vi.valid[:2].any(), "e2e: black intro skipped")
    ok([s["name"] for s in secs] == ["كوبليه 1", "الكورس 1", "كوبليه 2", "الكورس الأخير"], "e2e: sections")
    # review edits: nudge + text edit
    pr.lines[3].shift(0.1)
    pr.lines[3].shift(-0.1)
    for s in secs:
        s["checked"] = s["name"] in ("الكورس 1", "الكورس الأخير")
    secs[1]["hook"] = "لو قلبك *سألك*… هتقوله إيه؟"
    res = pr.render(secs)
    ok(len(res) == 2, "e2e: rendered 2 reels")
    for r in res:
        name = os.path.basename(r["mp4"])
        ok(re.fullmatch(r"أغنية تجربة - .+ \(\dm\d\d-\dm\d\d\)\.mp4", name) is not None, "e2e: file name '%s'" % name)
        ok(os.path.dirname(r["mp4"]) == os.path.join(out, "أغنية تجربة"), "e2e: in output/<song>/")
        check_reel(r["mp4"], r["jpg"], r["end"] - r["start"], "auto-" + str(res.index(r)), d)
    summ = os.path.join(out, "أغنية تجربة", "reels.txt")
    with open(summ, encoding="utf-8") as fh:
        txt = fh.read()
    ok("الكورس الأخير" in txt and "لو قلبك" in txt, "e2e: reels.txt lists sections + hooks")
    # timed file reused next time (no ASR)
    calls.clear()
    pr2 = A.Project(song, video, lyrics, song="أغنية تجربة", out_root=out, log=log, asr=asr, bg_only=True)
    pr2.analyze(reuse_timed=True)
    ok(not calls and len(pr2.lines) == len(TEXTS), "e2e: saved timing reused without listening again")
    # background-only mode (existing loop/centre-crop path)
    for s in pr2.sections:
        s["checked"] = s["name"] == "كوبليه 2"
    res2 = pr2.render()
    ok(len(res2) == 1, "e2e: background-only reel")
    check_reel(res2[0]["mp4"], res2[0]["jpg"], res2[0]["end"] - res2[0]["start"], "bgonly", d)
    return song


def test_broll(d):
    broll = os.path.join(d, "خلفية طويلة.mp4")
    make_broll(broll)
    vi = A.VideoInfo(broll)
    ok(not vi.has_audio and abs(vi.dur - 60) < 0.5, "b-roll: 60 s, no audio")
    used = []
    picks = []
    for _ in range(3):
        s = A.pick_window(vi, 15.0, used)
        used.append((s, s + 15.0))
        picks.append(s)
    ok(all(not (a < 4 or (a < 34 and a + 15 > 28)) for a in picks[:2]),
       "b-roll: picks avoid black parts %s" % ["%.1f" % p for p in picks])
    ok(abs(picks[0] - picks[1]) >= 15, "b-roll: first two picks don't reuse footage")
    clip = os.path.join(d, "قص.mp4")
    share = A.make_clip(vi, picks[0], 4.0, clip)
    dur, has_v, _ = engine.probe(clip)
    info = engine.run_ffmpeg(["-i", clip]).stderr.decode("utf-8", "replace")
    ok(has_v and "1080x1920" in info and abs(dur - 4.0) < 0.2, "b-roll: vertical clip 1080x1920 (faces %.0f%%)" % (share * 100))


def test_real_asr(d, name, song):
    print("---- real faster-whisper (%s) ----" % name, flush=True)
    import faster_whisper  # noqa: F401  (bundle check)
    import ctranslate2
    print("     ctranslate2", ctranslate2.__version__, flush=True)
    A.ensure_model(name, log=lambda m: print("     " + m, flush=True))
    ok(A.model_ready(name), "model %s downloaded into %s" % (name, A.model_dir(name)))
    audio = A.decode(song)[: A.SR * 20]
    words = A.transcribe(audio, name, "قلبي اختارك")
    ok(isinstance(words, list), "faster-whisper ran on the bundle (%d words, model path %s)"
       % (len(words), "short/ascii" if A._ascii_path(A.model_dir(name)) else "from memory"))
    # an Arabic folder without 8.3 short names: the model is handed over from memory
    real_ascii = A._ascii_path
    A._ascii_path = lambda p: None
    try:
        words = A.transcribe(audio[: A.SR * 8], name)
    finally:
        A._ascii_path = real_ascii
    ok(isinstance(words, list), "faster-whisper loads the model from memory too")


def write_gui_test(d, gt):
    """Inputs for the GUI run in CI: reelmaker.py --autotest-auto <d>/gui-autotest.json"""
    words = os.path.join(d, "asr-mock.json")
    with open(words, "w", encoding="utf-8") as fh:
        json.dump(mock_asr(gt), fh, ensure_ascii=False)
    shots = os.path.join(d, "shots")
    os.makedirs(shots, exist_ok=True)
    cfg = dict(audio=os.path.join(d, "أغنية تجربة.wav"), video=os.path.join(d, "الكليب الرسمي.mp4"),
               lyrics=os.path.join(d, "كلمات الأغنية.txt"), song="أغنية تجربة", asr_words=words,
               section="الكورس 1", report=os.path.join(d, "gui-report.json"), shots=shots)
    with open(os.path.join(d, "gui-autotest.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=1)


def gui_only(d):
    gt, total = ground_truth()
    song = os.path.join(d, "أغنية تجربة.wav")
    video = os.path.join(d, "الكليب الرسمي.mp4")
    os.makedirs(d, exist_ok=True)
    make_song(gt, total, song)
    make_video(song, total, video)
    with open(os.path.join(d, "كلمات الأغنية.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(TEXTS) + "\n")
    write_gui_test(d, gt)


def main():
    args = sys.argv[1:]
    real = None
    if "--real-asr" in args:
        k = args.index("--real-asr")
        real = args[k + 1]
        del args[k:k + 2]
    gui = "--gui-only" in args
    if gui:
        args.remove("--gui-only")
    d = os.path.abspath(args[0] if args else "selftest-auto-عربي")
    os.makedirs(d, exist_ok=True)
    if gui:
        return gui_only(d)
    test_normalize()
    lines, total = test_align()
    test_timed_roundtrip(lines, d)
    test_sections(lines, total)
    test_face_math()
    import cv2
    ok(not A.cascade().empty(), "OpenCV %s face detector loads" % cv2.__version__)
    blank = np.zeros((270, 480), np.uint8)
    ok(A.face_centres([blank]) == [None], "face detector: empty frame -> no face")
    gt, total = ground_truth()
    song = test_end_to_end(d, gt, total)
    test_broll(d)
    if real:
        test_real_asr(d, real, song)
    write_gui_test(d, gt)
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
