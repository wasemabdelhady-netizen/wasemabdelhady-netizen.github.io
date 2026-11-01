"""Lyric reel engine: 1080x1920 @ 30fps, Arabic text via Pillow + RAQM, video via ffmpeg.

Used by the GUI (reelmaker.py) and from the command line:
    python engine.py --bg clip.mp4 --audio song.mp3 --lyrics lyrics.txt --song "اسم" --hook "..."
"""
import io
import itertools
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from array import array
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

W, H, FPS = 1080, 1920, 30
GOLD = (0xE8, 0xB8, 0x5C)
WHITE = (255, 255, 255)
CROSSFADE = 1.5
HOOK_SECONDS = 5.0
CLOSING_SECONDS = 2.0
DEFAULT_CLOSING = "الأغنية كاملة على البروفايل 👆"
SIGNATURE = "وسيم"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONTS_DIR = os.path.join(ROOT, "fonts")
OUTPUT_DIR = os.path.join(ROOT, "output")

# Layout (pixels). Top/bottom margins keep text clear of the Reels/TikTok UI.
HOOK_Y = 330           # centre of the hook block
LYRIC_Y = 1060         # centre of the lyric block
SIGN_Y = 1585          # centre of the signature line
MAX_TEXT_W = 900

# Look: warm dark grade
GRADE = ("eq=brightness=-0.045:contrast=1.04:saturation=0.76:gamma=0.97,format=gbrp,"
         "curves=r='0/0 0.25/0.255 0.6/0.625 0.85/0.885 1/1'"
         ":g='0/0 0.6/0.605 0.85/0.86 1/0.985'"
         ":b='0/0.008 0.25/0.24 0.6/0.565 0.85/0.79 1/0.92',"
         "vignette=angle=PI/4.6")
COVER = ("scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,"
         "crop=%d:%d,setsar=1" % (W, H, W, H))

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


# ----------------------------------------------------------------------------- lyrics

TIME_RE = re.compile(
    r"^\s*\[?\s*(?:(\d{1,2}):)?(\d{1,2}):(\d{1,2}(?:[.,]\d+)?)\s*\]?\s*[-|:]?\s*"
    r"|^\s*\[?\s*(\d+(?:[.,]\d+)?)\s*\]?\s*[-|]\s*")


def parse_time(s):
    """'1:23.5', '01:02:03', '83.5' -> seconds (None if empty)."""
    s = (s or "").strip()
    if not s:
        return None
    parts = s.replace(",", ".").split(":")
    try:
        sec = 0.0
        for p in parts:
            sec = sec * 60 + float(p)
        return sec
    except ValueError:
        raise ValueError("وقت مش مفهوم: %s" % s)


