"""Self-test: builds sample oud-like tracks (Arabic file names, WAV + MP3, different levels),
a still and a short video background, renders two long videos and checks every output.

    python selftest.py <work_dir>
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import engine
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageStat, features

TRACKS = [("١ - ليل القاهرة - Cairo Night.wav", 22, 146.83, 0.5),
          ("نسمة - Breeze.mp3", 19, 164.81, 0.15),
          ("03_سهرة.wav", 17, 130.81, 0.9)]


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def ffok(args, what):
    r = engine.run_ffmpeg(["-y"] + args)
    ok(r.returncode == 0, what + ("" if r.returncode == 0 else r.stderr.decode("utf-8", "replace")[-500:]))


def make_assets(d):
    os.makedirs(d, exist_ok=True)
    paths = []
    for name, dur, root, amp in TRACKS:
        p = os.path.join(d, name)
        # plucked notes on a maqam-ish scale, decaying like a struck string
        step = "floor(t*2.2)"
        deg = "mod(%s*5+floor(%s/3),7)" % (step, step)
        semi = "(if(lt(%s,1),0,if(lt(%s,2),1.5,if(lt(%s,3),3.5,if(lt(%s,4),5,if(lt(%s,5),7,"\
               "if(lt(%s,6),8,10)))))))" % ((deg,) * 6)
        f = "%.2f*pow(2,%s/12)" % (root, semi)
        env = "exp(-4.5*(t-%s/2.2))" % step
        expr = ("%.2f*%s*(sin(2*PI*%s*t)+0.45*sin(4*PI*%s*t)+0.25*sin(6*PI*%s*t))"
                % (amp * 0.5, env, f, f, f))
        codec = ["-c:a", "libmp3lame", "-b:a", "192k"] if p.endswith(".mp3") else ["-c:a", "pcm_s16le"]
        ffok(["-f", "lavfi", "-i", "aevalsrc='%s|%s':s=44100:d=%d" % (expr, expr, dur),
              "-af", "aecho=0.8:0.5:60|120:0.3|0.2"] + codec + [p], "sample track " + name)
        paths.append(p)
    img = os.path.join(d, "خلفية عود.jpg")
    im = Image.new("RGB", (2400, 1600))
    dr = ImageDraw.Draw(im)
    for y in range(1600):
        dr.line((0, y, 2400, y), fill=(int(40 + 40 * y / 1600), int(26 + 20 * y / 1600), int(18 + 8 * y / 1600)))
    dr.ellipse((1500, 250, 1900, 650), fill=(224, 164, 88))
    for i in range(9):
        dr.rectangle((150 + i * 230, 900 - i * 37, 330 + i * 230, 1600), fill=(18 + i * 3, 14 + i * 2, 12))
    im.filter(ImageFilter.GaussianBlur(3)).save(img, quality=90)
    vid = os.path.join(d, "فيديو مطر.mp4")
    ffok(["-f", "lavfi", "-i", "gradients=s=1280x720:d=6:speed=0.03:c0=0x2a1a10:c1=0xc08a50:c2=0x101830:n=3:r=30",
          "-f", "lavfi", "-i", "testsrc2=s=1280x720:d=6:r=30", "-filter_complex",
          "[1]format=yuv420p[t];[0]format=yuv420p[g];[g][t]blend=all_mode=overlay:all_opacity=0.3,format=yuv420p",
          "-t", "6", "-c:v", "libx264", vid], "sample background video")
    return paths, img, vid


def info(path):
    r = engine.run_ffmpeg(["-i", path])
    return r.stderr.decode("utf-8", "replace")


def frame(path, t, png):
    r = engine.run_ffmpeg(["-y", "-ss", "%.3f" % t, "-i", path, "-frames:v", "1", png])
    ok(r.returncode == 0 and os.path.isfile(png), "frame at %.2fs" % t)
    return Image.open(png).convert("RGB")


def corner_energy(im, corner):
    w, h = im.size
    box = {"bottom-left": (40, h - 200, 700, h - 30), "bottom-right": (w - 700, h - 200, w - 40, h - 30)}[corner]
    edges = im.crop(box).convert("L").filter(ImageFilter.FIND_EDGES)
    return ImageStat.Stat(edges).mean[0]


def check_job(job, out, d, tag, corner):
    mp4, txt, jpg = out
    for p in out:
        ok(os.path.isfile(p) and os.path.getsize(p) > 50, "%s: %s" % (tag, os.path.basename(p)))
    expect = sum(t.dur for t in job.tracks) - engine.XFADE * (len(job.tracks) - 1)
    dur, has_v, has_a = engine.probe(mp4)
    ok(has_v and has_a, "%s: video + audio" % tag)
    ok(abs(dur - expect) < 0.15, "%s: duration %.2f (expected %.2f)" % (tag, dur, expect))
    s = info(mp4)
    ok("h264" in s and "1920x1080" in s and "25 fps" in s, "%s: h264 1920x1080 25fps" % tag)
    m = re.search(r"Audio: aac.*?48000 Hz, stereo.*?(\d+) kb/s", s)
    ok(m and int(m.group(1)) >= 250, "%s: aac 48k stereo %s kb/s" % (tag, m.group(1) if m else "?"))
    lufs, tp = job.loudness(mp4)
    ok(abs(lufs - engine.TARGET_LUFS) < 1.0, "%s: loudness %.1f LUFS" % (tag, lufs))
    ok(tp < -0.5, "%s: true peak %.1f dBTP" % (tag, tp))
    for t in job.tracks:
        ok(abs(t.lufs_out - engine.TARGET_LUFS) < 0.5, "%s: track %.1f → %.1f LUFS" % (tag, t.lufs_in, t.lufs_out))
    # description
    lines = open(txt, encoding="utf-8").read().splitlines()
    stamps = [l for l in lines if re.match(r"^\d{1,2}:\d{2}(:\d{2})? ", l)]
    ok(len(stamps) == len(job.tracks), "%s: %d timestamps" % (tag, len(stamps)))
    ok(stamps[0].startswith("00:00 "), "%s: first timestamp 00:00" % tag)
    for st, s0 in zip(stamps[1:], job.starts[1:]):
        mm, ss = map(int, st.split()[0].split(":"))
        ok(abs(mm * 60 + ss - (s0 + 1.5)) <= 1, "%s: %s" % (tag, st))
    print("     " + "\n     ".join(lines), flush=True)
    th = Image.open(jpg)
    ok(th.size == (1280, 720), "%s: thumbnail 1280x720" % tag)
    # titles: present mid-track, gone during the swap
    for i, s0 in enumerate(job.starts):
        a = 3.5 if i == 0 else s0 + 4.0
        im = frame(mp4, a, os.path.join(d, "%s_title%d.png" % (tag, i + 1)))
        ok(corner_energy(im, corner) > 2.5, "%s: title %d visible in %s" % (tag, i + 1, corner))
    if len(job.starts) > 1:
        im = frame(mp4, job.starts[1] + 1.2, os.path.join(d, "%s_swap.png" % tag))
        ok(corner_energy(im, corner) < corner_energy(frame(mp4, job.starts[1] + 4.0,
                                                           os.path.join(d, "%s_t2.png" % tag)), corner),
           "%s: title fades during the crossfade" % tag)
    return mp4


def continuity(mp4, d, tag, times):
    """Neighbouring frames around chunk joins / loop seams must not jump."""
    fr = os.path.join(d, tag + "_seq")
    os.makedirs(fr, exist_ok=True)
    for t in times:
        r = engine.run_ffmpeg(["-y", "-ss", "%.3f" % (t - 0.12), "-i", mp4, "-frames:v", "6",
                               "-vf", "scale=480:270", os.path.join(fr, "f%07.2f_%%d.png" % t)])
        ims = [Image.open(os.path.join(fr, "f%07.2f_%d.png" % (t, k))).convert("L") for k in range(1, 7)]
        diffs = [ImageStat.Stat(ImageChops.difference(a, b)).mean[0] for a, b in zip(ims, ims[1:])]
        ok(max(diffs) < 3 * (sorted(diffs)[1] + 1.0), "%s: smooth at %.2fs (%s)" % (
            tag, t, " ".join("%.1f" % x for x in diffs)))


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    ok(features.check("raqm"), "RAQM layout available (fribidi %s)" % features.version("fribidi"))
    tracks, img, vid = make_assets(d)
    out = os.path.join(d, "output")
    log = lambda m: print("     " + m, flush=True)
    names = [("ليل القاهرة", "Cairo Night"), ("نسمة", "Breeze"), ("سهرة", "")]

    job = engine.Job([engine.Track(p, *n) for p, n in zip(tracks, names)], img,
                     mix_ar="عود وليل", mix_en="Oud & Night · Lo-Fi", corner="bottom-left",
                     texture="vinyl", texture_vol=45, footer="#oud #lofi", out_dir=out, log=log)
    res = check_job(job, job.run(), d, "image", "bottom-left")

    job2 = engine.Job([engine.Track(p) for p in reversed(tracks)], vid, mix_ar="مطر", mix_en="",
                      corner="bottom-right", texture="rain", texture_vol=60, out_dir=out, log=log)
    out2 = job2.run()
    res2, res2_txt = check_job(job2, out2, d, "video", "bottom-right"), out2[1]
    ok(job2.tracks[0].path == tracks[-1], "track order follows the list")
    lf = int((6 - 0.15) * engine.FPS) - int(1.5 * engine.FPS)
    seams = [k * lf / float(engine.FPS) for k in range(1, int(job2.total * engine.FPS / lf) + 1)]
    joins = [a / float(engine.FPS) for a, _ in job2.chunks[1:]]
    ok(len(joins) >= 1, "video rendered in %d parallel chunks" % len(job2.chunks))
    continuity(res2, d, "video", [s for s in seams if s < job2.total - 1][:3] + joins)
    ok("00:00 سهرة" in open(res2_txt, encoding="utf-8").read(), "titles from file names")

    job3 = engine.Job([engine.Track(tracks[1], "نسمة", "Breeze")], vid, texture="file",
                      texture_file=tracks[2], texture_vol=30, out_dir=out, log=log)
    mp4, txt, jpg = job3.run()
    ok(abs(engine.probe(mp4)[0] - job3.tracks[0].dur) < 0.15, "single track + custom texture file")
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
