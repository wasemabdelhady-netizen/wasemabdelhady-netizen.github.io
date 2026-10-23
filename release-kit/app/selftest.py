"""Self-test: builds a sample cover (with transparency), a short clip and a song with a
known chorus, all with Arabic file names, renders the full kit twice and checks it.

    python selftest.py <work_dir>
"""
import io
import os
import re
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import numpy as np
import engine
from PIL import Image, ImageDraw, features

SR = 44100
# intro 0-6, verse 6-24, chorus 24-44, verse 44-60, chorus 60-80, outro 80-86
CHORUS = [(24, 44), (60, 80)]
SONG_LEN = 86


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def tone(freqs, dur, amp, bright=1.0):
    t = np.arange(int(dur * SR)) / SR
    y = np.zeros_like(t)
    for f in freqs:
        y += np.sin(2 * np.pi * f * t) + 0.35 * bright * np.sin(4 * np.pi * f * t) + 0.15 * bright * np.sin(6 * np.pi * f * t)
    env = np.minimum(1, t / 0.02) * np.minimum(1, (dur - t) / 0.08)
    return amp * y * env / max(1, len(freqs))


def make_song(path):
    rng = np.random.default_rng(3)
    A = 220.0
    chords = [[A, A * 1.189, A * 1.498], [174.6, 220, 261.6], [261.6, 329.6, 392], [196, 246.9, 293.7]]
    parts = []
    t = 0.0

    def kick(n_beats, amp):
        out = []
        for _ in range(n_beats):
            tt = np.arange(int(0.5 * SR)) / SR
            out.append(amp * np.sin(2 * np.pi * (50 + 80 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 9))
        return np.concatenate(out)

    sections = [("intro", 6), ("verse", 18), ("chorus", 20), ("verse", 16), ("chorus", 20), ("outro", 6)]
    for name, length in sections:
        if name == "chorus":
            seg = np.concatenate([tone(chords[i % 4], 2.0, 0.55, 1.4) for i in range(int(length / 2))])
            seg = seg + kick(int(length * 2), 0.5)
        elif name == "verse":
            notes = A * 2 ** (rng.integers(-5, 8, int(length * 2)) / 12.0)
            seg = np.concatenate([tone([f], 0.5, 0.22, 0.6) for f in notes])
            seg = seg + kick(int(length * 2), 0.18)
        else:
            seg = tone([110, 165], length, 0.08, 0.2)
        parts.append(seg[: int(length * SR)])
        t += length
    y = np.concatenate(parts)
    y = y / np.max(np.abs(y)) * 0.85
    st = np.stack([y + rng.normal(0, 0.006, len(y)), y + rng.normal(0, 0.006, len(y))], axis=1)
    pcm = (np.clip(st, -1, 1) * 32767).astype("<i2")
    with open(path, "wb") as fh:  # wave + an open file object: fine with Arabic paths
        with wave.open(fh, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(2)
            w.setframerate(SR)
            w.writeframes(pcm.tobytes())


def make_cover(path):
    s = 2400
    yy, xx = np.mgrid[0:s, 0:s] / float(s)
    r = (40 + 160 * xx * (1 - yy)).astype(np.uint8)
    g = (20 + 90 * yy).astype(np.uint8)
    b = (60 + 120 * (1 - xx)).astype(np.uint8)
    im = Image.fromarray(np.dstack([r, g, b]), "RGB").convert("RGBA")
    d = ImageDraw.Draw(im)
    d.ellipse((600, 500, 1800, 1700), fill=(224, 164, 88, 255))
    d.rectangle((300, 1900, 2100, 2000), fill=(245, 239, 230, 255))
    a = np.array(im)
    a[:300, :300, 3] = 0  # transparent corner: must be flattened for DistroKid
    buf = io.BytesIO()
    Image.fromarray(a, "RGBA").save(buf, "PNG")
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())


def make_clip(path):
    r = engine.run_ffmpeg(["-y", "-f", "lavfi", "-i",
                           "gradients=s=1280x720:d=5:speed=0.03:c0=0x3a2a1a:c1=0xc08a50:c2=0x1a2030:n=3",
                           "-t", "5", "-c:v", "libx264", "-pix_fmt", "yuv420p", path])
    ok(r.returncode == 0, "sample clip" + ("" if r.returncode == 0 else r.stderr.decode("utf-8", "replace")[-400:]))


def info(path):
    return engine.run_ffmpeg(["-i", path]).stderr.decode("utf-8", "replace")


def frame(path, t, png=None):
    """Frame number round(t*30), picked exactly with select= (seeking can miss the last one)."""
    n = int(round(t * 30))
    r = engine.run_ffmpeg(["-i", path, "-vf", "select=eq(n\\,%d)" % n, "-frames:v", "1", "-f", "rawvideo",
                           "-pix_fmt", "rgb24", "-"])
    head = info(path)
    w, h = map(int, re.search(r"(\d{3,4})x(\d{3,4})", head.split("Video:")[1]).groups())
    ok(r.returncode == 0 and len(r.stdout) >= w * h * 3, "decode frame %d of %s" % (n, os.path.basename(path)))
    a = np.frombuffer(r.stdout[: w * h * 3], np.uint8).reshape(h, w, 3).astype(np.float32)
    if png:
        Image.fromarray(a.astype(np.uint8)).save(png)
    return a


def motion(path, f0):
    """Largest frame-to-frame change over the first 6 frames (slowed clips repeat frames)."""
    prev, best = f0, 0.0
    for i in range(1, 7):
        f = frame(path, i / 30.0)
        best = max(best, float(np.abs(f - prev).mean()))
        prev = f
    return best


def check_video(path, size, dur, audio, tag, d):
    ok(os.path.isfile(path), "%s exists" % tag)
    hdr = info(path)
    d_, has_v, has_a = engine.probe(path)
    ok(has_v, "%s: video stream" % tag)
    ok(has_a == audio, "%s: audio stream %s" % (tag, "present" if audio else "absent"))
    ok(abs(d_ - dur) < 0.12, "%s: duration %.2f (expected %.2f)" % (tag, d_, dur))
    ok("h264" in hdr and "%dx%d" % size in hdr and "30 fps" in hdr, "%s: h264 %dx%d 30fps" % ((tag,) + size))
    if audio:
        ok("aac" in hdr, "%s: aac" % tag)


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    os.makedirs(d, exist_ok=True)
    ok(features.check("raqm"), "RAQM layout available (fribidi %s)" % features.version("fribidi"))
    cover = os.path.join(d, "غلاف تجربة.png")
    song = os.path.join(d, "ماستر تجربة.wav")
    clip = os.path.join(d, "كليب قصير.mp4")
    make_cover(cover)
    make_song(song)
    make_clip(clip)
    out = os.path.join(d, "output")
    log = lambda m: print("     " + m, flush=True)

    # 1. the full kit, Canvas from the cover
    job = engine.Job(cover, song, "أغنية التجربة", "Test Song", "Wasem Abdelhady",
                     out_dir=out, log=log)
    kit, files = job.run()
    ok(os.path.basename(kit) == "Test Song - Release Kit", "kit folder name: %s" % os.path.basename(kit))
    ok(len(files) == 9, "9 files written")
    sizes = {"cover": (3000, 3000), "thumb": (1280, 720), "story": (1080, 1920), "reelcover": (1080, 1920),
             "post": (1080, 1080), "portrait": (1080, 1350)}
    for k, sz in sizes.items():
        p = job.path(k)
        im = Image.open(p)
        ok(im.format == "JPEG" and im.mode == "RGB" and im.size == sz, "%s: JPEG RGB %dx%d" % ((k,) + sz))
    cv = Image.open(job.path("cover"))
    ok(os.path.getsize(job.path("cover")) < 10_000_000, "cover under 10 MB (%.1f MB)" % (os.path.getsize(job.path("cover")) / 1e6))
    corner = cv.getpixel((60, 60))
    ok(max(corner) < 12, "transparent corner flattened to black %s" % (corner,))
    ok("A" not in cv.getbands(), "cover has no alpha")

    canvas = job.path("canvas")
    check_video(canvas, (1080, 1920), 8.0, False, "canvas(cover)", d)
    f0 = frame(canvas, 0, os.path.join(d, "canvas_cover_0s.png"))
    f4 = frame(canvas, 4.0, os.path.join(d, "canvas_cover_4s.png"))
    fl = frame(canvas, 8.0 - 1.0 / 30)
    seam = np.abs(fl - f0).mean()
    step = motion(canvas, f0)
    ok(np.abs(f4 - f0).mean() > 1.0, "canvas(cover) animates (mid vs start %.2f)" % np.abs(f4 - f0).mean())
    ok(seam < step * 2.0 + 0.5, "canvas(cover) seamless loop (last->first %.2f, one step %.2f)" % (seam, step))

    viz = job.path("visualizer")
    check_video(viz, (1920, 1080), SONG_LEN, True, "visualizer", d)
    aline = info(viz).split("Audio:")[1].split("\n")[0]
    kb = int(re.search(r"(\d+) kb/s", aline).group(1))
    ok("stereo" in aline and kb >= 250, "visualizer: AAC stereo ~320k (%d kb/s measured)" % kb)
    quiet = frame(viz, 3.0, os.path.join(d, "visualizer_intro.png"))
    loud = frame(viz, 30.0, os.path.join(d, "visualizer_chorus.png"))
    frame(viz, 50.0, os.path.join(d, "visualizer_verse.png"))
    # bars live in the right column under the title
    region = (slice(380, 1000), slice(900, 1780))
    ok(loud[region].mean() > quiet[region].mean() + 1.5,
       "visualizer bars react (chorus %.1f vs intro %.1f)" % (loud[region].mean(), quiet[region].mean()))

    teaser = job.path("teaser")
    check_video(teaser, (1080, 1920), 20.0, True, "teaser", d)
    st = job.teaser_start
    ok(any(a - 3 <= st <= a + 3 for a, _ in CHORUS), "teaser starts at the chorus (%.2f s; chorus at %s)" % (st, [a for a, _ in CHORUS]))
    frame(teaser, 6.0, os.path.join(d, "teaser_6s.png"))

    # 2. Canvas from a short clip (slowed + crossfade loop), Arabic-only title
    job2 = engine.Job(cover, "", "أغنية بالعربي بس", "", "وسيم عبد الهادي", clip=clip,
                      parts=["canvas"], out_dir=out, log=log)
    kit2, files2 = job2.run()
    ok(os.path.basename(kit2) == "أغنية بالعربي بس - Release Kit", "Arabic kit folder: %s" % os.path.basename(kit2))
    c2 = job2.path("canvas")
    check_video(c2, (1080, 1920), 8.0, False, "canvas(clip)", d)
    g0 = frame(c2, 0, os.path.join(d, "canvas_clip_0s.png"))
    gl = frame(c2, 8.0 - 1.0 / 30)
    seam2, step2 = np.abs(gl - g0).mean(), motion(c2, g0)
    ok(seam2 < step2 * 2.0 + 0.5, "canvas(clip) seamless loop (last->first %.2f, one step %.2f)" % (seam2, step2))

    # 3. the stills with an Arabic-only title render too
    job3 = engine.Job(cover, "", "أغنية بالعربي بس", "", "وسيم عبد الهادي", parts=["images"], out_dir=out, log=log)
    job3.run()
    ok(os.path.isfile(job3.path("thumb")), "Arabic-only thumbnail")
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
