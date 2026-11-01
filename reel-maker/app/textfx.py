"""Lyric text effects shared by the lyric mode and the cinematic mode.

  fonts      every .ttf/.otf in fonts/, listed by its real family name
  packs      curated look = lyric font + gold font + word animation + line in/out + glow
  FxLine     one lyric line: layout fixed once (RTL, ≤ 2 rows); per frame only the
             state of each word changes (dim -> lit through the pack's animation)

A word's animation never runs past the next word's start, so every word is fully
readable before the next one. Transforms (scale, rise, blur, wipe) act on the word's
own sprite inside a padded box; the layout never moves, re-wraps or doubles.
Layers are premultiplied float32 RGBA; blend() puts them on an RGB frame.
"""
import glob
import json
import math
import os
import re

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import engine

FONTS_DIR = engine.FONTS_DIR
PACKS_FILE = os.path.join(engine.ROOT, "data", "packs.json")
CUSTOM_PACKS = os.path.join(engine.ROOT, "packs")
SAMPLE = "بقولك ايه *كفاية* كلام"

GOLD = engine.GOLD
WHITE = (255, 255, 255)
STROKE = (12, 9, 7)
DIM_FILL = (150, 146, 140)
DIM_GOLD = (150, 112, 52)

ANIMS = {
    "fade": "Fade",
    "pop": "Pop",
    "slide": "Slide up",
    "type": "Typewriter",
    "blur": "Blur-in",
    "glow": "Glow pulse",
    "bounce": "Bounce",
    "wave": "Wave",
    "mask": "Mask reveal",
}
ANIM_LEN = dict(fade=0.12, pop=0.24, slide=0.26, type=0.5, blur=0.28, glow=0.45, bounce=0.42,
                wave=0.5, mask=0.32)
LINE_IN = {"fade": "Fade", "slide": "Slide", "none": "None"}
LINE_OUT = {"fade": "Fade", "blur": "Blur-out", "none": "None"}
GLOWS = {  # (white glow strength, gold glow strength, white glow colour)
    "soft": (0.25, 1.1, (255, 238, 218)),
    "cinematic": (0.35, 1.6, (255, 238, 218)),
    "strong": (0.45, 2.3, (255, 238, 218)),
    "warm": (0.55, 1.6, (255, 205, 150)),
    "none": (0.0, 0.8, (255, 238, 218)),
}
GOLD_GLOW = (255, 172, 48)


# ----------------------------------------------------------------------------- fonts

_families = None


def _family(path):
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=0, lazy=True)
        n = tt["name"]
        return (n.getDebugName(16) or n.getDebugName(1) or "").strip()
    except Exception:
        pass
    try:
        with open(path, "rb") as fh:
            import io
            f = ImageFont.truetype(io.BytesIO(fh.read()), 20)
        return f.getname()[0]
    except Exception:
        return ""


def _rank(path):
    """Prefer the variable font, then Bold, then ExtraBold, then anything."""
    b = os.path.basename(path).lower()
    if "[" in b or "variable" in b:
        return 0
    for k, tag in enumerate(("-bold.", "bold", "extrabold", "black", "semibold", "medium", "regular")):
        if tag in b:
            return 1 + k
    return 9


def families(refresh=False):
    """{family name: font file} for every font in fonts/ that has Arabic letters."""
    global _families
    if _families is not None and not refresh:
        return _families
    out = {}
    paths = sorted(glob.glob(os.path.join(FONTS_DIR, "*.ttf")) + glob.glob(os.path.join(FONTS_DIR, "*.otf")))
    for p in paths:
        if not arabic_ok(p):
            continue
        fam = _family(p) or os.path.splitext(os.path.basename(p))[0]
        if fam not in out or _rank(p) < _rank(out[fam]):
            out[fam] = p
    _families = dict(sorted(out.items(), key=lambda kv: kv[0].lower()))
    return _families


