"""Session: tracker frames in, live target/error out, and per-note results as
each step finishes. Session time = input samples / sr (the mic clock); the
latency compensation `lat` maps sung frames back onto the target clock."""
import bisect
import datetime

import numpy as np

import analysis as A
import engine as E


class Session:
    def __init__(self, plan, lat=0.0):
        self.plan = plan
        self.steps = plan.steps if plan else []
        self.mode = plan.mode if plan else "free"
        self.info = dict(plan.info) if plan else {}
        self.info["mode"] = self.mode
        self.lat = float(lat)
        self.T, self.M = [], []        # raw frame time, sung midi (NaN = silence)
        self.frames = []               # (t_aligned, midi|None, target|None, live err|None, phase, step)
        self.notes = []                # per-note results (analysis.score_step)
        self.scored_upto = 0
        self.now = 0.0
        self.level_db = -120.0
        if plan and plan.end is not None:
            self.end = plan.end
        elif self.steps:
            self.end = self.steps[-1].sing1 + self.lat + 0.6
        else:
            self.end = None
        self._si = 0

    def done(self):
        return self.end is not None and self.now >= self.end

    @property
    def now_a(self):
        return self.now - self.lat

    def phase_at(self, t):
        """('lead'|'listen'|'gap'|'sing'|'tail'|'end', step) at target time t."""
        if not self.steps:
            return ("calib" if self.mode == "calib" else "free"), None
        while self._si + 1 < len(self.steps) and t >= self.steps[self._si + 1].listen0:
            self._si += 1
        while self._si > 0 and t < self.steps[self._si].listen0:
            self._si -= 1
        st = self.steps[self._si]
        if t < st.listen0:
            return "lead", st
        if st.listen and t < st.listen1:
            return "listen", st
        if t < st.sing0:
            return "gap", st
        if t < st.sing1:
            return "sing", st
        if self._si == len(self.steps) - 1:
            return "end", st
        return "tail", st

    @staticmethod
    def seg_at(st, rel):
        for s in st.segs:
            if s.t0 <= rel < s.t1:
                return s
        return None

    def target_at(self, t):
        ph, st = self.phase_at(t)
        if ph == "listen":
            s = self.seg_at(st, t - st.listen0)
            return (s.at(t - st.listen0), s) if s else (None, None)
        if ph == "sing":
            s = self.seg_at(st, t - st.sing0)
            return (s.at(t - st.sing0), s) if s else (None, None)
        return None, None

    def live_error(self, t, m):
        """cents against what should be sung at target time t (None = not now)."""
        ph, st = self.phase_at(t)
        if ph != "sing":
            return None, None
        rel = t - st.sing0
        s = self.seg_at(st, rel)
        if s is None:
            return None, None
        if s.glide:
            ts = np.linspace(max(s.t0, rel - E.GLIDE_SLACK), min(s.t1, rel + E.GLIDE_SLACK), 11)
            cand = np.array([s.at(x) for x in ts])
            e = (m - cand) * 100.0
            return float(e[np.argmin(np.abs(e))]), s.at(rel)
        return (m - s.m0) * 100.0, s.m0

    def process(self, frames):
        for t, f0, conf, db in frames:
            self.now = t
            self.level_db = db
            m = E.hz_to_midi(f0) if f0 else None
            self.T.append(t)
            self.M.append(np.nan if m is None else m)
            ta = t - self.lat
            if self.mode in ("free", "calib"):
                tgt = float(np.floor(m + 0.5)) if m is not None else None
                err = (m - tgt) * 100.0 if m is not None else None
                self.frames.append((ta, m, tgt, err, self.mode, -1))
                continue
            ph, st = self.phase_at(ta)
            err, tgt = (None, None)
            if m is not None:
                err, tgt = self.live_error(ta, m)
            if tgt is None:
                tgt, _ = self.target_at(ta)
            self.frames.append((ta, m, tgt, err, ph, st.index if st else -1))
        self.score_ready()

    def step_arrays(self, st):
        a = bisect.bisect_left(self.T, st.sing0 + self.lat - 0.4)
        b = bisect.bisect_right(self.T, st.sing1 + self.lat + 0.6)
        return np.array(self.T[a:b]) - self.lat, np.array(self.M[a:b], float)

    def score_ready(self, final=False):
        while self.scored_upto < len(self.steps):
            st = self.steps[self.scored_upto]
            if not final and self.now_a < st.sing1 + 0.4:
                break
            if final and self.now_a < st.sing0 + 0.3:
                break
            ta, m = self.step_arrays(st)
            res = A.score_step(st, ta, m, self.mode)
            if final and self.now_a < st.sing1:   # stopped mid-step: keep notes that ended
                res = [r for r in res if r["t"] + (st.segs[r["k"]].t1 - st.segs[r["k"]].t0) <= self.now_a]
            self.notes += res
            self.scored_upto += 1

    def finish(self):
        self.score_ready(final=True)

    # ---- summary
    def score(self):
        res = {"lat_ms": round(self.lat * 1000)}
        errs = np.array([f[3] for f in self.frames if f[3] is not None and f[1] is not None])
        voiced = [f for f in self.frames if f[1] is not None]
        res["frames"] = int(len(errs))
        res["voiced_s"] = round(len(voiced) * E.HOP / E.SR, 1)
        if len(errs):
            a = np.abs(errs)
            near = a[a <= E.WRONG_NOTE]
            res["avg_cents"] = round(float(near.mean()), 1) if len(near) else None
            res["in_tune_pct"] = round(100.0 * float(np.mean(a <= E.GREEN)), 1)
            res["near_pct"] = round(100.0 * float(np.mean(a <= E.YELLOW)), 1)
            res["bias_cents"] = round(float(np.median(errs[a <= E.WRONG_NOTE])), 1) if len(near) else None
        else:
            res.update(avg_cents=None, in_tune_pct=None, near_pct=None, bias_cents=None)
        hi, lo = E.stable_range([f[1] for f in self.frames])
        res["highest_midi"], res["lowest_midi"] = hi, lo
        res["highest"], res["lowest"] = E.note_label_rounded(hi), E.note_label_rounded(lo)
        res.update(note_summary(self.notes))
        return res


