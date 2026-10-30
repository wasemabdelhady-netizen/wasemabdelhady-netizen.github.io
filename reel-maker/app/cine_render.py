"""Cinematic mode — pixels: one reel from the footage + master, following a cine_edit plan.

    ffmpeg (decode, 30 fps, [HDR→SDR], v5 LUT) --rgb24--> numpy/OpenCV compose per shot
    --> temporal effects --> optics (bloom/leaks/burns, edge blur, vignette, grain)
    --> bars / gutters / iris --> karaoke + hook + stamp + signature
    --rgb24--> ffmpeg (H.264 high, AAC 320k)
"""
import os
import queue
import shutil
import subprocess
import threading
import time

import numpy as np

import cine_edit
import cine_look
import cine_text
import engine

FPS = 30


# ----------------------------------------------------------------------------- files

def versioned(folder, base, ext, trash="_to_delete"):
    """Never overwrite: base.ext, then base v2.ext, v3… ; earlier versions of the same
    reel move to <folder>/_to_delete (never deleted). Returns the new path."""
    os.makedirs(folder, exist_ok=True)
    tdir = os.path.join(folder, trash)

    def ver(name):
        stem = os.path.splitext(name)[0]
        if stem == base:
            return 1
        if stem.startswith(base + " v") and stem[len(base) + 2:].isdigit():
            return int(stem[len(base) + 2:])
        return 0
    seen = []
    for d in (folder, tdir):
        if os.path.isdir(d):
            for n in os.listdir(d):
                if n.lower().endswith(ext.lower()) and ver(n):
                    seen.append((ver(n), d, n))
    nxt = max([v for v, _, _ in seen] or [0]) + 1
    for v, d, n in seen:
        if d == folder:
            to_trash(os.path.join(d, n), trash)
    name = base + ext if nxt == 1 else "%s v%d%s" % (base, nxt, ext)
    return os.path.join(folder, name)


def to_trash(path, trash="_to_delete"):
    """Move a file into <its folder>/_to_delete (unique name) — never delete."""
    d = os.path.join(os.path.dirname(path), trash)
    os.makedirs(d, exist_ok=True)
    stem, ext = os.path.splitext(os.path.basename(path))
    dst = os.path.join(d, stem + ext)
    k = 2
    while os.path.exists(dst):
        dst = os.path.join(d, "%s (%d)%s" % (stem, k, ext))
        k += 1
    shutil.move(path, dst)
    return dst


# ----------------------------------------------------------------------------- video io

def video_meta(path):
    """(width, height, transfer) from ffmpeg's banner (rotation applied)."""
    import autoreel
    w, h = autoreel.video_size(path)
    r = engine.run_ffmpeg(["-i", path])
    err = r.stderr.decode("utf-8", "replace")
    trc = ""
    for t in ("arib-std-b67", "smpte2084"):
        if t in err:
            trc = t
    return w, h, trc


def decode_filter(sw, sh, trc, max_h):
    vf = ["fps=%d" % FPS]
    if trc:  # HLG / PQ footage -> SDR Rec.709 before the grade
        vf.append("zscale=t=linear:npl=203,format=gbrpf32le,zscale=p=bt709,"
                  "tonemap=mobius:desat=0,zscale=t=bt709:m=bt709:r=tv")
    if sh > max_h:
        vf.append("scale=-2:%d:flags=lanczos" % max_h)
    vf.append("lut3d=file=%s:interp=tetrahedral" % os.path.basename(cine_look.cube_path()))
    vf.append("format=rgb24")
    return ",".join(vf)