def arabic_ok(path):
    """Arabic letters, joining forms and mark positioning present (shaping test)."""
    try:
        from fontTools.ttLib import TTFont
        tt = TTFont(path, fontNumber=0, lazy=True)
        cmap = tt.getBestCmap() or {}
        if not all(ord(c) in cmap for c in "ابتثجحخدذرزسشصضطظعغفقكلمنهوية"):
            return False
        if "GSUB" not in tt:
            return False
        tags = {f.FeatureTag for f in tt["GSUB"].table.FeatureList.FeatureRecord}
        return {"init", "medi", "fina"} <= tags
    except Exception:
        return False


def font_file(family_or_file):
    fam = families()
    if family_or_file in fam:
        return fam[family_or_file]
    p = family_or_file if os.path.isabs(family_or_file) else os.path.join(FONTS_DIR, family_or_file)
    if os.path.isfile(p):
        return p
    return fam.get("Cairo") or os.path.join(FONTS_DIR, "Cairo.ttf")


_norm = {}


def size_factor(path):
    """Scale so every font reads about as big as Cairo (some draw small on the em)."""
    if path in _norm:
        return _norm[path]

    def wh(p):  # line width and ink height: Naskh/Ruqaa ascenders fool either one alone
        f = engine.load_font(p, 200, 700)
        txt = "بقولك ايه كفاية كلام"
        bb = f.getbbox(txt, direction="rtl", language="ar")
        return max(1.0, f.getlength(txt, direction="rtl", language="ar")), max(1, bb[3] - bb[1])
    try:
        (w0, h0), (w1, h1) = wh(os.path.join(FONTS_DIR, "Cairo.ttf")), wh(path)
        k = math.sqrt((w0 / w1) * (h0 / float(h1)))
    except Exception:
        k = 1.0
    _norm[path] = min(1.35, max(0.85, k))
    return _norm[path]


def load(family_or_file, size):
    p = font_file(family_or_file)
    return engine.load_font(p, int(round(size * size_factor(p))), 700)


# ----------------------------------------------------------------------------- packs

def builtin_packs():
    with open(PACKS_FILE, encoding="utf-8") as fh:
        return json.load(fh)["packs"]


def custom_packs():
    out = []
    for p in sorted(glob.glob(os.path.join(CUSTOM_PACKS, "*.json"))):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            d["custom"] = True
            out.append(d)
        except (OSError, ValueError):
            continue
    return out


def all_packs():
    return builtin_packs() + custom_packs()


def get_pack(name):
    for p in all_packs():
        if p["name"] == name or p.get("id") == name:
            return dict(p)
    return dict(builtin_packs()[-1])


def default_pack():
    for p in builtin_packs():
        if p.get("default"):
            return dict(p)
    return dict(builtin_packs()[0])


def save_pack(pack):
    os.makedirs(CUSTOM_PACKS, exist_ok=True)
    name = re.sub(r'[\\/:*?"<>|]+', " ", pack["name"]).strip() or "باك"
    path = os.path.join(CUSTOM_PACKS, name + ".json")
    d = {k: pack[k] for k in ("name", "font", "gold_font", "anim", "line_in", "line_out", "glow") if k in pack}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=1)
    return path


# ----------------------------------------------------------------------------- sprites

