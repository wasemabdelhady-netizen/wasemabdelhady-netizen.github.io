"""Oud lo-fi long-video engine: 1920x1080 @ 25fps, H.264 + AAC 320k.

Tracks are loudness-matched (-14 LUFS), joined with 3 s crossfades, laid over a seamless
background loop, with the current track title in a corner. The video is rendered in
parallel chunks and joined without re-encoding.

Used by the GUI (oudlofi.py) and from the command line:
    python engine.py --bg bg.jpg --mix-ar "..." --mix-en "..." track1.wav track2.mp3 ...
"""
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

W, H, FPS = 1920, 1080, 25
TW, TH = 1280, 720
RATE = 48000
XFADE = 3.0             # crossfade between tracks (seconds)
TARGET_LUFS = -14.0
LIMIT = 0.84            # sample-peak ceiling (-1.5 dBFS) so AAC stays under -1 dBTP
TEX_REF_LUFS = -30.0    # level the texture loops are stored at
GOLD = (0xE0, 0xA4, 0x58)
CREAM = (0xF5, 0xEF, 0xE6)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(ROOT, "fonts")
OUTPUT_DIR = os.path.join(ROOT, "output")

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
CORNERS = ("bottom-left", "bottom-right", "top-left", "top-right")
TEXTURES = ("none", "vinyl", "rain", "vinyl+rain", "file")

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------------- ffmpeg

def ffmpeg_bin():
    exe = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    local = os.path.join(ROOT, "ffmpeg", exe)
    return local if os.path.isfile(local) else exe


def run_ffmpeg(args, **kw):
    return subprocess.run([ffmpeg_bin(), "-hide_banner", "-nostdin"] + args,
                          stdout=kw.pop("stdout", subprocess.PIPE),
                          stderr=subprocess.PIPE, creationflags=NO_WINDOW, **kw)


def probe(path):
    """(duration_seconds, has_video, has_audio) from ffmpeg's own banner."""
    r = run_ffmpeg(["-i", path])
    err = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        raise RuntimeError("مش قادر أقرا الملف:\n%s\n\n%s" % (path, err[-600:]))
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return dur, " Video:" in err, " Audio:" in err


def wav_seconds(path):
    """Exact length of a PCM WAV written by ffmpeg (reads the RIFF header)."""
    with open(path, "rb") as f:
        if f.read(4) not in (b"RIFF", b"RF64"):
            raise RuntimeError("not a wav: " + path)
        f.read(8)
        rate = block = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            cid, size = hdr[:4], struct.unpack("<I", hdr[4:])[0]
            if cid == b"fmt ":
                fmt = f.read(size)
                rate = struct.unpack("<I", fmt[4:8])[0]
                block = struct.unpack("<H", fmt[12:14])[0]
            elif cid == b"data":
                if size == 0xFFFFFFFF:
                    size = os.path.getsize(path) - f.tell()
                return size / float(block * rate)
            else:
                f.seek(size + (size & 1), 1)
    raise RuntimeError("bad wav: " + path)


def fmt_time(sec, long_form=False):
    sec = int(sec)
    h, m, s = sec // 3600, sec // 60 % 60, sec % 60
    if long_form or h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%02d:%02d" % (m, s)


def default_titles(path):
    """Guess (arabic, english) titles from a file name like 'ليل - Night.wav'."""
    stem = os.path.splitext(os.path.basename(path))[0].replace("_", " ").strip()
    stem = re.sub(r"^\s*\d{1,3}(\s*[-.)]\s*|\s+)", "", stem)  # leading track number
    parts = [p.strip() for p in re.split(r"\s+[-–—|]\s+", stem) if p.strip()]
    ar = [p for p in parts if re.search(r"[\u0600-\u06FF]", p)]
    en = [p for p in parts if not re.search(r"[\u0600-\u06FF]", p)]
    return (" - ".join(ar), " - ".join(en))


# ----------------------------------------------------------------------------- text

def font(name, size, weight=None):
    f = ImageFont.truetype(os.path.join(FONTS_DIR, name), size)
    if weight:
        try:
            f.set_variation_by_name(weight)
        except Exception:
            pass
    return f


def text_w(fnt, s, rtl=False, spacing=0):
    if spacing:
        return sum(fnt.getlength(c) for c in s) + spacing * max(len(s) - 1, 0)
    kw = {"direction": "rtl", "language": "ar"} if rtl and features.check("raqm") else {}
    return fnt.getlength(s, **kw)


def draw_line(d, xy, s, fnt, fill, rtl=False, spacing=0):
    """Draw at left x, baseline-ish top y ('la' anchor)."""
    x, y = xy
    if spacing:
        for c in s:
            d.text((x, y), c, font=fnt, fill=fill, anchor="ls")
            x += fnt.getlength(c) + spacing
        return
    kw = {"direction": "rtl", "language": "ar"} if rtl and features.check("raqm") else {}
    d.text((x, y), s, font=fnt, fill=fill, anchor="ls", **kw)