def read_text(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-16", "cp1256"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and not raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def parse_lyrics(text):
    """Return list of (time_or_None, text). A timed empty line ends the previous one."""
    out = []
    for raw in text.splitlines():
        line = raw.strip().replace("\u200f", "").replace("\u200e", "")
        if not line or line.startswith("#"):
            continue
        t = None
        m = TIME_RE.match(line)
        if m:
            if m.group(4) is not None:
                t = float(m.group(4).replace(",", "."))
            else:
                t = (int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60
                     + float(m.group(3).replace(",", ".")))
            line = line[m.end():].strip()
        out.append((t, line))
    return out


# ----------------------------------------------------------------------------- vocal pauses

def envelope(audio, start, dur, rate=16000, hop=0.02):
    """Vocal-band loudness in dB per 20 ms."""
    r = run_ffmpeg(["-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", audio, "-vn", "-ac", "1",
                    "-ar", str(rate), "-af", "highpass=f=200,lowpass=f=3500",
                    "-f", "s16le", "-"])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-600:])
    pcm = array("h")
    pcm.frombytes(r.stdout[: len(r.stdout) // 2 * 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    n = int(rate * hop)
    env = []
    for i in range(0, len(pcm) - n + 1, n):
        seg = pcm[i:i + n]
        ms = sum(x * x for x in seg) / n
        env.append(10 * math.log10(ms + 1.0))
    k = 3  # 140 ms moving average
    sm = []
    for i in range(len(env)):
        a, b = max(0, i - k), min(len(env), i + k + 1)
        sm.append(sum(env[a:b]) / (b - a))
    return sm, hop


def voiced_runs(env, hop):
    if not env:
        return [], 0.0
    srt = sorted(env)
    floor = srt[int(len(srt) * 0.10)]
    peak = srt[int(len(srt) * 0.95)]
    thr = floor + 0.38 * (peak - floor)
    runs, cur = [], None
    for i, v in enumerate(env):
        if v > thr:
            if cur is None:
                cur = [i, i]
            cur[1] = i
        elif cur is not None:
            runs.append(cur)
            cur = None
    if cur is not None:
        runs.append(cur)
    merged = []
    for r in runs:  # bridge gaps shorter than 120 ms
        if merged and (r[0] - merged[-1][1]) * hop < 0.12:
            merged[-1][1] = r[1]
        else:
            merged.append(list(r))
    runs = [(a * hop, (b + 1) * hop) for a, b in merged if (b - a + 1) * hop >= 0.15]
    return runs, thr


def auto_times(lines, env, hop, total):
    """Place N lines on the vocal: pick N-1 pauses so each line's sung length
    matches its share of the letters. Returns list of (start, end)."""
    n = len(lines)
    runs, _ = voiced_runs(env, hop)
    if not runs:
        step = total / max(n, 1)
        return [(i * step, (i + 1) * step) for i in range(n)]
    weights = [max(len(re.sub(r"\s", "", l)), 1) for l in lines]
    if n == 1:
        return [(runs[0][0], runs[-1][1])]
    # cumulative voiced time at each run boundary
    cum = [0.0]
    for a, b in runs:
        cum.append(cum[-1] + (b - a))
    voiced_total = cum[-1]
    gaps = [(runs[i][1], runs[i + 1][0]) for i in range(len(runs) - 1)]
    m = len(gaps)
    if m < n - 1:
        # not enough pauses: split the voiced span by letters
        s0, s1 = runs[0][0], runs[-1][1]
        acc, out, tw = 0, [], sum(weights)
        for w in weights:
            a = s0 + (s1 - s0) * acc / tw
            acc += w
            out.append((a, s0 + (s1 - s0) * acc / tw))
        return out
    tw = float(sum(weights))
    target = [voiced_total * w / tw for w in weights]
    glen = [b - a for a, b in gaps]
    gmax = max(glen) or 1.0
    INF = float("inf")
    # dp[k][j]: best cost with line k ending right before gap j (j = index into gaps)
    # segment for line k spans runs (prev_gap+1 .. j)
    def seg_cost(k, a, b):  # runs a..b inclusive
        d = cum[b + 1] - cum[a]
        t = target[k]
        return ((d - t) / max(t, 0.6)) ** 2

    dp = [[INF] * m for _ in range(n - 1)]
    back = [[-1] * m for _ in range(n - 1)]
    for j in range(m):
        dp[0][j] = seg_cost(0, 0, j) - 0.8 * glen[j] / gmax
    for k in range(1, n - 1):
        for j in range(k, m):
            best, arg = INF, -1
            for i in range(k - 1, j):
                if dp[k - 1][i] == INF:
                    continue
                c = dp[k - 1][i] + seg_cost(k, i + 1, j)
                if c < best:
                    best, arg = c, i
            if arg >= 0:
                dp[k][j] = best - 0.8 * glen[j] / gmax
                back[k][j] = arg
    best, arg = INF, -1
    for j in range(n - 2, m):
        if dp[n - 2][j] == INF:
            continue
        c = dp[n - 2][j] + seg_cost(n - 1, j + 1, len(runs) - 1)
        if c < best:
            best, arg = c, j
    cuts = [arg]
    for k in range(n - 2, 0, -1):
        cuts.append(back[k][cuts[-1]])
    cuts.reverse()
    out, first = [], 0
    for c in cuts + [len(runs) - 1]:
        out.append((runs[first][0], runs[c][1]))
        first = c + 1
    return out


# ----------------------------------------------------------------------------- text

ARABIC_RE = re.compile("[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
EMOJI_RE = re.compile("([\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF]"
                      "[\uFE0F\u200D\U0001F3FB-\U0001F3FF\U0001F000-\U0001FAFF\u2600-\u27BF]*)")
HARAKAT_RE = re.compile("[\u064B-\u065F\u0670\u0640]")


def font_list():
    try:
        names = sorted(f for f in os.listdir(FONTS_DIR) if f.lower().endswith((".ttf", ".otf")))
    except OSError:
        names = []
    return names


def load_font(name_or_path, size, weight=None):
    path = name_or_path if os.path.isabs(name_or_path) else os.path.join(FONTS_DIR, name_or_path)
    with open(path, "rb") as fh:  # bytes: works with any (Arabic) path on Windows
        data = fh.read()
    f = ImageFont.truetype(io.BytesIO(data), size, layout_engine=ImageFont.Layout.RAQM)
    if weight:
        try:
            axes = f.get_variation_axes()
            f.set_variation_by_axes([max(a["minimum"], min(a["maximum"], weight))
                                     if a.get("name") in (b"Weight", "Weight") else a["default"]
                                     for a in axes])
        except Exception:
            pass
    return f


_emoji_font = None


def emoji_font():
    global _emoji_font
    if _emoji_font is None:
        cands = [os.path.join(FONTS_DIR, "emoji", "NotoColorEmoji.ttf"),
                 r"C:\Windows\Fonts\seguiemj.ttf",
                 "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"]
        _emoji_font = False
        for c in cands:
            if os.path.isfile(c):
                for size in (109, 64):
                    try:
                        with open(c, "rb") as fh:
                            _emoji_font = (ImageFont.truetype(io.BytesIO(fh.read()), size), size)
                        break
                    except OSError:
                        continue
            if _emoji_font:
                break
    return _emoji_font or None


def is_rtl(text):
    return bool(ARABIC_RE.search(text))


class Glyphs:
    """A rendered word with soft shadow. `adv` is the layout width; the sprite may
    stick out on both sides (Ruqaa swashes) by `ox` pixels on the left."""

    def __init__(self, img, ox, oy, adv):
        self.img, self.ox, self.oy, self.adv = img, ox, oy, adv


def _text_kw(text):
    if is_rtl(text):
        return dict(direction="rtl", language="ar")
    return dict(direction="ltr")


def render_word(word, font, color, shadow=0.55, blur=None):
    """Render one word (may contain emoji) onto a transparent sprite with shadow.
    Origin is the baseline-left of the word's advance box."""
    size = font.size
    asc, desc = font.getmetrics()
    parts = [p for p in EMOJI_RE.split(word) if p]
    pieces = []  # (kind, text, adv, bbox)
    for p in parts:
        if EMOJI_RE.fullmatch(p):
            ef = emoji_font()
            if not ef:
                continue
            efont, esize = ef
            scale = size * 0.92 / esize
            try:
                adv = efont.getlength(p)
            except Exception:
                continue
            pieces.append(("emoji", p, adv * scale * 1.05 + size * 0.04, (efont, esize, scale)))
        else:
            kw = _text_kw(p)
            pieces.append(("text", p, font.getlength(p, **kw), kw))
    # visual order of pieces inside the word
    if is_rtl(word):
        pieces.reverse()
    adv_total = sum(p[2] for p in pieces)
    pad = int(size * 0.6)
    blur = blur if blur is not None else max(4, size // 9)
    w = int(adv_total + 2 * pad)
    h = int(asc + desc + 2 * pad)
    color_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    mask = Image.new("L", (w, h), 0)
    d_col = ImageDraw.Draw(color_layer)
    d_mask = ImageDraw.Draw(mask)
    x = pad
    base = pad + asc
    for kind, p, adv, extra in pieces:
        if kind == "text":
            d_col.text((x, base), p, font=font, fill=color + (255,), anchor="ls", **extra)
            d_mask.text((x, base), p, font=font, fill=255, anchor="ls", **extra)
        else:
            efont, esize, scale = extra
            bb = efont.getbbox(p)
            em = Image.new("RGBA", (bb[2] + 4, bb[3] + 4), (0, 0, 0, 0))
            ImageDraw.Draw(em).text((0, 0), p, font=efont, embedded_color=True)
            em = em.crop(em.getbbox() or (0, 0, 1, 1))
            th = int(size * 0.95)
            tw = max(1, int(em.width * th / max(em.height, 1)))
            em = em.resize((tw, th), Image.LANCZOS)
            ey = int(base - th * 0.86)
            ex = int(x + (adv - tw) / 2)
            color_layer.alpha_composite(em, (ex, ey))
            mask.paste(255, (ex, ey), em.getchannel("A"))
        x += adv
    # soft shadow: blurred, slightly offset, under the text
    sh_a = mask.filter(ImageFilter.GaussianBlur(blur)).point(lambda v: int(v * shadow))
    tight = mask.filter(ImageFilter.GaussianBlur(max(1, blur // 4))).point(lambda v: int(v * shadow * 0.6))
    shadow_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    black = Image.new("RGBA", (w, h), (8, 5, 2, 255))
    off = max(2, size // 22)
    shadow_img.paste(black, (0, off), sh_a)
    tight_img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    tight_img.paste(black, (0, max(1, off // 2)), tight)
    out = Image.alpha_composite(shadow_img, tight_img)
    out = Image.alpha_composite(out, color_layer)
    return Glyphs(out, pad, base, adv_total)


def clean_word(w):
    return w.replace("*", "")


def pick_gold(words):
    """Index of the highlighted word: *marked* word, else the longest word (later wins)."""
    for i, w in enumerate(words):
        if w.startswith("*") and w.endswith("*") and len(w) > 2:
            return i
    best, bi = -1, len(words) - 1
    for i, w in enumerate(words):
        core = HARAKAT_RE.sub("", EMOJI_RE.sub("", clean_word(w)))
        core = re.sub(r"[^\w]", "", core)
        if len(core) >= best:
            best, bi = len(core), i
    return bi


def wrap_rows(advs, space, maxw, soft=()):
    """Split word advances into the fewest balanced rows that fit maxw.
    `soft` = indices of words after which a break reads naturally (… ، ؟)."""
    n = len(advs)
    total = sum(advs) + space * (n - 1)
    if total <= maxw or n == 1:
        return [list(range(n))]
    for rows in range(2, n + 1):
        best = None
        # try all cut combinations for rows <= 3, greedy otherwise
        if rows <= 3:
            for cuts in itertools.combinations(range(1, n), rows - 1):
                idx = [0] + list(cuts) + [n]
                widths = [sum(advs[idx[k]:idx[k + 1]]) + space * (idx[k + 1] - idx[k] - 1)
                          for k in range(rows)]
                score = max(widths) * (0.88 ** sum(1 for c in cuts if c - 1 in soft))
                if best is None or score < best[0]:
                    best = (score, idx)
            if best and max(sum(advs[best[1][k]:best[1][k + 1]])
                            + space * (best[1][k + 1] - best[1][k] - 1)
                            for k in range(rows)) <= maxw:
                idx = best[1]
                return [list(range(idx[k], idx[k + 1])) for k in range(rows)]
        else:
            break
    # greedy fallback
    out, cur, cw = [], [], 0
    for i, a in enumerate(advs):
        add = a if not cur else a + space
        if cur and cw + add > maxw:
            out.append(cur)
            cur, cw = [i], a
        else:
            cur.append(i)
            cw += add
    if cur:
        out.append(cur)
    return out


class TextBlock:
    """A line of words laid out centred around (cx, cy); each word keeps its own
    sprite so it can animate on its own."""

    def __init__(self, text, font_name, size, cy, weight=None, gold=True, maxw=MAX_TEXT_W,
                 color=WHITE, max_rows=3, line_gap=1.0, shadow=0.55):
        words = text.split()
        self.words = words
        gi = pick_gold(words) if gold and words else -1
        for attempt in range(12):
            font = load_font(font_name, size, weight)
            space = font.getlength(" ") * 1.15
            sprites = [render_word(clean_word(w), font, GOLD if i == gi else color, shadow=shadow)
                       for i, w in enumerate(words)]
            advs = [s.adv for s in sprites]
            soft = {i for i, w in enumerate(words) if re.search("[…،,.؟?!:؛]$", clean_word(w))}
            rows = wrap_rows(advs, space, maxw, soft)
            if len(rows) <= max_rows and max(advs or [0]) <= maxw:
                break
            size = int(size * 0.9)
        asc, desc = font.getmetrics()
        row_h = (asc + desc) * line_gap
        rtl = is_rtl(text)
        self.size = size
        self.items = []  # (sprite, x, y) in reading order
        top = cy - row_h * len(rows) / 2.0
        for r, row in enumerate(rows):
            row_w = sum(advs[i] for i in row) + space * (len(row) - 1)
            base_y = top + r * row_h + asc + (row_h - asc - desc) / 2.0
            if rtl:
                x = (W + row_w) / 2.0
                for i in row:
                    x -= advs[i]
                    self.items.append((sprites[i], x, base_y))
                    x -= space
            else:
                x = (W - row_w) / 2.0
                for i in row:
                    self.items.append((sprites[i], x, base_y))
                    x += advs[i] + space

    def draw(self, canvas, alphas, dys):
        for (g, x, y), a, dy in zip(self.items, alphas, dys):
            if a <= 0.003:
                continue
            img = g.img
            if a < 0.997:
                img = img.copy()
                lut = [int(v * a) for v in range(256)]
                img.putalpha(img.getchannel("A").point(lut))
            canvas.alpha_composite(img, (int(round(x - g.ox)), int(round(y - g.oy + dy))))

    def key(self, alphas, dys):
        return tuple((int(a * 255), int(round(d))) for a, d in zip(alphas, dys))


def ease_out(p):
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def fade(t, t0, t1, fin=0.35, fout=0.45):
    if t < t0 or t > t1:
        return 0.0
    a = 1.0
    if fin > 0:
        a = min(a, (t - t0) / fin)
    if fout > 0:
        a = min(a, (t1 - t) / fout)
    return max(0.0, min(1.0, a))


# ----------------------------------------------------------------------------- render

def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip().strip(".")
    return s[:60] or "reel"


class Job:
    def __init__(self, bg, audio, lyrics, song="", hook="", closing=DEFAULT_CLOSING,
                 lyric_font="ArefRuqaa-Bold.ttf", hook_font="Alexandria.ttf",
                 start=None, end=None, out_dir=None, progress=None, log=None, cancel=None,
                 lines=None, out_name=None, thumb_text=None, bg_crop=None, pack=None):
        self.bg, self.audio, self.lyrics = bg, audio, lyrics
        self.song, self.hook = song.strip(), hook.strip()
        self.closing = closing.strip() if closing is not None else DEFAULT_CLOSING
        self.lyric_font, self.hook_font = lyric_font, hook_font
        self.start, self.end = start, end
        self.out_dir = out_dir or OUTPUT_DIR
        self.progress = progress or (lambda f, msg="": None)
        self.log = log or (lambda m: print(m, flush=True))
        self.cancel = cancel or threading.Event()
        # auto mode: ready-made [(start, end, text, word_starts|None)] in the audio's timeline,
        # a fixed output name (no extension) and the thumbnail text
        self.preset, self.out_name, self.thumb_text = lines, out_name, thumb_text
        self.word_starts = {}
        self.cover = (bg_crop + "," + COVER) if bg_crop else COVER  # bg_crop: drop letterbox bars
        self.pack = pack  # textfx style pack (None = the classic lyric look)
        self.fx = None

    # -- timing ---------------------------------------------------------------
    def plan(self):
        if not features.check("raqm"):
            raise RuntimeError("RAQM مش شغال — الحروف العربي هتطلع مقطّعة. تأكد إن ملف fribidi موجود جنب python.exe")
        for p, label in ((self.bg, "فيديو الخلفية"), (self.audio, "الصوت")):
            if not p or not os.path.isfile(p):
                raise RuntimeError("اختار %s الأول" % label)
        a_dur, _, has_a = probe(self.audio)
        if not has_a:
            raise RuntimeError("ملف الصوت مفيهوش صوت")
        bg_dur, has_v, _ = probe(self.bg)
        if not has_v:
            raise RuntimeError("ملف الخلفية مفيهوش فيديو")
        s = max(0.0, self.start or 0.0)
        e = min(a_dur, self.end) if self.end else a_dur
        if e - s < 3:
            raise RuntimeError("الريل لازم يكون 3 ثواني على الأقل")
        self.a0, self.dur, self.bg_dur = s, e - s, bg_dur
        self.log("الصوت: %.1f ث (من %.1f لحد %.1f) — الخلفية: %.1f ث" % (self.dur, s, e, bg_dur))

        entries = []
        if self.lyrics and os.path.isfile(self.lyrics):
            entries = parse_lyrics(read_text(self.lyrics))
        texts = [t for _, t in entries if t]
        timed = [e for e in entries if e[0] is not None]
        lyric_end = self.dur - CLOSING_SECONDS - 0.15 if self.closing else self.dur - 0.3
        lines = []
        if self.preset is not None:
            for item in self.preset:
                ts, te, txt = item[0] - s, min(item[1] - s, lyric_end), item[2]
                ws = item[3] if len(item) > 3 else None
                if not txt or te - max(ts, 0.0) < 0.5:
                    continue
                if ws:
                    self.word_starts[len(lines)] = [w - s for w in ws]
                lines.append((max(ts, 0.0), te, txt))
            self.log("الكلمات: %d سطر (توقيت أوتوماتيك)" % len(lines))
        elif timed and len(timed) == len(entries):
            # times are in the audio file's timeline
            for i, (t, txt) in enumerate(entries):
                if not txt:
                    continue
                ts = t - s
                nxt = entries[i + 1][0] - s if i + 1 < len(entries) else None
                te = nxt - 0.05 if nxt is not None else ts + 6.0
                te = min(te, ts + 9.0, lyric_end)
                ts = max(ts, 0.0)  # a line already running when the trim starts
                if te - ts >= 0.6:
                    lines.append((ts, te, txt))
            self.log("الكلمات: %d سطر بالتوقيت اللي في الملف" % len(lines))
        elif texts:
            if timed:
                self.log("تنبيه: بعض السطور ليها وقت وبعضها لأ — هتجاهل الأوقات وأحدد من الصوت")
            self.log("بحدد أماكن السطور من وقفات الصوت…")
            env, hop = envelope(self.audio, s, self.dur)
            # don't place lyrics inside the closing window
            limit = int(lyric_end / hop)
            spans = auto_times(texts, env[:limit], hop, lyric_end)
            for i, ((a, b), txt) in enumerate(zip(spans, texts)):
                ts = max(0.0, a - 0.12)
                nxt = spans[i + 1][0] - 0.12 if i + 1 < len(spans) else None
                if nxt is None:
                    te = b + 1.0
                elif nxt - b < 2.5:
                    te = nxt - 0.05  # short breath: hold the line until the next one
                else:
                    te = min(b + 1.0, nxt - 0.05)
                te = min(te, lyric_end)
                if te - ts >= 0.5:
                    lines.append((ts, te, txt))
            for ts, te, txt in lines:
                self.log("  %6.2f → %6.2f  %s" % (ts + s, te + s, txt))
        self.lines = lines
        return lines

    # -- graphics -------------------------------------------------------------
    def build(self):
        self.blocks = []
        for k, (ts, te, txt) in enumerate(self.lines):
            blk = TextBlock(txt, self.lyric_font, 86, LYRIC_Y, weight=700, gold=True)
            n = len(blk.items)
            ws = self.word_starts.get(k)
            if ws and len(ws) == n:  # sung word times (auto mode)
                starts, last = [], ts
                for w in ws:
                    last = min(max(w, last), max(ts, te - 0.5))
                    starts.append(last)
                self.blocks.append((ts, te, blk, starts))
                continue
            span = min(max((te - ts) * 0.6, 0.22 * n), 0.55 * n, (te - ts) - 0.5)
            span = max(span, 0.0)
            lens = [max(len(HARAKAT_RE.sub("", clean_word(w))), 1) for w in blk.words]
            tot = float(sum(lens))
            starts, acc = [], 0
            for L in lens:
                starts.append(ts + span * acc / tot)
                acc += L
            # items are in reading order, same as words
            self.blocks.append((ts, te, blk, starts))
        self.hook_blk = (TextBlock(self.hook, self.hook_font, 66, HOOK_Y, weight=700, gold=True,
                                   maxw=880, max_rows=3, line_gap=1.05)
                         if self.hook else None)
        self.close_blk = (TextBlock(self.closing, self.hook_font, 58, LYRIC_Y, weight=600,
                                    gold=False, maxw=880, line_gap=1.1)
                          if self.closing else None)
        self.sign = self.signature_sprite()
        if self.pack:
            self.build_fx()

    def build_fx(self):
        """Lines through textfx: words appear with the pack's animation as they are sung."""
        import textfx
        lines, spans = [], []
        for ts, te, blk, starts in self.blocks:
            times = [(s, starts[i + 1] if i + 1 < len(starts) else te) for i, s in enumerate(starts)]
            lines.append((" ".join(blk.words), times, -1))
            spans.append((ts, te))
        self.fx = textfx.FxSchedule(lines, self.pack, cy=LYRIC_Y, karaoke=False, size=92, spans=spans)

    def signature_sprite(self):
        name_f = load_font(self.lyric_font, 50, 700)
        song_f = load_font(self.hook_font, 30, 400)
        parts = [render_word(SIGNATURE, name_f, GOLD, shadow=0.6)]
        if self.song:
            parts.append(render_word("·", song_f, (230, 220, 205), shadow=0.6))
            parts.append(render_word(self.song, song_f, (236, 228, 214), shadow=0.6))
        canvas = Image.new("RGBA", (W, 240), (0, 0, 0, 0))
        gap = 18
        total = sum(p.adv for p in parts) + gap * (len(parts) - 1)
        x = (W + total) / 2.0  # RTL: the signature sits on the right
        base = 140
        for p in parts:
            x -= p.adv
            canvas.alpha_composite(p.img, (int(x - p.ox), int(base - p.oy)))
            x -= gap
        bb = canvas.getbbox() or (0, 0, 1, 1)
        img = canvas.crop((0, bb[1], W, bb[3]))
        return img

    def frame(self, t):
        """Return (key, RGBA image or None). Same key => identical frame."""
        layers = []
        fx_on = self.fx is not None and self.fx.visible(t)
        for ts, te, blk, starts in ([] if self.fx is not None else self.blocks):
            if t < ts - 0.01 or t > te + 0.01:
                continue
            out_a = fade(t, ts, te, fin=0, fout=0.4)
            out_dy = -10 * (1 - min(1.0, (te - t) / 0.4)) if te - t < 0.4 else 0
            alphas, dys = [], []
            for st in starts:
                p = ease_out((t - st) / 0.5)
                alphas.append(p * out_a)
                dys.append((1 - p) * 24 + out_dy)
            layers.append((blk, alphas, dys))
        if self.hook_blk and t < HOOK_SECONDS:
            a = fade(t, 0.0, HOOK_SECONDS, fin=0.3, fout=0.5)
            n = len(self.hook_blk.items)
            dy = (1 - ease_out(t / 0.6)) * 16
            layers.append((self.hook_blk, [a] * n, [dy] * n))
        if self.close_blk and t >= self.dur - CLOSING_SECONDS:
            t0 = self.dur - CLOSING_SECONDS
            a = fade(t, t0, self.dur + 1, fin=0.35, fout=0)
            n = len(self.close_blk.items)
            dy = (1 - ease_out((t - t0) / 0.5)) * 20
            layers.append((self.close_blk, [a] * n, [dy] * n))
        sign_a = fade(t, 0.4, self.dur + 1, fin=0.8, fout=0) * 0.92
        key = (int(sign_a * 255),) + tuple((id(b), b.key(a, d)) for b, a, d in layers)
        if fx_on:  # pack text changes every frame while a line is up
            key += (("fx", int(round(t * FPS))),)
            layers.append(("fx", t, None))
        return key, layers, sign_a

    def compose(self, layers, sign_a):
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        if sign_a > 0:
            s = self.sign
            if sign_a < 0.997:
                s = s.copy()
                s.putalpha(s.getchannel("A").point([int(v * sign_a) for v in range(256)]))
            canvas.alpha_composite(s, (0, int(SIGN_Y - s.height / 2)))
        import_fx = None
        for blk, alphas, dys in layers:
            if blk == "fx":
                import textfx as import_fx
                for arr, x, y, a in self.fx.layers(alphas):
                    cx, cy = max(0, -x), max(0, -y)  # clip to the frame
                    arr = arr[cy:H - y if y + arr.shape[0] > H else None, cx:W - x if x + arr.shape[1] > W else None]
                    if arr.size:
                        canvas.alpha_composite(import_fx.to_rgba(arr, a), (x + cx, y + cy))
                continue
            blk.draw(canvas, alphas, dys)
        return canvas

    # -- output ---------------------------------------------------------------
    def names(self):
        os.makedirs(self.out_dir, exist_ok=True)
        if self.out_name:
            p = os.path.join(self.out_dir, self.out_name)
            return p + ".mp4", p + ".jpg"
        base = safe_name(self.song or os.path.splitext(os.path.basename(self.audio))[0])
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return (os.path.join(self.out_dir, "%s_reel_%s.mp4" % (base, stamp)),
                os.path.join(self.out_dir, "%s_thumb_%s.jpg" % (base, stamp)))

    def check(self, r, what):
        if r.returncode != 0:
            raise RuntimeError("ffmpeg وقع في %s:\n%s" % (what, r.stderr.decode("utf-8", "replace")[-900:]))

    def loop_unit(self, tmp):
        """Seamless loop: the clip's tail crossfades into its own head."""
        D = self.bg_dur
        X = min(CROSSFADE, D / 2.5)
        unit = os.path.join(tmp, "loop.mp4")
        fc = ("[0:v]fps=%d,%s,format=yuv420p,split[a][b];"
              "[a]trim=start=%.3f,setpts=PTS-STARTPTS[a2];"
              "[b]trim=end=%.3f,setpts=PTS-STARTPTS[b2];"
              "[a2][b2]xfade=transition=fade:duration=%.3f:offset=%.3f,format=yuv420p[v]"
              % (FPS, self.cover, X, X, X, D - 2 * X))
        self.log("الخلفية أقصر من الصوت — بعمل loop بـ crossfade %.1f ث…" % X)
        r = run_ffmpeg(["-y", "-i", self.bg, "-filter_complex", fc, "-map", "[v]", "-an",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", unit])
        self.check(r, "الـ loop")
        return unit

    def thumbnail(self, path, src):
        t = min(1.0, self.bg_dur / 2)
        tmp_png = path + ".bg.png"
        r = run_ffmpeg(["-y", "-ss", "%.2f" % t, "-i", src, "-frames:v", "1", "-vf",
                        "%s,%s,eq=brightness=-0.06" % (self.cover, GRADE), tmp_png])
        self.check(r, "الصورة المصغّرة")
        with open(tmp_png, "rb") as fh:
            bg = Image.open(io.BytesIO(fh.read())).convert("RGBA")
        os.remove(tmp_png)
        text = self.hook if self.thumb_text is None else self.thumb_text.strip()
        if text:
            blk = TextBlock(text, self.hook_font, 118, H * 0.47, weight=800, gold=True,
                            maxw=920, max_rows=4, line_gap=1.08, shadow=0.75)
            blk.draw(bg, [1] * len(blk.items), [0] * len(blk.items))
        bg.alpha_composite(self.sign, (0, int(SIGN_Y - self.sign.height / 2)))
        out = io.BytesIO()
        bg.convert("RGB").save(out, "JPEG", quality=92, subsampling=0)
        with open(path, "wb") as fh:
            fh.write(out.getvalue())

    def run(self):
        t_start = time.time()
        self.plan()
        self.progress(0.02, "بجهز الكلام…")
        self.build()
        out_mp4, out_jpg = self.names()
        with tempfile.TemporaryDirectory(prefix="reel_") as tmp:
            src = self.bg
            if self.bg_dur < self.dur + 0.05:
                src = self.loop_unit(tmp)
            if self.cancel.is_set():
                raise Cancelled()
            self.progress(0.05, "بعمل الصورة المصغّرة…")
            self.thumbnail(out_jpg, self.bg)
            total = int(round(self.dur * FPS))
            fo = max(0.0, self.dur - 0.8)
            afilt = "afade=t=out:st=%.3f:d=0.8" % fo
            if self.a0 > 0:
                afilt = "afade=t=in:d=0.15," + afilt
            fc = ("[0:v]fps=%d,%s,%s,tpad=stop_mode=clone:stop_duration=2[bg];"
                  "[2:v]format=rgba[ov];"
                  "[bg][ov]overlay=0:0:format=gbrp:eof_action=pass,format=yuv420p[v];"
                  "[1:a]%s,aresample=48000[a]" % (FPS, self.cover if src == self.bg else COVER, GRADE, afilt))
            cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
                   "-stream_loop", "-1", "-t", "%.3f" % (self.dur + 1.0), "-i", src,
                   "-ss", "%.3f" % self.a0, "-t", "%.3f" % self.dur, "-i", self.audio,
                   "-f", "rawvideo", "-pix_fmt", "rgba", "-s", "%dx%d" % (W, H),
                   "-framerate", str(FPS), "-i", "pipe:0",
                   "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
                   "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high",
                   "-pix_fmt", "yuv420p", "-r", str(FPS),
                   "-c:a", "aac", "-b:a", "192k", "-frames:v", str(total), "-t", "%.3f" % self.dur,
                   "-movflags", "+faststart", out_mp4]
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, creationflags=NO_WINDOW)
            err = []
            th = threading.Thread(target=lambda: err.append(p.stderr.read()), daemon=True)
            th.start()
            last_key, last_bytes = None, None
            try:
                for i in range(total):
                    if self.cancel.is_set():
                        raise Cancelled()
                    t = i / float(FPS)
                    key, layers, sign_a = self.frame(t)
                    if key != last_key:
                        last_bytes = self.compose(layers, sign_a).tobytes()
                        last_key = key
                    p.stdin.write(last_bytes)
                    if i % 15 == 0:
                        self.progress(0.07 + 0.92 * i / total,
                                      "بعمل الفيديو… %d%%" % int(100 * i / total))
                p.stdin.close()
            except (BrokenPipeError, OSError, Cancelled) as e:
                try:
                    p.kill()
                except Exception:
                    pass
                p.wait()
                th.join(2)
                if os.path.exists(out_mp4):
                    try:
                        os.remove(out_mp4)
                    except OSError:
                        pass
                if isinstance(e, Cancelled):
                    raise
                raise RuntimeError("ffmpeg وقف:\n" + b"".join(err).decode("utf-8", "replace")[-900:])
            p.wait()
            th.join(5)
            if p.returncode != 0:
                raise RuntimeError("ffmpeg وقع:\n" + b"".join(err).decode("utf-8", "replace")[-900:])
        self.progress(1.0, "خلص ✔")
        self.log("خلص في %.0f ث\n%s\n%s" % (time.time() - t_start, out_mp4, out_jpg))
        return out_mp4, out_jpg


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Lyric reel maker")
    ap.add_argument("--bg", required=True)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--lyrics", default="")
    ap.add_argument("--song", default="")
    ap.add_argument("--hook", default="")
    ap.add_argument("--closing", default=DEFAULT_CLOSING)
    ap.add_argument("--lyric-font", default="ArefRuqaa-Bold.ttf")
    ap.add_argument("--hook-font", default="Alexandria.ttf")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args(argv)
    job = Job(a.bg, a.audio, a.lyrics, a.song, a.hook, a.closing, a.lyric_font, a.hook_font,
              parse_time(a.start), parse_time(a.end), a.out)
    last = [0]

    def prog(f, msg=""):
        if f - last[0] >= 0.1 or f >= 1:
            last[0] = f
            print("[%3d%%] %s" % (f * 100, msg), flush=True)
    job.progress = prog
    return job.run()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
