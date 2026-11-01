"""Cinematic mode — the edit: shots and effects per style (pure timing logic, no pixels).

A reel's plan is a list of shots (what is on screen and how the camera moves) plus
timed effects. Times are reel seconds (0 = first frame).

Styles:
  A  Cinematic  beat punch-ins + push-ins, light leaks, film burn between sections
  B  Panels     3 stacked panels with beat cuts, at most 2 soft flashes, never strobing
  C  Letterbox  2.39-style bars, film-burn transitions, echo on long notes
  D  Drama      freeze + red stamp on the key line, split screen, double exposure,
                iris wipe, step-print
Shared: cuts land on beats, something changes every 2–3 s, the reel opens on its
strongest line inside the first second, and the face is on screen from frame one.
New styles: add a builder to STYLES (e.g. a Remotion/mg-styles lyric layer or
Rough.js beat scribbles would add their own effect types here and in cine_render).
"""
import random

STYLE_NAMES = {
    "A": "A — سينمائي (Cinematic)",
    "B": "B — بانلز (Panels)",
    "C": "C — ليتربوكس (Letterbox)",
    "D": "D — دراما (Drama)",
}

# framings: face = how much of the frame height the face takes (works for 16:9 and
# vertical footage alike); z = plain zoom (1 = widest) when there is no `face`;
# eye = where the face centre sits (fraction of the frame height, negative = below
# the frame, i.e. the shot looks at the hands/chest); dx/dy = offset in frame sizes
WIDE = dict(z=1.0, eye=0.40)
MED = dict(face=0.22, eye=0.40)
CLOSE = dict(face=0.30, eye=0.42)
ECU = dict(face=0.36, eye=0.45)        # chin ends ~0.63 H, above the lyric block (0.67–0.79 H)
IG_OPEN = dict(face=0.24, eye=0.48)    # IG/FB opening: room for the hook above the head
P_WIDE = dict(z=1.0, eye=0.45)         # panel framings (landscape strips)
P_FACE = dict(face=0.75, eye=0.50)
P_EYES = dict(face=1.45, eye=0.62)
P_HANDS = dict(face=0.55, eye=-0.75)   # gestures / chest (the bottom panel, under the lyrics)
P_HANDS_R = dict(face=0.55, eye=-0.75, dx=0.14)
P_HANDS_L = dict(face=0.55, eye=-0.75, dx=-0.14)
P_SIDE = dict(face=0.6, eye=0.48, dx=0.18)


class Ctx:
    """What a builder knows about the reel."""

    def __init__(self, dur, beats, lines, period, ig=False, seed=1):
        self.dur = dur
        self.beats = beats            # [(t, strength, downbeat)]
        self.lines = lines            # [dict(text, start, end, words=[(s, e)], key)]
        self.period = period
        self.ig = ig
        self.rng = random.Random(seed)
        st = sorted(b[1] for b in beats) or [0.0]
        self.strong = st[int(len(st) * 0.7)] if st else 0.0

    # -- timing helpers ---------------------------------------------------------
    def cut_times(self, lo=2.0, hi=3.0, first=None):
        """Shot boundaries on beats, every lo..hi seconds (downbeats/line starts preferred)."""
        cuts = [0.0]
        bt = [b for b in self.beats if 0.3 < b[0] < self.dur - 0.8]
        starts = [l["start"] for l in self.lines]
        t = 0.0
        if first:
            t = first
            cuts.append(first)
        while True:
            cand = [b for b in bt if t + lo <= b[0] <= t + hi]
            if not cand:
                nxt = t + (lo + hi) / 2
                near = [b for b in bt if abs(b[0] - nxt) < self.period * 0.6]
                if near:
                    cand = near
                elif nxt < self.dur - 1.0:
                    cuts.append(nxt)
                    t = nxt
                    continue
                else:
                    break

            def score(b):
                s = b[1] + (0.6 if b[2] else 0.0)
                if any(abs(b[0] - x) < 0.25 for x in starts):
                    s += 0.5
                return s
            best = max(cand, key=score)
            if best[0] > self.dur - 1.0:
                break
            cuts.append(best[0])
            t = best[0]
        cuts.append(self.dur)
        return cuts

    def section_breaks(self):
        """Times between line groups (gaps or a new phrase) — where transitions go."""
        out = []
        for a, b in zip(self.lines, self.lines[1:]):
            gap = b["start"] - a["end"]
            if gap > 0.6 or (b["start"] - self.lines[0]["start"] > 6 and gap > 0.25):
                out.append((a["end"] + b["start"]) / 2)
        out = [t for t in out if 2.0 < t < self.dur - 2.0]
        if not out and self.dur > 8:
            out = [self.dur * 0.55]
        # keep them ≥ 5 s apart
        res = []
        for t in out:
            if not res or t - res[-1] > 5.0:
                res.append(t)
        return res

    def snap(self, t, window=0.4):
        near = [b[0] for b in self.beats if abs(b[0] - t) <= window]
        return min(near, key=lambda x: abs(x - t)) if near else t

    def key_line(self):
        """Index of the line with a *marked* key word, else the longest held line —
        among lines that sit fully inside the reel (room for a freeze after them)."""
        best, bi = -1.0, 0
        for i, l in enumerate(self.lines):
            s = (l["end"] - l["start"]) + (3.0 if l.get("marked") else 0.0)
            inside = 1.0 < l["start"] and l["end"] < self.dur - 1.2
            if not inside:
                s -= 10.0
            if s > best:
                best, bi = s, i
        return bi

    def long_notes(self, min_len=0.85):
        out = []
        for l in self.lines:
            for s, e in l["words"]:
                if e - s >= min_len and s > 0.5:
                    out.append((s, e))
        return out

    def strong_downbeats(self, t0, t1, gap=1.0):
        out = []
        for t, s, d in self.beats:
            if t0 + 0.15 <= t < t1 - 0.2 and d and s >= self.strong and (not out or t - out[-1] >= gap):
                out.append(t)
        return out