def note_summary(notes):
    out = {"notes": len(notes)}
    if not notes:
        out.update(exact_pct=None, close_pct=None, note_avg_cents=None, missed=0, worst=[])
        return out
    sung = [n for n in notes if n["err"] is not None and abs(n["err"]) <= E.WRONG_NOTE]
    out["exact_pct"] = round(100.0 * sum(n["exact"] for n in notes) / len(notes), 1)
    out["close_pct"] = round(100.0 * sum(n["close"] for n in notes) / len(notes), 1)
    out["note_avg_cents"] = round(float(np.mean([abs(n["err"]) for n in sung])), 1) if sung else None
    out["note_bias"] = round(float(np.median([n["err"] for n in sung])), 1) if sung else None
    out["missed"] = sum(n["missed"] for n in notes)
    worst = sorted([n for n in notes if n["err"] is not None], key=lambda n: -abs(n["err"]))[:6]
    out["worst"] = [{"label": n["label"], "deg": n["deg"], "err": n["err"], "t": n["song_t"]} for n in worst]
    ons = [n["onset"] for n in notes if n.get("onset")]
    if ons:
        sl = [o["slide_ms"] for o in ons if o["slide_ms"] is not None]
        out["onset"] = {"n": len(ons), "landed_pct": round(100.0 * sum(o["landed"] for o in ons) / len(ons), 1),
                        "slide_c": round(float(np.mean([abs(o["start_c"]) for o in ons])), 1),
                        "from_below_pct": round(100.0 * sum(o["start_c"] < -o["land_c"] for o in ons) / len(ons), 1),
                        "from_above_pct": round(100.0 * sum(o["start_c"] > o["land_c"] for o in ons) / len(ons), 1),
                        "slide_ms": round(float(np.mean(sl)), 0) if sl else None,
                        "late_pct": round(100.0 * sum(o["slide_ms"] is None for o in ons) / len(ons), 1)}
    lp = [n for n in notes if "leap_ok" in n]
    if lp:
        by = {}
        for n in lp:
            by.setdefault(n["leap"], []).append(n["leap_ok"])
        out["leaps"] = {"ok_pct": round(100.0 * sum(n["leap_ok"] for n in lp) / len(lp), 1),
                        "by": {k: round(100.0 * sum(v) / len(v)) for k, v in by.items()}}
    sb = [n["stab"] for n in notes if n.get("stab")]
    if sb:
        vr = [s["vib_rate"] for s in sb if s["vib_rate"]]
        out["stab"] = {"n": len(sb), "drift_c": round(float(np.mean([s["drift_c"] for s in sb])), 1),
                       "end_drop_c": round(float(np.mean([s["end_drop_c"] for s in sb])), 1),
                       "vib_rate": round(float(np.mean(vr)), 2) if vr else None,
                       "vib_width": round(float(np.mean([s["vib_width"] for s in sb if s["vib_rate"]])), 1) if vr else 0.0}
    return out



def history_entry(session, duration_s, when=None, wav=None, blind=False):
    when = when or datetime.datetime.now()
    e = {"date": when.strftime("%Y-%m-%d"), "time": when.strftime("%H:%M"),
         "duration_s": round(duration_s, 1), "wav": wav, "blind": bool(blind)}
    e.update(session.info)
    e.update(session.score())
    if session.mode == "song":
        e["off_notes"] = off_notes(session.notes)[:40]
    return e


def off_notes(notes):
    """Song mode: where you went off (beyond ±15c, or not sung), in song order."""
    out = [n for n in notes if n["missed"] or abs(n["err"]) > E.GREEN]
    return [{"t": n["song_t"], "label": n["label"], "err": n["err"],
             "zone": "missed" if n["missed"] else E.zone(n["err"])} for n in sorted(out, key=lambda n: n["song_t"])]
