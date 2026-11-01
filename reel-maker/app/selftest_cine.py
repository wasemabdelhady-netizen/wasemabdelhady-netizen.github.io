"""Cinematic mode self-test: synthetic song + footage with a face -> one reel per style.

    python selftest_cine.py <work_dir> [--quick] [--align] [--gui-only]

--align: run the real ONNX alignment model (downloads it once); otherwise rough times only.
--gui-only: build the inputs + gui-autotest.json for START.bat --autotest-cine and stop.

Builds a 60 s "master" (beat + pad + voice-like bursts on the lyric lines), footage that
starts 12.30 s into it (camera audio = master + room noise) showing a face that moves,
lyrics + .srt. Checks: auto-sync within 40 ms, words.json, one reel per style A-D,
an IG/FB version, versioning (v2 + _to_delete), thumbnails, preview, Arabic paths.
"""
import json
import os
import subprocess
import sys
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autoreel  # noqa: E402
import cinematic  # noqa: E402
import engine  # noqa: E402

SR = 44100
BPM = 100.0
OFFSET = 12.30
LINES = ["بقولك ايه كفاية كلام", "مفيش ولا كلمة هتداوي", "باين في عنيك كلامك كله *كدب*",
         "وعلى الفراق ناوي", "جرحتك؟", "كنت فين وانا قلبي بيبكي", "من الفراق دلوقتي بتشكي",
         "بس قلبي حبيبي خلاص اكتفى"]
TIMES = [(14.0, 17.6), (18.0, 21.4), (22.0, 26.8), (27.2, 29.6), (31.0, 32.0), (32.6, 36.0),
         (36.4, 39.8), (40.2, 44.6)]