def shot(t0, t1, kind="single", **kw):
    d = dict(t0=t0, t1=t1, kind=kind)
    d.update(kw)
    return d


def fx(kind, t0, t1, **kw):
    d = dict(type=kind, t0=t0, t1=t1)
    d.update(kw)
    return d


# ----------------------------------------------------------------------------- A

def build_a(c):
    cuts = c.cut_times()
    breaks = [c.snap(b) for b in c.section_breaks()]
    # a cut at every section break (the burn hides it)
    for b in breaks:
        if all(abs(b - x) > 0.8 for x in cuts):
            cuts.append(b)
    cuts = sorted(cuts)
    kl = c.lines[c.key_line()] if c.lines else None
    cycle = [MED, CLOSE, WIDE, CLOSE, MED, ECU]
    shots, effects = [], []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        f = CLOSE if i == 0 else cycle[i % len(cycle)]
        if kl and a <= kl["start"] < b:
            f = ECU
        push = (1.0, 1.07) if f in (WIDE, MED) else (1.0, 1.04)
        dx = c.rng.choice([-0.06, 0.0, 0.06]) if f is WIDE else 0.0
        punches = c.strong_downbeats(a, b) if f in (MED, WIDE, CLOSE) else []
        shots.append(shot(a, b, framing=f, push=push, dx=dx, punches=punches))
    for b in breaks:
        effects.append(fx("burn", b - 0.32, b + 0.32, white=not c.ig, seed=int(b * 10)))
    # light leaks: on a few line starts, ≥ 5 s apart, not over a burn
    t_last = -10
    for l in c.lines:
        t = l["start"]
        if t - t_last >= 6.0 and all(abs(t - b) > 1.5 for b in breaks) and t < c.dur - 1.5:
            effects.append(fx("leak", t - 0.2, min(c.dur, t + 1.4), seed=int(t * 7), strength=0.85))
            t_last = t
    return shots, effects


# ----------------------------------------------------------------------------- B

