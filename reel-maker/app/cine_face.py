"""Cinematic mode — face tracking (YuNet CNN detector in OpenCV + temporal smoothing).

track(footage) -> FaceTrack: per sample (time, cx, cy, size, eyes) normalised to the
picture, cached per footage file. Gaps are held/interpolated, jumps rejected.
"""
import json
import os

import numpy as np

import autoreel
import engine

YUNET = os.path.join(engine.ROOT, "data", "face_detection_yunet_2023mar.onnx")
TRACK_FPS = 6.0
TRACK_W = 640


def _detector(w, h):
    import cv2
    with open(YUNET, "rb") as fh:  # bytes: Arabic install folders
        buf = np.frombuffer(fh.read(), np.uint8)
    try:
        return cv2.FaceDetectorYN.create("onnx", buf, np.zeros(0, np.uint8), (w, h), 0.6, 0.3, 50)
    except Exception:  # older OpenCV without the buffer overload
        return cv2.FaceDetectorYN.create(_ascii_model(), "", (w, h), 0.6, 0.3, 50)


def _ascii_model():
    """OpenCV's file overload needs an ASCII path; copy the model to a temp folder."""
    try:
        YUNET.encode("ascii")
        return YUNET
    except UnicodeEncodeError:
        pass
    import shutil
    import tempfile
    p = os.path.join(tempfile.gettempdir(), "reelmaker_yunet.onnx")
    try:
        p.encode("ascii")
    except UnicodeEncodeError:
        p = os.path.join(os.environ.get("PUBLIC", "C:\\Users\\Public"), "reelmaker_yunet.onnx")
    if not os.path.isfile(p):
        shutil.copyfile(YUNET, p)
    return p


def bgr_frames(path, w, h, fps, start=None, dur=None, vf_extra=""):
    """Generator of small BGR frames (ffmpeg pipe, no temp files)."""
    import subprocess
    args = [engine.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
    if start is not None:
        args += ["-ss", "%.3f" % start]
    if dur is not None:
        args += ["-t", "%.3f" % dur]
    vf = "fps=%s,%sscale=%d:%d:flags=area,format=bgr24" % (fps, vf_extra, w, h)
    args += ["-i", path, "-an", "-vf", vf, "-f", "rawvideo", "-"]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         creationflags=engine.NO_WINDOW)
    n = w * h * 3
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield np.frombuffer(buf, np.uint8).reshape(h, w, 3)
    finally:
        p.stdout.close()
        p.kill()
        p.wait()


class FaceTrack:
    """Smoothed face position over the footage's timeline."""

    def __init__(self, t, cx, cy, size, eyes, found):
        self.t = np.asarray(t, np.float64)
        self.cx, self.cy, self.size = (np.asarray(a, np.float64) for a in (cx, cy, size))
        self.eyes = np.asarray(eyes, np.float64).reshape(-1, 4)  # rx, ry, lx, ly
        self.found = float(found)

    def at(self, t):
        """(cx, cy, size, eyes[4]) normalised, at footage time t."""
        if len(self.t) == 0:
            return 0.5, 0.42, 0.18, np.array([0.45, 0.4, 0.55, 0.4])
        f = lambda a: float(np.interp(t, self.t, a))
        eyes = np.array([np.interp(t, self.t, self.eyes[:, k]) for k in range(4)])
        return f(self.cx), f(self.cy), f(self.size), eyes

    def to_json(self):
        return dict(t=self.t.tolist(), cx=self.cx.tolist(), cy=self.cy.tolist(),
                    size=self.size.tolist(), eyes=self.eyes.tolist(), found=self.found)

    @classmethod
    def from_json(cls, d):
        return cls(d["t"], d["cx"], d["cy"], d["size"], d["eyes"], d.get("found", 1.0))


def _smooth(x, fps, window):
    n = len(x)
    if n < 3:
        return x
    pad = np.pad(x, 2, mode="edge")
    x = np.median(np.stack([pad[i:i + n] for i in range(5)]), axis=0)
    k = max(1, int(round(window * fps)))
    for _ in range(2):
        pad = np.pad(x, (k // 2, k - 1 - k // 2), mode="edge")
        x = np.convolve(pad, np.ones(k) / k, mode="valid")
    return x


def track(path, progress=None, cancel=None, log=None):
    """Detect + smooth the main face across the whole footage (cached)."""
    progress = progress or (lambda f, m="": None)
    key = autoreel.file_key(path)
    cache = os.path.join(autoreel.CACHE_DIR, "face-%s.json" % key)
    if os.path.isfile(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                return FaceTrack.from_json(json.load(fh))
        except (OSError, ValueError, KeyError):
            pass
    dur, _, _ = engine.probe(path)
    sw, sh = autoreel.video_size(path)
    w = TRACK_W
    h = max(2, int(round(w * sh / float(sw))) // 2 * 2)
    det = _detector(w, h)
    ts, raw = [], []
    prev = None
    total = max(1, int(dur * TRACK_FPS))
    for i, img in enumerate(bgr_frames(path, w, h, TRACK_FPS)):
        if cancel is not None and cancel.is_set():
            raise engine.Cancelled()
        t = i / TRACK_FPS
        _, faces = det.detect(img)
        best = None
        if faces is not None and len(faces):
            bs = -1e9
            for f in faces:
                x, y, fw, fh = f[:4]
                cx, cy = (x + fw / 2) / w, (y + fh / 2) / h
                s = fw * fh / float(w * h) + f[14] * 0.02
                if prev is not None:
                    s -= abs(cx - prev[0]) * 0.15 + abs(cy - prev[1]) * 0.15
                if s > bs:
                    bs = s
                    # landmarks: right eye, left eye (image coords)
                    best = (cx, cy, fh / h, f[4] / w, f[5] / h, f[6] / w, f[7] / h)
        if best is not None and prev is not None and abs(best[0] - prev[0]) > 0.25 and i - prev[7] < 3:
            best = None  # one-frame jump to another face/object
        if best is not None:
            prev = best + (i,)
        ts.append(t)
        raw.append(best)
        if i % 12 == 0:
            progress(min(1.0, i / float(total)), "بتتبع الوش… %d%%" % min(100, 100 * i // total))
    found = sum(1 for r in raw if r is not None) / float(max(1, len(raw)))
    known = [i for i, r in enumerate(raw) if r is not None]
    n = len(raw)
    if not known:
        cols = np.tile(np.array([0.5, 0.42, 0.18, 0.45, 0.4, 0.55, 0.4]), (max(n, 1), 1))
        ts = ts or [0.0]
    else:
        arr = np.array([raw[i] for i in known])
        cols = np.stack([np.interp(np.arange(n), known, arr[:, k]) for k in range(7)], axis=1)
    fps = TRACK_FPS
    cx = _smooth(cols[:, 0], fps, 0.9)
    cy = _smooth(cols[:, 1], fps, 0.9)
    size = _smooth(cols[:, 2], fps, 2.0)
    eyes = np.stack([_smooth(cols[:, k], fps, 0.35) for k in range(3, 7)], axis=1)
    ft = FaceTrack(ts, cx, cy, size, eyes, found)
    os.makedirs(autoreel.CACHE_DIR, exist_ok=True)
    with open(cache, "w", encoding="utf-8") as fh:
        json.dump(ft.to_json(), fh)
    if log:
        log("الوش: ظاهر في %d%% من اللقطات" % (found * 100))
    progress(1.0, "تتبع الوش خلص")
    return ft
