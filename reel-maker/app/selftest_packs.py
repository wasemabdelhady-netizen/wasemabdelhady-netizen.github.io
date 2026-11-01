"""Style packs / fonts / word animations self-test (runs on the Windows bundle in CI).

    python selftest_packs.py <out_dir>

- every bundled font: Arabic letters, joining forms, marks (textfx.arabic_ok), listed by name
- every pack + every animation: a line rendered through its whole life; the layout
  (canvas box, word boxes) never changes; each word is fully lit before the next starts
- the lyric mode (engine.Job) renders a short reel with a pack
- a contact sheet of all packs is written for eyeballing
"""
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import engine  # noqa: E402
import textfx  # noqa: E402

LINE = "بقولك ايه *كفاية* كلام مِنْ قَلبِي"


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-packs")
    os.makedirs(out, exist_ok=True)
    fams = textfx.families()
    print("fonts:", ", ".join(fams))
    want = ["Cairo", "Aref Ruqaa", "Tajawal", "Amiri", "Reem Kufi", "Lalezar", "Marhey", "Rakkas",
            "El Messiri", "Changa", "Lemonada", "Mada", "Alexandria"]
    missing = [f for f in want if f not in fams]
    assert not missing, "fonts missing / failed the Arabic check: %s" % missing
    for p in textfx.builtin_packs():
        assert p["font"] in fams and p["gold_font"] in fams, p
    n = len(LINE.split())
    times = [(0.3 + i * 0.42, 0.3 + i * 0.42 + 0.38) for i in range(n)]
    packs = textfx.builtin_packs() + [dict(textfx.default_pack(), name=a, anim=a) for a in textfx.ANIMS]
    for p in packs:
        for karaoke in (True, False):
            fl = textfx.FxLine(LINE, times, -1, p, karaoke=karaoke)
            box = (fl.x0, fl.y0, fl.x1, fl.y1)
            boxes = [(it["x"], it["y"]) for it in fl.items]
            shape = None
            for i in range(0, 120):
                t = i / 30.0
                arr = fl.layer(t)
                shape = shape or arr.shape
                assert arr.shape == shape, "layer size changed (%s)" % p["name"]
                assert np.isfinite(arr).all() and arr.min() >= -1e-4 and arr[..., 3].max() <= 1.0001
            assert (fl.x0, fl.y0, fl.x1, fl.y1) == box and [(it["x"], it["y"]) for it in fl.items] == boxes
            for i, (s, e) in enumerate(times[:-1]):
                assert s + fl.dur[i] <= times[i + 1][0] + 1e-6, "word %d still animating when the next starts" % i
        sch = textfx.FxSchedule([(LINE, times, -1)], p)
        frame = np.zeros((1920, 1080, 3), np.uint8)
        sch.draw(frame, 1.5)
        assert frame.max() > 0
        print("  ok  %-12s %s" % (p["name"], textfx.ANIMS.get(p["anim"])))
    # contact sheet
    from PIL import Image
    ims = [textfx.preview_image(p, 520, 110) for p in textfx.builtin_packs()]
    sheet = Image.new("RGB", (520, 110 * len(ims)))
    for i, im in enumerate(ims):
        sheet.paste(im, (0, i * 110))
    sheet.save(os.path.join(out, "packs-preview.png"))
    # lyric mode with a pack (Arabic output path)
    bg = os.path.join(out, "خلفية.mp4")
    subprocess.run([engine.ffmpeg_bin(), "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=0x302520:s=1080x1920:d=6",
                    "-f", "lavfi", "-i", "sine=f=220:d=6", "-shortest", "-pix_fmt", "yuv420p", bg], check=True,
                   creationflags=engine.NO_WINDOW)
    lyr = os.path.join(out, "كلمات.txt")
    with open(lyr, "w", encoding="utf-8") as fh:
        fh.write("[00:00.5] بقولك ايه *كفاية* كلام\n[00:03.0] مفيش ولا كلمة هتداوي\n[00:05.5]\n")
    for pid in ("writing", "bold"):
        job = engine.Job(bg, bg, lyr, song="بقولك ايه", closing="", out_dir=out, out_name="ستايل-" + pid,
                         pack=textfx.get_pack(pid), log=lambda m: None)
        mp4, _ = job.run()
        assert os.path.getsize(mp4) > 20000, mp4
        print("  lyric mode ok:", os.path.basename(mp4))
    # custom pack round trip
    path = textfx.save_pack(dict(textfx.get_pack("drama"), name="تجربة باك", anim="wave"))
    assert textfx.get_pack("تجربة باك")["anim"] == "wave"
    os.remove(path)
    print("OK packs")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
