"""Cinematic mode: footage of Wasem singing the whole song to camera -> several edited reels.

    sync (camera audio ↔ master) -> word timing (whisper/.srt -> CTC alignment, words.json)
    -> beats -> face track -> suggested reels (start on the strongest line)
    -> cine_render per reel (style A/B/C/D, optional Instagram/Facebook version)

Command line (tests / batch):
    python cinematic.py --footage F.mp4 --master M.wav --lyrics L.txt [--stem V.wav] [--srt L.srt]
                        [--reels 4] [--ig] [--deglare] [--out DIR] [--preview]
"""
import os
import re
import threading
import time


import autoreel
import cine_align
import cine_audio
import cine_edit
import cine_face
import cine_look
import textfx
import cine_render
import engine

FOOTAGE_DIR_HINT = "footage"      # «لقطات - Footage»
REELS_DIR = "الريلز - Reels"
MIN_LEN, MAX_LEN, TARGET = 14.0, 30.0, 24.0
LEAD = 0.45                       # the reel's first word lands at 0.45 s
STYLE_ORDER = "ABCD"


def parse_srt(path):
    """[(start, end, text)] from an .srt file."""
    txt = engine.read_text(path)
    out = []
    for block in re.split(r"\n\s*\n", txt.replace("\r", "")):
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", block)
        if not m:
            continue
        g = [int(x) for x in m.groups()]
        s = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
        e = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
        text = " ".join(l.strip() for l in block[m.end():].strip().splitlines() if l.strip())
        if text:
            out.append((s, e, text))
    return out


def song_folder(footage):
    """…\\الأغاني - Songs\\<song>\\لقطات - Footage\\x.mp4 -> …\\<song>"""
    d = os.path.dirname(os.path.abspath(footage))
    if FOOTAGE_DIR_HINT in os.path.basename(d).lower() or "لقطات" in os.path.basename(d):
        return os.path.dirname(d)
    return d


def default_song_name(footage):
    name = os.path.basename(song_folder(footage))
    return name.split(" - ")[0].strip() or name


def default_out_dir(footage):
    return os.path.join(song_folder(footage), REELS_DIR)