def build_b(c):
    cuts = c.cut_times()
    kl = c.lines[c.key_line()] if c.lines else None
    shots, effects = [], []
    # the bottom panel sits under the lyric line: always chest / hands, never a face
    sets = [[P_WIDE, P_EYES, P_HANDS], [P_EYES, P_FACE, P_HANDS_R], [P_FACE, P_WIDE, P_HANDS_L],
            [P_SIDE, P_EYES, P_HANDS]]
    k = 0
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if i == 0:
            shots.append(shot(a, b, framing=CLOSE, push=(1.0, 1.04)))
            continue
        if kl and a <= kl["start"] < b:
            shots.append(shot(a, b, framing=ECU, push=(1.0, 1.05)))
            continue
        if i % 3 == 2:
            shots.append(shot(a, b, framing=MED, push=(1.0, 1.06)))
            continue
        base = [dict(f) for f in sets[k % len(sets)]]
        k += 1
        # beat cuts inside the panel shot: one panel changes framing every 2 beats
        swaps = []
        bt = [x[0] for x in c.beats if a + 0.2 < x[0] < b - 0.2]
        pool = [P_WIDE, P_FACE, P_EYES, P_SIDE]
        hands = [P_HANDS, P_HANDS_R, P_HANDS_L]
        cur = list(base)
        for j, t in enumerate(bt[1::2]):
            panel = j % 3
            if panel == 2:
                choice = hands[(k + j) % len(hands)]
                if choice == cur[2]:
                    choice = hands[(k + j + 1) % len(hands)]
                cur[2] = choice
                swaps.append((t, 2, choice))
                continue
            n = (k + j * 2 + panel) % len(pool)
            for step in range(len(pool)):  # never the same framing in two panels
                choice = pool[(n + step) % len(pool)]
                if all(choice != cur[q] for q in range(3)):
                    break
            cur[panel] = choice
            swaps.append((t, panel, choice))
        shots.append(shot(a, b, kind="panels", panels=base, swaps=swaps))
    if not c.ig:  # max two soft flashes, far apart
        cands = []
        if kl:
            cands.append(c.snap(kl["start"], 0.25))
        best = sorted([x for x in c.beats if x[2] and 3.0 < x[0] < c.dur - 2.0], key=lambda x: -x[1])
        for x in best:
            if all(abs(x[0] - y) > 6.0 for y in cands):
                cands.append(x[0])
                break
        for t in cands[:2]:
            effects.append(fx("flash", t, t + 0.22))
    return shots, effects


# ----------------------------------------------------------------------------- C

def build_c(c):
    cuts = c.cut_times(2.2, 3.0)
    breaks = [c.snap(b) for b in c.section_breaks()]
    for b in breaks:
        if all(abs(b - x) > 0.8 for x in cuts):
            cuts.append(b)
    cuts = sorted(cuts)
    kl = c.lines[c.key_line()] if c.lines else None
    cycle = [dict(MED, dx=-0.07), dict(CLOSE), dict(WIDE, dx=0.07), dict(CLOSE, dx=0.04)]
    shots, effects = [], []
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        f = dict(CLOSE) if i == 0 else cycle[i % len(cycle)]
        if kl and a <= kl["start"] < b:
            f = dict(ECU)
        shots.append(shot(a, b, framing=f, push=(1.0, 1.09), dx=f.get("dx", 0.0)))
    for b in breaks:
        effects.append(fx("burn", b - 0.35, b + 0.35, white=not c.ig, seed=int(b * 13)))
    for s, e in sorted(c.long_notes(), key=lambda n: -(n[1] - n[0]))[:6]:
        if len([x for x in effects if x["type"] == "echo"]) >= 3:
            break
        if all(abs(s - x["t0"]) > 3.0 for x in effects if x["type"] == "echo"):
            effects.append(fx("echo", s, min(c.dur, e + 0.25)))
    return shots, effects, dict(bars=True)


# ----------------------------------------------------------------------------- D