def write_wav(path, x, sr=SR):
    x = np.clip(x, -1, 1)
    pcm = (x * 32767).astype("<i2")
    if pcm.ndim == 1:
        pcm = np.stack([pcm, pcm], axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def make_audio(dur=60.0):
    rng = np.random.default_rng(1)
    t = np.arange(int(dur * SR)) / SR
    x = np.zeros_like(t)
    beat = 60.0 / BPM
    for k in range(int(dur / beat)):
        a = int(k * beat * SR)
        n = int(0.18 * SR)
        tt = np.arange(n) / SR
        kick = np.sin(2 * np.pi * (55 + 90 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 14) * (1.0 if k % 4 == 0 else 0.6)
        x[a:a + n] += kick[:len(x[a:a + n])] * 0.6
        if k % 2 == 1:
            hn = int(0.05 * SR)
            x[a:a + hn] += rng.normal(0, 0.12, hn)[:len(x[a:a + hn])] * np.exp(-np.arange(hn) / SR * 60)[:len(x[a:a + hn])]
    x += 0.05 * np.sin(2 * np.pi * 110 * t) * (1 + 0.5 * np.sin(2 * np.pi * 0.25 * t))
    vocal = np.zeros_like(t)
    for (s, e), text in zip(TIMES, LINES):
        words = text.split()
        step = (e - s) / len(words)
        for i in range(len(words)):
            a, b = int((s + i * step) * SR), int((s + (i + 0.85) * step) * SR)
            tt = np.arange(b - a) / SR
            f0 = 220 + 40 * np.sin(2 * np.pi * 1.3 * tt + i)
            v = sum(np.sin(2 * np.pi * f0 * h * tt) / h for h in (1, 2, 3, 4))
            vocal[a:b] += 0.25 * v * np.minimum(1, np.minimum(tt / 0.03, (tt[-1] - tt) / 0.05 + 1e-3))
    return x * 0.7 + vocal, vocal


def make_footage(path, master, dur=40.0, size=(1920, 1080)):
    """Moving face (skimage astronaut, or a drawn one) + camera audio = master from OFFSET."""
    from PIL import Image, ImageDraw
    try:
        from skimage import data
        face = Image.fromarray(data.astronaut()).crop((130, 0, 380, 300))
    except Exception:
        face = Image.new("RGB", (250, 300), (200, 160, 140))
        d = ImageDraw.Draw(face)
        d.ellipse((60, 90, 110, 130), fill=(40, 30, 30))
        d.ellipse((140, 90, 190, 130), fill=(40, 30, 30))
    W, H = size
    face = face.resize((int(250 * 2.6), int(300 * 2.6)))
    wav = path + ".cam.wav"
    seg = master[int(OFFSET * SR):int((OFFSET + dur) * SR)]
    seg = seg * 0.6 + np.random.default_rng(2).normal(0, 0.01, len(seg))
    write_wav(wav, seg)
    p = subprocess.Popen([engine.ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-r", "30", "-i", "pipe:0",
                          "-i", wav, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-pix_fmt", "yuv420p",
                          "-c:a", "aac", "-shortest", path], stdin=subprocess.PIPE)
    bg = np.zeros((H, W, 3), np.uint8)
    yy, xx = np.mgrid[0:H, 0:W]
    bg[..., 0] = (40 + 30 * xx / W).astype(np.uint8)
    bg[..., 1] = (55 + 20 * yy / H).astype(np.uint8)
    bg[..., 2] = 70
    for i in range(int(dur * 30)):
        t = i / 30.0
        img = Image.fromarray(bg.copy())
        x = int(W / 2 - face.width / 2 + 260 * np.sin(t * 0.4))
        y = int(H * 0.18 + 30 * np.sin(t * 0.9))
        img.paste(face, (x, y))
        d = ImageDraw.Draw(img)
        d.ellipse((x + 230, y + 330, x + 300, y + 370), fill=(235, 240, 255))  # a glasses glint
        d.text((40, 40), "%.2f" % t, fill=(255, 255, 255))
        p.stdin.write(img.tobytes())
    p.stdin.close()
    p.wait()
    os.remove(wav)


def mock_rough(texts):
    # deliberately 1.5 s early, like Wasem's old .srt
    return [autoreel.Line(t, s - 1.5, e - 1.5) for t, (s, e) in zip(texts, TIMES)]


def main():
    work = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-cine")
    quick = "--quick" in sys.argv
    song_dir = os.path.join(work, "الأغاني - Songs", "بقولك ايه - Ba2olak Eh")
    fdir = os.path.join(song_dir, "لقطات - Footage")
    os.makedirs(fdir, exist_ok=True)
    master_p = os.path.join(song_dir, "ماستر.wav")
    stem_p = os.path.join(song_dir, "فوكال.wav")
    foot_p = os.path.join(fdir, "LNMN0870.mp4")
    lyr_p = os.path.join(song_dir, "كلمات.txt")
    x, vocal = make_audio()
    write_wav(master_p, x)
    write_wav(stem_p, vocal)
    with open(lyr_p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(LINES) + "\n")
    if not os.path.isfile(foot_p):
        make_footage(foot_p, x)
    if "--gui-only" in sys.argv:
        # the GUI has no rough-timing mock: give it an .srt (1.5 s early, like Wasem's)
        srt_p = os.path.join(song_dir, "كلمات.srt")
        with open(srt_p, "w", encoding="utf-8") as fh:
            for i, ((s, e), t) in enumerate(zip(TIMES, LINES)):
                ts = lambda x: "%02d:%02d:%02d,%03d" % (x // 3600, x % 3600 // 60, x % 60, (x % 1) * 1000)
                fh.write("%d\n%s --> %s\n%s\n\n" % (i + 1, ts(s - 1.5), ts(e - 1.5), t))
        with open(os.path.join(work, "gui-autotest.json"), "w", encoding="utf-8") as fh:
            json.dump(dict(footage=foot_p, master=master_p, stem=stem_p, lyrics=lyr_p, srt=srt_p,
                           out=os.path.join(work, "الريلز - واجهة"), shots=work,
                           report=os.path.join(work, "gui-report.json"), seconds=5, green=0.92, pack="كتابة"), fh, ensure_ascii=False)
        print("gui inputs ready")
        return
    logs = []
    p = cinematic.Project(foot_p, master_p, lyr_p, stem=stem_p, n_reels=4, ig=False, deglare=True,
                          log=lambda m: (logs.append(m), print(m, flush=True)), asr=mock_rough)
    if "--align" not in sys.argv:
        import cine_align
        cine_align.ensure_model = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("model skipped in selftest"))
    secs = p.analyze()
    rep = dict(offset=p.offset, conf=p.conf, sections=secs)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    assert abs(p.offset - OFFSET) < 0.04, "sync off: %.3f" % p.offset
    assert os.path.isfile(p.words_path)
    assert len(secs) >= 2, "no reels suggested"
    assert p.out_dir.endswith("الريلز - Reels"), p.out_dir
    for i, s in enumerate(secs):
        s["style"] = "ABCD"[i % 4]
    if quick:
        for s in secs:
            s["end"] = min(s["end"], s["start"] + 6.0)
    # preview
    pr = p.render(secs[:1], preview=True)
    assert os.path.getsize(pr[0]["mp4"]) > 10000
    res = p.render(secs)
    for r in res:
        assert os.path.getsize(r["mp4"]) > 50000 and os.path.isfile(r["jpg"]), r
    # IG version + versioning (render reel 1 again -> v2, first goes to _to_delete)
    p.ig = True
    res2 = p.render(secs[:1])
    names = [os.path.basename(r["mp4"]) for r in res2]
    assert any(" v2.mp4" in n for n in names), names
    assert any("IG-FB" in n for n in names), names
    assert os.path.isdir(os.path.join(p.out_dir, "_to_delete"))
    # frames for eyeballing
    shots = os.path.join(work, "frames")
    os.makedirs(shots, exist_ok=True)
    for r in res + res2:
        if not os.path.isfile(r["mp4"]):  # an earlier version now in _to_delete
            continue
        d = engine.probe(r["mp4"])[0]
        for q in (0.03, 0.3, 0.55, 0.8):
            out = os.path.join(shots, "%s_%02d.jpg" % (engine.safe_name(os.path.basename(r["mp4"])[:-4]), int(q * 100)))
            engine.run_ffmpeg(["-y", "-ss", "%.2f" % (d * q), "-i", r["mp4"], "-frames:v", "1", "-q:v", "3", out])
    with open(os.path.join(work, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(dict(rep, reels=[r["mp4"] for r in res + res2]), fh, ensure_ascii=False, indent=1)
    print("OK", len(res) + len(res2), "files")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
