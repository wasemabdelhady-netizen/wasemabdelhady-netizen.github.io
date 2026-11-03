"""«تسخين» — a 4 or 7 minute warm-up routine before singing: hum, «مممم» 5-note
scales from the low tonic, lip-trill sirens, «نغ» scales, «ما مي مو» arpeggios
and a slow Bayati jins on «آه». Every part: name, duration, one-line how-to,
a reference (oud / piano) playing the notes, soft claps before it. Nothing
above the chosen top (default D4, never above G4). Renders to one MP3."""
import datetime
import os
import subprocess
import sys

import numpy as np

import engine as E
import oud
import song as G

DEFAULT_DIR = r"E:\WORKING\clude\تمارين الصوت - Vocal Exercises\التسخين - Warm-up"
BREATH_GAP = 3.0      # silence before every exercise (breath), claps in its last 1.6 s


def out_dir():
    d = os.environ.get("PITCHTRAINER_WARMUP_DIR")
    if d:
        return d
    if os.name == "nt" and os.path.isdir("E:\\"):
        return DEFAULT_DIR
    return os.path.join(E.ROOT, "warmup")


class Part:
    def __init__(self, key, name, how, items):
        self.key, self.name, self.how = key, name, how
        self.items = items            # [("notes", [(t0, t1, midi)]), ("glide", m0, m1, dur), ("rest", dur)]

    def dur(self):
        return BREATH_GAP + sum(item_dur(i) for i in self.items)


def item_dur(i):
    return {"notes": lambda: i[1][-1][1], "glide": lambda: i[3], "rest": lambda: i[1]}[i[0]]()


def notes(rel, root, d, last=2.0):
    out, t = [], 0.0
    for k, r in enumerate(rel):
        L = d * (last if k == len(rel) - 1 else 1)
        out.append((t, t + L, root + r))
        t += L
    return out


def scale_steps(start, top, span, n):
    """Roots from start by semitones, at most n, never letting start+span pass top."""
    return [r for r in range(start, start + n) if r + span <= top]


def routine(start=43, top=62, length="normal"):
    """The routine as a list of Parts. length: 'short' (~4 min) or 'normal' (~7 min)."""
    top = min(top, E.TOP_LIMIT)
    short = length == "short"
    comfy = min(start + 5, top)
    parts = []
    n = {"hum": 4, "mmm": 6, "trill": 4, "ng": 4, "mamimo": 4, "bayati": 1} if short else \
        {"hum": 6, "mmm": 11, "trill": 7, "ng": 9, "mamimo": 8, "bayati": 3}
    reps = n["hum"]
    items = []
    for _ in range(reps):
        items += [("notes", [(0.0, 5.0, float(comfy))]), ("rest", 3.0)]
    parts.append(Part("hum", "همهمة", "شفايفك مقفولة وسنانك مفتوحة، همهمة خفيفة على النوتة — حس بالزنّة في وشّك.", items))
    sc = [0, 2, 4, 5, 7, 5, 4, 2, 0]
    items = []
    for r in scale_steps(start, top, 7, n["mmm"]):
        items += [("notes", notes(sc, float(r), 0.42)), ("rest", 4.6)]
    parts.append(Part("mmm", "«مممم» على سلم 5 نوتات", "نفس الهمهمة على السلم: اسمع مرة وكرّر، وكل مرة نص تون أعلى.", items))
    hi = float(min(start + 15, top))
    lo = min(float(start + 3), hi - 5)
    items = []
    for _ in range(n["trill"]):
        items += [("glide", hi, lo, 3.0), ("glide", lo, hi, 3.0), ("rest", 2.5)]
    parts.append(Part("trill", "«برررر» سايرن", "شفايف مرخية بتزنّ (lip trill) نازل وطالع مع الصوت — من غير ضغط على الزور.", items))
    items = []
    for r in scale_steps(start + 2, top, 7, n["ng"]):
        items += [("notes", notes(sc, float(r), 0.42)), ("rest", 4.6)]
    parts.append(Part("ng", "«نغ» على سلم 5 نوتات", "لسانك ورا عند سقف الحلق زي آخر «سِنْغ» — الصوت يطلع من المناخير.", items))
    ar = [0, 4, 7, 12, 7, 4, 0]
    items = []
    for r in scale_steps(start + 2, top, 12, n["mamimo"]):
        items += [("notes", notes(ar, float(r), 0.5)), ("rest", 4.6)]
    if not items:            # range too small for an octave: use a fifth arpeggio
        for r in scale_steps(start + 2, top, 7, n["mamimo"]):
            items += [("notes", notes([0, 4, 7, 4, 0], float(r), 0.5)), ("rest", 3.6)]
    parts.append(Part("mamimo", "«ما مي مو» على أربيجيو", "«ما» على أول نوتة، «مي» على اللي بعدها، «مو» فوق — بُق مفتوح وفك مرخي.", items))
    bay = [0, 1.5, 3, 5, 3, 1.5, 0]
    tonic = float(min(start + 7, top - 5))
    items = []
    for _ in range(n["bayati"]):
        items += [("notes", notes(bay, tonic, 1.5, last=2.0)), ("rest", 12.0)]
    parts.append(Part("bayati", "جنس بياتي على «آه»", "بطيء: اسمع الجنس وغنّيه على «آه» — امسك الـ «مي نص بيمول» في مكانها.", items))
    for p in parts:
        for i in p.items:
            if i[0] == "notes":
                assert max(n[2] for n in i[1]) <= top + 1e-9
            if i[0] == "glide":
                assert max(i[1], i[2]) <= top + 1e-9
    return parts


