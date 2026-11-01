"""Cinematic mode — text: karaoke lines, the big sound-off hook, the red stamp, signature.

Layout of a line is computed once (fixed positions, RTL); a frame only changes how
lit each word is (short crossfade dim -> lit), so text never jumps or doubles.
Layers are premultiplied float32 RGBA blended into the RGB frame with numpy.
"""
import math

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

import engine

GOLD = engine.GOLD
WHITE = (255, 255, 255)
STAMP_RED = (214, 32, 48)
BODY_FONT = "Cairo.ttf"
KEY_FONT = "ArefRuqaa-Bold.ttf"
XFADE = 0.12          # dim -> lit crossfade per word
LINE_IN, LINE_OUT = 0.22, 0.28
DIM_FILL = (150, 146, 140)       # a word before it is sung: grey inside the dark outline
DIM_GOLD = (150, 112, 52)
STROKE = (12, 9, 7)
GOLD_GLOW = (255, 172, 48)
LYRIC_SIZE = 101                 # px at 1080 wide (was 78: +30 % for sound-off reading)


def premul(img):
    """PIL RGBA -> float32 premultiplied [h, w, 4] in 0..1."""
    a = np.asarray(img, dtype=np.float32) / 255.0
    a[..., :3] *= a[..., 3:4]
    return a


def with_glow(g, color, radius, strength):
    """Add a soft coloured glow under a rendered word (same geometry)."""
    a = g.img.getchannel("A")
    glow_a = a.filter(ImageFilter.GaussianBlur(radius)).point(lambda v: int(min(255, v * strength)))
    glow = Image.new("RGBA", g.img.size, color + (0,))
    glow.putalpha(glow_a)
    return engine.Glyphs(Image.alpha_composite(glow, g.img), g.ox, g.oy, g.adv)


def fade_alpha(g, a):
    img = g.img.copy()
    img.putalpha(img.getchannel("A").point([int(v * a) for v in range(256)]))
    return engine.Glyphs(img, g.ox, g.oy, g.adv)


