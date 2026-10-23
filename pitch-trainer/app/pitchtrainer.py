"""PitchTrainer — window around engine.py (live mic pitch, exercises, maqam, score)."""
import datetime
import json
import os
import pathlib
import queue
import sys
import threading
import time
import traceback
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # embedded Python skips the script folder
ROOT = os.path.dirname(HERE)


def crash_log(text):
    try:
        with open(os.path.join(ROOT, "error.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


try:
    import tkinter as tk
    from tkinter import messagebox, ttk
    import numpy as np
    import engine as E
except Exception:
    crash_log(traceback.format_exc())
    raise

try:
    import sounddevice as sd
    SD_ERROR = None
except Exception as ex:  # no PortAudio: the window still opens, Start explains why
    sd, SD_ERROR = None, str(ex)

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

BG, PANEL, GOLD, TEXT, MUTED = "#0a0a0a", "#141210", "#e0a458", "#f5efe6", "#9a8f80"
ZONE = {"green": "#4cc38a", "yellow": "#e8c547", "red": "#e5534b", None: "#6d655b"}
SETTINGS = os.path.join(ROOT, "settings.json")
MODES = [("exercise", "تمارين"), ("maqam", "مقامات"), ("free", "حر")]
DIRS = [("up", "طالع"), ("down", "نازل"), ("updown", "طالع ونازل")]
TEMPO = [("slow", "بطيء"), ("normal", "عادي"), ("fast", "سريع")]
SPANS = [("jins", "الجنس (5 درجات)"), ("full", "السلم كامل")]
REFS = [("piano", "بيانو"), ("tone", "تون")]


def open_path(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)
        else:
            webbrowser.open(pathlib.Path(path).as_uri())
    except Exception:
        pass


# ------------------------------------------------------------------ audio I/O
class MicSource:
    def __init__(self, device, q):
        self.q = q
        self.stream = sd.InputStream(samplerate=E.SR, channels=1, dtype="float32", blocksize=E.HOP,
                                     latency="low", device=device, callback=self.cb)

    def cb(self, indata, frames, t, status):
        self.q.put(indata[:, 0].copy())

    def start(self):
        self.stream.start()

    def stop(self):
        try:
            self.stream.stop()
            self.stream.close()
        except Exception:
            pass


class SimSource:
    """Plays a synthetic singer into the pipeline in real time (CI has no mic)."""

    def __init__(self, signal, q):
        self.sig, self.q, self.run = signal, q, False

    def start(self):
        self.run = True
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        t0, i, n = time.perf_counter(), 0, E.HOP
        while self.run and i < len(self.sig):
            self.q.put(self.sig[i:i + n].copy())
            i += n
            wait = t0 + i / E.SR - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
        while self.run:  # silence after the end
            self.q.put(np.zeros(n, np.float32))
            time.sleep(n / E.SR)

    def stop(self):
        self.run = False


class Player:
    def __init__(self, device=None, enabled=True):
        self.device, self.ok, self.count = device, enabled and sd is not None, 0

    def play(self, y):
        self.count += 1
        if not self.ok:
            return
        try:
            sd.play(y, E.SR, device=self.device)
        except Exception as ex:
            self.ok = False
            crash_log("reference playback: %s" % ex)

    def stop(self):
        if self.ok:
            try:
                sd.stop()
            except Exception:
                pass


class Engine(threading.Thread):
    """Worker: mic blocks -> WAV + YIN -> Session; triggers the reference tones."""

    def __init__(self, session, source, player, ref_kind, recorder):
        super().__init__(daemon=True)
        self.session, self.source, self.player = session, source, player
        self.ref_kind, self.rec = ref_kind, recorder
        self.q = source.q
        self.tracker = E.PitchTracker()
        self.lock = threading.Lock()
        self.running = True
        self.played = set()
        self.t_start = time.time()

    def run(self):
        try:
            self.source.start()
            while self.running:
                try:
                    blk = self.q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if self.rec:
                    self.rec.write(blk)
                fr = self.tracker.feed(blk)
                with self.lock:
                    self.session.process(fr)
                    now = self.session.now
                for st in self.session.steps:
                    if st.index not in self.played and now >= st.listen0 - 0.08 and now < st.listen1:
                        self.played.add(st.index)
                        self.player.play(E.render_step(st, kind=self.ref_kind))
                if self.session.done():
                    break
        except Exception:
            crash_log(traceback.format_exc())
        finally:
            self.source.stop()
            self.player.stop()
            if self.rec:
                self.rec.close()
            self.running = False


# ----------------------------------------------------------------------- app
class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("PitchTrainer — وسيم")
        root.configure(bg=BG)
        self.eng = None
        self.session = None
        self.yc = 50.0
        self.last_score = None
        self.auto_results = []
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground=GOLD, font=("Segoe UI", 15, "bold"))
        st.configure("Cap.TLabel", foreground=MUTED, font=("Segoe UI", 9), background=PANEL)
        st.configure("Big.TLabel", foreground=TEXT, font=("Segoe UI", 24, "bold"), background=PANEL)
        st.configure("Val.TLabel", foreground=TEXT, font=("Segoe UI", 11, "bold"), background=PANEL)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TCombobox", fieldbackground="#1a1714", foreground=TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", TEXT)])
        st.configure("TButton", background="#2a241d", foreground=TEXT, padding=6)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")], foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background=GOLD, foreground=BG, font=("Segoe UI", 11, "bold"), padding=8)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("TRadiobutton", background=BG, foreground=TEXT)
        st.map("TRadiobutton", background=[("active", BG)])
        st.configure("TCheckbutton", background=BG, foreground=TEXT)
        st.map("TCheckbutton", background=[("active", BG)])
        st.configure("TLabelframe", background=BG, bordercolor="#2b2620")
        st.configure("TLabelframe.Label", background=BG, foreground=GOLD, font=("Segoe UI", 10, "bold"))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=24)
        st.configure("Treeview.Heading", background="#2a241d", foreground=GOLD)

        cfg = self.load_cfg()
        self.v = {
            "mode": tk.StringVar(value=cfg.get("mode", "exercise")),
            "ex": tk.StringVar(value=cfg.get("ex", "scale5")),
            "maqam": tk.StringVar(value=cfg.get("maqam", "Bayati")),
            "span": tk.StringVar(value=cfg.get("span", "jins")),
            "start": tk.StringVar(value=cfg.get("start", "C3")),
            "bottom": tk.StringVar(value=cfg.get("bottom", "A2")),
            "top": tk.StringVar(value=cfg.get("top", "G4")),
            "dir": tk.StringVar(value=cfg.get("dir", "up")),
            "tempo": tk.StringVar(value=cfg.get("tempo", "normal")),
            "ref": tk.StringVar(value=cfg.get("ref", "piano")),
            "mic": tk.StringVar(value=cfg.get("mic", "")),
            "rec": tk.BooleanVar(value=cfg.get("rec", False)),
        }

        outer = ttk.Frame(root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="PitchTrainer  ·  تمرين الصوت اليومي", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        # ---- controls
        c = ttk.Frame(outer)
        c.grid(row=1, column=0, sticky="nsw", padx=(0, 14))
        r = 0
        mf = ttk.Frame(c)
        mf.grid(row=r, column=0, columnspan=2, sticky="w", pady=(0, 8))
        for k, t in MODES:
            ttk.Radiobutton(mf, text=t, value=k, variable=self.v["mode"], command=self.sync).pack(side="left", padx=(0, 12))
        r += 1
        self.ex_cb = self.combo(c, r, "التمرين", "ex", [(k, E.EXERCISES[k]["ar"]) for k in E.EXERCISES]); r += 1
        self.mq_cb = self.combo(c, r, "المقام", "maqam", [(k, "%s  %s" % (E.MAQAMAT[k]["ar"], k)) for k in E.MAQAMAT]); r += 1
        self.sp_cb = self.combo(c, r, "المدى", "span", SPANS); r += 1
        notes = E.note_choices(E.LOW_LIMIT, E.TOP_LIMIT)
        self.start_cb = self.combo(c, r, "أول نوتة / القرار", "start", [(n, n) for n in notes]); r += 1
        self.bot_cb = self.combo(c, r, "أوطى نوتة", "bottom", [(n, n) for n in notes]); r += 1
        self.top_cb = self.combo(c, r, "أعلى نوتة", "top", [(n, n) for n in notes]); r += 1
        ttk.Label(c, text="السقف G4 — مفيش تمرين بيعدّيه", style="Hint.TLabel").grid(row=r, column=0, columnspan=2, sticky="w"); r += 1
        self.dir_cb = self.combo(c, r, "الاتجاه", "dir", DIRS); r += 1
        self.tmp_cb = self.combo(c, r, "السرعة", "tempo", TEMPO); r += 1
        self.ref_cb = self.combo(c, r, "صوت المرجع", "ref", REFS); r += 1
        self.mics = self.list_mics()
        self.mic_cb = self.combo(c, r, "الميكروفون", "mic", self.mics, width=24); r += 1
        ttk.Checkbutton(c, text="سجّل الجلسة WAV", variable=self.v["rec"]).grid(row=r, column=0, columnspan=2, sticky="w", pady=(6, 0)); r += 1
        self.go = ttk.Button(c, text="ابدأ  ▶", style="Go.TButton", command=self.start)
        self.go.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(12, 4)); r += 1
        self.stop_btn = ttk.Button(c, text="وقّف  ■", command=self.stop, state="disabled")
        self.stop_btn.grid(row=r, column=0, columnspan=2, sticky="ew", pady=4); r += 1
        ttk.Button(c, text="التقدّم  (السجل)", command=self.show_progress).grid(row=r, column=0, columnspan=2, sticky="ew", pady=4); r += 1
        ttk.Button(c, text="افتح التسجيلات", command=lambda: (os.makedirs(E.REC_DIR, exist_ok=True), open_path(E.REC_DIR))).grid(
            row=r, column=0, columnspan=2, sticky="ew", pady=4); r += 1

        # ---- live view
        m = ttk.Frame(outer)
        m.grid(row=1, column=1, sticky="nsew")
        m.columnconfigure(0, weight=1)
        m.rowconfigure(2, weight=1)
        ro = ttk.Frame(m, style="Panel.TFrame", padding=(12, 8))
        ro.grid(row=0, column=0, sticky="ew")
        self.read = {}
        for i, (k, cap) in enumerate((("target", "الهدف"), ("sung", "صوتك"), ("diff", "الفرق (cents)"))):
            ro.columnconfigure(i, weight=1)
            ttk.Label(ro, text=cap, style="Cap.TLabel").grid(row=0, column=i, sticky="w")
            lb = ttk.Label(ro, text="—", style="Big.TLabel")
            lb.grid(row=1, column=i, sticky="w")
            self.read[k] = lb
        self.sub = ttk.Label(ro, text="", style="Cap.TLabel")
        self.sub.grid(row=2, column=0, columnspan=3, sticky="w")
        sf = ttk.Frame(m)
        sf.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self.phase_lb = ttk.Label(sf, text="اختار التمرين ودوس ابدأ", font=("Segoe UI", 12, "bold"), foreground=GOLD)
        self.phase_lb.pack(side="left")
        self.step_lb = ttk.Label(sf, text="", style="Hint.TLabel")
        self.step_lb.pack(side="left", padx=10)
        self.meter = tk.Canvas(sf, width=140, height=10, bg="#1a1714", highlightthickness=0)
        self.meter.pack(side="right")
        ttk.Label(sf, text="mic", style="Hint.TLabel").pack(side="right", padx=4)
        self.cv = tk.Canvas(m, bg=PANEL, highlightthickness=0, width=760, height=380)
        self.cv.grid(row=2, column=0, sticky="nsew")

        sc = ttk.LabelFrame(m, text="  نتيجة الجلسة  ", padding=10)
        sc.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self.score_lb = {}
        for i, (k, cap) in enumerate((("avg", "متوسط الانحراف"), ("intune", "مظبوط ±15c"), ("near", "قريب ±35c"),
                                      ("hi", "أعلى نوتة ثابتة"), ("lo", "أوطى نوتة ثابتة"))):
            sc.columnconfigure(i, weight=1)
            ttk.Label(sc, text=cap, style="Hint.TLabel").grid(row=0, column=i, sticky="w")
            lb = ttk.Label(sc, text="—", font=("Segoe UI", 13, "bold"))
            lb.grid(row=1, column=i, sticky="w")
            self.score_lb[k] = lb
        self.saved_lb = ttk.Label(sc, text="", style="Hint.TLabel")
        self.saved_lb.grid(row=2, column=0, columnspan=5, sticky="w", pady=(4, 0))

        self.sync()
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(40, self.tick)
        if autotest:
            root.after(600, self.auto_next)

    # -- widgets ---------------------------------------------------------------
    def combo(self, f, r, label, key, pairs, width=18):
        ttk.Label(f, text=label).grid(row=r, column=1, sticky="e", pady=3)
        cb = ttk.Combobox(f, state="readonly", width=width, values=[t for _, t in pairs])
        cb.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
        keys = [k for k, _ in pairs]
        cur = self.v[key].get()
        if cur in keys:
            cb.current(keys.index(cur))
        elif keys:
            cb.current(0)
            self.v[key].set(keys[0])

        def pick(_e=None):
            self.v[key].set(keys[cb.current()])
            self.sync()
        cb.bind("<<ComboboxSelected>>", pick)
        cb.keys = keys
        return cb

    def set_combo(self, cb, key, value):
        self.v[key].set(value)
        cb.current(cb.keys.index(value))

    def list_mics(self):
        out = [("", "الافتراضي")]
        if sd is None:
            return out
        try:
            apis = sd.query_hostapis()
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] > 0:
                    out.append((str(i), "%s (%s)" % (d["name"][:28], apis[d["hostapi"]]["name"].replace("Windows ", ""))))
        except Exception:
            pass
        return out

    def sync(self):
        mode = self.v["mode"].get()
        running = self.eng is not None
        for cb, on in ((self.ex_cb, mode == "exercise"), (self.mq_cb, mode == "maqam"), (self.sp_cb, mode == "maqam"),
                       (self.start_cb, mode != "free"), (self.bot_cb, mode != "free"), (self.top_cb, mode != "free"),
                       (self.dir_cb, mode != "free"), (self.tmp_cb, mode != "free"), (self.ref_cb, mode != "free"),
                       (self.mic_cb, True)):
            cb.configure(state="readonly" if on and not running else "disabled")
        self.go.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def load_cfg(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_cfg(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump({k: v.get() for k, v in self.v.items()}, f, ensure_ascii=False)
        except OSError:
            pass

    # -- session -----------------------------------------------------------------
    def plan(self):
        mode = self.v["mode"].get()
        if mode == "free":
            return [], {"mode": "free", "exercise": "free"}
        start, bottom = E.parse_note(self.v["start"].get()), E.parse_note(self.v["bottom"].get())
        top = min(E.parse_note(self.v["top"].get()), E.TOP_LIMIT)
        kind = "maqam" if mode == "maqam" else self.v["ex"].get()
        mq, span = self.v["maqam"].get(), self.v["span"].get()
        steps = E.build_plan(kind, start, bottom, top, self.v["dir"].get(), self.v["tempo"].get(),
                             mq if mode == "maqam" else None, span)
        info = {"mode": mode, "exercise": kind, "start": self.v["start"].get(), "top": E.note_name(top),
                "direction": self.v["dir"].get(), "tempo": self.v["tempo"].get()}
        if mode == "maqam":
            info.update(maqam=mq, span=span)
        if not steps:
            sp = E.span_of(kind, mq if mode == "maqam" else None, span)
            raise ValueError("أول نوتة عالية على التمرين ده.\nأعلى بداية ممكنة: %s" % E.note_name(int(top - sp + 1e-9)))
        return steps, info

    def start(self, sim=None):
        if self.eng:
            return
        try:
            steps, info = self.plan()
        except ValueError as ex:
            messagebox.showwarning("PitchTrainer", str(ex))
            return
        self.save_cfg()
        q = queue.Queue()
        if sim is not None:
            source, player = SimSource(sim(steps), q), Player(enabled=False)
        else:
            if sd is None:
                messagebox.showerror("PitchTrainer", "مكتبة الصوت مش شغالة:\n" + (SD_ERROR or ""))
                return
            dev = self.v["mic"].get()
            try:
                source = MicSource(int(dev) if dev else None, q)
            except Exception as ex:
                messagebox.showerror("PitchTrainer", "مش قادر أفتح الميكروفون:\n%s" % ex)
                return
            player = Player()
        label = info["exercise"] if info["mode"] != "maqam" else "maqam %s" % info["maqam"]
        self.session = E.Session(steps, info["mode"], info)
        rec = None
        self.wav = None
        if self.v["rec"].get():
            self.wav = E.recording_path("%s %s" % (label, info.get("start", "")))
            rec = E.WavRecorder(self.wav)
        self.eng = Engine(self.session, source, player, self.v["ref"].get(), rec)
        self.t0 = time.time()
        self.yc = float(steps[0].segs[0].m0) if steps else 50.0
        for k in self.score_lb:
            self.score_lb[k].configure(text="—", foreground=TEXT)
        self.saved_lb.configure(text="")
        self.eng.start()
        self.sync()

    def stop(self):
        if self.eng:
            self.eng.running = False

    def finish(self):
        eng, self.eng = self.eng, None
        eng.join(timeout=3)
        self.ref_plays = eng.player.count
        dur = time.time() - self.t0
        e = E.history_entry(self.session, dur, wav=self.wav)
        self.last_score = e
        if e["frames"] > 0:
            E.save_session(e)
            msg = "اتحفظت في السجل"
        else:
            msg = "مفيش غُنا متسجّل — الجلسة مش هتتحفظ"
        if self.wav:
            msg += "   |   WAV: " + os.path.basename(self.wav)
        self.saved_lb.configure(text=msg)
        self.show_score(e)
        self.phase_lb.configure(text="خلصت ✔" if self.session.done() else "اتوقفت")
        self.sync()
        if self.autotest:
            self.auto_results.append(dict(e, ref_plays=self.ref_plays, steps=len(self.session.steps),
                                          max_target=max(st.top() for st in self.session.steps)))
            self.root.after(800, self.auto_next)

    def show_score(self, e):
        f = lambda v, fmt: "—" if v is None else fmt % v
        avg = e.get("avg_cents")
        self.score_lb["avg"].configure(text=f(avg, "%.1f c"), foreground=ZONE[E.zone(avg)] if avg is not None else TEXT)
        self.score_lb["intune"].configure(text=f(e.get("in_tune_pct"), "%.0f%%"))
        self.score_lb["near"].configure(text=f(e.get("near_pct"), "%.0f%%"))
        self.score_lb["hi"].configure(text=e.get("highest", "—"))
        self.score_lb["lo"].configure(text=e.get("lowest", "—"))

    def quit(self):
        self.stop()
        if self.eng:
            self.eng.join(timeout=2)
        self.root.destroy()

    # -- drawing -------------------------------------------------------------------
    def tick(self):
        try:
            if self.eng is not None:
                if not self.eng.running and not self.eng.is_alive():
                    self.finish()
                else:
                    self.update_live()
        except Exception:
            crash_log(traceback.format_exc())
        self.root.after(40, self.tick)

    def update_live(self):
        s = self.session
        with self.eng.lock:
            now = s.now
            frames = [f for f in s.frames[-1300:] if f[0] >= now - 5.2]
            ph, st = s.phase_at(now)
            tgt, seg = s.target_at(now)
            db = s.level_db
        if s.mode == "free":
            self.phase_lb.configure(text="غنّي أي حاجة  ●")
            self.step_lb.configure(text="%.0fs" % now)
        else:
            self.phase_lb.configure(text={"lead": "استعد…", "listen": "اسمع  ♪", "gap": "جهّز نفسك…", "sing": "دورك  ●",
                                          "tail": "استريح…", "end": "خلصت ✔"}.get(ph, ""))
            self.step_lb.configure(text="%d / %d   ·   root %s" % (st.index + 1, len(s.steps), E.note_name(st.root)))
        # readouts
        last = frames[-1] if frames else None
        sung = last[1] if last and last[1] is not None else None
        if s.mode == "free":
            tgt = last[2] if last else None
        self.read["target"].configure(text=E.note_label(tgt) if tgt is not None else "—")
        sub = ""
        if seg is not None and s.mode == "maqam":
            sub = "%s  ·  %s from tonic %s" % (s.info["maqam"], seg.label, E.note_name(st.root))
        elif tgt is not None and s.mode != "free":
            sub = "%.1f Hz" % E.midi_to_hz(tgt)
        self.sub.configure(text=sub)
        self.read["sung"].configure(text=E.note_label(round(sung * 100) / 100) if sung is not None else "—")
        err = None
        if sung is not None and tgt is not None:
            err = (sung - tgt) * 100.0
        if err is not None and abs(err) < 600:
            self.read["diff"].configure(text="%+.0f" % err, foreground=ZONE[E.zone(err)])
        else:
            self.read["diff"].configure(text="—", foreground=TEXT)
        # level meter
        self.meter.delete("all")
        w = max(0.0, min(1.0, (db + 60) / 60.0)) * 140
        self.meter.create_rectangle(0, 0, w, 10, fill=GOLD if db > -50 else "#5a4a35", width=0)
        self.draw_graph(now, frames, tgt)

    def draw_graph(self, now, frames, tgt):
        cv = self.cv
        cv.delete("all")
        W, H = max(cv.winfo_width(), 200), max(cv.winfo_height(), 150)
        L = 52
        t_a, t_b = now - 5.0, now + 1.5
        if tgt is not None:
            want = tgt
        else:
            v = [f[1] for f in frames[-40:] if f[1] is not None]
            want = float(np.median(v)) if v else self.yc
        self.yc += (want - self.yc) * 0.12
        half = 7.5
        lo, hi = self.yc - half, self.yc + half
        X = lambda t: L + (t - t_a) / (t_b - t_a) * (W - L - 8)
        Y = lambda m: H - 10 - (m - lo) / (hi - lo) * (H - 20)
        for n in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
            y = Y(n)
            cv.create_line(L, y, W, y, fill="#2b2620" if n % 12 == 0 else "#1d1a16")
        if lo < E.TOP_LIMIT + 0.5 < hi + 1:
            y = Y(E.TOP_LIMIT + 0.5)
            cv.create_line(L, y, W, y, fill="#7a2e2a", dash=(4, 4))
            cv.create_text(W - 10, y - 8, text="G4 top", anchor="e", fill="#c0504a", font=("Segoe UI", 8))
        xn = X(now)
        cv.create_line(xn, 6, xn, H - 6, fill="#3a3128")
        # targets
        s = self.session
        for st in s.steps:
            for base, sing in ((st.listen0, False), (st.sing0, True)):
                if base + st.dur < t_a or base > t_b:
                    continue
                for g in st.segs:
                    ts = np.linspace(base + g.t0, base + g.t1, 24 if g.glide else 2)
                    pts = []
                    for t in ts:
                        pts += [X(t), Y(g.at(t - base))]
                    if sing:
                        band = (E.GREEN / 100.0) * (H - 20) / (hi - lo)
                        if not g.glide:
                            cv.create_rectangle(pts[0], pts[1] - band, pts[-2], pts[1] + band, fill="#1f2a22", width=0)
                        cv.create_line(*pts, fill=GOLD, width=3, capstyle="round")
                    else:
                        cv.create_line(*pts, fill="#6b5536", width=2, dash=(3, 3))
        # sung pitch, grouped by colour
        run, col = [], None
        for f in frames[::2]:
            t, m, tg, err = f[0], f[1], f[2], f[3]
            if err is None and tg is not None and m is not None and f[4] in ("sing", "free"):
                err = (m - tg) * 100.0   # attack frames: coloured, just not scored
            if m is None or not (lo - 1 < m < hi + 1):
                if len(run) >= 4:
                    cv.create_line(*run, fill=ZONE[col], width=3)
                run, col = [], None
                continue
            c = E.zone(err) if err is not None and abs(err) <= 600 else None
            if c != col and run:
                run += [X(t), Y(m)]
                if len(run) >= 4:
                    cv.create_line(*run, fill=ZONE[col], width=3)
                run = []
            col = c
            run += [X(t), Y(m)]
        if len(run) >= 4:
            cv.create_line(*run, fill=ZONE[col], width=3)
        elif len(run) == 2:
            cv.create_oval(run[0] - 2, run[1] - 2, run[0] + 2, run[1] + 2, fill=ZONE[col], width=0)
        cv.create_rectangle(0, 0, L, H, fill=PANEL, width=0)   # note-name gutter on top
        for n in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
            cv.create_text(L - 6, Y(n), text=E.note_name(n), anchor="e", font=("Segoe UI", 8),
                           fill=MUTED if n % 12 == 0 else "#5d544a")

    # -- progress ------------------------------------------------------------------
    def show_progress(self):
        rows = E.daily(E.load_history())
        top = tk.Toplevel(self.root)
        top.title("PitchTrainer — التقدّم")
        top.configure(bg=BG)
        top.geometry("860x700")
        f = ttk.Frame(top, padding=12)
        f.pack(fill="both", expand=True)
        if not rows:
            ttk.Label(f, text="لسه مفيش جلسات متسجلة. خلّص تمرين وارجع هنا.").pack(pady=40)
            return top
        last = rows[-30:]
        for title, key, fmt, lo, color in (("% in tune (±15c) — كل يوم", "in_tune_pct", "%.0f%%", 0.0, GOLD),
                                           ("Average error (cents) — كل يوم، الأقل أحسن", "avg_cents", "%.1f", 0.0, "#8fb3d9")):
            ttk.Label(f, text=title, foreground=GOLD, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            cv = tk.Canvas(f, bg=PANEL, height=190, highlightthickness=0)
            cv.pack(fill="x", pady=(2, 12))
            cv.update_idletasks()
            self.bar_line(cv, last, key, fmt, lo, color, line=(key == "in_tune_pct"))
        tv = ttk.Treeview(f, columns=("d", "s", "m", "c", "i", "h", "l"), show="headings", height=8)
        for k, t, w in (("d", "Date", 110), ("s", "Sessions", 80), ("m", "Minutes", 80), ("c", "Avg cents", 90),
                        ("i", "In tune", 80), ("h", "Highest stable", 120), ("l", "Lowest stable", 120)):
            tv.heading(k, text=t)
            tv.column(k, width=w, anchor="center")
        for r in reversed(rows):
            tv.insert("", "end", values=(r["date"], r["sessions"], "%.0f" % r["minutes"],
                                         "—" if r["avg_cents"] is None else "%.1f" % r["avg_cents"],
                                         "—" if r["in_tune_pct"] is None else "%.0f%%" % r["in_tune_pct"],
                                         E.note_label_rounded(r["highest_midi"]), E.note_label_rounded(r["lowest_midi"])))
        tv.pack(fill="both", expand=True)
        self.progress_win = top
        return top

    @staticmethod
    def bar_line(cv, rows, key, fmt, lo, color, line):
        W, H = max(cv.winfo_width(), 600), 190
        L, B, T = 44, 26, 26
        vals = [r[key] for r in rows]
        hiv = max([v for v in vals if v is not None] + [1.0])
        hiv = 100.0 if line else max(20.0, float(np.ceil(hiv * 1.15 / 10.0) * 10))
        n = len(rows)
        step = (W - L - 10) / max(n, 1)
        Y = lambda v: H - B - (v - lo) / (hiv - lo) * (H - B - T)
        for g in np.linspace(lo, hiv, 5):
            cv.create_line(L, Y(g), W - 6, Y(g), fill="#1d1a16")
            cv.create_text(L - 6, Y(g), text=fmt % g, anchor="e", fill=MUTED, font=("Segoe UI", 8))
        pts = []
        for i, (r, v) in enumerate(zip(rows, vals)):
            xc = L + step * (i + 0.5)
            if i % max(1, n // 10) == 0 or i == n - 1:
                cv.create_text(xc, H - B + 12, text=r["date"][5:], fill=MUTED, font=("Segoe UI", 8))
            if v is None:
                continue
            if line:
                pts.append((xc, Y(v)))
            else:
                bw = min(28, step * 0.6)
                cv.create_rectangle(xc - bw / 2, Y(v), xc + bw / 2, Y(lo), fill=color, width=0)
        if line and pts:
            if len(pts) > 1:
                cv.create_line(*[c for p in pts for c in p], fill=color, width=2)
            for x, y in pts:
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline=PANEL, width=2)
        if vals and vals[-1] is not None:   # label the latest value only
            xc = L + step * (n - 0.5)
            cv.create_text(xc, Y(vals[-1]) - 12, text=fmt % vals[-1], fill=TEXT, font=("Segoe UI", 9, "bold"))

    # -- CI autotest -----------------------------------------------------------------
    def auto_next(self):
        """Windows CI: two short sessions sung by the synthetic singer, then the
        progress window, then a JSON result for the workflow."""
        plans = [
            {"mode": "exercise", "ex": "scale5", "start": "B3", "top": "G4", "tempo": "fast", "offset": 6.0, "rec": True},
            {"mode": "maqam", "maqam": "Bayati", "span": "jins", "start": "D3", "top": "A3", "tempo": "fast", "offset": -4.0, "rec": False},
        ]
        i = len(self.auto_results)
        if i >= len(plans):
            win = self.show_progress()
            win.geometry("+%d+%d" % (self.root.winfo_rootx() + 380, self.root.winfo_rooty() + 40))
            self.root.after(1500, self.auto_write)
            return
        p = plans[i]
        self.v["mode"].set(p["mode"])
        if p["mode"] == "exercise":
            self.set_combo(self.ex_cb, "ex", p["ex"])
        else:
            self.set_combo(self.mq_cb, "maqam", p["maqam"])
            self.set_combo(self.sp_cb, "span", p["span"])
        self.set_combo(self.start_cb, "start", p["start"])
        self.set_combo(self.top_cb, "top", p["top"])
        self.set_combo(self.dir_cb, "dir", "up")
        self.set_combo(self.tmp_cb, "tempo", p["tempo"])
        self.v["rec"].set(p["rec"])
        off = p["offset"]

        def sim(steps):
            end = steps[-1].sing1 + 1.0
            tr = E.singer_track(steps, end, offset_cents=off)
            return E.synth_voice(tr, vibrato_cents=6.0, seed=i)
        self.start(sim=sim)

    def auto_write(self):
        out = {"sessions": self.auto_results, "history_days": E.daily(E.load_history()),
               "history_file": E.HISTORY_FILE}
        with open(self.autotest, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1)


def main():
    autotest = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = sys.argv[2]
    root = tk.Tk()
    root.minsize(1100, 720)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