def clap(sr=E.SR, seed=0):
    rng = np.random.default_rng(seed)
    n = int(0.06 * sr)
    w = rng.standard_normal(n)
    w = np.convolve(w, [1, -0.9], mode="same")
    return (0.12 * w * np.exp(-np.arange(n) / (0.012 * sr))).astype(np.float32)


def render(parts, kind="oud", sr=E.SR):
    """One mono track; returns (audio, [(part, t_start, t_end)])."""
    total = sum(p.dur() for p in parts) + 1.0
    y = np.zeros(int(total * sr), np.float32)
    marks, t = [], 0.0
    for p in parts:
        t0 = t
        for k in range(4):                         # soft claps: ready, 2, 3, 4
            c = clap(seed=k)
            a = int((t + BREATH_GAP - 1.6 + 0.4 * k) * sr)
            y[a:a + len(c)] += c
        t += BREATH_GAP
        for i in p.items:
            a = int(t * sr)
            if i[0] == "notes":
                segs = [E.Seg(n0, n1, m) for n0, n1, m in i[1]]
                z = oud.render_step(E.Step(0, 0, segs), kind=kind)
            elif i[0] == "glide":
                z = oud.glide_tone(i[1], i[2], i[3], sr).astype(np.float32)
            else:
                z = np.zeros(0, np.float32)
            y[a:a + len(z)] += z[:len(y) - a]
            t += item_dur(i)
        marks.append((p, t0, t))
    return np.clip(y, -0.95, 0.95), marks


def export_mp3(parts, kind="oud", length="normal", folder=None, when=None):
    folder = folder or out_dir()
    os.makedirs(folder, exist_ok=True)
    when = when or datetime.datetime.now()
    y, _ = render(parts, kind)
    mins = int(round(len(y) / E.SR / 60.0))
    name = "%s - تسخين %s %d دقايق.mp3" % (when.strftime("%Y-%m-%d"), "قصير" if length == "short" else "عادي", mins)
    path = os.path.join(folder, name)
    ff = G.ffmpeg_path()
    if not ff:
        raise IOError("ffmpeg missing")
    kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    r = subprocess.run([ff, "-y", "-v", "error", "-f", "f32le", "-ac", "1", "-ar", str(E.SR), "-i", "-",
                        "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k", path],
                       input=np.ascontiguousarray(y, np.float32).tobytes(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    if r.returncode != 0:
        raise IOError(r.stderr.decode("utf-8", "replace")[-300:])
    return path
