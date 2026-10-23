"""ReleaseKit engine: one cover + master audio -> every release asset.

Used by the GUI (releasekit.py) and from the command line:
    python engine.py --cover cover.png --audio master.wav --ar "اسم" --en "Name" --artist "Wasem"

Writes "<song> - Release Kit/" with cover sizes, a Spotify Canvas loop, a YouTube
visualizer and a 20 s teaser. Arabic text via Pillow + RAQM, video via ffmpeg.
"""
import io
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, features

GOLD = (0xE0, 0xA4, 0x58)
CREAM = (0xF5, 0xEF, 0xE6)
INK = (0x0A, 0x0A, 0x0A)
FPS = 30
CANVAS_SECONDS = 8.0
TEASER_SECONDS = 20.0
DEFAULT_TAG = "متاح الآن  ·  OUT NOW"
DEFAULT_AR_FONT = "ArefRuqaa-Bold.ttf"
DEFAULT_EN_FONT = "Syne.ttf"
MAX_COVER_BYTES = 9_800_000  # DistroKid limit is 10 MB

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(ROOT, "fonts")
OUTPUT_DIR = os.path.join(ROOT, "output")

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0

# file names inside the kit folder (numbered so they sort in a sensible order)
NAMES = {
    "cover": "01 Cover 3000x3000 (DistroKid).jpg",
    "thumb": "02 YouTube Thumbnail 1280x720.jpg",
    "story": "03 Story 1080x1920.jpg",
    "reelcover": "04 Reel Cover 1080x1920.jpg",
    "post": "05 Post 1080x1080.jpg",
    "portrait": "06 Post Portrait 1080x1350.jpg",
    "canvas": "07 Spotify Canvas 1080x1920.mp4",
    "visualizer": "08 YouTube Visualizer 1920x1080.mp4",
    "teaser": "09 Teaser 20s 1080x1920.mp4",
}
ALL_PARTS = ("images", "canvas", "visualizer", "teaser")


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


def check(r, what):
    if r.returncode != 0:
        raise RuntimeError("ffmpeg وقع في %s:\n%s" % (what, r.stderr.decode("utf-8", "replace")[-900:]))


def decode_mono(audio, rate=44100, start=None, dur=None):
    args = []
    if start is not None:
        args += ["-ss", "%.3f" % start]
    if dur is not None:
        args += ["-t", "%.3f" % dur]
    r = run_ffmpeg(args + ["-i", audio, "-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"])
    check(r, "قراية الصوت")
    return np.frombuffer(r.stdout[: len(r.stdout) // 4 * 4], dtype="<f4").astype(np.float32)


class Encoder:
    """ffmpeg reading raw frames from a pipe; stderr drained on a thread."""

    def __init__(self, args, cancel):
        self.cancel = cancel
        self.p = subprocess.Popen([ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"] + args,
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self.err = []
        self.th = threading.Thread(target=lambda: self.err.append(self.p.stderr.read()), daemon=True)
        self.th.start()

    def write(self, data):
        if self.cancel.is_set():
            raise Cancelled()
        try:
            self.p.stdin.write(data)
        except (BrokenPipeError, OSError):
            self.fail()

    def fail(self):
        try:
            self.p.kill()
        except Exception:
            pass
        self.p.wait()
        self.th.join(2)
        raise RuntimeError("ffmpeg وقف:\n" + b"".join(self.err).decode("utf-8", "replace")[-900:])

    def abort(self):
        try:
            self.p.kill()
        except Exception:
            pass
        self.p.wait()

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        self.p.wait()
        self.th.join(5)
        if self.p.returncode != 0:
            raise RuntimeError("ffmpeg وقع:\n" + b"".join(self.err).decode("utf-8", "replace")[-900:])


# ----------------------------------------------------------------------------- images

def open_image(path):
    with open(path, "rb") as fh:  # bytes: works with any (Arabic) path on Windows
        im = Image.open(io.BytesIO(fh.read()))
        im.load()
    return ImageOps.exif_transpose(im)


def flatten(im, matte=(0, 0, 0)):
    """Any mode (RGBA, P+transparency, CMYK, 16-bit) -> plain RGB, transparency on `matte`."""
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.point(lambda v: v / 256).convert("L")
    if im.mode == "P":
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, matte + (255,))
        return Image.alpha_composite(bg, rgba).convert("RGB")
    return im.convert("RGB")


def square(im):
    w, h = im.size
    if w == h:
        return im, False
    s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s)), True


def save_jpg(im, path, quality=92, max_bytes=None):
    q = quality
    while True:
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=q, subsampling=0 if q >= 90 else 2, optimize=True,
                progressive=False, dpi=(72, 72))
        if max_bytes is None or buf.tell() <= max_bytes or q <= 60:
            break
        q -= 3
    with open(path, "wb") as fh:
        fh.write(buf.getvalue())
    return q, buf.tell()


def fill(im, w, h):
    """Scale to cover w x h and centre-crop."""
    s = max(w / im.width, h / im.height)
    nw, nh = max(w, int(round(im.width * s))), max(h, int(round(im.height * s)))
    im = im.resize((nw, nh), Image.LANCZOS)
    x, y = (nw - w) // 2, (nh - h) // 2
    return im.crop((x, y, x + w, y + h))


