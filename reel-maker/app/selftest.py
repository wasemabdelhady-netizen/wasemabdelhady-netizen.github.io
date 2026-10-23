"""Self-test: builds sample media with Arabic file names, renders two reels
(auto timing + timed lyrics) and checks the outputs.

    python selftest.py <work_dir>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import engine
from PIL import Image, features

GATES = [(0.8, 3.6), (4.2, 7.5), (8.3, 10.1), (10.6, 13.9), (14.8, 18.2)]
LYRICS = ["قلبي اختارك من أول نظرة",
          "والليل بيحكي عن *عيونك* لوحده",
          "مش هقدر أبعد",
          "كل الطرق بتوديني ليك يا حبيبي",
          "خليك جنبي 🌙"]


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def make_assets(d):
    os.makedirs(d, exist_ok=True)
    bg = os.path.join(d, "خلفية قصيرة.mp4")
    audio = os.path.join(d, "صوت تجربة.mp3")
    r = engine.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                           "gradients=s=1280x720:d=4:speed=0.02:c0=0x3a2a1a:c1=0xc08a50:c2=0x1a2030:n=3",
                           "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=4",
                           "-filter_complex", "[1]format=yuv420p,colorchannelmixer=aa=0.25[t];"
                           "[0]format=yuv420p[g];[g][t]blend=all_mode=overlay:all_opacity=0.35,format=yuv420p",
                           "-t", "4", "-c:v", "libx264", bg])
    ok(r.returncode == 0, "sample background" + ("" if r.returncode == 0 else r.stderr.decode("utf-8", "replace")[-400:]))
    gate = "+".join("between(t\\,%s\\,%s)" % g for g in GATES)
    voice = ("0.35*(%s)*(sin(2*PI*(220+8*sin(2*PI*5*t))*t)+0.5*sin(4*PI*220*t)+0.3*sin(6*PI*220*t))"
             "*(0.6+0.4*abs(sin(2*PI*2.3*t)))" % gate)
    r = engine.run_ffmpeg(["-y", "-f", "lavfi", "-i", "aevalsrc=%s:s=44100:d=20" % voice,
                           "-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.02:d=20",
                           "-filter_complex", "[0][1]amix=inputs=2:normalize=0",
                           "-c:a", "libmp3lame", "-b:a", "192k", audio])
    ok(r.returncode == 0, "sample audio" + ("" if r.returncode == 0 else r.stderr.decode("utf-8", "replace")[-400:]))
    plain = os.path.join(d, "كلمات.txt")
    with open(plain, "w", encoding="utf-8") as f:
        f.write("\n".join(LYRICS) + "\n")
    timed = os.path.join(d, "كلمات بالوقت.txt")
    with open(timed, "w", encoding="utf-8") as f:
        for (a, _), line in zip(GATES, LYRICS):
            f.write("[%02d:%05.2f] %s\n" % (int(a // 60), a % 60, line))
    return bg, audio, plain, timed


def check_video(path, expect_dur, d, tag):
    dur, has_v, has_a = engine.probe(path)
    ok(has_v and has_a, "%s: video + audio streams" % tag)
    ok(abs(dur - expect_dur) < 0.25, "%s: duration %.2f (expected %.2f)" % (tag, dur, expect_dur))
    r = engine.run_ffmpeg(["-i", path])
    info = r.stderr.decode("utf-8", "replace")
    ok("h264" in info and "1080x1920" in info and "30 fps" in info, "%s: h264 1080x1920 30fps" % tag)
    ok("aac" in info, "%s: aac audio" % tag)
    for t in (1.0, 2.5, 6.0, 12.0, expect_dur - 0.6):
        png = os.path.join(d, "%s_%04.1f.png" % (tag, t))
        r = engine.run_ffmpeg(["-y", "-ss", str(t), "-i", path, "-frames:v", "1", png])
        ok(r.returncode == 0 and os.path.isfile(png), "%s: frame at %.1fs" % (tag, t))


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    ok(features.check("raqm"), "RAQM layout available (fribidi %s)" % features.version("fribidi"))
    bg, audio, plain, timed = make_assets(d)
    out = os.path.join(d, "output")
    log = lambda m: print("     " + m, flush=True)
    job = engine.Job(bg, audio, plain, song="أغنية التجربة", hook="لو قلبك سألك… هتقوله إيه؟",
                     out_dir=out, log=log)
    mp4, jpg = job.run()
    starts = [ts for ts, _, _ in job.lines]
    ok(len(starts) == len(GATES), "auto timing placed %d lines" % len(starts))
    for (g, _), s in zip(GATES, starts):
        ok(abs(g - s) < 0.5, "auto line at %.2f near vocal at %.2f" % (s, g))
    check_video(mp4, 20.0, d, "auto")
    im = Image.open(jpg)
    ok(im.size == (1080, 1920), "thumbnail 1080x1920")

    job2 = engine.Job(bg, audio, timed, song="أغنية التجربة", hook="", start=0.5, end=19.5,
                      out_dir=out, log=log)
    mp4b, _ = job2.run()
    ok(len(job2.lines) == 5, "timed lyrics: %d lines" % len(job2.lines))
    check_video(mp4b, 19.0, d, "timed")
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