def premul(img):
    a = np.asarray(img, dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return a


def word_sprite(word, font, fill, glow=None, pad_k=0.7):
    """Word with dark outline + shadow (+ optional glow). Returns (RGBA image, ox, oy, adv)."""
    size = font.size
    asc, desc = font.getmetrics()
    kw = engine._text_kw(word)
    adv = font.getlength(word, **kw)
    sw = max(2, int(round(size * 0.075)))
    pad = int(size * pad_k)
    w, h = int(adv + 2 * pad), int(asc + desc + 2 * pad)
    base = pad + asc
    outline = Image.new("L", (w, h), 0)
    ImageDraw.Draw(outline).text((pad, base), word, font=font, fill=255, anchor="ls",
                                 stroke_width=sw, stroke_fill=255, **kw)
    body = Image.new("L", (w, h), 0)
    ImageDraw.Draw(body).text((pad, base), word, font=font, fill=255, anchor="ls", **kw)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if glow and glow[1] > 0:
        col, strength = glow
        for radius, s in ((size * 0.16, strength), (size * 0.42, strength * 0.55)):
            ga = body.filter(ImageFilter.GaussianBlur(max(1.0, radius))).point(lambda v, s=s: int(min(255, v * s)))
            layer = Image.new("RGBA", (w, h), col + (0,))
            layer.putalpha(ga)
            out = Image.alpha_composite(out, layer)
    sh = outline.filter(ImageFilter.GaussianBlur(max(2, size // 10))).point(lambda v: int(v * 0.7))
    shadow = Image.new("RGBA", (w, h), STROKE + (0,))
    shadow.putalpha(sh)
    out.alpha_composite(shadow, (0, max(2, size // 24)))
    ring = Image.new("RGBA", (w, h), STROKE + (0,))
    ring.putalpha(outline.point(lambda v: int(v * 0.92)))
    out = Image.alpha_composite(out, ring)
    face = Image.new("RGBA", (w, h), tuple(fill) + (0,))
    face.putalpha(body)
    return Image.alpha_composite(out, face), pad, base, adv


def glow_only(word, font, color, strength, pad_k=0.7):
    """Just the glow of a word (for the Glow pulse animation)."""
    size = font.size
    asc, desc = font.getmetrics()
    kw = engine._text_kw(word)
    adv = font.getlength(word, **kw)
    pad = int(size * pad_k)
    w, h = int(adv + 2 * pad), int(asc + desc + 2 * pad)
    body = Image.new("L", (w, h), 0)
    ImageDraw.Draw(body).text((pad, pad + asc), word, font=font, fill=255, anchor="ls", **kw)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for radius, s in ((size * 0.12, strength), (size * 0.35, strength * 0.8), (size * 0.7, strength * 0.4)):
        ga = body.filter(ImageFilter.GaussianBlur(radius)).point(lambda v, s=s: int(min(255, v * s)))
        layer = Image.new("RGBA", (w, h), color + (0,))
        layer.putalpha(ga)
        out = Image.alpha_composite(out, layer)
    return out


def letter_cuts(word, font):
    """Right-edge reveal widths after each letter (RTL), measured on the shaped prefix,
    so Typewriter reveals whole joined letters without splitting glyphs."""
    kw = engine._text_kw(word)
    total = font.getlength(word, **kw)
    cuts = []
    letters = [i for i, c in enumerate(word) if not re.match("[ً-ٰٟ]", c)]
    for i in letters[1:]:
        cuts.append(min(total, font.getlength(word[:i], **kw)))
    cuts.append(total)
    return cuts, total


# ----------------------------------------------------------------------------- easing

def clamp(x):
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ease_out(p):
    return 1 - (1 - clamp(p)) ** 3


def ease_back(p, s=1.9):
    p = clamp(p) - 1
    return 1 + (s + 1) * p ** 3 + s * p ** 2


# ----------------------------------------------------------------------------- line

class FxLine:
    """One lyric line rendered with a pack.

    times: [(start, end)] per word in the reel's timeline. key: gold word index (-1 = auto).
    karaoke=True: the whole line shows dim first (cinematic); False: words appear as sung."""

    def __init__(self, text, times, key, pack, k=1.0, cy=1400, maxw=960, size=101, W=1080, karaoke=True):
        import cv2  # noqa: F401  (needed by draw)
        self.text, self.times, self.pack, self.karaoke = text, times, pack, karaoke
        words = text.split()
        self.words = [w.replace("*", "") for w in words]
        if key is None or key < 0:
            key = engine.pick_gold(words) if len(words) > 1 else -1
        self.key = key
        self.anim = pack.get("anim", "fade")
        gw, gg, gcol = GLOWS.get(pack.get("glow", "cinematic"), GLOWS["cinematic"])
        size = int(size * k)
        maxw = int(maxw * k)
        for _ in range(10):
            body = load(pack.get("font", "Cairo"), size)
            keyf = load(pack.get("gold_font", "Aref Ruqaa"), int(size * 1.16))  # Ruqaa: ×1.16×1.10 ≈ the v3.0 look
            space = body.getlength(" ") * 1.2
            lit, dim, fonts = [], [], []
            for i, w in enumerate(self.words):
                f = keyf if i == key else body
                fonts.append(f)
                if i == key:
                    lit.append(word_sprite(w, f, GOLD, glow=(GOLD_GLOW, gg)))
                    dim.append(word_sprite(w, f, DIM_GOLD))
                else:
                    lit.append(word_sprite(w, f, WHITE, glow=(gcol, gw) if gw > 0 else None))
                    dim.append(word_sprite(w, f, DIM_FILL))
            advs = [s[3] for s in lit]
            soft = {i for i, w in enumerate(self.words) if w and w[-1] in "…،,.؟?!:؛"}
            rows = engine.wrap_rows(advs, space, maxw, soft)
            if len(rows) <= 2 and max(advs or [0]) <= maxw:
                break
            size = int(size * 0.9)
        self.size = size
        asc, desc = body.getmetrics()
        row_h = (asc + desc) * 1.08
        rtl = engine.is_rtl(text)
        top = cy - row_h * len(rows) / 2.0
        place = [None] * len(self.words)
        for r, row in enumerate(rows):
            row_w = sum(advs[i] for i in row) + space * (len(row) - 1)
            base = top + r * row_h + asc + (row_h - asc - desc) / 2.0
            x = (W + row_w) / 2.0 if rtl else (W - row_w) / 2.0
            for i in row:
                if rtl:
                    x -= advs[i]
                    place[i] = (x, base)
                    x -= space
                else:
                    place[i] = (x, base)
                    x += advs[i] + space
        # word boxes in frame coordinates (sprite incl. padding)
        self.items = []
        for i in range(len(self.words)):
            img, ox, oy, adv = lit[i]
            x, b = place[i]
            bx, by = int(round(x - ox)), int(round(b - oy))
            it = dict(x=bx, y=by, w=img.width, h=img.height, lit=premul(img), dim=premul(dim[i][0]),
                      ox=ox, adv=adv)
            if self.anim == "type":
                cuts, total = letter_cuts(self.words[i], fonts[i])
                it["cuts"], it["total"] = cuts, total
            if self.anim == "glow":
                it["glow"] = premul(glow_only(self.words[i], fonts[i], GOLD_GLOW, 1.6))
            if self.anim == "blur":
                it["blurs"] = self._blurs(it["lit"], size)
            self.items.append(it)
        pad = int(size * 0.5)
        self.x0 = min(it["x"] for it in self.items) - pad
        self.y0 = min(it["y"] for it in self.items) - pad
        self.x1 = max(it["x"] + it["w"] for it in self.items) + pad
        self.y1 = max(it["y"] + it["h"] for it in self.items) + pad
        self.rtl = rtl
        # each word's animation ends before the next word starts
        self.dur = []
        L = ANIM_LEN.get(self.anim, 0.12)
        for i, (s, e) in enumerate(times):
            nxt = times[i + 1][0] if i + 1 < len(times) else s + 10
            if self.anim == "type":
                L = min(0.07 * max(1, len(self.items[i].get("cuts", [1]))), 0.6)
            self.dur.append(max(0.08, min(L, (nxt - s) * 0.9)))

    @staticmethod
    def _blurs(arr, size):
        import cv2
        out = []
        for r in (0.14, 0.08, 0.035):
            s = max(0.5, size * r)
            out.append(np.stack([cv2.GaussianBlur(arr[..., c], (0, 0), s) for c in range(4)], axis=-1))
        return out

    # -- per-word state ---------------------------------------------------------------
    def word_layer(self, it, p, i):
        """(premultiplied patch, dx, dy) of the lit word at progress p (0..1+)."""
        import cv2
        a = self.anim
        lit = it["lit"]
        if p >= 1.0 and a != "glow":
            return lit, 0, 0, 1.0
        pe = ease_out(p)
        size = self.size
        if a == "fade":
            return lit, 0, 0, clamp(p)
        if a == "slide":
            return lit, 0, int(round((1 - pe) * size * 0.28)), clamp(p * 1.6)
        if a == "wave":  # rises a little above the line and settles: a wave along the line
            return lit, 0, int(round(-math.sin(math.pi * clamp(p)) * size * 0.11 + (1 - clamp(p * 3)) * size * 0.08)), clamp(p * 2.5)
        if a == "bounce":
            dy = -abs(math.sin(math.pi * 1.5 * clamp(p))) * (1 - clamp(p)) * size * 0.16
            return lit, 0, int(round(dy)), clamp(p * 4)
        if a == "pop":
            s = 0.85 + 0.15 * ease_back(p)
            h, w = lit.shape[:2]
            M = cv2.getRotationMatrix2D((it["ox"] + it["adv"] / 2.0, h * 0.55), 0, s)
            return cv2.warpAffine(lit, M, (w, h), flags=cv2.INTER_LINEAR), 0, 0, clamp(p * 3)
        if a == "blur":
            bl = it["blurs"]
            q = clamp(p) * len(bl)
            j = int(q)
            src = bl + [lit]
            t = q - j
            arr = src[min(j, len(src) - 1)] * (1 - t) + src[min(j + 1, len(src) - 1)] * t
            return arr, 0, 0, clamp(p * 1.5)
        if a in ("mask", "type"):
            h, w = lit.shape[:2]
            if a == "mask":
                frac = pe
                shown = it["adv"] * frac
                soft = max(2.0, size * 0.18)
            else:
                cuts = it["cuts"]
                n = int(clamp(p) * len(cuts) + 0.999)
                shown = cuts[max(0, n - 1)] if n > 0 else 0.0
                soft = max(1.0, size * 0.03)
            xs = np.arange(w, dtype=np.float32)
            if self.rtl:  # reveal from the right edge of the word's advance box
                edge = it["ox"] + it["adv"] - shown
                m = np.clip((xs - edge) / soft + 0.5, 0, 1)
                m[xs >= it["ox"] + it["adv"]] = 1.0
            else:
                edge = it["ox"] + shown
                m = np.clip((edge - xs) / soft + 0.5, 0, 1)
                m[xs <= it["ox"]] = 1.0
            return lit * m[None, :, None], 0, 0, -1.0  # -1: keep the dim word under the wipe
        if a == "glow":
            g = it["glow"]
            env = math.sin(math.pi * clamp(p)) if p < 1 else 0.0
            arr = lit + g * (env * 0.9)
            return np.clip(arr, 0, 1), 0, 0, clamp(p * 4)
        return lit, 0, 0, clamp(p)

    def layer(self, t):
        """Premultiplied RGBA of the whole line at time t, plus its (x, y)."""
        W, H = self.x1 - self.x0, self.y1 - self.y0
        canvas = np.zeros((H, W, 4), np.float32)
        for i, it in enumerate(self.items):
            s = self.times[i][0]
            p = (t - s) / self.dur[i]
            x, y = it["x"] - self.x0, it["y"] - self.y0
            h, w = it["h"], it["w"]
            if p <= 0:
                if self.karaoke:
                    _over(canvas, it["dim"], x, y)
                continue
            arr, dx, dy, a = self.word_layer(it, p, i)
            if a < 0:  # wipe: the not-yet-revealed part stays dim (or empty without karaoke)
                if self.karaoke:
                    _over(canvas, it["dim"], x, y)
                _over(canvas, arr, x + dx, y + dy)
                continue
            if self.karaoke and a < 1.0:
                _over(canvas, it["dim"] * (1 - a), x, y)
            if a > 0:
                _over(canvas, arr * a, x + dx, y + dy)
        return canvas


def _over(canvas, arr, x, y):
    """Premultiplied `arr` over `canvas` at (x, y) (clipped)."""
    H, W = canvas.shape[:2]
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    src = arr[y0 - y:y1 - y, x0 - x:x1 - x]
    dst = canvas[y0:y1, x0:x1]
    dst *= (1.0 - src[..., 3:4])
    dst += src


def blend(frame, arr, x, y, alpha=1.0):
    """Premultiplied `arr` over the uint8 RGB frame at (x, y)."""
    if alpha <= 0.003:
        return frame
    H, W = frame.shape[:2]
    h, w = arr.shape[:2]
    x0, y0, x1, y1 = max(0, x), max(0, y), min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return frame
    src = arr[y0 - y:y1 - y, x0 - x:x1 - x]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = np.clip(roi * (1.0 - src[..., 3:4] * alpha) + src[..., :3] * (255.0 * alpha), 0, 255).astype(np.uint8)
    return frame


class FxSchedule:
    """Which line is on screen when (never two at once), with the pack's line in/out."""

    LINE_IN_S, LINE_OUT_S = 0.24, 0.3

    def __init__(self, lines, pack, k=1.0, cy=1400, W=1080, karaoke=True, size=101):
        self.pack, self.k = pack, k
        self.items = []
        n = len(lines)
        for i, (text, times, key) in enumerate(lines):
            show = times[0][0] - self.LINE_IN_S - 0.08
            hide = times[-1][1] + 0.45
            if i + 1 < n:
                nxt = lines[i + 1][1][0][0] - self.LINE_IN_S - 0.08
                hide = min(max(hide, min(nxt, times[-1][1] + 1.6)), nxt)
            if self.items:
                show = max(show, self.items[-1][2])
            if hide - show < 0.3:
                hide = show + 0.3
            self.items.append((FxLine(text, times, key, pack, k, cy, W=W, karaoke=karaoke, size=size), show, hide))

    def draw(self, frame, t, alpha=1.0):
        import cv2
        lin, lout = self.pack.get("line_in", "fade"), self.pack.get("line_out", "fade")
        for fl, show, hide in self.items:
            if not (show <= t <= hide):
                continue
            a_in = clamp((t - show) / self.LINE_IN_S)
            a_out = clamp((hide - t) / self.LINE_OUT_S)
            a, dy, blur = alpha, 0, 0.0
            if lin == "fade":
                a *= a_in
            elif lin == "slide":
                a *= a_in
                dy += int(round((1 - ease_out(a_in)) * 26 * self.k))
            if lout in ("fade", "blur"):
                a *= a_out
                if lout == "blur":
                    blur = (1 - a_out) * 12 * self.k
            if a <= 0.003:
                continue
            arr = fl.layer(t)
            if blur > 0.4:
                arr = np.stack([cv2.GaussianBlur(arr[..., c], (0, 0), blur) for c in range(4)], axis=-1)
            blend(frame, arr, fl.x0, fl.y0 + dy, a)
        return frame


def preview_image(pack, width=520, height=110, font=None, gold_font=None, t_frac=0.6):
    """Small RGB preview of SAMPLE in a pack (GUI pickers), mid-way through the gold word."""
    p = dict(pack)
    if font:
        p["font"] = font
    if gold_font:
        p["gold_font"] = gold_font
    words = SAMPLE.split()
    times = [(0.2 + i * 0.5, 0.6 + i * 0.5) for i in range(len(words))]
    k = width / 1080.0 * 1.4
    fl = FxLine(SAMPLE, times, -1, p, k=k, cy=height / 2, maxw=width / k * 0.95 / 1.0, size=101, W=width)
    t = times[2][0] + fl.dur[2] * t_frac
    frame = np.zeros((height, width, 3), np.uint8)
    frame[:] = (26, 23, 20)
    blend(frame, fl.layer(t), fl.x0, fl.y0)
    return Image.fromarray(frame)