class Reader:
    """Background ffmpeg decoder -> frames (graded RGB, source resolution)."""

    def __init__(self, path, start, dur, max_h, hw=True):
        sw, sh, trc = video_meta(path)
        if sh > max_h:
            sw, sh = int(round(sw * max_h / float(sh))) // 2 * 2, max_h
        self.w, self.h = sw, sh
        args = [engine.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error"]
        if hw:
            args += ["-hwaccel", "auto"]
        args += ["-ss", "%.3f" % max(0.0, start), "-t", "%.3f" % (dur + 0.5), "-i", path, "-an",
                 "-vf", decode_filter(*video_meta(path)[:2], trc=trc, max_h=max_h),
                 "-f", "rawvideo", "-"]
        self.p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=os.path.dirname(cine_look.cube_path()), creationflags=engine.NO_WINDOW)
        self.q = queue.Queue(maxsize=6)
        self.err = b""
        self.n = 0
        threading.Thread(target=self._run, daemon=True).start()
        threading.Thread(target=self._err, daemon=True).start()

    def _err(self):
        self.err = self.p.stderr.read()

    def _run(self):
        size = self.w * self.h * 3
        try:
            while True:
                buf = self.p.stdout.read(size)
                if len(buf) < size:
                    break
                self.q.put(np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3))
                self.n += 1
        finally:
            self.q.put(None)

    def get(self, timeout=60):
        return self.q.get(timeout=timeout)

    def close(self):
        try:
            self.p.kill()
        except Exception:
            pass
        try:
            self.p.wait(5)
        except Exception:
            pass


def grab(path, t, max_h=1080):
    """One graded frame at footage time t (thumbnails / colour check)."""
    sw, sh, trc = video_meta(path)
    if sh > max_h:
        sw, sh = int(round(sw * max_h / float(sh))) // 2 * 2, max_h
    r = subprocess.run([engine.ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error",
                        "-ss", "%.3f" % max(0.0, t), "-i", path, "-frames:v", "1", "-an",
                        "-vf", decode_filter(*video_meta(path)[:2], trc=trc, max_h=max_h),
                        "-f", "rawvideo", "-"],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       cwd=os.path.dirname(cine_look.cube_path()), creationflags=engine.NO_WINDOW)
    if len(r.stdout) < sw * sh * 3:
        return None
    return np.frombuffer(r.stdout[:sw * sh * 3], np.uint8).reshape(sh, sw, 3).copy()


# ----------------------------------------------------------------------------- geometry

def ease(p):
    p = min(max(p, 0.0), 1.0)
    return p * p * (3 - 2 * p)


def rect_for(face, sw, sh, aspect, f, zmul=1.0):
    """Source rectangle (x0, y0, w, h) for framing `f` around the face."""
    if sw / float(sh) > aspect:
        rh = float(sh)
        rw = rh * aspect
    else:
        rw = float(sw)
        rh = rw / aspect
    z = max(1.0, f.get("z", 1.0) * zmul)
    rw, rh = rw / z, rh / z
    fx, fy, fs = face[0], face[1], face[2]
    cx = fx * sw + f.get("dx", 0.0) * rw
    y0 = fy * sh - f.get("eye", 0.42) * rh + f.get("dy", 0.0) * rh
    x0 = min(max(cx - rw / 2.0, 0.0), sw - rw)
    y0 = min(max(y0, 0.0), sh - rh)
    return x0, y0, rw, rh