class Project:
    def __init__(self, footage, master, lyrics, stem="", srt="", song="", out_dir="",
                 n_reels=4, ig=False, deglare=False, whisper="small", progress=None, log=None,
                 green=cine_look.GREEN_DEFAULT, pack=None,
                 cancel=None, asr=None, offset=None):
        self.footage, self.master, self.lyrics = footage, master, lyrics
        self.stem, self.srt = stem or "", srt or ""
        self.song = (song or "").strip() or default_song_name(footage)
        self.out_dir = out_dir or default_out_dir(footage)
        self.n_reels, self.ig, self.deglare, self.whisper = n_reels, ig, deglare, whisper
        self.green = green             # green gain of the v5 grade for this footage
        self.pack = pack or textfx.default_pack()  # lyric fonts + word animation
        self.progress = progress or (lambda f, m="": None)
        self.log = log or (lambda m: print(m, flush=True))
        self.cancel = cancel or threading.Event()
        self.asr = asr                 # tests: callable(texts) -> autoreel.Line list
        self.offset = offset           # set by the user to skip / override the auto sync
        self.rate = 1.0                # master seconds per footage second (camera clock drift)
        self.conf = None
        self.lines, self.sections = [], []

    @property
    def words_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.lyrics)),
                            "%s.words.json" % engine.safe_name(self.song))

    def stage(self, a, b):
        return lambda f, m="": self.progress(a + (b - a) * max(0.0, min(1.0, f)), m)

    def check(self):
        for p, label in ((self.footage, "الفيديو"), (self.master, "الماستر"), (self.lyrics, "ملف الكلمات")):
            if not p or not os.path.isfile(p):
                raise RuntimeError("اختار %s الأول" % label)
        for p, label in ((self.stem, "الفوكال ستيم"), (self.srt, "ملف الـ srt")):
            if p and not os.path.isfile(p):
                raise RuntimeError("%s مش موجود:\n%s" % (label, p))
        if not engine.features.check("raqm"):
            raise RuntimeError("RAQM مش شغال — الحروف العربي هتطلع مقطّعة.")

    # -- analysis -------------------------------------------------------------------
    def analyze(self, redo_words=False):
        self.check()
        self.master_dur, _, has_a = engine.probe(self.master)
        if not has_a:
            raise RuntimeError("الماستر مفيهوش صوت")
        self.footage_dur, has_v, has_fa = engine.probe(self.footage)
        if not has_v:
            raise RuntimeError("ملف الفيديو مفيهوش صورة")
        self.log("الماستر %s — الفيديو %s" % (autoreel.fmt(self.master_dur, False), autoreel.fmt(self.footage_dur, False)))
        self.progress(0.01, "بزامن الفيديو مع الماستر…")
        if self.offset is None:
            if not has_fa:
                raise RuntimeError("الفيديو مفيهوش صوت — اكتب الفرق بإيدك (ثانية الماستر اللي الفيديو بيبدأ عندها)")
            self.offset, self.conf, self.rate = cine_audio.sync_offset(self.master, self.footage, self.log)
        self.progress(0.05, "بحدد توقيت الكلمات…")
        self.load_or_make_words(redo_words, self.stage(0.05, 0.75))
        self.progress(0.76, "بحسب الإيقاع…")
        self.beats = cine_audio.Beats(self.master, self.log)
        self.progress(0.78, "بتتبع الوش…")
        self.face = cine_face.track(self.footage, self.stage(0.78, 0.98), self.cancel, self.log)
        self.sections = self.suggest(self.n_reels)
        self.log("الريلز المقترحة: " + " | ".join("%s %s→%s" % (s["style"], autoreel.fmt(s["start"], False),
                                                             autoreel.fmt(s["end"], False)) for s in self.sections))
        self.progress(1.0, "التحليل خلص ✔")
        return self.sections

    def load_or_make_words(self, redo=False, progress=None):
        texts = autoreel.read_plain_lyrics(self.lyrics)
        if not texts:
            raise RuntimeError("ملف الكلمات فاضي")
        if not redo and os.path.isfile(self.words_path):
            lines = cine_align.load_words(self.words_path, texts)
            if lines:
                self.lines = lines
                self.log("استخدمت التوقيت المتسجّل: %s" % os.path.basename(self.words_path))
                return lines
            self.log("الكلمات اتغيرت عن words.json — هعمل توقيت جديد (القديم هيتنقل لـ _to_delete)")
        progress = progress or (lambda f, m="": None)
        rough = self.rough_lines(texts, lambda f, m="": progress(0.6 * f, m))
        if self.cancel.is_set():
            raise engine.Cancelled()
        vocal_path = self.stem or self.master
        vocal = autoreel.decode(vocal_path, cine_align.SR)
        try:
            cine_align.ensure_model(lambda f, m="": progress(0.6 + 0.1 * f, m), self.log, self.cancel)
            lines = cine_align.refine(rough, vocal, self.master_dur, lambda f, m="": progress(0.7 + 0.3 * f, m),
                                      self.cancel, self.log)
            source = "ctc-" + ("stem" if self.stem else "master")
        except engine.Cancelled:
            raise
        except Exception as ex:
            self.log("التوقيت الدقيق مش متاح (%s) — هستخدم التوقيت التقريبي وأقسّم بالحروف" % str(ex).splitlines()[0])
            lines = []
            for ln in rough:
                ws = ln.text.split()
                if ln.words and len(ln.words) == len(ws):
                    ends = ln.words[1:] + [ln.end]
                    times = list(zip(ln.words, ends))
                else:
                    times = cine_align.split_by_chars(ws, ln.start, ln.end)
                lines.append(dict(text=ln.text, start=times[0][0], end=times[-1][1],
                                  words=[dict(w=w, s=round(a, 3), e=round(b, 3), ok=False)
                                         for w, (a, b) in zip(ws, times)]))
            cine_align.fix_order(lines)
            source = "rough"
        if os.path.isfile(self.words_path):
            cine_render.to_trash(self.words_path)
        cine_align.save_words(self.words_path, self.song, lines, source)
        self.lines = cine_align.load_words(self.words_path)
        bad = sum(1 for l in self.lines for w in l["words"] if not w.get("ok", True))
        self.log("التوقيت: %d سطر — %d كلمة محتاجة مراجعة (ok=false) — اتحفظ: %s" % (
            len(self.lines), bad, os.path.basename(self.words_path)))
        return self.lines

    def rough_lines(self, texts, progress):
        """Line times good to ±2 s: from an .srt, else faster-whisper + autoreel.align."""
        if self.asr:
            return self.asr(texts)
        if self.srt:
            subs = parse_srt(self.srt)
            if len(subs) == len(texts):
                self.log("التوقيت التقريبي من: %s" % os.path.basename(self.srt))
                return [autoreel.Line(t, s, e) for t, (s, e, _) in zip(texts, subs)]
            self.log("الـ srt فيه %d سطر والكلمات %d — هسمع الأغنية بدلها" % (len(subs), len(texts)))
        src = self.stem or self.master
        cine_align.ensure_whisper(self.whisper, lambda f, m="": progress(0.25 * f, m), self.log, self.cancel)
        key = "%s-%s-%s" % (autoreel.file_key(src), self.whisper, "stem" if self.stem else "mix")
        cache = os.path.join(autoreel.CACHE_DIR, "asr-%s.json" % key)
        words = None
        if os.path.isfile(cache):
            try:
                import json
                with open(cache, encoding="utf-8") as fh:
                    words = json.load(fh)
            except (OSError, ValueError):
                words = None
        if words is None:
            self.log("بسمع %s (ممكن ياخد كام دقيقة)…" % ("الفوكال ستيم" if self.stem else "الأغنية"))
            words = autoreel.transcribe(autoreel.decode(src), self.whisper, "",
                                        lambda f, m="": progress(0.25 + 0.75 * f, m), self.cancel, None)
            import json
            os.makedirs(autoreel.CACHE_DIR, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump(words, fh, ensure_ascii=False)
        return autoreel.align(texts, words, self.master_dur)

    def reload_words(self):
        self.lines = cine_align.load_words(self.words_path)
        self.log("اتقرا: %s" % os.path.basename(self.words_path))
        return self.lines

    # -- reels ------------------------------------------------------------------------
    def usable(self):
        """Master-time range the footage covers."""
        return max(0.0, self.offset + 0.1), min(self.master_dur, self.offset + self.rate * self.footage_dur - 0.2)

    def strengths(self):
        vocal = autoreel.decode(self.stem or self.master, 8000)
        texts = [autoreel.loose(l["text"]) for l in self.lines]
        out = []
        for i, l in enumerate(self.lines):
            db = cine_audio.vocal_db(vocal, 8000, l["start"], l["end"])
            rep = sum(1 for t in texts if t == texts[i]) >= 2
            out.append(db + (2.5 if rep else 0.0) + (1.5 if l.get("key", -1) >= 0 else 0.0)
                       + min(1.5, (l["end"] - l["start"]) * 0.25))
        return out

    def span_from(self, i, lo, hi):
        """Reel that opens on line i: first word at LEAD s, ends after a whole line."""
        s = self.lines[i]["start"] - LEAD
        if s < lo:
            return None
        best = None
        for j in range(i, len(self.lines)):
            e = self.lines[j]["end"] + 0.9
            if e > hi:
                break
            if e - s > MAX_LEN:
                break
            if e - s >= MIN_LEN:
                gap_after = (self.lines[j + 1]["start"] - self.lines[j]["end"]) if j + 1 < len(self.lines) else 9
                score = -abs(e - s - TARGET) + (2.0 if gap_after > 0.8 else 0.0)
                if best is None or score > best[0]:
                    best = (score, j, e)
        if best is None:
            return None
        return s, best[2], best[1]

    def suggest(self, n):
        lo, hi = self.usable()
        st = self.strengths()
        cands = []
        for i in range(len(self.lines)):
            sp = self.span_from(i, lo, hi)
            if sp:
                cands.append((st[i], i) + sp)
        cands.sort(key=lambda c: -c[0])
        chosen = []
        for c in cands:
            _, i, s, e, j = c
            txt = autoreel.loose(self.lines[i]["text"])
            ok = True
            for d in chosen:
                ov = max(0.0, min(e, d[3]) - max(s, d[2]))
                if ov > 0.35 * min(e - s, d[3] - d[2]):
                    ok = False
                if autoreel.loose(self.lines[d[1]]["text"]) == txt and ov > 0:
                    ok = False
            if ok:
                chosen.append(c)
            if len(chosen) >= n:
                break
        if len(chosen) < n:  # short footage: allow overlap, different opening lines
            for c in cands:
                if len(chosen) >= n:
                    break
                if all(c[1] != d[1] for d in chosen):
                    chosen.append(c)
        chosen.sort(key=lambda c: c[2])
        out = []
        for k, (_, i, s, e, j) in enumerate(chosen):
            out.append(dict(name="ريل %d" % (k + 1), start=round(s, 2), end=round(e, 2),
                            style=STYLE_ORDER[k % 4], checked=True, first=self.lines[i]["text"]))
        return out

    def render(self, sections=None, preview=False, preview_from=None):
        secs = [s for s in (sections or self.sections) if s.get("checked", True)]
        if not secs:
            raise RuntimeError("اختار ريل واحد على الأقل")
        if self.offset is None:
            raise RuntimeError("حلّل الأول")
        jobs = []
        for s in secs:
            jobs.append((s, False))
            if self.ig and not preview:
                jobs.append((s, True))
        if preview:
            jobs = jobs[:1]
        results = []
        lo, hi = self.usable()
        os.makedirs(self.out_dir, exist_ok=True)
        t_all = time.time()
        for n, (s, ig) in enumerate(jobs):
            if self.cancel.is_set():
                raise engine.Cancelled()
            a, b = n / float(len(jobs)), (n + 1) / float(len(jobs))
            start, end = max(lo, float(s["start"])), min(hi, float(s["end"]))
            if end - start < 3:
                raise RuntimeError("%s: الوقت ده برا الفيديو (الفيديو بيغطي %s → %s من الماستر)" % (
                    s["name"], autoreel.fmt(lo, False), autoreel.fmt(hi, False)))
            label = "%s (%s)%s" % (s["name"], s["style"], " IG/FB" if ig else "")
            self.log("— %s  %s → %s" % (label, autoreel.fmt(start), autoreel.fmt(end)))
            reel = cine_render.Reel(self.footage, self.master, self.offset, start, end, self.lines, self.beats,
                                    self.face, s["style"], self.song, ig=ig, deglare=self.deglare,
                                    hook_text=(s.get("hook") or None), log=self.log,
                                    progress=lambda f, m="", a=a, b=b, label=label: self.progress(
                                        a + (b - a) * f, "%s — %d%%" % (label, f * 100)),
                                    cancel=self.cancel, seed=n + 1, rate=self.rate, green=self.green,
                                    pack=self.pack)
            base = "%s - %s - %s%s" % (engine.safe_name(self.song), s["name"], cine_edit.STYLE_NAMES[s["style"]].split(" (")[0].replace("—", "-"),
                                       " - IG-FB" if ig else "")
            base = engine.safe_name(base) if len(base) > 60 else re.sub(r'[\\/:*?"<>|]+', " ", base).strip()
            if preview:
                d = os.path.join(autoreel.CACHE_DIR, "preview")
                os.makedirs(d, exist_ok=True)
                out = os.path.join(d, "preview-%d.mp4" % int(time.time()))
                pf = 0.0 if preview_from is None else max(0.0, preview_from - start)
                reel.render(out, None, preview=True, preview_from=pf)
                results.append(dict(name=s["name"], mp4=out, jpg=None))
                continue
            tmp_mp4 = os.path.join(self.out_dir, ".rendering-%d.mp4" % os.getpid())
            tmp_jpg = os.path.join(self.out_dir, ".rendering-%d.jpg" % os.getpid())
            try:
                reel.render(tmp_mp4, tmp_jpg)
            except BaseException:
                for p in (tmp_mp4, tmp_jpg):  # our own unfinished temp file, not a reel
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                raise
            mp4 = cine_render.versioned(self.out_dir, base, ".mp4")
            os.replace(tmp_mp4, mp4)
            jpg = cine_render.versioned(self.out_dir, base, ".jpg")
            os.replace(tmp_jpg, jpg)
            results.append(dict(name=s["name"], style=s["style"], ig=ig, start=start, end=end, mp4=mp4, jpg=jpg))
            self.log("  ✔ %s" % os.path.basename(mp4))
        self.progress(1.0, "خلص ✔ — %d ملف في %.0f د" % (len(results), (time.time() - t_all) / 60))
        return results


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Cinematic reels")
    ap.add_argument("--footage", required=True)
    ap.add_argument("--master", required=True)
    ap.add_argument("--lyrics", required=True)
    ap.add_argument("--stem", default="")
    ap.add_argument("--srt", default="")
    ap.add_argument("--song", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--reels", type=int, default=4)
    ap.add_argument("--styles", default="")
    ap.add_argument("--offset", type=float, default=None)
    ap.add_argument("--ig", action="store_true")
    ap.add_argument("--deglare", action="store_true")
    ap.add_argument("--pack", default="", help="style pack name or id (default: سينمائي)")
    ap.add_argument("--green", type=float, default=cine_look.GREEN_DEFAULT,
                    help="green gain of the grade, 0.80-1.00 (v5 preset: 0.86)")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--only", type=int, default=0, help="render only reel N (1-based)")
    a = ap.parse_args(argv)
    last = [0.0]

    def prog(f, m=""):
        if f - last[0] >= 0.05 or f >= 1:
            last[0] = f
            print("[%3d%%] %s" % (f * 100, m), flush=True)
    p = Project(a.footage, a.master, a.lyrics, a.stem, a.srt, a.song, a.out, a.reels, a.ig, a.deglare,
                progress=prog, offset=a.offset, green=a.green,
                pack=textfx.get_pack(a.pack) if a.pack else None)
    secs = p.analyze()
    for i, s in enumerate(secs):
        if a.styles:
            s["style"] = a.styles[i % len(a.styles)]
        if a.only:
            s["checked"] = (i + 1 == a.only)
    res = p.render(secs, preview=a.preview)
    for r in res:
        print(r["mp4"])
    return p, res


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