def stroked_word(word, font, fill, glow=None, k=1.0):
    """One word with a dark outline + soft drop shadow (readable on any background),
    optionally a glow (colour, strength). Same geometry contract as engine.render_word."""
    size = font.size
    asc, desc = font.getmetrics()
    kw = engine._text_kw(word)
    adv = font.getlength(word, **kw)
    sw = max(2, int(round(size * 0.075)))
    pad = int(size * 0.7)
    w, h = int(adv + 2 * pad), int(asc + desc + 2 * pad)
    base = pad + asc
    outline = Image.new("L", (w, h), 0)
    ImageDraw.Draw(outline).text((pad, base), word, font=font, fill=255, anchor="ls",
                                 stroke_width=sw, stroke_fill=255, **kw)
    body = Image.new("L", (w, h), 0)
    ImageDraw.Draw(body).text((pad, base), word, font=font, fill=255, anchor="ls", **kw)
    out = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if glow:
        col, strength = glow
        for radius, s in ((size * 0.16, strength), (size * 0.42, strength * 0.55)):
            ga = body.filter(ImageFilter.GaussianBlur(max(1.0, radius))).point(lambda v, s=s: int(min(255, v * s)))
            layer = Image.new("RGBA", (w, h), col + (0,))
            layer.putalpha(ga)
            out = Image.alpha_composite(out, layer)
    sh = outline.filter(ImageFilter.GaussianBlur(max(2, size // 10))).point(lambda v: int(v * 0.7))
    shadow = Image.new("RGBA", (w, h), STROKE + (0,))
    shadow.putalpha(sh)
    off = max(2, size // 24)
    out.alpha_composite(shadow, (0, off))
    ring = Image.new("RGBA", (w, h), STROKE + (0,))
    ring.putalpha(outline.point(lambda v: int(v * 0.92)))
    out = Image.alpha_composite(out, ring)
    face = Image.new("RGBA", (w, h), tuple(fill) + (0,))
    face.putalpha(body)
    out = Image.alpha_composite(out, face)
    return engine.Glyphs(out, pad, base, adv)


class Layer:
    """A premultiplied RGBA patch at (x, y) in frame pixels."""

    def __init__(self, arr, x, y):
        self.a, self.x, self.y = arr, int(x), int(y)


def blend(frame, arr, x, y, alpha=1.0):
    """Premultiplied `arr` over the uint8 RGB frame at (x, y), clipped to the frame."""
    if alpha <= 0.003:
        return frame
    H, W = frame.shape[:2]
    h, w = arr.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    if x1 <= x0 or y1 <= y0:
        return frame
    src = arr[y0 - y:y1 - y, x0 - x:x1 - x]
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    rgb, a = src[..., :3] * (255.0 * alpha), src[..., 3:4] * alpha
    frame[y0:y1, x0:x1] = np.clip(roi * (1.0 - a) + rgb, 0, 255).astype(np.uint8)
    return frame


class KaraokeLine:
    """One lyric line: words dim until sung, lit exactly on their start time."""

    def __init__(self, text, times, key, k=1.0, cy=1400, maxw=960, size=LYRIC_SIZE, W=1080):
        self.text, self.times = text, times
        words = text.split()
        self.words = [w.replace("*", "") for w in words]
        if key is None or key < 0:
            key = engine.pick_gold(words) if len(words) > 1 else -1
        self.key = key
        size = int(size * k)
        maxw = int(maxw * k)
        for _ in range(10):
            body = engine.load_font(BODY_FONT, size, 700)
            keyf = engine.load_font(KEY_FONT, int(size * 1.28), 700)
            space = body.getlength(" ") * 1.2
            lit, dim = [], []
            for i, w in enumerate(self.words):
                if i == key:   # gold Ruqaa word: strong two-layer gold glow when sung
                    lit.append(stroked_word(w, keyf, GOLD, glow=(GOLD_GLOW, 1.6), k=k))
                    dim.append(stroked_word(w, keyf, DIM_GOLD, k=k))
                else:
                    lit.append(stroked_word(w, body, WHITE, glow=((255, 238, 218), 0.35), k=k))
                    dim.append(stroked_word(w, body, DIM_FILL, k=k))
            advs = [g.adv for g in lit]
            soft = {i for i, w in enumerate(self.words) if w and w[-1] in "…،,.؟?!:؛"}
            rows = engine.wrap_rows(advs, space, maxw, soft)
            if len(rows) <= 2 and max(advs or [0]) <= maxw:
                break
            size = int(size * 0.9)
        asc, desc = body.getmetrics()
        row_h = (asc + desc) * 1.05
        rtl = engine.is_rtl(text)
        top = cy - row_h * len(rows) / 2.0
        place = [None] * len(self.words)  # (x_left_of_advance, baseline)
        for r, row in enumerate(rows):
            row_w = sum(advs[i] for i in row) + space * (len(row) - 1)
            base = top + r * row_h + asc + (row_h - asc - desc) / 2.0
            if rtl:
                x = (W + row_w) / 2.0
                for i in row:
                    x -= advs[i]
                    place[i] = (x, base)
                    x -= space
            else:
                x = (W - row_w) / 2.0
                for i in row:
                    place[i] = (x, base)
                    x += advs[i] + space
        # canvas covering every sprite, positions fixed for the life of the line
        boxes = []
        for i, g in enumerate(lit):
            x, b = place[i]
            ox, oy = int(round(x - g.ox)), int(round(b - g.oy))
            boxes.append((ox, oy, ox + g.img.width, oy + g.img.height))
        X0 = min(b[0] for b in boxes)
        Y0 = min(b[1] for b in boxes)
        X1 = max(b[2] for b in boxes)
        Y1 = max(b[3] for b in boxes)
        self.x, self.y = X0, Y0
        base = Image.new("RGBA", (X1 - X0, Y1 - Y0), (0, 0, 0, 0))
        for i, g in enumerate(dim):
            base.alpha_composite(g.img, (boxes[i][0] - X0, boxes[i][1] - Y0))
        self.base = premul(base)
        self.delta = []  # per word: (y0, y1, x0, x1, lit - dim) in canvas coords
        for i in range(len(self.words)):
            x0, y0, x1, y1 = boxes[i]
            d = premul(lit[i].img) - premul(dim[i].img)
            self.delta.append((y0 - Y0, y1 - Y0, x0 - X0, x1 - X0, d))
        self.size = size
        self.rows = len(rows)

    def word_states(self, t):
        return [min(1.0, max(0.0, (t - s) / XFADE)) for s, _ in self.times]

    def layer(self, t):
        states = self.word_states(t)
        arr = self.base.copy()
        for p, (y0, y1, x0, x1, d) in zip(states, self.delta):
            if p > 0:
                arr[y0:y1, x0:x1] += d * p
        return np.clip(arr, 0, 1)

    def draw(self, frame, t, alpha=1.0):
        return blend(frame, self.layer(t), self.x, self.y, alpha)


class Schedule:
    """Which line is on screen when: in/out fades, never two lines at once."""

    def __init__(self, lines, k=1.0, cy=1400, W=1080, maxw=960, size=LYRIC_SIZE):
        """lines: [(text, [(s, e) per word], key)] in reel time, sorted."""
        self.items = []
        n = len(lines)
        for i, (text, times, key) in enumerate(lines):
            show = times[0][0] - LINE_IN - 0.08
            hide = times[-1][1] + 0.45
            if i + 1 < n:
                nxt = lines[i + 1][1][0][0] - LINE_IN - 0.08
                hide = min(max(hide, min(nxt, times[-1][1] + 1.6)), nxt)
            if i > 0 and self.items:
                show = max(show, self.items[-1][2])
            if hide - show < 0.3:
                hide = show + 0.3
            kl = KaraokeLine(text, times, key, k, cy, maxw, size, W)
            self.items.append((kl, show, hide))

    def draw(self, frame, t, alpha=1.0):
        for kl, show, hide in self.items:
            if show <= t <= hide:
                a = min(1.0, (t - show) / LINE_IN, (hide - t) / LINE_OUT)
                kl.draw(frame, t, alpha * max(0.0, a))
        return frame

    def line_at(self, t):
        for kl, show, hide in self.items:
            if show <= t <= hide:
                return kl
        return None


class Hook:
    """Big sound-off hook line for the first second(s) (Instagram/Facebook version)."""

    def __init__(self, text, key=-1, k=1.0, cy=380, W=1080, until=2.6, font=BODY_FONT):
        words = text.split()
        if key is None or key < 0:
            key = engine.pick_gold(words) if len(words) > 1 else -1
        clean = " ".join(("*%s*" % w.replace("*", "")) if i == key else w.replace("*", "")
                         for i, w in enumerate(words))
        blk = engine.TextBlock(clean, font, int(104 * k), cy, weight=900, gold=key >= 0,
                               maxw=int(960 * k), max_rows=2, line_gap=1.0, shadow=0.85)
        canvas = Image.new("RGBA", (W, int(cy * 2 + 200 * k)), (0, 0, 0, 0))
        blk.draw(canvas, [1] * len(blk.items), [0] * len(blk.items))
        bb = canvas.getbbox() or (0, 0, 1, 1)
        self.arr = premul(canvas.crop(bb))
        self.x, self.y = bb[0], bb[1]
        self.until = until

    def draw(self, frame, t):
        if t > self.until:
            return frame
        a = min(1.0, (self.until - t) / 0.35)
        return blend(frame, self.arr, self.x, self.y, a)


class Stamp:
    """Red rubber-stamp word (Drama style): slams in, holds."""

    def __init__(self, word, k=1.0, cx=540, cy=760, angle=-7.0, seed=3):
        f = engine.load_font(BODY_FONT, int(150 * k), 900)
        w = word.replace("*", "")
        kw = engine._text_kw(w)
        tw = f.getlength(w, **kw)
        asc, desc = f.getmetrics()
        pad = int(34 * k)
        bw = int(10 * k)
        W, H = int(tw + 2 * pad + 2 * bw), int(asc + desc * 0.6 + 2 * pad)
        img = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((bw // 2, bw // 2, W - bw // 2 - 1, H - bw // 2 - 1), radius=int(16 * k), outline=255, width=bw)
        d.text((W / 2, H / 2 + asc * 0.12), w, font=f, fill=255, anchor="mm", **kw)
        # ink texture: speckled wear
        rng = np.random.default_rng(seed)
        noise = Image.fromarray((rng.random((H // 3 + 1, W // 3 + 1)) * 255).astype(np.uint8)).resize((W, H))
        wear = noise.point(lambda v: 0 if v > 205 else 255)
        img = ImageChops.multiply(img, wear).filter(ImageFilter.GaussianBlur(0.6))
        rgba = Image.new("RGBA", (W, H), STAMP_RED + (0,))
        rgba.putalpha(img.point(lambda v: int(v * 0.93)))
        rgba = rgba.rotate(angle, resample=Image.BICUBIC, expand=True)
        self.img = rgba
        self.cx, self.cy = cx, cy

    def draw(self, frame, p):
        """p: seconds since the stamp hit (negative = not yet)."""
        if p < 0:
            return frame
        s = 1.0 + 0.55 * max(0.0, 1 - p / 0.11) ** 2
        a = min(1.0, p / 0.06 + 0.2)
        img = self.img
        if abs(s - 1) > 1e-3:
            img = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.BILINEAR)
        shake = 6 * math.exp(-p * 18) * math.sin(p * 90)
        arr = premul(img)
        return blend(frame, arr, int(self.cx - img.width / 2 + shake), int(self.cy - img.height / 2), a)


def signature(song, k=1.0, W=1080):
    """«وسيم · song» small, gold name — same as the lyric reels."""
    name_f = engine.load_font(KEY_FONT, int(46 * k), 700)
    song_f = engine.load_font(BODY_FONT, int(28 * k), 500)
    parts = [engine.render_word(engine.SIGNATURE, name_f, GOLD, shadow=0.6)]
    if song:
        parts.append(engine.render_word("·", song_f, (230, 220, 205), shadow=0.6))
        parts.append(engine.render_word(song, song_f, (236, 228, 214), shadow=0.6))
    canvas = Image.new("RGBA", (W, int(220 * k)), (0, 0, 0, 0))
    gap = int(16 * k)
    total = sum(p.adv for p in parts) + gap * (len(parts) - 1)
    x = (W + total) / 2.0
    base = int(130 * k)
    for p in parts:
        x -= p.adv
        canvas.alpha_composite(p.img, (int(x - p.ox), int(base - p.oy)))
        x -= gap
    bb = canvas.getbbox() or (0, 0, 1, 1)
    return premul(canvas.crop(bb)), bb[0], bb[1] - base