def blurred_bg(cover, w, h, dim=0.5):
    """The cover stretched to fill the frame, heavily blurred, darkened, with a vignette."""
    small = fill(cover, max(16, w // 6), max(16, h // 6))
    small = small.filter(ImageFilter.GaussianBlur(max(w, h) / 6 / 22))
    big = small.resize((w, h), Image.BICUBIC).filter(ImageFilter.GaussianBlur(4))
    a = np.asarray(big, dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    r = np.sqrt(((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    vig = np.clip(1.0 - 0.45 * np.clip(r - 0.35, 0, None) ** 1.4, 0.35, 1.0)
    grad = 1.0 - 0.25 * (yy / h) ** 2  # a touch darker at the bottom, where the text sits
    a = a * (dim * vig * grad)[..., None]
    a += (np.array(INK, np.float32) * 0.25)  # lift blacks toward the brand black
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "RGB")


def paste_cover(canvas, cover, size, cx, cy, shadow=0.65):
    size = int(size)
    x, y = int(round(cx - size / 2)), int(round(cy - size / 2))
    if shadow:
        pad = int(size * 0.12)
        m = Image.new("L", (size + 2 * pad, size + 2 * pad), 0)
        ImageDraw.Draw(m).rectangle((pad, pad + size * 0.02, pad + size, pad + size * 1.02), fill=int(255 * shadow))
        m = m.filter(ImageFilter.GaussianBlur(size * 0.035))
        canvas.paste(Image.new("RGB", m.size, (0, 0, 0)), (x - pad, y - pad + int(size * 0.015)), m)
    canvas.paste(cover.resize((size, size), Image.LANCZOS), (x, y))
    return (x, y, x + size, y + size)


# ----------------------------------------------------------------------------- text

ARABIC_RE = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")


def is_rtl(text):
    return bool(ARABIC_RE.search(text or ""))


def font_list():
    try:
        return sorted(f for f in os.listdir(FONTS_DIR) if f.lower().endswith((".ttf", ".otf")))
    except OSError:
        return []


_font_bytes = {}


def load_font(name_or_path, size, weight=None):
    path = name_or_path if os.path.isabs(name_or_path) else os.path.join(FONTS_DIR, name_or_path)
    if path not in _font_bytes:
        with open(path, "rb") as fh:  # bytes: works with any (Arabic) path on Windows
            _font_bytes[path] = fh.read()
    f = ImageFont.truetype(io.BytesIO(_font_bytes[path]), int(size), layout_engine=ImageFont.Layout.RAQM)
    if weight:
        try:
            axes = f.get_variation_axes()
            f.set_variation_by_axes([max(a["minimum"], min(a["maximum"], weight))
                                     if a.get("name") in (b"Weight", "Weight") else a["default"]
                                     for a in axes])
        except Exception:
            pass
    return f


def _kw(text):
    return dict(direction="rtl", language="ar") if is_rtl(text) else dict(direction="ltr")


def _measure(text, font, tracking):
    kw = _kw(text)
    if tracking and not is_rtl(text):
        x, boxes = 0.0, []
        for ch in text:
            l, t, r, b = font.getbbox(ch, anchor="ls")
            boxes.append((x + l, t, x + r, b))
            x += font.getlength(ch) + tracking
        if not boxes:
            return (0, 0, 1, 1)
        return (min(b[0] for b in boxes), min(b[1] for b in boxes),
                max(b[2] for b in boxes), max(b[3] for b in boxes))
    return font.getbbox(text, anchor="ls", **kw)


def text_sprite(text, font_name, size, color=CREAM, weight=None, tracking=0.0, maxw=None, shadow=0.55):
    """Tight RGBA sprite of one line of text with a soft drop shadow; centre it with place()."""
    text = (text or "").strip()
    if not text:
        return None
    size = int(size)
    for _ in range(6):
        font = load_font(font_name, size, weight)
        trk = tracking * size
        l, t, r, b = _measure(text, font, trk)
        if not maxw or r - l <= maxw or size <= 12:
            break
        size = max(12, int(size * maxw / (r - l) * 0.98))
    pad = int(size * 0.5)
    w, h = int(r - l) + 2 * pad, int(b - t) + 2 * pad
    ox, oy = pad - l, pad - t
    col = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    dc, dm = ImageDraw.Draw(col), ImageDraw.Draw(mask)
    if trk and not is_rtl(text):
        x = ox
        for ch in text:
            dc.text((x, oy), ch, font=font, fill=color + (255,), anchor="ls")
            dm.text((x, oy), ch, font=font, fill=255, anchor="ls")
            x += font.getlength(ch) + trk
    else:
        kw = _kw(text)
        dc.text((ox, oy), text, font=font, fill=color + (255,), anchor="ls", **kw)
        dm.text((ox, oy), text, font=font, fill=255, anchor="ls", **kw)
    if shadow:
        blur = max(3, size // 8)
        sh = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * shadow))
        out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        out.paste(Image.new("RGBA", (w, h), (4, 3, 2, 255)), (0, max(2, size // 22)), sh)
        col = Image.alpha_composite(out, col)
    col.pad = pad  # transparent margin on every side (shadow room)
    return col


def line_sprite(text, ar_font, en_font, size, color=CREAM, weight=None, tracking=0.0, maxw=None, shadow=0.55):
    """Like text_sprite, but a line mixing Arabic and English parts separated by "·"
    gets each part in its own font (Arabic font for Arabic, English font + tracking
    for Latin), laid out right-to-left when it starts in Arabic."""
    text = (text or "").strip()
    if not text:
        return None
    segs = [p.strip() for p in text.split("·") if p.strip()]

    def one(seg, sz):
        if is_rtl(seg):
            return text_sprite(seg, ar_font, sz, color, weight, 0, None, shadow)
        return text_sprite(seg.upper(), en_font, sz * 0.92, color, weight, tracking, None, shadow)
    for _ in range(6):
        parts = [one(p, size) for p in segs]
        dot = text_sprite("·", en_font, size, color, weight, 0, None, shadow) if len(parts) > 1 else None
        seq = []
        for i, p in enumerate(parts):
            if i:
                seq.append(dot)
            seq.append(p)
        if is_rtl(segs[0]):
            seq.reverse()
        pad = min(p.pad for p in seq)
        gap = int(size * 0.35)
        widths = [p.width - 2 * p.pad for p in seq]
        vis_w = sum(widths) + gap * (len(seq) - 1)
        if not maxw or vis_w <= maxw or size <= 12:
            break
        size = max(12, int(size * maxw / vis_w * 0.98))
    hmax = max(p.height for p in seq)
    out = Image.new("RGBA", (vis_w + 2 * pad, hmax), (0, 0, 0, 0))
    x = pad
    for p, w in zip(seq, widths):
        out.alpha_composite(p, (int(x - p.pad), (hmax - p.height) // 2))
        x += w + gap
    out.pad = pad
    return out


def vh(sprite):
    """Visual (ink) height of a sprite, without its shadow margin."""
    return 0 if sprite is None else sprite.height - 2 * sprite.pad


def place_top(canvas, sprite, cx, top):
    """Place `sprite` centred on cx with its ink starting at y = top."""
    if sprite is not None:
        place(canvas, sprite, cx, top - sprite.pad + sprite.height / 2)


def place(canvas, sprite, cx, cy):
    """Alpha-composite `sprite` centred on (cx, cy); works on RGB or RGBA canvases."""
    if sprite is None:
        return 0
    x, y = int(round(cx - sprite.width / 2)), int(round(cy - sprite.height / 2))
    if canvas.mode == "RGBA":
        canvas.alpha_composite(sprite, (x, y))
    else:
        canvas.paste(sprite, (x, y), sprite)
    return sprite.height


# ----------------------------------------------------------------------------- audio analysis

def band_edges(nbars, fmin=45.0, fmax=14000.0):
    return fmin * (fmax / fmin) ** (np.arange(nbars + 1) / nbars)


def spectrum_bars(x, rate, fps=FPS, nbars=48, cancel=None):
    """Bar heights 0..1 for each video frame: log-spaced bands, normalised to the song,
    fast attack / slow release so the bars move with the music without flicker."""
    n = int(math.ceil(len(x) / rate * fps))
    win = 4096
    hann = np.hanning(win).astype(np.float32)
    pad = np.concatenate([np.zeros(win // 2, np.float32), x, np.zeros(win, np.float32)])
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    edges = band_edges(nbars)
    # band matrix: bins inside each band, or the nearest bin for narrow bass bands
    M = np.zeros((len(freqs), nbars), np.float32)
    for k in range(nbars):
        sel = np.where((freqs >= edges[k]) & (freqs < edges[k + 1]))[0]
        if len(sel) == 0:
            sel = [int(np.argmin(np.abs(freqs - math.sqrt(edges[k] * edges[k + 1]))))]
        M[sel, k] = 1.0 / len(sel)
    db = np.empty((n, nbars), np.float32)
    starts = (np.arange(n) * rate / fps).astype(np.int64)
    for c in range(0, n, 256):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        idx = starts[c:c + 256, None] + np.arange(win)[None, :]
        fr = pad[idx] * hann
        p = np.abs(np.fft.rfft(fr, axis=1)) ** 2
        db[c:c + 256] = 10 * np.log10(p @ M + 1e-10)
    loud = db.max(axis=1)
    active = loud > np.percentile(loud, 20) - 1 if n > 10 else np.ones(n, bool)
    ref = db[active] if active.any() else db
    hi = 0.5 * np.percentile(ref, 97, axis=0) + 0.5 * np.percentile(ref, 97)
    lo = hi - 42.0
    v = np.clip((db - lo) / (hi - lo), 0, 1) ** 1.6
    v[:, 1:-1] = 0.25 * v[:, :-2] + 0.5 * v[:, 1:-1] + 0.25 * v[:, 2:]
    out = np.empty_like(v)
    prev = np.zeros(nbars, np.float32)
    for i in range(n):
        cur = v[i]
        prev = np.where(cur > prev, prev + 0.7 * (cur - prev), prev * 0.86 + cur * 0.14)
        out[i] = prev
    return out


def strongest_window(x, rate, length=TEASER_SECONDS, log=None):
    """Start time of the strongest `length` seconds: loud AND repeated (the chorus).
    Loudness per second + chroma self-similarity along time lags, then snapped to the
    nearest onset so the teaser starts on a hit, not mid-phrase."""
    dur = len(x) / float(rate)
    if dur <= length + 1:
        return 0.0, {}
    hop = rate // 4
    win = 8192
    hann = np.hanning(win).astype(np.float32)
    pad = np.concatenate([x, np.zeros(win, np.float32)])
    nf = len(x) // hop
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    sel = np.where((freqs > 65) & (freqs < 2100))[0]
    pc = (np.round(12 * np.log2(freqs[sel] / 440.0)) + 69).astype(int) % 12
    P = np.zeros((len(sel), 12), np.float32)
    P[np.arange(len(sel)), pc] = 1
    chroma = np.zeros((nf, 12), np.float32)
    rms = np.zeros(nf, np.float32)
    for c in range(0, nf, 256):
        st = np.arange(c, min(nf, c + 256)) * hop
        fr = pad[st[:, None] + np.arange(win)[None, :]]
        rms[c:c + len(st)] = np.sqrt(np.mean(fr[:, :hop] ** 2, axis=1))
        mag = np.abs(np.fft.rfft(fr * hann, axis=1))[:, sel]
        chroma[c:c + len(st)] = mag @ P
    # 1-second blocks
    nb = nf // 4
    cb = chroma[:nb * 4].reshape(nb, 4, 12).mean(axis=1)
    cb = np.log1p(cb * 10)
    cb -= cb.mean(axis=1, keepdims=True)
    cb /= np.linalg.norm(cb, axis=1, keepdims=True) + 1e-9
    lb = 10 * np.log10(np.mean(rms[:nb * 4].reshape(nb, 4) ** 2, axis=1) + 1e-10)
    W = int(round(length))
    nc = nb - W + 1
    if nc < 2:
        return 0.0, {}
    loud = np.convolve(lb, np.ones(W) / W, "valid")[:nc]
    S = cb @ cb.T
    rep = np.full(nc, -1.0)
    for L in range(10, nb - W + 1):
        d = np.diagonal(S, L)
        m = np.convolve(d, np.ones(W) / W, "valid")  # m[t]: window t vs window t+L
        k = min(len(m), nc)
        rep[:k] = np.maximum(rep[:k], m[:k])
        k2 = min(len(m), nc - L)
        if k2 > 0:
            rep[L:L + k2] = np.maximum(rep[L:L + k2], m[:k2])
    if rep.max() < 0:
        rep[:] = 0

    def z(a):
        return (a - a.mean()) / (a.std() + 1e-9)
    score = z(loud) + 0.8 * z(rep)
    # gentle preference for windows that are not the very start or the fade-out
    t = np.arange(nc)
    score -= 0.4 * (t < 5) + 0.4 * (t + W > nb - 3)
    best = int(np.argmax(score))
    # snap to the strongest onset within [best-1.5 s, best+0.75 s] (50 ms energy steps)
    h2 = rate // 20
    e = np.array([np.mean(x[i:i + h2] ** 2) for i in range(0, len(x) - h2, h2)], np.float32)
    edb = 10 * np.log10(e + 1e-10)
    flux = np.zeros_like(edb)
    flux[4:] = edb[4:] - edb[:-4]
    a, b = max(0, int((best - 1.5) * 20)), min(len(flux) - 1, int((best + 0.75) * 20))
    start = float(best)
    if b > a:
        j = a + int(np.argmax(flux[a:b + 1]))
        if flux[j] > 3.0:  # a clear hit; otherwise keep the block boundary
            start = max(0.0, (j - 4) / 20.0 + 0.05)
    start = min(max(0.0, start), dur - length)
    info = {"block": best, "loud_db": float(loud[best]), "repeat": float(rep[best])}
    if log:
        log("أقوى 20 ثانية: من %s (علو %.1f dB، تكرار %.2f)" % (clock(start), loud[best], rep[best]))
    return start, info


def clock(t):
    return "%d:%04.1f" % (int(t // 60), t % 60)


# ----------------------------------------------------------------------------- bars strip

class Bars:
    """Draws mirrored, rounded spectrum bars (and an optional progress line) on a
    transparent strip; frames with the same quantised heights are reused."""

    def __init__(self, w, h, nbars, color=GOLD, fill_ratio=0.5, progress=False):
        self.w, self.h, self.n = w, h, nbars
        self.color = color
        self.progress = progress
        self.bar_area = h - (26 if progress else 0)
        slot = w / float(nbars)
        self.bw = max(3, int(slot * fill_ratio))
        self.xs = [int(round(slot * i + (slot - self.bw) / 2)) for i in range(nbars)]
        self.cache_key = None
        self.cache = None

    def frame(self, heights, prog=None):
        cy = self.bar_area / 2.0
        maxh = self.bar_area - 4
        q = tuple(int(v * 60) for v in heights)
        pq = int(prog * self.w) if (self.progress and prog is not None) else -1
        key = (q, pq)
        if key == self.cache_key:
            return self.cache
        im = Image.new("RGBA", (self.w, self.h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        r = self.bw / 2.0
        for x, v in zip(self.xs, heights):
            bh = max(self.bw, v * maxh)
            # centre-weighted alpha: taller bars read brighter
            a = int(150 + 105 * min(1.0, v * 1.4))
            d.rounded_rectangle((x, cy - bh / 2, x + self.bw, cy + bh / 2), radius=r,
                                fill=self.color + (a,))
        if self.progress:
            y = self.h - 8
            d.rounded_rectangle((0, y - 2, self.w, y + 2), radius=2, fill=CREAM + (50,))
            if pq > 0:
                d.rounded_rectangle((0, y - 2, max(4, pq), y + 2), radius=2, fill=GOLD + (230,))
        self.cache_key, self.cache = key, im.tobytes()
        return self.cache


# ----------------------------------------------------------------------------- job

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s or "").strip().strip(".")
    return s[:80] or "Song"


class Job:
    def __init__(self, cover, audio, title_ar="", title_en="", artist="", clip="",
                 tag=DEFAULT_TAG, ar_font=DEFAULT_AR_FONT, en_font=DEFAULT_EN_FONT,
                 parts=ALL_PARTS, out_dir=None, progress=None, log=None, cancel=None):
        self.cover_path, self.audio, self.clip = cover, audio, (clip or "").strip()
        self.title_ar, self.title_en = (title_ar or "").strip(), (title_en or "").strip()
        self.artist = (artist or "").strip()
        self.tag = (tag or "").strip()
        self.ar_font, self.en_font = ar_font or DEFAULT_AR_FONT, en_font or DEFAULT_EN_FONT
        self.parts = set(parts)
        self.out_root = out_dir or OUTPUT_DIR
        self.progress = progress or (lambda f, msg="": None)
        self.log = log or (lambda m: print(m, flush=True))
        self.cancel = cancel or threading.Event()
        self.files = []
        self.teaser_start = None

    # -- setup ------------------------------------------------------------------
    def prepare(self):
        if not features.check("raqm"):
            raise RuntimeError("RAQM مش شغال — الحروف العربي هتطلع مقطّعة. تأكد إن ملف fribidi موجود جنب python.exe")
        if not self.cover_path or not os.path.isfile(self.cover_path):
            raise RuntimeError("اختار صورة الغلاف الأول")
        need_audio = self.parts & {"visualizer", "teaser"}
        if need_audio and (not self.audio or not os.path.isfile(self.audio)):
            raise RuntimeError("اختار ملف الماستر (الصوت) الأول")
        if not (self.title_ar or self.title_en):
            raise RuntimeError("اكتب اسم الأغنية (عربي أو إنجليزي)")
        for name in (self.ar_font, self.en_font):
            p = name if os.path.isabs(name) else os.path.join(FONTS_DIR, name)
            if not os.path.isfile(p):
                raise RuntimeError("الفونت مش موجود في فولدر fonts:\n%s" % name)
        try:
            raw = open_image(self.cover_path)
        except Exception as e:
            raise RuntimeError("مش قادر أفتح صورة الغلاف:\n%s\n%s" % (self.cover_path, e))
        had_alpha = raw.mode in ("RGBA", "LA", "PA", "P") or "transparency" in raw.info
        img, cropped = square(flatten(raw))
        self.log("الغلاف: %dx%d %s" % (raw.width, raw.height, raw.mode))
        if cropped:
            self.log("تنبيه: الغلاف مش مربع — قصّيته من النص لـ %dx%d" % img.size)
        if had_alpha:
            self.log("الغلاف فيه شفافية — اتشالت (خلفية سودا) عشان DistroKid")
        if img.width < 3000:
            self.log("تنبيه: الغلاف %dpx — هيتكبر لـ 3000. الأحسن تبعت نسخة 3000×3000 أصلية." % img.width)
        self.cover = img
        self.audio_dur = None
        if self.audio and os.path.isfile(self.audio):
            dur, _, has_a = probe(self.audio)
            if not has_a:
                raise RuntimeError("ملف الماستر مفيهوش صوت")
            self.audio_dur = dur
            self.log("الماستر: %s" % clock(dur))
        if self.clip:
            if not os.path.isfile(self.clip):
                raise RuntimeError("ملف الفيديو القصير مش موجود:\n%s" % self.clip)
            cd, has_v, _ = probe(self.clip)
            if not has_v:
                raise RuntimeError("الفيديو القصير مفيهوش صورة")
            self.clip_dur = cd
        song = self.title_en or self.title_ar
        self.dir = os.path.join(self.out_root, "%s - Release Kit" % safe_name(song))
        os.makedirs(self.dir, exist_ok=True)
        self.log("الفولدر: %s" % self.dir)

    def path(self, key):
        return os.path.join(self.dir, NAMES[key])

    def tick(self):
        if self.cancel.is_set():
            raise Cancelled()

    # -- text blocks --------------------------------------------------------------
    def title_lines(self, scale, maxw):
        """Sprites (ar, en, artist) and the gaps after each; EN/artist uppercase + tracked like the site."""
        ar = text_sprite(self.title_ar, self.ar_font, 118 * scale, CREAM, weight=700, maxw=maxw, shadow=0.6)
        if is_rtl(self.title_en):
            en = text_sprite(self.title_en, self.ar_font, (70 if ar else 118) * scale, CREAM, 700, 0, maxw, 0.55)
        else:
            en = text_sprite(self.title_en.upper(), self.en_font, (54 if ar else 92) * scale,
                             (232, 222, 206) if ar else CREAM, weight=700, tracking=0.08, maxw=maxw, shadow=0.55)
        art = line_sprite(self.artist, self.ar_font, self.en_font, 30 * scale, GOLD, weight=600,
                          tracking=0.28, maxw=maxw, shadow=0.5)
        items = [x for x in (ar, en, art) if x is not None]
        gaps = []
        for a, b in zip(items, items[1:]):
            gaps.append(int((34 if a is ar else 26) * scale))
        return items, gaps + [0]

    def column(self, im, safe_top, safe_bottom, max_cover, scale, maxw, cover_gap=64, tag=None,
               tag_gap=56, after_h=0):
        """Tag, cover, title stack centred as one group inside [safe_top, safe_bottom];
        the cover shrinks if the text needs the room. Returns the y under the text."""
        W = im.width
        items, gaps = self.title_lines(scale, maxw)
        text_h = sum(vh(x) for x in items) + sum(gaps) + after_h
        tag_h = vh(tag) + tag_gap if tag is not None else 0
        avail = safe_bottom - safe_top
        cover = int(min(max_cover, avail - text_h - cover_gap - tag_h))
        y = safe_top + (avail - (tag_h + cover + cover_gap + text_h)) / 2.0
        if tag is not None:
            place_top(im, tag, W / 2, y)
            y += tag_h
        paste_cover(im, self.cover, cover, W / 2, y + cover / 2)
        y += cover + cover_gap
        for x, g in zip(items, gaps):
            place_top(im, x, W / 2, y)
            y += vh(x) + g
        return y

    def text_block(self, im, cx, cy, scale, maxw, after_h=0):
        """Title stack centred on (cx, cy) (plus `after_h` of room below); returns the y under it."""
        items, gaps = self.title_lines(scale, maxw)
        y = cy - (sum(vh(x) for x in items) + sum(gaps) + after_h) / 2.0
        for x, g in zip(items, gaps):
            place_top(im, x, cx, y)
            y += vh(x) + g
        return y

    # -- stills ---------------------------------------------------------------------
    def img_cover(self):
        im = self.cover if self.cover.width == 3000 else self.cover.resize((3000, 3000), Image.LANCZOS)
        q, size = save_jpg(im, self.path("cover"), quality=95, max_bytes=MAX_COVER_BYTES)
        self.log("Cover 3000: JPG q%d — %.1f MB (RGB، من غير شفافية)" % (q, size / 1e6))
        self.files.append(self.path("cover"))

    def img_post(self):
        save_jpg(self.cover.resize((1080, 1080), Image.LANCZOS), self.path("post"), quality=93)
        self.files.append(self.path("post"))

    def img_thumb(self):
        W, H = 1280, 720
        im = blurred_bg(self.cover, W, H, dim=0.5)
        paste_cover(im, self.cover, 580, 70 + 290, 360)
        self.text_block(im, 650 + (W - 650) / 2, 360, 0.9, 540)
        save_jpg(im, self.path("thumb"), quality=92, max_bytes=1_900_000)
        self.files.append(self.path("thumb"))

    def img_story(self):
        im = blurred_bg(self.cover, 1080, 1920, dim=0.48)
        tag = line_sprite(self.tag, self.ar_font, self.en_font, 40, GOLD, weight=600, tracking=0.16,
                          maxw=900, shadow=0.5) if self.tag else None
        # between the IG story top bar and the reply box
        self.column(im, 250, 1600, 860, 1.0, 940, tag=tag)
        save_jpg(im, self.path("story"), quality=93)
        self.files.append(self.path("story"))

    def img_reelcover(self):
        im = blurred_bg(self.cover, 1080, 1920, dim=0.48)
        # inside the centre 3:4 crop the profile grid shows (240..1680), with margin
        self.column(im, 330, 1590, 800, 1.0, 900)
        save_jpg(im, self.path("reelcover"), quality=93)
        self.files.append(self.path("reelcover"))

    def img_portrait(self):
        im = blurred_bg(self.cover, 1080, 1350, dim=0.48)
        self.column(im, 80, 1270, 860, 0.85, 940, cover_gap=52)
        save_jpg(im, self.path("portrait"), quality=93)
        self.files.append(self.path("portrait"))

    def images(self):
        for step in (self.img_cover, self.img_post, self.img_thumb, self.img_story,
                     self.img_reelcover, self.img_portrait):
            self.tick()
            step()
        self.log("الصور: 6 مقاسات ✔")

    # -- Spotify Canvas -------------------------------------------------------------
    def canvas(self, prog):
        out = self.path("canvas")
        if self.clip:
            self.canvas_from_clip(out)
        else:
            self.canvas_from_cover(out, prog)
        self.files.append(out)

    def canvas_from_clip(self, out):
        D = self.clip_dur
        X = 1.0  # the fade ends one frame early so the last frame is the clip's own head
        need = CANVAS_SECONDS + X
        slow = ""
        if D < need:
            f = need / max(D, 0.1)
            slow = "setpts=%.5f*PTS," % f
            self.log("الكليب %.1f ث — أقصر من %.0f ث، هبطّأه %.2fx عشان الـ loop يكمل 8 ثواني" % (D, need, f))
        fc = ("[0:v]%sfps=%d,scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,"
              "crop=1080:1920,setsar=1,format=yuv420p,trim=end=%.3f,setpts=PTS-STARTPTS,split[a][b];"
              "[a]trim=start=%.3f,setpts=PTS-STARTPTS[a2];"
              "[b]trim=end=%.3f,setpts=PTS-STARTPTS[b2];"
              "[a2][b2]xfade=transition=fade:duration=%.3f:offset=%.3f,format=yuv420p[v]"
              % (slow, FPS, need, X, X, X - 1.0 / FPS, CANVAS_SECONDS - X))
        r = run_ffmpeg(["-y", "-i", self.clip, "-filter_complex", fc, "-map", "[v]", "-an",
                        "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high",
                        "-pix_fmt", "yuv420p", "-r", str(FPS), "-frames:v", str(int(CANVAS_SECONDS * FPS)),
                        "-movflags", "+faststart", out])
        check(r, "الـ Canvas")
        self.log("Spotify Canvas من الكليب: 8 ث loop (crossfade ثانية) ✔")

    def canvas_from_cover(self, out, prog):
        W, H = 1080, 1920
        n = int(CANVAS_SECONDS * FPS)
        base = blurred_bg(self.cover, W, H, dim=0.5)
        paste_cover(base, self.cover, 900, W / 2, H / 2)
        cx, cy = W / 2.0, H / 2.0
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        u = (xx * 0.5 + yy * 0.866) / (W * 0.5 + H * 0.866)  # 0..1 along the sweep direction
        light = np.array([255, 236, 205], np.float32) / 255.0
        rng = np.random.default_rng(7)
        grains = []
        for _ in range(6):
            g = rng.normal(0, 1, (H // 2, W // 2)).astype(np.float32)
            g = np.asarray(Image.fromarray(np.clip(g * 40 + 128, 0, 255).astype(np.uint8), "L")
                           .resize((W, H), Image.BILINEAR), np.float32)
            grains.append((g - 128) / 40 * 2.6)  # +-2.6 levels of fine grain
        enc = Encoder(["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-framerate", str(FPS),
                       "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                       "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
                       "-frames:v", str(n), "-movflags", "+faststart", out], self.cancel)
        try:
            for i in range(n):
                t = i / float(FPS)
                ph = 2 * math.pi * t / CANVAS_SECONDS
                s = 1.0 + 0.025 * (1 - math.cos(ph)) / 2  # soft zoom, 2.5% max, same at both ends
                dx = 6 * math.sin(ph)  # a few pixels of drift, back where it started
                fr = base.transform((W, H), Image.AFFINE,
                                    (1 / s, 0, cx - (cx + dx) / s, 0, 1 / s, cy - cy / s),
                                    resample=Image.BICUBIC)
                a = np.asarray(fr, np.float32)
                # light sweep crosses once between 1.5 s and 6 s (invisible at the loop seam)
                if 1.0 < t < 6.5:
                    p = (t - 1.5) / 4.5
                    c = -0.25 + 1.5 * p
                    env = math.sin(math.pi * min(max((t - 1.0) / 5.5, 0), 1))
                    band = np.exp(-((u - c) / 0.07) ** 2) * (0.16 * env)
                    a = a + (255 - a) * band[..., None] * light
                a += grains[i % 6][..., None]
                enc.write(np.clip(a, 0, 255).astype(np.uint8).tobytes())
                if i % 10 == 0:
                    prog(i / float(n), "Spotify Canvas… %d%%" % (100 * i // n))
        except BaseException:
            enc.abort()
            raise
        enc.close()
        self.log("Spotify Canvas من الغلاف: zoom 2.5% + light sweep + grain، 8 ث loop ✔")

    # -- audio-driven videos -----------------------------------------------------------
    def analyse(self):
        if hasattr(self, "bars"):
            return
        self.log("بحلل الصوت…")
        self.rate = 44100
        x = decode_mono(self.audio, self.rate)
        if len(x) < self.rate:
            raise RuntimeError("الماستر أقصر من ثانية")
        self.samples = x
        self.bars = spectrum_bars(x, self.rate, FPS, nbars=36, cancel=self.cancel)

    def bars_video(self, out, size, bg, strip_box, bars, audio_args, afilt, nframes, prog, label,
                   vfade=None, abitrate="320k", progress_line=False):
        W, H = size
        x0, y0, sw, sh = strip_box
        with tempfile.TemporaryDirectory(prefix="releasekit_") as tmp:
            bgp = os.path.join(tmp, "bg.png")
            buf = io.BytesIO()
            bg.save(buf, "PNG")
            with open(bgp, "wb") as fh:
                fh.write(buf.getvalue())
            vf = "[0:v][1:v]overlay=%d:%d:format=auto" % (x0, y0)
            if vfade:
                vf += "," + vfade
            fc = vf + ",format=yuv420p[v];[2:a]%s[a]" % afilt
            enc = Encoder(["-loop", "1", "-framerate", str(FPS), "-i", bgp,
                           "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (sw, sh),
                           "-framerate", str(FPS), "-i", "pipe:0"] + audio_args +
                          ["-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                           "-pix_fmt", "yuv420p", "-r", str(FPS), "-g", str(FPS * 2),
                           "-c:a", "aac", "-ac", "2", "-b:a", abitrate, "-frames:v", str(nframes),
                           "-t", "%.3f" % (nframes / float(FPS)), "-movflags", "+faststart", out],
                          self.cancel)
            nb = bars.shape[1]
            drawer = Bars(sw, sh, nb, progress=progress_line, fill_ratio=0.5)
            try:
                for i in range(nframes):
                    row = bars[min(i, len(bars) - 1)]
                    enc.write(drawer.frame(row, i / float(max(1, nframes - 1))))
                    if i % 30 == 0:
                        prog(i / float(nframes), "%s… %d%%" % (label, 100 * i // nframes))
            except BaseException:
                enc.abort()
                raise
            enc.close()

    def visualizer(self, prog):
        self.analyse()
        W, H = 1920, 1080
        bg = blurred_bg(self.cover, W, H, dim=0.45)
        paste_cover(bg, self.cover, 680, 120 + 340, 540)
        sw, sh, gap = 840, 200, 64
        bottom = self.text_block(bg, 1340, 540, 1.0, 780, after_h=gap + sh)
        strip = (1340 - sw // 2, int(bottom + gap), sw, sh)
        n = int(round(self.audio_dur * FPS))
        self.bars_video(self.path("visualizer"), (W, H), bg, strip, self.bars,
                        ["-i", self.audio], "aresample=async=1", n, prog, "YouTube Visualizer",
                        progress_line=True)
        self.files.append(self.path("visualizer"))
        self.log("YouTube Visualizer: %s — H.264 + AAC 320k ✔" % clock(self.audio_dur))

    def teaser(self, prog):
        self.analyse()
        L = min(TEASER_SECONDS, self.audio_dur)
        start, _ = strongest_window(self.samples, self.rate, L, log=self.log)
        self.teaser_start = start
        W, H = 1080, 1920
        bg = blurred_bg(self.cover, W, H, dim=0.48)
        sw, sh, gap = 800, 150, 44
        # whole group above the Reels/TikTok caption area
        bottom = self.column(bg, 230, 1580, 800, 0.95, 940, after_h=gap + sh)
        strip = ((W - sw) // 2, int(bottom + gap), sw, sh)
        n = int(round(L * FPS))
        i0 = int(round(start * FPS))
        bars = self.bars[i0:i0 + n]
        fo = max(0.0, L - 1.2)
        afilt = "afade=t=in:d=0.3,afade=t=out:st=%.3f:d=1.2,aresample=48000" % fo
        vfade = "fade=t=in:d=0.4,fade=t=out:st=%.3f:d=0.6" % max(0.0, L - 0.6)
        self.bars_video(self.path("teaser"), (W, H), bg, strip, bars,
                        ["-ss", "%.3f" % start, "-t", "%.3f" % L, "-i", self.audio], afilt, n, prog,
                        "Teaser", vfade=vfade)
        self.files.append(self.path("teaser"))
        self.log("Teaser %.0f ث: من %s لحد %s ✔" % (L, clock(start), clock(start + L)))

    # -- run --------------------------------------------------------------------------
    def write_info(self):
        lines = ["ReleaseKit", "",
                 "Song (AR): %s" % self.title_ar, "Song (EN): %s" % self.title_en, "Artist: %s" % self.artist,
                 "Cover source: %s" % os.path.basename(self.cover_path)]
        if self.audio:
            lines.append("Master: %s (%s)" % (os.path.basename(self.audio), clock(self.audio_dur or 0)))
        if self.teaser_start is not None:
            lines.append("Teaser: %s -> %s" % (clock(self.teaser_start),
                                                clock(self.teaser_start + min(TEASER_SECONDS, self.audio_dur))))
        lines += ["", "Files:"] + ["  " + os.path.basename(f) for f in self.files]
        with open(os.path.join(self.dir, "info.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def run(self):
        t0 = time.time()
        self.prepare()
        plan = [p for p in ("images", "canvas", "teaser", "visualizer") if p in self.parts]
        weights = {"images": 1, "canvas": 2, "teaser": 2, "visualizer": 6}
        if "visualizer" in plan and self.audio_dur:
            weights["visualizer"] = max(2.0, self.audio_dur / 30.0)
        total = float(sum(weights[p] for p in plan)) or 1.0
        done = 0.0
        for p in plan:
            self.tick()
            w = weights[p]

            def prog(f, msg="", _d=done, _w=w):
                self.progress(min(0.999, (_d + _w * f) / total), msg)
            prog(0, {"images": "بعمل الصور…", "canvas": "Spotify Canvas…",
                     "teaser": "بدوّر على أقوى 20 ثانية…", "visualizer": "YouTube Visualizer…"}[p])
            if p == "images":
                self.images()
            else:
                getattr(self, p)(prog)
            done += w
        self.write_info()
        self.progress(1.0, "خلص ✔")
        self.log("خلص في %.0f ث\n%s" % (time.time() - t0, self.dir))
        return self.dir, list(self.files)


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="ReleaseKit")
    ap.add_argument("--cover", required=True)
    ap.add_argument("--audio", default="")
    ap.add_argument("--clip", default="")
    ap.add_argument("--ar", default="")
    ap.add_argument("--en", default="")
    ap.add_argument("--artist", default="")
    ap.add_argument("--tag", default=DEFAULT_TAG)
    ap.add_argument("--ar-font", default=DEFAULT_AR_FONT)
    ap.add_argument("--en-font", default=DEFAULT_EN_FONT)
    ap.add_argument("--parts", default=",".join(ALL_PARTS))
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    last = [0]

    def prog(f, msg=""):
        if f - last[0] >= 0.05 or f >= 1:
            last[0] = f
            print("[%3d%%] %s" % (f * 100, msg), flush=True)
    job = Job(a.cover, a.audio, a.ar, a.en, a.artist, a.clip, a.tag, a.ar_font, a.en_font,
              [p for p in a.parts.split(",") if p], a.out, progress=prog)
    return job.run()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