def fit_font(name, weight, size, s, max_w, rtl=False, spacing_em=0.0, min_size=12):
    while size > min_size:
        f = font(name, size, weight)
        if text_w(f, s, rtl, spacing_em * size) <= max_w:
            return f, size
        size -= 2
    return font(name, min_size, weight), min_size


def title_card(ar, en, path, align="left"):
    """Small corner title: Arabic (Cairo) over English (Syne caps, gold), soft shadow."""
    ar, en = (ar or "").strip(), (en or "").strip().upper()
    lines = []
    if ar:
        f, _ = fit_font("Cairo.ttf", "SemiBold", 40, ar, 760, rtl=True)
        lines.append((ar, f, CREAM + (240,), True, 0, 58))
    if en:
        f, sz = fit_font("Syne.ttf", "SemiBold", 20, en, 760, spacing_em=0.2)
        lines.append((en, f, GOLD + (235,), False, 0.2 * sz, 34))
    pad = 30
    width = int(max(text_w(f, s, rtl, sp) for s, f, _, rtl, sp, _ in lines)) + 2 * pad
    height = sum(h for *_, h in lines) + 2 * pad
    shadow = Image.new("L", (width, height), 0)
    im = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ds, di = ImageDraw.Draw(shadow), ImageDraw.Draw(im)
    y = pad
    for s, f, fill, rtl, sp, h in lines:
        y += h - (12 if rtl else 8)
        w = text_w(f, s, rtl, sp)
        x = pad if align == "left" else width - pad - w
        draw_line(ds, (x, y + 2), s, f, 255, rtl, sp)
        draw_line(di, (x, y), s, f, fill, rtl, sp)
        y += (12 if rtl else 8)
    shadow = shadow.filter(ImageFilter.GaussianBlur(7)).point(lambda v: int(v * 0.6))
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    out.putalpha(shadow)
    out = Image.alpha_composite(out, im)
    out.save(path)
    return out.size


def cover(im, w, h):
    return ImageOps.fit(im.convert("RGB"), (w, h), Image.LANCZOS, centering=(0.5, 0.5))