def build_d(c):
    cuts = c.cut_times()
    breaks = [c.snap(b) for b in c.section_breaks()]
    ki = c.key_line()
    kl = c.lines[ki] if c.lines else None
    shots, effects = [], []
    # freeze + stamp on the key word of the key line
    freeze = None
    if kl:
        kw = kl.get("key", -1)
        ok = [j for j in range(len(kl["words"])) if 1.2 < kl["words"][j][0] < c.dur - 2.0]
        if kw is None or kw not in ok:  # the gold word the karaoke shows, if it fits
            import engine
            g = engine.pick_gold(kl["text"].split())
            kw = g if g in ok else (max(ok, key=lambda j: kl["words"][j][1] - kl["words"][j][0]) if ok else -1)
        t = kl["words"][kw][0] if kw >= 0 else -1
        if 1.2 < t < c.dur - 2.0:
            freeze = (t, min(t + 1.15, c.dur - 0.6))
            effects.append(fx("freeze", freeze[0], freeze[1]))
            effects.append(fx("stamp", freeze[0], min(c.dur, freeze[1] + 0.45), word=kl["text"].split()[kw]))
            cuts = [x for x in cuts if not (freeze[0] - 0.6 < x < freeze[1] + 0.3)] + [freeze[0], freeze[1]]
            cuts = sorted(set(cuts))
    specials = ["split", "double"]
    used = set()
    for i, (a, b) in enumerate(zip(cuts, cuts[1:])):
        if i == 0:
            shots.append(shot(a, b, framing=CLOSE, push=(1.0, 1.05)))
            continue
        if freeze and abs(a - freeze[0]) < 1e-6:
            shots.append(shot(a, b, framing=ECU, push=(1.0, 1.0)))
            continue
        long_enough = b - a >= 1.8
        if long_enough and i >= 2 and specials and i % 2 == 0:
            kind = specials.pop(0)
            used.add(kind)
            if kind == "split":
                shots.append(shot(a, b, kind="split", a=dict(CLOSE), b=dict(WIDE), push=(1.0, 1.06)))
            else:
                shots.append(shot(a, b, kind="double", base=dict(WIDE), over=dict(ECU), push=(1.0, 1.08)))
            continue
        f = [MED, CLOSE, WIDE, ECU][i % 4]
        shots.append(shot(a, b, framing=f, push=(1.0, 1.06)))
    # iris wipe at a section break (or the middle)
    iris_t = breaks[0] if breaks else c.snap(c.dur * 0.6)
    if freeze and abs(iris_t - freeze[0]) < 2.0:
        iris_t = breaks[1] if len(breaks) > 1 else None
    if iris_t and 2.0 < iris_t < c.dur - 1.5:
        effects.append(fx("iris", iris_t - 0.25, iris_t + 0.35, at=iris_t))
        if all(abs(iris_t - s["t0"]) > 0.3 for s in shots):  # make the wipe reveal a new shot
            for s in shots:
                if s["t0"] < iris_t < s["t1"] and s["kind"] == "single":
                    s2 = dict(s, t0=iris_t, framing=dict(CLOSE if s["framing"] is not CLOSE else WIDE))
                    s["t1"] = iris_t
                    shots.insert(shots.index(s) + 1, s2)
                    break
    # step-print on a long note away from the freeze (or a 1.3 s phrase in the second half)
    spots = [(s, e) for s, e in c.long_notes(0.7)
             if not freeze or abs(s - freeze[0]) > 2.5]
    if spots:
        s, e = spots[0]
        effects.append(fx("step", s, min(e + 0.2, s + 1.6)))
    else:
        t = c.snap(c.dur * 0.75)
        if not freeze or abs(t - freeze[0]) > 2.5:
            effects.append(fx("step", t, min(c.dur - 0.3, t + 1.3)))
    return shots, effects


STYLES = {"A": build_a, "B": build_b, "C": build_c, "D": build_d}


def plan(style, ctx):
    res = STYLES[style](ctx)
    shots, effects = res[0], res[1]
    opts = res[2] if len(res) > 2 else {}
    shots = [s for s in shots if s["t1"] - s["t0"] > 0.05]
    shots.sort(key=lambda s: s["t0"])
    effects.sort(key=lambda e: e["t0"])
    if ctx.ig:  # nothing may cover the face in the first second
        effects = [e for e in effects if e["t0"] >= 1.0 or e["type"] in ("echo",)]
        # face lower in the opening shot: clean headroom for the big sound-off hook
        first = shots[0]
        if first["kind"] != "single":
            first.clear()
            first.update(shot(0.0, shots[1]["t0"] if len(shots) > 1 else ctx.dur))
        first["framing"] = dict(IG_OPEN)
        first["push"] = (1.0, 1.03)
        first.pop("punches", None)
        first.pop("dx", None)
    return dict(style=style, shots=shots, effects=effects, opts=opts)


def shot_at(p, t):
    for s in p["shots"]:
        if s["t0"] <= t < s["t1"]:
            return s
    return p["shots"][-1]
