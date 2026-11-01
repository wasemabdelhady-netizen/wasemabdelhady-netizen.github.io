"""Cinematic mode — the approved "v5" look and the optical effects.

Grade (colour only) is baked into a 256³ LUT once; per frame it is one gather.
Spatial passes (bloom, edge blur, vignette, grain) run with OpenCV.
"""
import os

import numpy as np

import autoreel

# --------------------------------------------------------------------------- v5 grade

S_CURVE = 0.32
SHADOW_TINT = np.array([0.06, -0.01, -0.01], np.float32)
HIGHLIGHT_TINT = np.array([-0.01, -0.015, 0.005], np.float32)
GAINS = np.array([0.97, 0.86, 1.06], np.float32)  # R, G, B
LUT_VERSION = "v5.1"


def _smoothstep(a, b, x):
    t = np.clip((x - a) / (b - a), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def _bump(h, centre, width):
    """1 at `centre` degrees, 0 beyond ±width (circular)."""
    d = np.abs(((h - centre + 180.0) % 360.0) - 180.0)
    return 1.0 - _smoothstep(0.0, width, d)


def rgb_to_hsv(c):
    r, g, b = c[:, 0], c[:, 1], c[:, 2]
    mx, mn = c.max(axis=1), c.min(axis=1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-6
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = (60.0 * ((g[rm] - b[rm]) / d[rm])) % 360.0
    h[gm] = 60.0 * ((b[gm] - r[gm]) / d[gm]) + 120.0
    h[bm] = 60.0 * ((r[bm] - g[bm]) / d[bm]) + 240.0
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    return h, s, mx


def hsv_to_rgb(h, s, v):
    h = (h % 360.0) / 60.0
    i = np.floor(h).astype(np.int32) % 6
    f = h - np.floor(h)
    p, q, t = v * (1 - s), v * (1 - s * f), v * (1 - s * (1 - f))
    out = np.empty((len(h), 3), np.float32)
    for k, (a, b, c) in enumerate(((v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q))):
        m = i == k
        out[m, 0], out[m, 1], out[m, 2] = a[m], b[m], c[m]
    return out


def grade_float(c):
    """c: [N, 3] float32 RGB 0..1 -> graded. Order as approved: S-curve, hue work,
    split tone, channel gains."""
    c = c + S_CURVE * (c * c * (3 - 2 * c) - c)
    h, s, v = rgb_to_hsv(c)
    wg = _bump(h, 115.0, 50.0) * _smoothstep(0.05, 0.25, s)   # greens (and green-yellows)
    s = s * (1.0 - 0.55 * wg)
    wp = _bump(h, 285.0, 45.0)                                 # purples / magentas
    h = h - 28.0 * wp                                          # toward blue (240)
    s = s * (1.0 - 0.15 * wp)
    c = hsv_to_rgb(h, s, v)
    L = (0.2126 * c[:, 0] + 0.7152 * c[:, 1] + 0.0722 * c[:, 2])[:, None]
    ws = 1.0 - _smoothstep(0.0, 0.55, L)
    wh = _smoothstep(0.45, 1.0, L)
    c = c + ws * SHADOW_TINT + wh * HIGHLIGHT_TINT
    c = c * GAINS
    return np.clip(c, 0.0, 1.0)


CUBE_N = 65


def cube_path():
    """The v5 grade as a 65³ .cube file (ffmpeg lut3d applies it while decoding)."""
    name = "grade-%s.cube" % LUT_VERSION
    path = os.path.join(autoreel.CACHE_DIR, name)
    if os.path.isfile(path):
        return path
    n = CUBE_N
    lv = np.arange(n, dtype=np.float32) / (n - 1)
    bb, gg, rr = np.meshgrid(lv, lv, lv, indexing="ij")  # .cube: red changes fastest
    c = np.stack([rr.ravel(), gg.ravel(), bb.ravel()], axis=1)
    out = grade_float(c)
    os.makedirs(autoreel.CACHE_DIR, exist_ok=True)
    lines = ["TITLE \"Wasem v5\"", "LUT_3D_SIZE %d" % n]
    lines += ["%.5f %.5f %.5f" % tuple(v) for v in out]
    with open(path + ".tmp", "w", encoding="ascii") as fh:
        fh.write("\n".join(lines) + "\n")
    os.replace(path + ".tmp", path)
    return path


def apply_cube(img):
    """Python version of the cube (stills / tests): trilinear on the 65³ grid."""
    n = CUBE_N
    c = img.reshape(-1, 3).astype(np.float32) / 255.0
    lv = np.arange(n, dtype=np.float32) / (n - 1)
    rr, gg, bb = np.meshgrid(lv, lv, lv, indexing="ij")
    grid = grade_float(np.stack([rr.ravel(), gg.ravel(), bb.ravel()], axis=1)).reshape(n, n, n, 3)
    x = c * (n - 1)
    i0 = np.clip(np.floor(x).astype(np.int32), 0, n - 2)
    f = x - i0
    out = np.zeros_like(c)
    for dr in (0, 1):
        for dg in (0, 1):
            for db in (0, 1):
                w = ((f[:, 0] if dr else 1 - f[:, 0]) * (f[:, 1] if dg else 1 - f[:, 1])
                     * (f[:, 2] if db else 1 - f[:, 2]))
                out += w[:, None] * grid[i0[:, 0] + dr, i0[:, 1] + dg, i0[:, 2] + db]
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8).reshape(img.shape)


def cast(img):
    """Mean Lab a*, b* of the mid-tones (positive b* = yellow, negative a* = green)."""
    import cv2
    small = cv2.resize(img, (90, 160), interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)
    L = lab[:, 0] * 100 / 255.0
    m = (L > 25) & (L < 80)
    if m.sum() < 50:
        m = np.ones(len(L), bool)
    return float(lab[m, 1].mean() - 128), float(lab[m, 2].mean() - 128)


def neutral_fix(samples, log=None):
    """Per-reel gains that remove a yellow/green cast (only those) found in graded samples."""
    if not samples:
        return None
    ab = np.array([cast(s) for s in samples])
    a, b = float(np.median(ab[:, 0])), float(np.median(ab[:, 1]))
    fix = np.ones(3, np.float32)
    if b > 4.0:      # yellow: lift blue, lower red+green a little
        k = min((b - 4.0) / 60.0, 0.10)
        fix *= np.array([1 - k * 0.4, 1 - k * 0.4, 1 + k], np.float32)
    if a < -2.0:     # green: lower green
        k = min((-2.0 - a) / 70.0, 0.08)
        fix *= np.array([1 + k * 0.3, 1 - k, 1 + k * 0.3], np.float32)
    if log:
        log("  الألوان: a*=%.1f b*=%.1f%s" % (a, b, "" if np.allclose(fix, 1) else " → اتصلّح (من غير اصفرار/اخضرار)"))
    return None if np.allclose(fix, 1) else fix


# --------------------------------------------------------------------------- spatial

class Optics:
    """Bloom, edge blur, vignette and grain for one output size (uint8, OpenCV)."""

    def __init__(self, w, h, seed=7):
        import cv2
        self.w, self.h = w, h
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        nx, ny = (xx / w - 0.5) * 2, (yy / h - 0.5) * 2
        r = np.sqrt((nx * 0.9) ** 2 + (ny * 0.72) ** 2)
        vig = 1.0 - 0.34 * _smoothstep(0.45, 1.35, r)
        self.vig3 = np.repeat((vig * 255)[..., None], 3, axis=2).astype(np.uint8)
        e = (_smoothstep(0.62, 1.25, r) * 0.85).astype(np.float32)
        self.edge, self.keep = e, (1.0 - e).astype(np.float32)
        rng = np.random.default_rng(seed)
        self.grain = []
        for _ in range(6):
            g = rng.normal(0, 1, (h, w)).astype(np.float32)
            g = cv2.GaussianBlur(g, (0, 0), 0.65)
            g = g / (g.std() + 1e-6) * 3.2
            g3 = np.repeat(g[..., None], 3, axis=2)
            self.grain.append((np.clip(g3, 0, 255).astype(np.uint8), np.clip(-g3, 0, 255).astype(np.uint8)))
        self.k = 0

    def bloom(self, img, amount=0.22):
        import cv2
        sw, sh = max(8, self.w // 8), max(8, self.h // 8)
        small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA).astype(np.float32)
        hi = np.maximum(small - 175.0, 0.0) * (255.0 / 80.0 * amount)
        return cv2.GaussianBlur(hi, (0, 0), max(2.0, sw / 40.0))

    def finish(self, img, extra=None, grain=True, gains=None):
        """img uint8 RGB (graded) -> + bloom/extra light, edge blur, vignette, grain.
        `extra`: additive light at 1/8 size (float32) or full size."""
        import cv2
        light = self.bloom(img)
        if extra is not None:
            if extra.shape[:2] != light.shape[:2]:
                extra = cv2.resize(extra, (light.shape[1], light.shape[0]), interpolation=cv2.INTER_AREA)
            light = light + extra
        light = cv2.resize(np.clip(light, 0, 255).astype(np.uint8), (self.w, self.h),
                           interpolation=cv2.INTER_LINEAR)
        f = cv2.add(img, light)
        if gains is not None:
            f = cv2.multiply(f, (float(gains[0]), float(gains[1]), float(gains[2]), 0.0))
        sw, sh = max(8, self.w // 4), max(8, self.h // 4)
        blur = cv2.resize(cv2.GaussianBlur(cv2.resize(f, (sw, sh), interpolation=cv2.INTER_AREA), (0, 0), 2.2),
                          (self.w, self.h), interpolation=cv2.INTER_LINEAR)
        f = cv2.blendLinear(f, blur, self.keep, self.edge)
        f = cv2.multiply(f, self.vig3, scale=1 / 255.0)
        if grain:
            gp, gn = self.grain[self.k % len(self.grain)]
            self.k += 1
            f = cv2.subtract(cv2.add(f, gp), gn)
        return f


# --------------------------------------------------------------------------- effects

def noise_field(w, h, seed, cells=(5, 9)):
    import cv2
    rng = np.random.default_rng(seed)
    g = rng.random((cells[1], cells[0])).astype(np.float32)
    return cv2.resize(g, (w, h), interpolation=cv2.INTER_CUBIC)


class Light:
    """Light leaks, film burns and soft flashes as additive light (float32, 1/8 size)."""

    LEAK_COLOURS = (np.array([255, 96, 46], np.float32),    # ember orange-red
                    np.array([255, 70, 105], np.float32),   # rose
                    np.array([230, 120, 70], np.float32))   # copper (no yellow)

    def __init__(self, w, h):
        self.w, self.h = w, h
        self.sw, self.sh = max(8, w // 8), max(8, h // 8)
        yy, xx = np.mgrid[0:self.sh, 0:self.sw].astype(np.float32)
        self.xx, self.yy = xx / self.sw, yy / self.sh

    def leak(self, p, seed, strength=1.0):
        """p 0..1 through the leak's life."""
        rng = np.random.default_rng(seed)
        env = np.sin(np.pi * np.clip(p, 0, 1)) ** 1.5 * strength
        out = np.zeros((self.sh, self.sw, 3), np.float32)
        for k in range(2):
            col = self.LEAK_COLOURS[(seed + k) % len(self.LEAK_COLOURS)]
            side = rng.choice([-0.15, 1.15])
            x0 = side + (0.35 if side < 0 else -0.35) * p * (1 + k * 0.4)
            y0 = rng.uniform(0.15, 0.85)
            sx, sy = rng.uniform(0.25, 0.4), rng.uniform(0.35, 0.6)
            g = np.exp(-(((self.xx - x0) / sx) ** 2 + ((self.yy - y0) / sy) ** 2))
            g = g * (0.35 + 0.65 * _smoothstep(0.12, 0.42, np.sqrt(((self.xx - 0.5) * 1.3) ** 2 + ((self.yy - 0.42) * 0.9) ** 2)))
            out += g[..., None] * col * (0.55 if k else 0.8)
        return out * env

    def burn(self, p, seed, white=True):
        """Film burn: organic hot blobs that grow, peak at p=0.5, then clear."""
        a = noise_field(self.sw, self.sh, seed)
        b = noise_field(self.sw, self.sh, seed + 1)
        n = a * (1 - p) + b * p
        env = np.sin(np.pi * np.clip(p, 0, 1))
        thr = 1.0 - 0.5 * env              # at the peak about a third of the frame burns
        m = np.clip((n - thr) / 0.22 + (self.xx - 0.5) * 0.25, 0, 1)
        m = m ** 1.6
        # burns come in from the edges; the face area (centre, upper half) stays clear
        d = np.sqrt(((self.xx - 0.5) * 1.3) ** 2 + ((self.yy - 0.42) * 0.9) ** 2)
        m = m * _smoothstep(0.18, 0.5, d)
        orange = np.array([255, 105, 38], np.float32)
        core = np.array([255, 225, 195], np.float32) if white else np.array([255, 140, 80], np.float32)
        hot = np.clip((m - 0.65) / 0.35, 0, 1)[..., None]
        light = m[..., None] * orange * 1.1 * (1 - hot) + hot * core * 1.15
        light += env * 22.0 * np.array([1.0, 0.45, 0.25], np.float32)
        return light.astype(np.float32)

    def flash(self, p):
        """Soft exposure flash (not white): fast up, slower down. Returns scalar lift."""
        if p < 0 or p > 1:
            return 0.0
        return float((1 - p) ** 2) * 120.0


# --------------------------------------------------------------------------- glasses

def deglare(img, boxes, graded=False):
    """Remove reflections on glasses inside eye `boxes` [(x0, y0, x1, y1)] (image px).
    Mask = (tophat > 18) & (gray > 120) & (b + 12 >= r); skin is never touched.
    graded=True: the frame already has the v5 gains, so r and b are measured back
    through them (the test stays the one on the camera's colours)."""
    import cv2
    h, w = img.shape[:2]
    for x0, y0, x1, y1 in boxes:
        x0, y0 = max(0, int(x0)), max(0, int(y0))
        x1, y1 = min(w, int(x1)), min(h, int(y1))
        if x1 - x0 < 8 or y1 - y0 < 6:
            continue
        roi = img[y0:y1, x0:x1]
        gray = cv2.cvtColor(roi, cv2.COLOR_RGB2GRAY)
        k = max(5, int((x1 - x0) / 9) | 1)
        ker = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        th = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, ker)
        r = roi[..., 0].astype(np.float32)
        b = roi[..., 2].astype(np.float32)
        if graded:
            r, b = r / GAINS[0], b / GAINS[2]
        mask = ((th > 18) & (gray > 120) & (b + 12 >= r)).astype(np.uint8) * 255
        if not mask.any():
            continue
        mask = cv2.dilate(mask, np.ones((3, 3), np.uint8))
        img[y0:y1, x0:x1] = cv2.inpaint(np.ascontiguousarray(roi), mask, 3, cv2.INPAINT_TELEA)
    return img


def eye_boxes(eyes, fsize, sw, sh):
    """Glasses region in source pixels from YuNet eye landmarks (normalised)."""
    rx, ry, lx, ly = eyes
    cx, cy = (rx + lx) / 2 * sw, (ry + ly) / 2 * sh
    d = max(abs(lx - rx) * sw, fsize * sh * 0.3)
    return [(cx - d * 1.05, cy - d * 0.42, cx + d * 1.05, cy + d * 0.38)]