def thumbnail(bg, ar, en, path):
    """1280x720 YouTube thumbnail: warm-darkened background, big Arabic + English title."""
    im = cover(bg, TW, TH)
    im = Image.blend(im, Image.new("RGB", im.size, (20, 14, 8)), 0.2)
    # vignette + centre shade so the title reads on any picture
    yy, xx = TH / 2.0, TW / 2.0
    mask = Image.new("L", (TW // 8, TH // 8))
    mask.putdata([int(255 * min(1.0, 0.25 + 0.95 * (((x * 8 - xx) / xx) ** 2 + ((y * 8 - yy) / yy) ** 2) / 2))
                  for y in range(TH // 8) for x in range(TW // 8)])
    mask = mask.resize((TW, TH), Image.BICUBIC)
    im = Image.composite(Image.new("RGB", im.size, (6, 4, 2)), im, mask.point(lambda v: int(v * 0.6)))
    shade = Image.new("L", (TW, TH), 0)
    ImageDraw.Draw(shade).ellipse((TW * 0.12, TH * 0.22, TW * 0.88, TH * 0.78), fill=150)
    shade = shade.filter(ImageFilter.GaussianBlur(90))
    im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, shade)

    ar, en = (ar or "").strip(), (en or "").strip().upper()
    blocks = []
    if ar:
        f, sz = fit_font("ReemKufi.ttf", "Bold", 132, ar, 1100, rtl=True, min_size=48)
        blocks.append(("t", ar, f, CREAM, True, 0, sz))
    if ar and en:
        blocks.append(("rule", None, None, GOLD, False, 0, 0))
    if en:
        f, sz = fit_font("Syne.ttf", "SemiBold", 46, en, 1100, spacing_em=0.22, min_size=18)
        blocks.append(("t", en, f, GOLD, False, 0.22 * sz, sz))
    heights = [int(b[6] * 1.25) if b[0] == "t" else 44 for b in blocks]
    y = (TH - sum(heights)) / 2.0
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sh = Image.new("L", im.size, 0)
    dl, ds = ImageDraw.Draw(layer), ImageDraw.Draw(sh)
    for (kind, s, f, col, rtl, sp, sz), h in zip(blocks, heights):
        if kind == "rule":
            dl.rectangle((TW / 2 - 40, y + h / 2 - 1.5, TW / 2 + 40, y + h / 2 + 1.5), fill=col + (230,))
        else:
            w = text_w(f, s, rtl, sp)
            base = y + h * (0.78 if rtl else 0.8)
            draw_line(ds, (TW / 2 - w / 2, base + 4), s, f, 255, rtl, sp)
            draw_line(dl, (TW / 2 - w / 2, base), s, f, col + (255,), rtl, sp)
        y += h
    sh = sh.filter(ImageFilter.GaussianBlur(12)).point(lambda v: int(v * 0.7))
    im = Image.composite(Image.new("RGB", im.size, (0, 0, 0)), im, sh)
    im = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
    im.save(path, quality=92)


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip(" .")
    return s[:60] or "oud-lofi"


# ----------------------------------------------------------------------------- job

class Track:
    def __init__(self, path, ar="", en=""):
        self.path, self.ar, self.en = path, ar, en
        self.wav = None
        self.dur = 0.0
        self.lufs_in = None


class Job:
    def __init__(self, tracks, background, mix_ar="", mix_en="", corner="bottom-left",
                 texture="none", texture_file="", texture_vol=40, footer="", out_dir=None,
                 progress=None, log=None, cancel=None, workers=None):
        self.tracks = [t if isinstance(t, Track) else Track(*t) for t in tracks]
        for t in self.tracks:
            t.ar, t.en = (t.ar or "").strip(), (t.en or "").strip()
            if not t.ar and not t.en:
                t.ar, t.en = default_titles(t.path)
                if not t.ar and not t.en:
                    t.en = os.path.splitext(os.path.basename(t.path))[0]
        self.bg = background
        self.mix_ar, self.mix_en = (mix_ar or "").strip(), (mix_en or "").strip()
        self.corner = corner if corner in CORNERS else "bottom-left"
        self.texture = texture if texture in TEXTURES else "none"
        self.texture_file = texture_file
        self.texture_vol = max(0, min(100, int(texture_vol)))
        self.footer = (footer or "").strip()
        self.out_dir = out_dir or OUTPUT_DIR
        self._progress = progress or (lambda f, m="": None)
        self.log = log or (lambda m: None)
        self.cancel = cancel or threading.Event()
        cpu = os.cpu_count() or 4
        self.workers = workers or max(1, min(6, cpu // 2))
        self.threads = max(2, cpu // self.workers)
        self.procs = set()
        self.lock = threading.Lock()
        self.stage = (0.0, 1.0)

    # -- helpers -------------------------------------------------------------------
    def progress(self, frac, msg=""):
        a, b = self.stage
        self._progress(a + (b - a) * max(0.0, min(1.0, frac)), msg)

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()

    def ff(self, args, on_frame=None, on_time=None):
        """Run ffmpeg with -progress; kill it on cancel. Returns stderr text."""
        self.check()
        err = tempfile.TemporaryFile()
        cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"] + args
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err, creationflags=NO_WINDOW)
        with self.lock:
            self.procs.add(p)
        try:
            for raw in p.stdout:
                if self.cancel.is_set():
                    p.kill()
                    break
                line = raw.decode("ascii", "replace").strip()
                if on_frame and line.startswith("frame="):
                    try:
                        on_frame(int(line[6:]))
                    except ValueError:
                        pass
                elif on_time and line.startswith("out_time_us="):
                    try:
                        on_time(int(line[12:]) / 1e6)
                    except ValueError:
                        pass
            p.wait()
        finally:
            with self.lock:
                self.procs.discard(p)
        self.check()
        err.seek(0)
        text = err.read().decode("utf-8", "replace")
        err.close()
        if p.returncode != 0:
            raise RuntimeError("ffmpeg error:\n" + text[-1500:])
        return text

    def kill_all(self):
        with self.lock:
            for p in list(self.procs):
                try:
                    p.kill()
                except OSError:
                    pass

    def parallel(self, calls, workers, on_done=None):
        """Run (fn, *args) calls on a thread pool; the first real error cancels the rest."""
        errors = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(c[0], *c[1:]) for c in calls]
            for f in futs:
                try:
                    f.result()
                    if on_done:
                        on_done()
                except Cancelled as e:
                    errors.append(e)
                except BaseException as e:
                    errors.insert(0, e)
                    self.cancel.set()
                    self.kill_all()
        if errors:
            if not isinstance(errors[0], Cancelled):
                self.cancel.clear()
            raise errors[0]

    def loudness(self, path):
        """(integrated LUFS, true peak dBTP)."""
        txt = self.ff(["-i", path, "-vn", "-af", "ebur128=peak=true:framelog=quiet", "-f", "null", "-"])
        summ = txt[txt.rfind("Summary:"):]
        i = re.search(r"I:\s*(-?[\d.]+|-inf)\s*LUFS", summ)
        tp = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", summ)
        val = lambda m: -70.0 if not m or "inf" in m.group(1) else float(m.group(1))
        return val(i), val(tp)

    # -- steps -----------------------------------------------------------------------
    def validate(self):
        if not self.tracks:
            raise RuntimeError("ضيف تراك واحد على الأقل.")
        for t in self.tracks:
            if not os.path.isfile(t.path):
                raise RuntimeError("الملف مش موجود:\n" + t.path)
        if not self.bg or not os.path.isfile(self.bg):
            raise RuntimeError("اختار الخلفية (صورة أو فيديو).")
        if self.texture == "file" and not os.path.isfile(self.texture_file or ""):
            raise RuntimeError("اختار ملف الـ texture (أو غيّر الاختيار).")
        if not features.check("raqm"):
            self.log("تحذير: RAQM مش شغال — العربي ممكن يطلع مقطّع.")

    def prep_track(self, i, t):
        """Decode to 48 kHz stereo WAV at -14 LUFS with a peak limiter."""
        lufs, _ = self.loudness(t.path)
        if lufs <= -60:
            raise RuntimeError("التراك ده صامت أو مش متقري:\n" + t.path)
        t.lufs_in = lufs
        gain = TARGET_LUFS - lufs
        t.wav = os.path.join(self.tmp, "t%03d.wav" % i)
        for _ in range(3):
            af = ("aresample=%d,aformat=sample_fmts=flt:channel_layouts=stereo,volume=%.2fdB,"
                  "alimiter=limit=%.3f:attack=5:release=80:level=disabled" % (RATE, gain, LIMIT))
            self.ff(["-i", t.path, "-vn", "-map_metadata", "-1", "-af", af, "-c:a", "pcm_s24le", t.wav])
            out, _ = self.loudness(t.wav)
            if abs(out - TARGET_LUFS) < 0.35:
                break
            gain += TARGET_LUFS - out
        t.lufs_out = out
        t.dur = wav_seconds(t.wav)
        if t.dur < 2 * XFADE + 2:
            raise RuntimeError("التراك قصير جداً (لازم أطول من 8 ثواني):\n" + t.path)

    def make_texture(self):
        """A ~60 s seamless texture loop at TEX_REF_LUFS, or None."""
        if self.texture == "none" or self.texture_vol <= 0:
            return None
        n, L, X = "48000", 60.0, 2.0
        parts = []
        if self.texture == "file":
            src = ["-i", self.texture_file]
            dur, _, has_a = probe(self.texture_file)
            if not has_a:
                raise RuntimeError("ملف الـ texture مفيهوش صوت.")
            L = max(4.0, min(dur - 0.1, 300.0))
            X = min(2.0, L / 4)
            L -= X
            graph = "[0:a]aresample=%s,aformat=sample_fmts=flt:channel_layouts=stereo[src]" % n
        else:
            src, chains, k = [], [], 0
            if "vinyl" in self.texture:
                for seed in (11, 23):  # two crackle channels so the stereo image is wide
                    src += ["-f", "lavfi", "-i", "anoisesrc=c=white:a=1:r=%s:d=%.1f:seed=%d" % (n, L + X, seed)]
                    chains.append("[%d]aeval='if(gt(val(0)\\,0.9994)\\,(val(0)-0.9994)/0.0006\\,0)"
                                  "+if(gt(val(0)\\,0.999992)\\,1.5\\,0)':c=mono,highpass=f=900,lowpass=f=7000,"
                                  "volume=1.6,aformat=channel_layouts=mono[c%d]" % (k, k))
                    k += 1
                src += ["-f", "lavfi", "-i", "anoisesrc=c=pink:a=0.05:r=%s:d=%.1f:seed=5" % (n, L + X)]
                chains.append("[%d]highpass=f=2500,lowpass=f=9000,volume=0.18,"
                              "aformat=channel_layouts=stereo[h%d]" % (k, k))
                src += ["-f", "lavfi", "-i", "anoisesrc=c=brown:a=0.05:r=%s:d=%.1f:seed=9" % (n, L + X)]
                chains.append("[%d]lowpass=f=90,volume=0.6,aformat=channel_layouts=stereo[r%d]" % (k + 1, k + 1))
                chains.append("[c%d][c%d]join=inputs=2:channel_layout=stereo[cr]" % (k - 2, k - 1))
                chains.append("[cr][h%d][r%d]amix=inputs=3:normalize=0[vin]" % (k, k + 1))
                k += 2
                parts.append("vin")
            if "rain" in self.texture:
                for c, seed in (("pink", 31), ("brown", 37)):
                    src += ["-f", "lavfi", "-i", "anoisesrc=c=%s:a=0.4:r=%s:d=%.1f:seed=%d" % (c, n, L + X, seed)]
                src += ["-f", "lavfi", "-i", "anoisesrc=c=white:a=1:r=%s:d=%.1f:seed=41" % (n, L + X)]
                chains.append("[%d]highpass=f=500,lowpass=f=7500,volume=0.5,aformat=channel_layouts=mono[rp]" % k)
                chains.append("[%d]highpass=f=120,lowpass=f=900,volume=0.5,aformat=channel_layouts=mono[rb]" % (k + 1))
                chains.append("[%d]aeval='if(gt(val(0)\\,0.9985)\\,(val(0)-0.9985)/0.0015\\,0)':c=mono,"
                              "bandpass=f=3200:width_type=o:w=1.5,volume=0.5,aformat=channel_layouts=mono[rd]" % (k + 2))
                chains.append("[rp][rb]join=inputs=2:channel_layout=stereo[rs]")
                chains.append("[rd]asplit[rd1][rd2];[rd2]adelay=17[rd3];[rd1][rd3]join=inputs=2:"
                              "channel_layout=stereo[rdd]")
                chains.append("[rs][rdd]amix=inputs=2:normalize=0[rain]")
                k += 3
                parts.append("rain")
            mix = "".join("[%s]" % p for p in parts)
            chains.append(mix + ("amix=inputs=%d:normalize=0" % len(parts) if len(parts) > 1 else "anull")
                          + ",aformat=sample_fmts=flt:channel_layouts=stereo[src]")
            graph = ";".join(chains)
        raw = os.path.join(self.tmp, "tex_raw.wav")
        # seamless loop: body [X, L) then the tail [L, L+X) fading out over the head [0, X)
        graph += (";[src]atrim=0:%.3f,asetpts=N/SR/TB,asplit=3[a][b][c];"
                  "[a]atrim=0:%.3f,asetpts=N/SR/TB,afade=t=in:d=%.3f:curve=qsin[head];"
                  "[b]atrim=%.3f:%.3f,asetpts=N/SR/TB[body];"
                  "[c]atrim=%.3f,asetpts=N/SR/TB,afade=t=out:d=%.3f:curve=qsin[tail];"
                  "[tail][head]amix=inputs=2:normalize=0:duration=first[seam];"
                  "[body][seam]concat=n=2:v=0:a=1[out]") % (L + X, X, X, X, L, L, X)
        self.ff(src + ["-filter_complex", graph, "-map", "[out]", "-c:a", "pcm_s24le", raw])
        lufs, _ = self.loudness(raw)
        if lufs <= -65:
            raise RuntimeError("ملف الـ texture صامت.")
        # keep clicks and drops from poking out of the mix: peaks at most 16 dB over the loudness
        lim = 10 ** ((TEX_REF_LUFS + 16) / 20.0)
        out = os.path.join(self.tmp, "texture.wav")
        mid = os.path.join(self.tmp, "tex_mid.wav")
        self.ff(["-i", raw, "-af", "volume=%.2fdB,alimiter=limit=%.4f:attack=1:release=30:level=disabled"
                 % (TEX_REF_LUFS - lufs, lim), "-c:a", "pcm_s24le", mid])
        lufs2, _ = self.loudness(mid)
        self.ff(["-i", mid, "-af", "volume=%.2fdB" % (TEX_REF_LUFS - lufs2), "-c:a", "pcm_s24le", out])
        return out

    def master_audio(self, tex, total):
        """Crossfade chain + texture + limiter -> AAC 320k."""
        args, fc = [], []
        for t in self.tracks:
            args += ["-i", t.wav]
        n = len(self.tracks)
        if n == 1:
            fc.append("[0:a]anull[mus]")
        else:
            prev = "0:a"
            for i in range(1, n):
                lab = "mus" if i == n - 1 else "x%d" % i
                fc.append("[%s][%d:a]acrossfade=d=%.3f:c1=qsin:c2=qsin[%s]" % (prev, i, XFADE, lab))
                prev = lab
        last = "mus"
        if tex:
            args += ["-stream_loop", "-1", "-i", tex]
            target = -46.0 + 0.28 * self.texture_vol
            fc.append("[%d:a]atrim=0:%.3f,volume=%.2fdB[tex]" % (n, total, target - TEX_REF_LUFS))
            fc.append("[mus][tex]amix=inputs=2:normalize=0:duration=first[mx]")
            last = "mx"
        fc.append("[%s]alimiter=limit=%.3f:attack=5:release=80:level=disabled,"
                  "atrim=0:%.3f[aout]" % (last, LIMIT, total))
        out = os.path.join(self.tmp, "audio.m4a")
        self.ff(args + ["-filter_complex", ";".join(fc), "-map", "[aout]",
                        "-c:a", "aac", "-b:a", "320k", "-ar", str(RATE), out],
                on_time=lambda s: self.progress(s / total, "الصوت…"))
        return out

    def prep_background(self):
        """Returns ('image', png) or ('video', loop_mp4, loop_frames); also a PIL still."""
        ext = os.path.splitext(self.bg)[1].lower()
        if ext in IMAGE_EXT:
            try:
                im = ImageOps.exif_transpose(Image.open(self.bg))
            except Exception as ex:
                raise RuntimeError("مش قادر أفتح الصورة:\n%s\n%s" % (self.bg, ex))
            still = cover(im, W, H)
            png = os.path.join(self.tmp, "bg.png")
            still.save(png)
            return ("image", png), still
        dur, has_v, _ = probe(self.bg)
        if not has_v:
            raise RuntimeError("ملف الخلفية مفيهوش فيديو:\n" + self.bg)
        if dur < 2.0:
            raise RuntimeError("فيديو الخلفية قصير جداً (أقل من ثانيتين).")
        frames = int((dur - 0.15) * FPS)
        xf = int(min(1.5, dur / 4) * FPS)
        lf = frames - xf
        # loop = body [X, L) + crossfade(tail [L, N) -> head [0, X)); last frame flows into the first
        fc = ("[0:v]fps=%d,scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d,"
              "setsar=1,format=yuv420p,trim=end_frame=%d,setpts=N/%d/TB,split=3[a][b][c];"
              "[a]trim=end_frame=%d,setpts=N/%d/TB[head];"
              "[b]trim=start_frame=%d:end_frame=%d,setpts=N/%d/TB[body];"
              "[c]trim=start_frame=%d,setpts=N/%d/TB[tail];"
              "[tail][head]xfade=transition=fade:duration=%.4f:offset=0[seam];"
              "[body][seam]concat=n=2:v=1:a=0[out]") % (
            FPS, W, H, W, H, frames, FPS, xf, FPS, xf, lf, FPS, lf, FPS, xf / float(FPS))
        loop = os.path.join(self.tmp, "loop.mp4")
        self.ff(["-i", self.bg, "-filter_complex", fc, "-map", "[out]", "-an", "-c:v", "libx264",
                 "-preset", "fast", "-crf", "14", "-g", "50", "-pix_fmt", "yuv420p", loop],
                on_frame=lambda f: self.progress(f / float(lf), "تجهيز لوب الخلفية…"))
        stillp = os.path.join(self.tmp, "still.png")
        run_ffmpeg(["-y", "-ss", "%.2f" % (lf / 3.0 / FPS), "-i", loop, "-frames:v", "1", stillp])
        still = Image.open(stillp).convert("RGB") if os.path.isfile(stillp) else Image.new("RGB", (W, H))
        return ("video", loop, lf), still

    # -- video -------------------------------------------------------------------------
    def title_windows(self, starts, total):
        """Per track: (fade_in_start, fade_out_start or None). 1.5 s fades."""
        wins = []
        for i, s in enumerate(starts):
            a = 0.8 if i == 0 else s + 1.5
            b = starts[i + 1] - 0.3 if i + 1 < len(starts) else None
            wins.append((a, b))
        return wins

    def chunk_bounds(self, total_frames, busy):
        n = max(1, min(64, int(math.ceil(total_frames / float(FPS) / 20.0)),
                       max(self.workers * 3, int(math.ceil(total_frames / float(FPS) / 240.0)))))
        cuts = []
        for k in range(1, n):
            f = int(round(total_frames * k / float(n)))
            for a, b in busy:
                if a * FPS - 2 <= f <= b * FPS + 2:
                    f = int(b * FPS) + 12
            if (not cuts or f - cuts[-1] >= FPS * 4) and f <= total_frames - FPS * 4:
                cuts.append(f)
        edges = [0] + cuts + [total_frames]
        return [(edges[i], edges[i + 1]) for i in range(len(edges) - 1)]

    def render_chunk(self, idx, f0, f1, bgspec, titles, total_frames, path, done):
        nf = f1 - f0
        t0, dur = f0 / float(FPS), nf / float(FPS)
        args, fc = [], []
        if bgspec[0] == "video":
            _, loop, lf = bgspec
            args += ["-stream_loop", "-1", "-i", loop]
            fc.append("[0:v]trim=start_frame=%d,setpts=N/%d/TB,format=yuv420p[bg]" % (f0 % lf, FPS))
        else:
            args += ["-loop", "1", "-framerate", str(FPS), "-i", bgspec[1]]
            # slow periodic zoom + drift (sub-pixel) as a function of absolute time
            t = "((in+%d)/%d)" % (f0, FPS)
            z = "(1.05+0.022*sin(2*PI*%s/47))" % t
            cx = "(W/2+13*sin(2*PI*%s/71))" % t
            cy = "(H/2+8*sin(2*PI*%s/59+1.3))" % t
            hw, hh = "(W/2/%s)" % z, "(H/2/%s)" % z
            x0, x1 = "%s-%s" % (cx, hw), "%s+%s" % (cx, hw)
            y0, y1 = "%s-%s" % (cy, hh), "%s+%s" % (cy, hh)
            ta = "(t+%.4f)" % t0
            flick = ("0.011*sin(2*PI*%s/4.3)+0.007*sin(2*PI*%s/1.7+1.1)+0.004*sin(2*PI*%s/0.83+2.3)"
                     % (ta, ta, ta))
            fc.append("[0:v]format=yuv420p,perspective=x0='{x0}':y0='{y0}':x1='{x1}':y1='{y0}':"
                      "x2='{x0}':y2='{y1}':x3='{x1}':y3='{y1}':interpolation=linear:eval=frame,"
                      "eq=brightness='{fl}':gamma='1+1.5*({fl})':eval=frame,"
                      "noise=c0s=7:c0f=t+u[bg]".format(
                          x0=x0, y0=y0, x1=x1, y1=y1, fl=flick))
        cur = "bg"
        for k, (png, size, a, b) in enumerate(titles):
            la, lb = a - t0, (None if b is None else b - t0)
            if lb is not None and lb + 1.5 <= 0 or la >= dur:
                continue
            args += ["-loop", "1", "-framerate", str(FPS), "-t", "%.3f" % (dur + 0.5), "-i", png]
            inp = sum(1 for x in args if x == "-i") - 1
            ch = "[%d:v]format=rgba" % inp
            if la > 0:
                ch += ",fade=t=in:st=%.3f:d=1.5:alpha=1" % la
            if lb is not None and lb < dur:
                ch += ",fade=t=out:st=%.3f:d=1.5:alpha=1" % lb
            x, y = self.title_xy(size)
            en = "between(t\\,%.3f\\,%.3f)" % (max(la, 0) - 0.05, (lb + 1.6) if lb is not None else dur + 1)
            fc.append(ch + "[t%d]" % k)
            fc.append("[%s][t%d]overlay=x=%d:y=%d:enable='%s'[v%d]" % (cur, k, x, y, en, k))
            cur = "v%d" % k
        tail = []
        if f0 == 0:
            tail.append("fade=t=in:st=0:d=1.2")
        if f1 == total_frames:
            tail.append("fade=t=out:st=%.3f:d=3" % (dur - 3.0))
        fc.append("[%s]%sformat=yuv420p[vout]" % (cur, "".join(x + "," for x in tail)))
        last = [0]

        def on_frame(fr):
            done[idx] = fr
            last[0] = fr
        self.ff(args + ["-filter_complex", ";".join(fc), "-map", "[vout]", "-frames:v", str(nf),
                        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                        "-maxrate", "12M", "-bufsize", "24M", "-g", "50", "-keyint_min", "25",
                        "-profile:v", "high", "-pix_fmt", "yuv420p", "-threads", str(self.threads),
                        "-video_track_timescale", "12800", path], on_frame=on_frame)
        done[idx] = nf

    def title_xy(self, size):
        w, h = size
        mx, my = 56, 40
        left = "left" in self.corner
        top = "top" in self.corner
        return (mx if left else W - mx - w), (my if top else H - my - h)

    # -- outputs --------------------------------------------------------------------------
    def description(self, starts, total):
        long_form = total >= 3600
        head = " | ".join(x for x in (self.mix_ar, self.mix_en) if x)
        lines = []
        if head:
            lines += [head, ""]
        lines.append("Tracklist — قائمة المقطوعات")
        for i, (t, s) in enumerate(zip(self.tracks, starts)):
            ts = 0 if i == 0 else int(s + XFADE / 2)
            stamp = "00:00" if i == 0 else fmt_time(ts, long_form and ts >= 3600)
            name = " — ".join(x for x in (t.ar, t.en) if x)
            lines.append("%s %s" % (stamp, name))
        if self.footer:
            lines += ["", self.footer]
        return "\n".join(lines) + "\n"

    def run(self):
        t_start = time.time()
        self.validate()
        os.makedirs(self.out_dir, exist_ok=True)
        self.tmp = tempfile.mkdtemp(prefix="oudlofi_")
        try:
            return self._run(t_start)
        except Cancelled:
            self.kill_all()
            raise
        finally:
            self.kill_all()
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, t_start):
        n = len(self.tracks)
        # 1. tracks (parallel)
        self.stage = (0.0, 0.14)
        self.progress(0, "تحليل وتظبيط الصوت…")
        done = [0]

        def tick():
            done[0] += 1
            self.progress(done[0] / float(n), "تحليل وتظبيط الصوت… %d/%d" % (done[0], n))
        self.parallel([(self.prep_track, i, t) for i, t in enumerate(self.tracks)],
                      max(2, min(n, os.cpu_count() or 4)), tick)
        for i, t in enumerate(self.tracks):
            self.log("%d. %s  %s  (%.1f → %.1f LUFS)" % (
                i + 1, fmt_time(t.dur), os.path.basename(t.path), t.lufs_in, t.lufs_out))
        starts, s = [], 0.0
        for i, t in enumerate(self.tracks):
            starts.append(s)
            s += t.dur - XFADE
        total = s + XFADE
        total_frames = int(total * FPS)
        total = total_frames / float(FPS)
        self.log("المدة الكلية: %s" % fmt_time(total, total >= 3600))

        # 2. texture + master audio
        self.stage = (0.14, 0.2)
        self.progress(0, "الـ texture…")
        tex = self.make_texture()
        self.stage = (0.2, 0.3)
        audio = self.master_audio(tex, total)
        lufs, tp = self.loudness(audio)
        self.log("الصوت النهائي: %.1f LUFS، true peak %.1f dBTP" % (lufs, tp))
        self.audio_lufs, self.audio_tp = lufs, tp

        # 3. background
        self.stage = (0.3, 0.36)
        self.progress(0, "تجهيز الخلفية…")
        bgspec, still = self.prep_background()

        # 4. titles
        align = "left" if "left" in self.corner else "right"
        wins = self.title_windows(starts, total)
        titles = []
        for i, (t, (a, b)) in enumerate(zip(self.tracks, wins)):
            png = os.path.join(self.tmp, "title%03d.png" % i)
            size = title_card(t.ar, t.en, png, align)
            titles.append((png, size, a, b))
        busy = [(0, 2.5), (total - 3.5, total)]
        for a, b in wins:
            busy.append((a - 0.5, a + 2.0))
            if b is not None:
                busy.append((b - 0.5, b + 2.0))
        busy.sort()

        # 5. video chunks (parallel)
        chunks = self.chunks = self.chunk_bounds(total_frames, busy)
        self.log("الرندر: %d أجزاء، %d في نفس الوقت" % (len(chunks), self.workers))
        self.stage = (0.36, 0.97)
        prog = [0] * len(chunks)
        stop = threading.Event()

        def ticker():
            while not stop.wait(0.5):
                fr = sum(prog)
                el = time.time() - t_render
                eta = (total_frames - fr) * el / fr if fr > FPS * 4 else None
                self.progress(fr / float(total_frames), "رندر الفيديو… %d%%%s" % (
                    100 * fr // max(total_frames, 1),
                    "" if eta is None else "  (فاضل حوالي %s)" % fmt_time(eta)))
        t_render = time.time()
        tk = threading.Thread(target=ticker, daemon=True)
        tk.start()
        paths = [os.path.join(self.tmp, "c%03d.mp4" % i) for i in range(len(chunks))]
        try:
            self.parallel([(self.render_chunk, i, a, b, bgspec, titles, total_frames, paths[i], prog)
                           for i, (a, b) in enumerate(chunks)], self.workers)
        finally:
            stop.set()
        self.check()

        # 6. join + mux
        self.stage = (0.97, 1.0)
        self.progress(0, "تجميع الفيديو…")
        lst = os.path.join(self.tmp, "chunks.txt")
        with open(lst, "w", encoding="utf-8") as f:
            for p in paths:
                f.write("file '%s'\n" % os.path.basename(p))
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        base = safe_name(self.mix_en or self.mix_ar or "oud-lofi") + "_" + stamp
        mp4 = os.path.join(self.out_dir, base + ".mp4")
        self.ff(["-f", "concat", "-safe", "0", "-i", lst, "-i", audio, "-map", "0:v", "-map", "1:a",
                 "-c", "copy", "-t", "%.3f" % total, "-movflags", "+faststart",
                 "-metadata", "title=" + (self.mix_en or self.mix_ar), mp4])
        txt = os.path.join(self.out_dir, base + "_description.txt")
        with open(txt, "w", encoding="utf-8", newline="\r\n" if IS_WIN else None) as f:
            f.write(self.description(starts, total))
        jpg = os.path.join(self.out_dir, base + "_thumbnail.jpg")
        thumbnail(still, self.mix_ar, self.mix_en, jpg)
        self.starts, self.total = starts, total
        self.progress(1, "خلص ✔")
        self.log("خلص في %s" % fmt_time(time.time() - t_start))
        return mp4, txt, jpg


# ----------------------------------------------------------------------------- CLI

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("tracks", nargs="+")
    ap.add_argument("--bg", required=True)
    ap.add_argument("--mix-ar", default="")
    ap.add_argument("--mix-en", default="")
    ap.add_argument("--corner", default="bottom-left", choices=CORNERS)
    ap.add_argument("--texture", default="none", choices=TEXTURES)
    ap.add_argument("--texture-file", default="")
    ap.add_argument("--texture-vol", type=int, default=40)
    ap.add_argument("--out", default=OUTPUT_DIR)
    a = ap.parse_args()
    tr = [Track(p, *default_titles(p)) for p in a.tracks]
    job = Job(tr, a.bg, a.mix_ar, a.mix_en, a.corner, a.texture, a.texture_file, a.texture_vol,
              out_dir=a.out, log=print,
              progress=lambda f, m="": sys.stdout.write("\r%5.1f%% %s   " % (f * 100, m)))
    print("\n".join(job.run()))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