def warp(src, half, rect, ow, oh):
    import cv2
    x0, y0, rw, rh = rect
    s = ow / rw
    img = src
    if s < 0.55 and half is not None:  # big downscale: start from the half-size frame
        img, x0, y0, s = half, x0 / 2.0, y0 / 2.0, s * 2.0
    M = np.array([[s, 0, -x0 * s], [0, s, -y0 * s]], np.float32)
    return cv2.warpAffine(img, M, (ow, oh), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


# ----------------------------------------------------------------------------- renderer

class Reel:
    """Everything needed to render one reel."""

    def __init__(self, footage, master, offset, start, end, lines, beats, face, style,
                 song="", ig=False, deglare=False, hook_text=None, hook_key=-1,
                 log=None, progress=None, cancel=None, seed=1):
        self.footage, self.master, self.offset = footage, master, offset
        self.start, self.end = start, end
        self.dur = end - start
        self.lines, self.beats, self.face = lines, beats, face
        self.style, self.song, self.ig, self.deglare = style, song, ig, deglare
        self.hook_text, self.hook_key = hook_text, hook_key
        self.log = log or (lambda m: None)
        self.progress = progress or (lambda f, m="": None)
        self.cancel = cancel or threading.Event()
        self.seed = seed

    # -- plan ---------------------------------------------------------------------
    def reel_lines(self):
        """words.json lines -> reel time, only those inside the reel."""
        out = []
        for ln in self.lines:
            if ln["end"] <= self.start + 0.15 or ln["start"] >= self.end - 0.3:
                continue
            ws = [(w["s"] - self.start, w["e"] - self.start) for w in ln["words"]]
            out.append(dict(text=ln["text"], start=ws[0][0], end=ws[-1][1], words=ws,
                            key=ln.get("key", -1), marked=ln.get("key", -1) >= 0))
        return out

    def make_plan(self):
        rl = self.reel_lines()
        bt = [(t - self.start, s, d) for t, s, d in self.beats.in_span(self.start, self.end)]
        ctx = cine_edit.Ctx(self.dur, bt, rl, self.beats.period, self.ig, self.seed)
        return cine_edit.plan(self.style, ctx), rl

    # -- render -------------------------------------------------------------------
    def render(self, out_mp4, out_jpg=None, preview=False, preview_from=0.0, preview_len=5.0):
        import cv2
        t_start = time.time()
        k = 0.5 if preview else 1.0
        W, H = int(1080 * k), int(1920 * k)
        plan, rl = self.make_plan()
        self.plan = plan
        t0 = preview_from if preview else 0.0
        dur = min(preview_len, self.dur - t0) if preview else self.dur
        total = max(1, int(round(dur * FPS)))
        vstart = self.start + t0 - self.offset
        max_h = 1080 if preview else 2160
        self.log("  الخطة: %d لقطة — %s" % (len(plan["shots"]), "، ".join(sorted({e["type"] for e in plan["effects"]})) or "—"))

        # colour check on graded samples (yellow/green casts get neutralised)
        gains = None
        if not preview:
            samples = []
            for q in (0.2, 0.5, 0.8):
                g = grab(self.footage, self.start + self.dur * q - self.offset, 540)
                if g is not None:
                    samples.append(g)
            gains = cine_look.neutral_fix(samples, self.log)

        optics = cine_look.Optics(W, H, seed=self.seed)
        light = cine_look.Light(W, H)
        bars = plan["opts"].get("bars")
        bar_h = int(300 * k) if bars else 0
        band = (bar_h, H - bar_h)
        text_cy = int(1330 * k) if not bars else int(1395 * k)
        sched = cine_text.Schedule([(l["text"], l["words"], l["key"]) for l in rl], k=k, cy=text_cy, W=W)
        hook = None
        if self.ig:
            ht = self.hook_text or (rl[0]["text"] if rl else "")
            if ht:
                hook = cine_text.Hook(ht, self.hook_key if self.hook_text else (rl[0]["key"] if rl else -1),
                                      k=k, cy=int((380 if not bars else 420) * k), W=W)
        stamps = {}
        for e in plan["effects"]:
            if e["type"] == "stamp":
                stamps[id(e)] = cine_text.Stamp(e["word"], k=k, cx=W // 2, cy=int(1120 * k), angle=-6.0)
        sig, sig_x, sig_dy = cine_text.signature(self.song, k, W)
        sig_y = int(1585 * k) + sig_dy if not bars else int(1700 * k) + sig_dy

        reader = Reader(self.footage, vstart, dur, max_h)
        sw, sh = reader.w, reader.h
        cmd = [engine.ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (W, H), "-framerate", str(FPS), "-i", "pipe:0",
               "-ss", "%.3f" % (self.start + t0), "-t", "%.3f" % dur, "-i", self.master,
               "-map", "0:v", "-map", "1:a",
               "-vf", "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p",
               "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]
        if preview:
            cmd += ["-preset", "veryfast", "-crf", "24"]
        else:
            cmd += ["-preset", "medium", "-crf", "16", "-maxrate", "30M", "-bufsize", "60M", "-g", "60"]
        fo = max(0.0, dur - 0.8)
        cmd += ["-af", "afade=t=in:d=0.12,afade=t=out:st=%.3f:d=0.8,aresample=48000" % fo,
                "-c:a", "aac", "-b:a", "320k", "-ar", "48000",
                "-frames:v", str(total), "-movflags", "+faststart", out_mp4]
        enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, creationflags=engine.NO_WINDOW)
        enc_err = []
        threading.Thread(target=lambda: enc_err.append(enc.stderr.read()), daemon=True).start()

        state = dict(trail=None, frozen=None, held=None, prev_held=None, last=None)
        thumb_t = self.thumb_time(rl, plan)
        thumb_frame = None
        src = None
        try:
            for i in range(total):
                if self.cancel.is_set():
                    raise engine.Cancelled()
                nxt = reader.get()
                if nxt is not None:
                    src = nxt
                elif src is None:
                    raise RuntimeError("ffmpeg مش قادر يقرا الفيديو:\n%s" % reader.err.decode("utf-8", "replace")[-700:])
                t = t0 + i / float(FPS)          # reel time
                vt = self.start + t - self.offset  # footage time
                face = self.face.at(vt)
                frame_src = src
                if self.deglare:
                    frame_src = src.copy()
                    cine_look.deglare(frame_src, cine_look.eye_boxes(face[3], face[2], sw, sh), graded=True)
                half = None
                if cine_edit.shot_at(plan, t)["kind"] in ("panels", "split", "double") and sw > 1.6 * W:
                    half = cv2.resize(frame_src, (sw // 2, sh // 2), interpolation=cv2.INTER_AREA)
                canvas = self.compose(plan, t, frame_src, half, face, W, H, band)
                canvas = self.temporal(plan, t, canvas, state, W, H)
                extra = self.light_at(plan, t, light)
                frame = optics.finish(canvas, extra, gains=gains)
                frame = self.after(plan, t, frame, face, W, H, band, k)
                if thumb_frame is None and t >= thumb_t:
                    thumb_frame = frame.copy()
                sched.draw(frame, t)
                if hook is not None:
                    hook.draw(frame, t)
                for e in plan["effects"]:
                    if e["type"] == "stamp" and e["t0"] <= t <= e["t1"]:
                        a = min(1.0, (e["t1"] - t) / 0.2)
                        if a >= 0.999:
                            stamps[id(e)].draw(frame, t - e["t0"])
                        else:
                            tmp = frame.copy()
                            stamps[id(e)].draw(tmp, t - e["t0"])
                            frame[:] = cv2.addWeighted(tmp, a, frame, 1 - a, 0)
                sa = min(1.0, max(0.0, (t - 0.3) / 0.6)) * 0.92
                if self.ig:
                    sa = 0.92
                cine_text.blend(frame, sig, sig_x, sig_y, sa)
                enc.stdin.write(frame.tobytes())
                if i % 6 == 0:
                    self.progress(i / float(total), "")
            enc.stdin.close()
        except (BrokenPipeError, OSError) as ex:
            reader.close()
            enc.kill()
            enc.wait()
            raise RuntimeError("ffmpeg وقف أثناء الرندر:\n%s\n%s" % (
                ex, b"".join(enc_err).decode("utf-8", "replace")[-700:]))
        except BaseException:
            reader.close()
            enc.kill()
            enc.wait()
            raise
        reader.close()
        enc.wait()
        if enc.returncode != 0:
            raise RuntimeError("ffmpeg وقع في التصدير:\n" + b"".join(enc_err).decode("utf-8", "replace")[-900:])
        if out_jpg:
            self.thumbnail(out_jpg, thumb_frame if thumb_frame is not None else frame, rl, k)
        self.log("  اترندر في %.0f ث (%.1f فريم/ث)" % (time.time() - t_start, total / max(0.1, time.time() - t_start)))
        return out_mp4

    def thumb_time(self, rl, plan):
        for e in plan["effects"]:
            if e["type"] == "freeze":
                return e["t0"] + 0.3
        if rl:
            return min(self.dur - 0.1, rl[0]["start"] + 0.6)
        return min(1.0, self.dur / 2)

    # -- composition ---------------------------------------------------------------
    def compose(self, plan, t, src, half, face, W, H, band):
        import cv2
        s = cine_edit.shot_at(plan, t)
        sw, sh = src.shape[1], src.shape[0]
        y0b, y1b = band
        bh = y1b - y0b
        p = (t - s["t0"]) / max(0.05, s["t1"] - s["t0"])
        push = s.get("push", (1.0, 1.0))
        zp = push[0] + (push[1] - push[0]) * ease(p)
        zp *= self.punch(s, t)
        kind = s["kind"]
        if kind == "single":
            f = dict(s["framing"])
            if "dx" in s:
                f["dx"] = s["dx"]
            if bh != H:
                canvas = np.zeros((H, W, 3), np.uint8)
                canvas[y0b:y1b] = warp(src, half, rect_for(face, sw, sh, W / float(bh), f, zp), W, bh)
                return canvas
            return warp(src, half, rect_for(face, sw, sh, W / float(H), f, zp), W, H)
        if kind == "panels":
            hs, g = self.panel_rows(H)
            fr = [dict(x) for x in s["panels"]]
            for (ts, panel, choice) in s.get("swaps", []):
                if t >= ts:
                    fr[panel] = dict(choice)
            canvas = np.zeros((H, W, 3), np.uint8)
            y = 0
            for j in range(3):
                canvas[y:y + hs[j]] = warp(src, half, rect_for(face, sw, sh, W / float(hs[j]), fr[j], zp), W, hs[j])
                y += hs[j] + g
            return canvas
        if kind == "split":
            a = warp(src, half, rect_for(face, sw, sh, W / float(H), s["a"], zp), W, H)
            b = warp(src, half, rect_for(face, sw, sh, W / float(H), dict(s["b"], dx=0.12), 1.0), W, H)
            m = self.diag_mask(W, H)
            out = (a.astype(np.float32) * m + b.astype(np.float32) * (1 - m))
            return out.astype(np.uint8)
        if kind == "double":
            base = warp(src, half, rect_for(face, sw, sh, W / float(H), s["base"], zp), W, H)
            over = warp(src, half, rect_for(face, sw, sh, W / float(H), s["over"], 1.0 + 0.05 * p), W, H)
            m = self.radial(W, H)
            bf, of = base.astype(np.float32), over.astype(np.float32)
            scr = 255 - (255 - bf) * (255 - of) / 255.0
            out = bf + (scr - bf) * (0.62 * m)
            return np.clip(out, 0, 255).astype(np.uint8)
        return warp(src, half, rect_for(face, sw, sh, W / float(H), cine_edit.MED, zp), W, H)

    def punch(self, s, t):
        z = 1.0
        for tb in s.get("punches", []):
            if t >= tb:
                z = max(z, 1.0 + 0.11 * np.exp(-(t - tb) / 0.32))
        return z

    _masks = {}

    def diag_mask(self, W, H):
        key = ("diag", W, H)
        if key not in self._masks:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            line = H * 0.60 - (xx / W) * H * 0.22
            seam = 3.0 * W / 1080.0
            m = np.clip((line - yy) / seam + 0.5, 0, 1)
            self._masks[key] = m[..., None]
        return self._masks[key]

    def radial(self, W, H):
        key = ("rad", W, H)
        if key not in self._masks:
            yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
            r = np.sqrt(((xx / W - 0.5) * 1.6) ** 2 + ((yy / H - 0.42) * 1.2) ** 2)
            self._masks[key] = (1 - cine_look._smoothstep(0.25, 0.75, r))[..., None].astype(np.float32)
        return self._masks[key]

    # -- effects ---------------------------------------------------------------------
    def temporal(self, plan, t, canvas, st, W, H):
        import cv2
        for e in plan["effects"]:
            if not (e["t0"] <= t <= e["t1"]):
                continue
            if e["type"] == "freeze":
                if st["frozen"] is None or st.get("frozen_id") != id(e):
                    st["frozen"], st["frozen_id"] = canvas.copy(), id(e)
                p = (t - e["t0"]) / max(0.05, e["t1"] - e["t0"])
                z = 1.0 + 0.06 * ease(p)
                M = cv2.getRotationMatrix2D((W / 2, H * 0.42), 0, z)
                fr = cv2.warpAffine(st["frozen"], M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
                gray = cv2.cvtColor(cv2.cvtColor(fr, cv2.COLOR_RGB2GRAY), cv2.COLOR_GRAY2RGB)
                canvas = cv2.addWeighted(fr, 0.62, gray, 0.38, 0)
            elif e["type"] == "step":
                n = int(round((t - e["t0"]) * FPS))
                if n % 3 == 0 or st["held"] is None:
                    st["prev_held"], st["held"] = st["held"], canvas.copy()
                ph = st["prev_held"] if st["prev_held"] is not None else st["held"]
                canvas = cv2.addWeighted(st["held"], 0.72, ph, 0.28, 0)
            elif e["type"] == "echo":
                p = min(1.0, (t - e["t0"]) / 0.25, (e["t1"] - t) / 0.25)
                if st["trail"] is None:
                    st["trail"] = canvas.copy()
                st["trail"] = cv2.addWeighted(st["trail"], 0.8, canvas, 0.2, 0)
                ghost = cv2.max(canvas, st["trail"])
                canvas = cv2.addWeighted(canvas, 1 - 0.5 * p, ghost, 0.5 * p, 0)
        if not any(e["type"] == "echo" and e["t0"] <= t <= e["t1"] for e in plan["effects"]):
            st["trail"] = None
        return canvas

    def light_at(self, plan, t, light):
        extra = None
        for e in plan["effects"]:
            if not (e["t0"] <= t <= e["t1"]):
                continue
            p = (t - e["t0"]) / max(0.05, e["t1"] - e["t0"])
            add = None
            if e["type"] == "leak":
                add = light.leak(p, e.get("seed", 1), e.get("strength", 1.0))
            elif e["type"] == "burn":
                add = light.burn(p, e.get("seed", 1), e.get("white", True))
            elif e["type"] == "flash" and not self.ig:
                v = light.flash(p)
                add = np.full((light.sh, light.sw, 3), v, np.float32) * np.array([1.0, 0.93, 0.86], np.float32)
            if add is not None:
                extra = add if extra is None else extra + add
        return extra

    def after(self, plan, t, frame, face, W, H, band, k):
        import cv2
        for e in plan["effects"]:
            if e["type"] != "iris" or not (e["t0"] <= t <= e["t1"]):
                continue
            at = e["at"]
            if t < at:
                p = 1.0 - (t - e["t0"]) / max(0.05, at - e["t0"])  # closing
            else:
                p = (t - at) / max(0.05, e["t1"] - at)              # opening
            p = ease(p)
            cx, cy = W * 0.5, H * 0.42
            R = np.hypot(W, H) * 0.62 * p + 1
            sw, sh = W // 4, H // 4
            yy, xx = np.mgrid[0:sh, 0:sw].astype(np.float32)
            d = np.hypot(xx * 4 - cx, yy * 4 - cy)
            m = np.clip((R - d) / (14.0 * k), 0, 1)
            m = cv2.resize(m, (W, H), interpolation=cv2.INTER_LINEAR)[..., None]
            frame = (frame.astype(np.float32) * m).astype(np.uint8)
        s = cine_edit.shot_at(plan, t)
        if band[0] > 0:
            frame[:band[0]] = 0
            frame[band[1]:] = 0
        if s["kind"] == "panels":
            hs, g = self.panel_rows(H)
            frame[hs[0]:hs[0] + g] = 0
            frame[hs[0] + g + hs[1]:hs[0] + 2 * g + hs[1]] = 0
        return frame

    @staticmethod
    def panel_rows(H):
        """Panel heights + gutter: two shorter panels on top, a taller one at the bottom
        so the lyric line (fixed at y≈1330) never sits on a gutter."""
        g = max(2, H // 192)
        top = int((H - 2 * g) * 0.29)
        return [top, top, H - 2 * g - 2 * top], g

    # -- thumbnail ------------------------------------------------------------------
    def thumbnail(self, path, frame, rl, k):
        from PIL import Image
        img = frame.copy()
        if k != 1.0:
            import cv2
            img = cv2.resize(img, (1080, 1920), interpolation=cv2.INTER_CUBIC)
        text = self.hook_text or (rl[0]["text"] if rl else "")
        key = self.hook_key if self.hook_text else (rl[0]["key"] if rl else -1)
        if text:
            h = cine_text.Hook(text, key, k=1.15, cy=1250, until=99)
            h.draw(img, 0.0)
        sig, x, dy = cine_text.signature(self.song, 1.0, 1080)
        cine_text.blend(img, sig, x, 1585 + dy, 0.95)
        tmp = path + ".tmp.jpg"
        Image.fromarray(img).save(tmp, "JPEG", quality=92, subsampling=0)
        os.replace(tmp, path)
