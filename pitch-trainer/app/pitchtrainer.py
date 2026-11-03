"""PitchTrainer — window around engine/session/analysis (live mic pitch, exercises,
maqam, drone, call & response, song, onset, stability, leaps, ajnas)."""
import datetime
import json
import os
import pathlib
import queue
import subprocess
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
    from tkinter import filedialog, messagebox, ttk
    import numpy as np
    import analysis as A
    import engine as E
    import oud
    import report
    import session as S
    import song as G
    import runs as RU
    import breath as BR
    import warmup as WU
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
SERIES = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]
SETTINGS = os.path.join(ROOT, "settings.json")
DIRS = [("up", "طالع"), ("down", "نازل"), ("updown", "طالع ونازل")]
TEMPO = [("slow", "بطيء"), ("normal", "عادي"), ("fast", "سريع")]
SPANS = [("jins", "الجنس (5 درجات)"), ("full", "السلم كامل")]
REFS = [("oud", "عود"), ("piano", "بيانو"), ("tone", "تون")]
PLAYS = [("stem", "الـ stem"), ("mix", "الميكس كامل")]
LISTEN_MODES = ("basic", "maqam", "callresp", "onset", "stability", "leaps", "ajnas")
SHOW = {   # which option rows each mode uses
    "ex": ("basic",), "maqam": ("maqam", "drone", "callresp", "onset"), "jins": ("ajnas",),
    "span": ("maqam", "drone"), "level": ("callresp",),
    "start": ("basic", "maqam", "drone", "callresp", "onset", "stability", "leaps", "ajnas"),
    "bottom": ("basic", "maqam", "callresp", "onset", "stability", "leaps"),
    "top": ("basic", "maqam", "drone", "callresp", "onset", "stability", "leaps", "ajnas"),
    "dir": ("basic", "maqam", "stability"),
    "tempo": ("basic", "maqam", "drone", "callresp", "onset", "leaps", "ajnas"),
    "ref": LISTEN_MODES, "fifth": ("drone",), "song": ("song",),
}
PLAN_ERR = {
    "drone": "القرار عالي على السلم ده — أعلى قرار ممكن: %s",
    "ajnas": "القرار عالي على الجنس ده — أعلى قرار ممكن: %s",
    "start": "أول نوتة عالية على التمرين ده — أعلى بداية ممكنة: %s",
    "range": "المدى ضيق — وسّع أوطى/أعلى نوتة%s",
    "section": "الجزء ده قصير أو مفيهوش غُنا%s",
}


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
        self.latency = float(self.stream.latency)

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
    latency = 0.0

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


class Mixer:
    """One output stream: reference phrases, song sections and the drone loop mixed."""

    def __init__(self, device=None, enabled=True):
        self.clips, self.drone, self.dpos = [], None, 0
        self.lock = threading.Lock()
        self.count = 0
        self.stream = None
        self.latency = 0.0
        if enabled and sd is not None:
            try:
                self.stream = sd.OutputStream(samplerate=E.SR, channels=2, dtype="float32", blocksize=E.HOP,
                                              latency="low", device=device, callback=self.cb)
                self.stream.start()
                self.latency = float(self.stream.latency)
            except Exception as ex:
                crash_log("output: %s" % ex)
                self.stream = None

    def cb(self, out, frames, t, status):
        buf = np.zeros((frames, 2), np.float32)
        with self.lock:
            if self.drone is not None:
                L = len(self.drone)
                idx = (self.dpos + np.arange(frames)) % L
                buf += self.drone[idx][:, None]
                self.dpos = (self.dpos + frames) % L
            keep = []
            for c in self.clips:
                y, p = c
                k = min(frames, len(y) - p)
                buf[:k] += y[p:p + k]
                c[1] = p + k
                if c[1] < len(y):
                    keep.append(c)
            self.clips = keep
        out[:] = np.clip(buf, -1, 1)

    def play(self, y):
        self.count += 1
        if self.stream is None:
            return
        y = np.asarray(y, np.float32)
        if y.ndim == 1:
            y = np.repeat(y[:, None], 2, 1)
        with self.lock:
            self.clips.append([y, 0])

    def set_drone(self, loop):
        with self.lock:
            self.drone, self.dpos = loop, 0

    def stop(self):
        with self.lock:
            self.clips, self.drone = [], None
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception:
                pass


class Engine(threading.Thread):
    """Worker: mic blocks -> WAV + YIN -> Session; triggers the reference audio."""

    def __init__(self, session, source, mixer, ref_kind, recorder):
        super().__init__(daemon=True)
        self.session, self.source, self.mixer = session, source, mixer
        self.ref_kind, self.rec = ref_kind, recorder
        self.q = source.q
        self.tracker = E.PitchTracker()
        self.lock = threading.Lock()
        self.running = True
        self.next_step = 0

    def triggers(self, now):
        s, plan = self.session, self.session.plan
        if plan.mode == "calib" and self.next_step == 0:
            self.next_step = 1
            self.mixer.play(oud.clicks(plan.clicks, plan.pitch))
            return
        while self.next_step < len(s.steps):
            st = s.steps[self.next_step]
            at = st.listen0 if st.listen else st.sing0
            if now < at - 0.03:
                break
            if getattr(st, "clip", None) is not None:      # runs: the recording (or the drill on oud)
                self.mixer.play(st.clip)
            elif st.listen:
                self.mixer.play(oud.render_step(st, kind=self.ref_kind))
            elif plan.mode == "song":
                a = plan.song.audio(plan.play)
                i0, i1 = int(st.song_t0 * E.SR), int((st.song_t0 + st.dur) * E.SR)
                self.mixer.play(a[i0:i1] * 0.9)
            self.next_step += 1

    def run(self):
        try:
            if self.session.plan.drone:
                d = self.session.plan.drone
                self.mixer.set_drone(oud.drone_loop(d["tonic"], d["fifth"]))
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
                    if hasattr(self.session, "feed_raw"):
                        self.session.feed_raw(blk)
                    self.session.process(fr)
                    now = self.session.now
                self.triggers(now)
                if self.session.done():
                    break
        except Exception:
            crash_log(traceback.format_exc())
        finally:
            self.source.stop()
            self.mixer.stop()
            if self.rec:
                self.rec.close()
            with self.lock:
                self.session.finish()
            self.running = False


# ----------------------------------------------------------------------- app
class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("PitchTrainer — وسيم")
        root.configure(bg=BG)
        self.eng = None
        self.session = None
        self.song = None
        self.yc = 50.0
        self.auto_results = []
        self.calib = None
        self.ended = False
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground=MUTED, font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground=GOLD, font=("Segoe UI", 14, "bold"))
        st.configure("Cap.TLabel", foreground=MUTED, font=("Segoe UI", 9), background=PANEL)
        st.configure("Big.TLabel", foreground=TEXT, font=("Segoe UI", 22, "bold"), background=PANEL)
        st.configure("Panel.TFrame", background=PANEL)
        st.configure("TCombobox", fieldbackground="#1a1714", foreground=TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", TEXT)])
        st.configure("TButton", background="#2a241d", foreground=TEXT, padding=4)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")], foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background=GOLD, foreground=BG, font=("Segoe UI", 11, "bold"), padding=6)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        for w in ("TRadiobutton", "TCheckbutton"):
            st.configure(w, background=BG, foreground=TEXT)
            st.map(w, background=[("active", BG)])
        st.configure("TLabelframe", background=BG, bordercolor="#2b2620")
        st.configure("TLabelframe.Label", background=BG, foreground=GOLD, font=("Segoe UI", 10, "bold"))
        st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=22)
        st.configure("Treeview.Heading", background="#2a241d", foreground=GOLD)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background="#2a241d", foreground=TEXT, padding=(16, 5), font=("Segoe UI", 11, "bold"))
        st.map("TNotebook.Tab", background=[("selected", GOLD)], foreground=[("selected", BG)])
        st.configure("Horizontal.TScale", background=BG, troughcolor="#1a1714")

        cfg = self.cfg = self.load_cfg()
        V = lambda k, d: tk.StringVar(value=cfg.get(k, d))
        self.v = {"mode": V("mode", "drone"), "ex": V("ex", "scale5"), "maqam": V("maqam", "Bayati"),
                  "jins": V("jins", "Bayati"), "span": V("span", "jins"), "level": V("level", "1"),
                  "start": V("start", "D3"), "bottom": V("bottom", "G2"), "top": V("top", "G4"),
                  "dir": V("dir", "up"), "tempo": V("tempo", "normal"), "ref": V("ref", "oud"),
                  "mic": V("mic", ""), "play": V("play", "stem"), "from": V("from", ""), "to": V("to", ""),
                  "stem": V("stem", ""), "mixf": V("mixf", "")}
        self.b = {k: tk.BooleanVar(value=cfg.get(k, d)) for k, d in (("rec", False), ("blind", False),
                                                                         ("fifth", True), ("loop", False))}
        if self.v["mode"].get() == "exercise":
            self.v["mode"].set("basic")

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  التمارين  ")
        outer = ttk.Frame(tab, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="PitchTrainer  ·  تمرين الصوت اليومي", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))

        # ---- controls
        c = ttk.Frame(outer)
        c.grid(row=1, column=0, sticky="nsw", padx=(0, 10))
        self.rows = {}
        r = 0
        self.mode_cb = self.combo(c, r, "النوع", "mode", E.MODES, width=20); r += 1
        self.ex_cb = self.combo(c, r, "التمرين", "ex", [(k, E.EXERCISES[k]["ar"]) for k in E.EXERCISES], row="ex"); r += 1
        self.mq_cb = self.combo(c, r, "المقام", "maqam", [(k, "%s  %s" % (E.MAQAMAT[k]["ar"], k)) for k in E.MAQAMAT], row="maqam"); r += 1
        self.jn_cb = self.combo(c, r, "الجنس", "jins", [(k, "%s  %s" % (E.AJNAS[k]["ar"], k)) for k in E.AJNAS], row="jins"); r += 1
        self.sp_cb = self.combo(c, r, "المدى", "span", SPANS, row="span"); r += 1
        self.lv_cb = self.combo(c, r, "المستوى", "level", [(str(k), t) for k, t in E.LEVELS], row="level"); r += 1
        notes = E.note_choices(E.LOW_LIMIT, E.TOP_LIMIT)
        self.start_cb = self.combo(c, r, "القرار / أول نوتة", "start", [(n, n) for n in notes], row="start"); r += 1
        self.bot_cb = self.combo(c, r, "أوطى نوتة", "bottom", [(n, n) for n in notes], row="bottom"); r += 1
        self.top_cb = self.combo(c, r, "أعلى نوتة", "top", [(n, n) for n in notes], row="top"); r += 1
        self.dir_cb = self.combo(c, r, "الاتجاه", "dir", DIRS, row="dir"); r += 1
        self.tmp_cb = self.combo(c, r, "السرعة", "tempo", TEMPO, row="tempo"); r += 1
        self.ref_cb = self.combo(c, r, "صوت المرجع", "ref", REFS, row="ref"); r += 1
        fw = ttk.Checkbutton(c, text="خامسة مع الدرون", variable=self.b["fifth"])
        fw.grid(row=r, column=0, columnspan=2, sticky="e")
        self.rows["fifth"] = [fw]; r += 1
        # song rows
        sf = ttk.Frame(c)
        sf.grid(row=r, column=0, columnspan=2, sticky="ew")
        self.rows["song"] = [sf]
        r += 1
        sf.columnconfigure(0, weight=1)
        self.stem_lb = ttk.Label(sf, text="", style="Hint.TLabel", width=30)
        self.stem_lb.grid(row=0, column=0, sticky="w")
        ttk.Button(sf, text="الـ vocal stem…", command=lambda: self.pick_file("stem")).grid(row=0, column=1, sticky="e", pady=2)
        self.mix_lb = ttk.Label(sf, text="", style="Hint.TLabel", width=30)
        self.mix_lb.grid(row=1, column=0, sticky="w")
        ttk.Button(sf, text="الميكس (اختياري)…", command=lambda: self.pick_file("mixf")).grid(row=1, column=1, sticky="e", pady=2)
        self.play_cb = self.combo(sf, 2, "يشغّل", "play", PLAYS, width=14)
        tf = ttk.Frame(sf)
        tf.grid(row=3, column=0, columnspan=2, sticky="e", pady=2)
        ttk.Checkbutton(tf, text="لوب", variable=self.b["loop"]).pack(side="left", padx=(0, 8))
        ttk.Entry(tf, textvariable=self.v["to"], width=7).pack(side="left")
        ttk.Label(tf, text="لحد").pack(side="left", padx=4)
        ttk.Entry(tf, textvariable=self.v["from"], width=7).pack(side="left")
        ttk.Label(tf, text="من").pack(side="left", padx=4)
        self.mics = self.list_mics()
        self.mic_cb = self.combo(c, r, "الميكروفون", "mic", self.mics, width=20); r += 1
        ck = ttk.Frame(c)
        ck.grid(row=r, column=0, columnspan=2, sticky="e", pady=(4, 0)); r += 1
        ttk.Checkbutton(ck, text="سجّل WAV", variable=self.b["rec"]).pack(side="right")
        ttk.Checkbutton(ck, text="أعمى (من غير شاشة)", variable=self.b["blind"]).pack(side="right", padx=(0, 10))
        self.go = ttk.Button(c, text="ابدأ  ▶", style="Go.TButton", command=self.start)
        self.go.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(8, 3)); r += 1
        bf = ttk.Frame(c)
        bf.grid(row=r, column=0, columnspan=2, sticky="ew"); r += 1
        bf.columnconfigure(0, weight=1)
        bf.columnconfigure(1, weight=1)
        self.stop_btn = ttk.Button(bf, text="وقّف  ■", command=self.stop, state="disabled")
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(2, 0), pady=2)
        self.cal_btn = ttk.Button(bf, text="معايرة التأخير", command=self.calibrate)
        self.cal_btn.grid(row=0, column=0, sticky="ew", padx=(0, 2), pady=2)
        ttk.Button(bf, text="التقدّم", command=self.show_progress).grid(row=1, column=1, sticky="ew", padx=(2, 0), pady=2)
        ttk.Button(bf, text="تقرير اليوم", command=self.day_report).grid(row=1, column=0, sticky="ew", padx=(0, 2), pady=2)
        ttk.Button(bf, text="افتح التسجيلات", command=lambda: (os.makedirs(E.REC_DIR, exist_ok=True), open_path(E.REC_DIR))).grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=2)
        lf = ttk.Frame(c)
        lf.grid(row=r, column=0, columnspan=2, sticky="e"); r += 1
        self.lat_lb = ttk.Label(lf, text="", style="Hint.TLabel")
        self.lat_lb.pack(side="right")
        self.show_lat()

        # ---- live view
        m = ttk.Frame(outer)
        m.grid(row=1, column=1, sticky="nsew")
        m.columnconfigure(0, weight=1)
        m.rowconfigure(2, weight=1)
        ro = ttk.Frame(m, style="Panel.TFrame", padding=(10, 6))
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
        sf2 = ttk.Frame(m)
        sf2.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        self.phase_lb = ttk.Label(sf2, text="اختار التمرين ودوس ابدأ", font=("Segoe UI", 12, "bold"), foreground=GOLD)
        self.phase_lb.pack(side="left")
        self.step_lb = ttk.Label(sf2, text="", style="Hint.TLabel")
        self.step_lb.pack(side="left", padx=10)
        self.meter = tk.Canvas(sf2, width=120, height=10, bg="#1a1714", highlightthickness=0)
        self.meter.pack(side="right")
        ttk.Label(sf2, text="mic", style="Hint.TLabel").pack(side="right", padx=4)
        self.cv = tk.Canvas(m, bg=PANEL, highlightthickness=0, width=560, height=230)
        self.cv.grid(row=2, column=0, sticky="nsew")

        sc = ttk.LabelFrame(m, text="  نتيجة الجلسة  ", padding=(8, 4))
        sc.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        self.slots = []
        for i in range(6):
            sc.columnconfigure(i, weight=1)
            cap = ttk.Label(sc, text="", style="Hint.TLabel")
            cap.grid(row=0, column=i, sticky="w")
            val = ttk.Label(sc, text="", font=("Segoe UI", 12, "bold"))
            val.grid(row=1, column=i, sticky="w")
            self.slots.append((cap, val))
        self.saved_lb = ttk.Label(sc, text="", style="Hint.TLabel")
        self.saved_lb.grid(row=2, column=0, columnspan=6, sticky="w")
        self.tree = ttk.Treeview(m, columns=("t", "n", "e", "x"), show="headings", height=5)
        for k, t, w in (("t", "الوقت", 90), ("n", "النوتة", 110), ("e", "الانحراف", 90), ("x", "ملاحظة", 260)):
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="center")
        self.tree.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        self.tree.bind("<Double-1>", self.tree_loop)
        self.tree_times = {}
        self.clear_score()

        self.sync()
        if self.v["stem"].get() and os.path.isfile(self.v["stem"].get()):
            self.stem_lb.configure(text=os.path.basename(self.v["stem"].get()))
        self.runs_tab = RunsTab(self)
        self.breath_tab = BreathTab(self)
        self.warm_tab = WarmupTab(self)
        self.nb.select(0 if autotest else min(int(self.cfg.get("tab", 0)), 3))
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.after(40, self.tick)
        if autotest:
            self.shots = autotest["shots"]
            root.after(800, self.auto_next)

    # -- widgets ---------------------------------------------------------------
    def combo(self, f, r, label, key, pairs, width=18, row=None):
        lb = ttk.Label(f, text=label)
        lb.grid(row=r, column=1, sticky="e", pady=2)
        cb = ttk.Combobox(f, state="readonly", width=width, values=[t for _, t in pairs])
        cb.grid(row=r, column=0, sticky="w", pady=2, padx=(0, 6))
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
        if row:
            self.rows[row] = [lb, cb]
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
                    out.append((str(i), "%s (%s)" % (d["name"][:24], apis[d["hostapi"]]["name"].replace("Windows ", ""))))
        except Exception:
            pass
        return out

    def sync(self):
        mode = self.v["mode"].get()
        running = self.eng is not None
        for key, widgets in self.rows.items():
            on = mode in SHOW.get(key, ())
            for w in widgets:
                if on:
                    w.grid()
                else:
                    w.grid_remove()
        for cb in (self.mode_cb, self.ex_cb, self.mq_cb, self.jn_cb, self.sp_cb, self.lv_cb, self.start_cb, self.bot_cb,
                   self.top_cb, self.dir_cb, self.tmp_cb, self.ref_cb, self.mic_cb, self.play_cb):
            cb.configure(state="disabled" if running else "readonly")
        self.go.configure(state="disabled" if running else "normal")
        self.cal_btn.configure(state="disabled" if running else "normal")
        self.stop_btn.configure(state="normal" if running else "disabled")

    def load_cfg(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_cfg(self):
        d = dict(self.cfg)               # keeps lat_ms, drill speeds, run settings
        d.update(self.extra_cfg())
        d.update({k: v.get() for k, v in self.v.items()})
        d.update({k: v.get() for k, v in self.b.items()})
        self.cfg = d
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except OSError:
            pass

    def extra_cfg(self):
        """The other tabs' fields, so the window opens as it was left."""
        d = {}
        rt, bt, wt = getattr(self, "runs_tab", None), getattr(self, "breath_tab", None), getattr(self, "warm_tab", None)
        if rt:
            d.update(drill_maq=rt.dv["maq"].get(), drill_kind=rt.dv["kind"].get(), drill_start=rt.dv["start"].get(),
                     run_speed=rt.pv["speed"].get(), run_semis=rt.pv["semis"].get(), run_rate=rt.rate.get(),
                     run_loop=rt.loop_v.get(), run_all=rt.all_v.get())
        if bt:
            d.update(hold_note=bt.note_v.get(), sss_pattern=bt.pat_v.get(), sss_rounds=bt.rounds_v.get())
        if wt:
            d.update(warm_start=wt.wv["start"].get(), warm_top=wt.wv["top"].get(), warm_len=wt.wv["len"].get(), warm_ref=wt.wv["ref"].get())
        if hasattr(self, "nb"):
            try:
                d["tab"] = self.nb.index(self.nb.select())
            except tk.TclError:
                pass
        return d

    def show_lat(self):
        v = self.cfg.get("lat_ms")
        self.lat_lb.configure(text=("latency %d ms (calibrated)" % v) if v is not None else "latency: auto")

    def pick_file(self, key):
        p = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.flac *.m4a *.ogg *.aif *.aiff"), ("All", "*.*")])
        if p:
            self.v[key].set(os.path.normpath(p))
            (self.stem_lb if key == "stem" else self.mix_lb).configure(text=os.path.basename(p))
            if key == "stem" or (self.song and self.song.stem_path == self.v["stem"].get()):
                self.song = None

    # -- plans -------------------------------------------------------------------
    def opts(self):
        v = {k: x.get() for k, x in self.v.items()}
        n = E.parse_note
        top = min(n(v["top"]), E.TOP_LIMIT)
        return v, n(v["start"]), n(v["bottom"]), top

    def make_plan(self):
        v, start, bottom, top = self.opts()
        mode = v["mode"]
        if mode == "free":
            return E.Plan("free", [], {})
        if mode == "basic":
            steps = E.build_plan(v["ex"], start, bottom, top, v["dir"], v["tempo"])
            if not steps:
                raise E.PlanError("start|%s" % E.note_name(E.max_start(v["ex"], top)))
            return E.Plan("basic", steps, {"exercise": v["ex"], "start": v["start"], "top": E.note_name(top)})
        if mode == "maqam":
            steps = E.build_plan("maqam", start, bottom, top, v["dir"], v["tempo"], v["maqam"], v["span"])
            if not steps:
                raise E.PlanError("start|%s" % E.note_name(E.max_start("maqam", top, v["maqam"], v["span"])))
            return E.Plan("maqam", steps, {"maqam": v["maqam"], "span": v["span"], "start": v["start"]})
        if mode == "drone":
            return E.plan_drone(start, v["maqam"], v["span"], v["tempo"], top, self.b["fifth"].get())
        if mode == "callresp":
            return E.plan_callresp(start, v["maqam"], int(v["level"]), v["tempo"], bottom, top,
                                   seed=int(time.time()) % 100000)
        if mode == "onset":
            return E.plan_onset(start, v["maqam"], v["tempo"], bottom, top, seed=int(time.time()) % 100000)
        if mode == "stability":
            return E.plan_stability(start, bottom, top, v["dir"])
        if mode == "leaps":
            return E.plan_leaps(start, bottom, top, v["tempo"])
        if mode == "ajnas":
            return E.plan_ajnas(start, v["jins"], v["tempo"], top)
        if mode == "song":
            if self.song is None:
                raise E.PlanError("nosong|")
            t0, t1 = G.parse_t(v["from"]) or 0.0, G.parse_t(v["to"])
            return G.plan_song(self.song, t0, t1, loops=50 if self.b["loop"].get() else 1, play=v["play"])
        raise ValueError(mode)

    def plan_error(self, ex):
        what, _, arg = str(ex).partition("|")
        if what == "nosong":
            return "اختار الـ vocal stem الأول"
        return PLAN_ERR.get(what, "%s") % arg

    # -- session -----------------------------------------------------------------
    def latency(self, source, mixer):
        if self.cfg.get("lat_ms") is not None:
            return self.cfg["lat_ms"] / 1000.0
        return getattr(source, "latency", 0.0) + getattr(mixer, "latency", 0.0)

    def start(self, sim=None, plan=None):
        if self.eng:
            return
        if plan is None and self.v["mode"].get() == "song" and self.song is None and self.v["stem"].get():
            self.load_song(then=lambda: self.start(sim=sim))
            return
        try:
            plan = plan or self.make_plan()
        except (E.PlanError, ValueError) as ex:
            messagebox.showwarning("PitchTrainer", self.plan_error(ex))
            return
        self.save_cfg()
        q = queue.Queue()
        if sim is not None:
            source, mixer = SimSource(sim(plan), q), Mixer(enabled=False)
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
            mixer = Mixer()
        lat = 0.0 if plan.mode == "calib" else self.latency(source, mixer)
        self.session = getattr(plan, "session_cls", S.Session)(plan, lat)
        rec = None
        self.wav = None
        if self.b["rec"].get() and plan.mode != "calib":
            self.wav = E.recording_path("%s %s" % (plan.mode, self.v["start"].get()))
            rec = E.WavRecorder(self.wav)
        self.blind = self.b["blind"].get() and plan.mode not in ("calib", "free", "hold", "sss", "phrase")
        self.eng = Engine(self.session, source, mixer, self.v["ref"].get(), rec)
        self.t0 = time.time()
        self.ended = False
        self.yc = float(plan.steps[0].segs[0].m0) if plan.steps else 50.0
        self.clear_score()
        self.eng.start()
        self.sync()

    def load_song(self, then=None):
        stem, mix = self.v["stem"].get(), self.v["mixf"].get() or None
        self.phase_lb.configure(text="بطلّع اللحن من الـ stem…")
        self.go.configure(state="disabled")
        box = {}

        def work():
            try:
                box["song"] = G.Song(stem, mix, progress=lambda p: box.__setitem__("p", p))
            except Exception as ex:
                box["err"] = str(ex)
                crash_log(traceback.format_exc())

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.step_lb.configure(text="%d%%" % (100 * box.get("p", 0)))
                self.root.after(150, wait)
                return
            self.go.configure(state="normal")
            self.step_lb.configure(text="")
            if "err" in box:
                self.phase_lb.configure(text="مش قادر أقرا الملف")
                messagebox.showerror("PitchTrainer", "مش قادر أقرا الملف:\n" + box["err"][:600])
                return
            self.song = box["song"]
            msg = "اللحن جاهز ✔"
            if self.song.shift:
                msg += "   (%+d)" % self.song.shift
            self.phase_lb.configure(text=msg)
            if then:
                then()
        wait()

    def stop(self):
        if self.eng:
            self.eng.running = False

    def finish(self):
        eng, self.eng = self.eng, None
        eng.join(timeout=3)
        self.plays = eng.mixer.count
        sess = self.session
        dur = time.time() - self.t0
        self.ended = True
        if sess.mode == "calib":
            self.finish_calib(sess)
            self.sync()
            return
        if sess.mode in ("runs", "drill"):
            self.sync()
            self.runs_tab.finished(sess, dur)
            return
        if sess.mode in ("hold", "sss", "phrase"):
            self.sync()
            self.breath_tab.finished(sess, dur)
            return
        e = S.history_entry(sess, dur, wav=self.wav, blind=self.blind)
        self.last = e
        if e["frames"] > 0 or e.get("notes"):
            E.save_session(e)
            msg = "اتحفظت في السجل"
        else:
            msg = "مفيش غُنا متسجّل — الجلسة مش هتتحفظ"
        if self.wav:
            msg += "   |   WAV: " + os.path.basename(self.wav)
        self.saved_lb.configure(text=msg)
        self.show_score(e, sess)
        self.phase_lb.configure(text="خلصت ✔" if sess.done() else "اتوقفت")
        self.draw_overview()
        self.sync()
        if self.autotest:
            truth = getattr(self, "auto_truth", {})
            d = [abs(n["err"] - truth[(n["step"], n["k"])]) for n in sess.notes
                 if n["err"] is not None and (n["step"], n["k"]) in truth]
            self.auto_results.append(dict(e, ref_plays=self.plays, steps=len(sess.steps),
                                          max_target=max([st.top() for st in sess.steps] or [0]),
                                          truth_n=len(d), truth_mae=round(float(np.mean(d)), 2) if d else None,
                                          truth_max=round(float(np.max(d)), 2) if d else None))
            self.auto_truth = {}
            self.shot("%02d-%s-result" % (len(self.auto_results), sess.mode))
            self.root.after(1500, self.auto_next)

    # -- calibration ---------------------------------------------------------------
    def calibrate(self, sim=None):
        if self.eng:
            return
        if sim is None and not messagebox.askokcancel(
                "PitchTrainer", "معايرة التأخير:\nالبس السماعات. هتسمع 8 نقرات عود.\nقول «تا» قصيرة مع كل نقرة بالظبط (مش بعدها).\n\nجاهز؟"):
            return
        p = E.plan_calibration(pitch=E.parse_note(self.v["start"].get()))
        self.start(sim=sim, plan=p)
        self.phase_lb.configure(text="قول «تا» مع كل نقرة")

    def finish_calib(self, sess):
        lat, spread, n = A.calibrate(sess.T, sess.M, sess.plan.clicks)
        self.calib = {"lat": lat, "spread": spread, "n": n}
        if lat is None or spread > 0.04 or not (0.0 <= lat <= 0.45):
            self.phase_lb.configure(text="المعايرة مش واضحة — جرّب تاني")
            if not self.autotest:
                messagebox.showwarning("PitchTrainer", "المعايرة مش واضحة (اتسمع %d من 6).\nجرّب تاني، و«تا» قصيرة وواضحة مع كل نقرة." % n)
            return
        self.cfg["lat_ms"] = int(round(lat * 1000))
        self.save_cfg()
        self.show_lat()
        self.phase_lb.configure(text="المعايرة اتحفظت ✔")
        self.step_lb.configure(text="%d ms" % self.cfg["lat_ms"])
        if self.autotest:
            self.root.after(800, self.auto_next)

    # -- score ---------------------------------------------------------------------
    def clear_score(self):
        for cap, val in self.slots:
            cap.configure(text="")
            val.configure(text="", foreground=TEXT)
        self.saved_lb.configure(text="")
        self.tree.delete(*self.tree.get_children())
        self.tree_times = {}

    def show_score(self, e, sess):
        f = lambda v, fmt: "—" if v is None else fmt % v
        slots = [("مظبوط", f(e.get("exact_pct"), "%.0f%%"), None), ("قريب", f(e.get("close_pct"), "%.0f%%"), None),
                 ("متوسط الانحراف", f(e.get("note_avg_cents"), "%.1f c"),
                  ZONE[E.zone(e["note_avg_cents"])] if e.get("note_avg_cents") is not None else None),
                 ("نوتات", str(e.get("notes", 0)), None)]
        if sess.mode == "free":
            slots = [("مظبوط (وقت)", f(e.get("in_tune_pct"), "%.0f%%"), None), ("قريب (وقت)", f(e.get("near_pct"), "%.0f%%"), None),
                     ("متوسط الانحراف", f(e.get("avg_cents"), "%.1f c"), None), ("", "", None)]
        o, lp, sb = e.get("onset"), e.get("leaps"), e.get("stab")
        if sess.mode == "stability" and sb:
            slots += [("الفايبراتو", ("%.1f Hz  ±%.0fc" % (sb["vib_rate"], sb["vib_width"])) if sb["vib_rate"] else "—", None),
                      ("وقعة آخر النفس", "%+.0f c" % sb["end_drop_c"], None)]
        elif sess.mode == "leaps" and lp:
            slots += [("القفزات صح", "%.0f%%" % lp["ok_pct"], None),
                      ("الزحلقة", "%.0fc · %sms" % (o["slide_c"], "—" if o["slide_ms"] is None else "%.0f" % o["slide_ms"]) if o else "—", None)]
        elif o and sess.mode in ("onset", "callresp", "ajnas", "drone", "song"):
            slots += [("الزحلقة", "%.0fc · %sms" % (o["slide_c"], "—" if o["slide_ms"] is None else "%.0f" % o["slide_ms"]), None),
                      ("نزلت مباشرة", "%.0f%%" % o["landed_pct"], None)]
        else:
            slots += [("أعلى ثابتة", e.get("highest", "—"), None), ("أوطى ثابتة", e.get("lowest", "—"), None)]
        for (cap, val), (c, v, col) in zip(self.slots, slots):
            cap.configure(text=c)
            val.configure(text=v, foreground=col or TEXT)
        # details
        self.tree.delete(*self.tree.get_children())
        self.tree_times = {}
        notes = sess.notes
        if sess.mode == "song":
            rows = S.off_notes(notes)
            for i, o_ in enumerate(rows[:200]):
                iid = self.tree.insert("", "end", values=(G.fmt_t(o_["t"]), o_["label"], "—" if o_["err"] is None else "%+.0f" % o_["err"],
                                                          {"missed": "مش متغنية", "yellow": "أصفر", "red": "أحمر"}.get(o_["zone"], "")))
                self.tree_times[iid] = o_["t"]
        elif sess.mode == "stability":
            for n in notes:
                s_ = n.get("stab")
                if not s_:
                    continue
                vib = ("%.1fHz ±%.0f" % (s_["vib_rate"], s_["vib_width"])) if s_["vib_rate"] else "—"
                self.tree.insert("", "end", values=("%.0fs" % s_["held_s"], n["label"], "%+.0f" % (n["err"] or 0),
                                                    "drift %+.0f  ·  end %+.0f  ·  vib %s" % (s_["drift_c"], s_["end_drop_c"], vib)))
        elif sess.mode == "leaps":
            for n in notes:
                if "leap_ok" not in n:
                    continue
                oo = n.get("onset") or {}
                self.tree.insert("", "end", values=(n["leap"], n["label"], "%+.0f" % oo.get("first_err", 0) if oo else "—",
                                                    "نزلت صح" if n["leap_ok"] else "زحلقت / بعيد"))
        else:
            worst = sorted([n for n in notes if n["err"] is not None], key=lambda n: -abs(n["err"]))[:12]
            for n in worst:
                oo = n.get("onset")
                note = {"green": "مظبوط", "yellow": "أصفر", "red": "أحمر"}[E.zone(n["err"])]
                if oo and not oo["landed"]:
                    note += " · زحلقة"
                self.tree.insert("", "end", values=("%d · %s" % (n["step"] + 1, n["deg"] if n["deg"] not in ("song", "orn") else ""),
                                                    n["label"], "%+.0f" % n["err"], note))

    def tree_loop(self, _e=None):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.tree_times:
            return
        t = self.tree_times[sel[0]]
        self.v["from"].set(G.fmt_t(max(0.0, t - 2.0)))
        self.v["to"].set(G.fmt_t(t + 2.5))
        self.b["loop"].set(True)
        self.phase_lb.configure(text="اللوب اتظبط — دوس ابدأ")

    def day_report(self):
        try:
            p = report.write(E.load_history())
        except Exception as ex:
            crash_log(traceback.format_exc())
            messagebox.showerror("PitchTrainer", "مش قادر أكتب التقرير:\n%s" % ex)
            return None
        self.saved_lb.configure(text="التقرير: " + os.path.basename(p))
        if not self.autotest:
            open_path(os.path.dirname(p))
        return p

    def quit(self):
        if not self.autotest:
            self.save_cfg()
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
                elif self.session.mode in ("runs", "drill"):
                    self.runs_tab.live()
                elif self.session.mode in ("hold", "sss", "phrase"):
                    self.breath_tab.live()
                else:
                    self.update_live()
        except Exception:
            crash_log(traceback.format_exc())
        self.root.after(40, self.tick)

    def update_live(self):
        s = self.session
        with self.eng.lock:
            now = s.now_a
            frames = [f for f in s.frames[-1400:] if f[0] >= now - 5.2]
            ph, st = s.phase_at(now)
            tgt, seg = s.target_at(now)
            db = s.level_db
        if s.mode in ("free", "calib"):
            self.phase_lb.configure(text="قول «تا» مع كل نقرة" if s.mode == "calib" else "غنّي أي حاجة  ●")
            self.step_lb.configure(text="%.0fs" % now)
        else:
            self.phase_lb.configure(text={"lead": "استعد…", "listen": "اسمع  ♪", "gap": "جهّز نفسك…", "sing": "دورك  ●",
                                          "tail": "استريح…", "end": "خلصت ✔"}.get(ph, ""))
            if s.mode == "song":
                self.step_lb.configure(text="%s   ·   x%d" % (G.fmt_t(st.song_t0 + max(0.0, now - st.sing0)), st.index + 1))
            else:
                self.step_lb.configure(text="%d / %d%s" % (st.index + 1, len(s.steps), ("   ·   " + st.label) if st.label else ""))
        last = frames[-1] if frames else None
        sung = last[1] if last and last[1] is not None else None
        if s.mode == "free":
            tgt = last[2] if last else None
        self.read["target"].configure(text=E.note_label(tgt) if tgt is not None and s.mode != "calib" else "—")
        sub = ""
        if seg is not None and s.mode in ("maqam", "drone", "callresp", "onset", "ajnas") and seg.label not in ("orn", ""):
            sub = "%s  ·  %s" % (s.info.get("maqam") or s.info.get("jins") or "", seg.label)
        elif tgt is not None and s.mode not in ("free", "calib"):
            sub = "%.1f Hz" % E.midi_to_hz(tgt)
        self.sub.configure(text=sub)
        if self.blind:
            self.read["sung"].configure(text="•••")
            self.read["diff"].configure(text="•••", foreground=MUTED)
        else:
            self.read["sung"].configure(text=E.note_label(round(sung * 100) / 100) if sung is not None else "—")
            err = (sung - tgt) * 100.0 if sung is not None and tgt is not None else None
            if err is not None and abs(err) < 600:
                self.read["diff"].configure(text="%+d" % int(round(err)), foreground=ZONE[E.zone(err)])
            else:
                self.read["diff"].configure(text="—", foreground=TEXT)
        self.meter.delete("all")
        w = max(0.0, min(1.0, (db + 60) / 60.0)) * 120
        self.meter.create_rectangle(0, 0, w, 10, fill=GOLD if db > -50 else "#5a4a35", width=0)
        if self.blind:
            self.draw_blind()
        else:
            self.draw_graph(now - 5.0, now + 1.5, now, frames, tgt)

    def draw_blind(self):
        cv = self.cv
        cv.delete("all")
        W, H = max(cv.winfo_width(), 200), max(cv.winfo_height(), 120)
        cv.create_text(W / 2, H / 2 - 10, text="وضع أعمى", fill=GOLD, font=("Segoe UI", 18, "bold"))
        cv.create_text(W / 2, H / 2 + 22, text="اسمع وغنّي — الرسم والنتيجة في الآخر", fill=MUTED, font=("Segoe UI", 11))

    def draw_overview(self):
        s = self.session
        if not s.steps or not s.frames:
            return
        t_a = s.steps[0].sing0 - 0.5 if s.mode in ("song", "drone") else s.steps[0].listen0
        t_b = min(s.now_a, s.steps[-1].sing1 + 0.5)
        if t_b - t_a < 2:
            return
        fr = s.frames
        stepn = max(1, len(fr) // 2500)
        self.draw_graph(t_a, t_b, None, fr[::stepn], None, overview=True)

    def draw_graph(self, t_a, t_b, now, frames, tgt, overview=False):
        cv = self.cv
        cv.delete("all")
        W, H = max(cv.winfo_width(), 200), max(cv.winfo_height(), 120)
        L = 50
        s = self.session
        if overview:
            vals = [f[1] for f in frames if f[1] is not None] + [g.m0 for st in s.steps for g in st.segs if g.scored]
            if not vals:
                return
            lo, hi = float(np.percentile(vals, 2)) - 1.5, float(np.percentile(vals, 98)) + 1.5
            if hi - lo < 8:
                c = (hi + lo) / 2
                lo, hi = c - 4, c + 4
        else:
            if tgt is not None:
                want = tgt
            else:
                v = [f[1] for f in frames[-40:] if f[1] is not None]
                want = float(np.median(v)) if v else self.yc
            self.yc += (want - self.yc) * 0.12
            lo, hi = self.yc - 6.5, self.yc + 6.5
        X = lambda t: L + (t - t_a) / (t_b - t_a) * (W - L - 8)
        Y = lambda m: H - 8 - (m - lo) / (hi - lo) * (H - 16)
        for n in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
            cv.create_line(L, Y(n), W, Y(n), fill="#2b2620" if n % 12 == 0 else "#1d1a16")
        if lo < E.TOP_LIMIT + 0.5 < hi + 1:
            y = Y(E.TOP_LIMIT + 0.5)
            cv.create_line(L, y, W, y, fill="#7a2e2a", dash=(4, 4))
            cv.create_text(W - 10, y - 8, text="G4 top", anchor="e", fill="#c0504a", font=("Segoe UI", 8))
        if s.plan.drone:
            d = s.plan.drone
            for m_, w_ in ((d["tonic"], 2), (d["tonic"] + 7, 1)) if d["fifth"] else ((d["tonic"], 2),):
                for k in (-12, 0, 12):
                    if lo < m_ + k < hi:
                        cv.create_line(L, Y(m_ + k), W, Y(m_ + k), fill="#4a3a26", width=w_)
        if now is not None:
            cv.create_line(X(now), 4, X(now), H - 4, fill="#3a3128")
        band = (E.GREEN / 100.0) * (H - 16) / (hi - lo)
        for st in s.steps:
            if st.sing1 < t_a or st.listen0 > t_b:
                continue
            if st.lane is not None:   # song: reference contour + note bars
                lt, lm = st.lane
                sel = (lt + st.sing0 >= t_a) & (lt + st.sing0 <= t_b)
                pts = []
                for t, m_ in zip(lt[sel][::3], lm[sel][::3]):
                    if np.isnan(m_) or not (lo - 1 < m_ < hi + 1):
                        if len(pts) >= 4:
                            cv.create_line(*pts, fill="#8a6a3c", width=1)
                        pts = []
                        continue
                    pts += [X(t + st.sing0), Y(m_)]
                if len(pts) >= 4:
                    cv.create_line(*pts, fill="#8a6a3c", width=1)
                for g in st.segs:
                    if g.scored and st.sing0 + g.t1 >= t_a and st.sing0 + g.t0 <= t_b:
                        x0, x1, y = X(st.sing0 + g.t0), X(st.sing0 + g.t1), Y(g.m0)
                        cv.create_rectangle(x0, y - band, x1, y + band, fill="#1f2a22", width=0)
                        cv.create_line(x0, y, x1, y, fill=GOLD, width=3)
                continue
            parts = [(st.sing0, True)] + ([(st.listen0, False)] if st.listen else [])
            for base, sing in parts:
                for g in st.segs:
                    if not sing and g.t0 >= st.listen_len:
                        continue
                    t1 = g.t1 if sing else min(g.t1, st.listen_len)
                    if base + t1 < t_a or base + g.t0 > t_b:
                        continue
                    ts = np.linspace(base + g.t0, base + t1, 24 if g.glide else 2)
                    pts = []
                    for t in ts:
                        pts += [X(t), Y(g.at(t - base))]
                    if sing:
                        if not g.glide and g.scored:
                            cv.create_rectangle(pts[0], pts[1] - band, pts[-2], pts[1] + band, fill="#1f2a22", width=0)
                        cv.create_line(*pts, fill=GOLD if g.scored else "#9a7a48", width=3 if g.scored else 2, capstyle="round")
                    else:
                        cv.create_line(*pts, fill="#6b5536", width=2, dash=(3, 3))
        run, col = [], None
        for f in frames[::2] if not overview else frames:
            t, m_, tg, err = f[0], f[1], f[2], f[3]
            if err is None and tg is not None and m_ is not None and f[4] in ("sing", "free"):
                err = (m_ - tg) * 100.0
            if m_ is None or not (lo - 1 < m_ < hi + 1) or t < t_a:
                if len(run) >= 4:
                    cv.create_line(*run, fill=ZONE[col], width=2 if overview else 3)
                run, col = [], None
                continue
            c = E.zone(err) if err is not None and abs(err) <= 600 else None
            if c != col and run:
                run += [X(t), Y(m_)]
                if len(run) >= 4:
                    cv.create_line(*run, fill=ZONE[col], width=2 if overview else 3)
                run = []
            col = c
            run += [X(t), Y(m_)]
        if len(run) >= 4:
            cv.create_line(*run, fill=ZONE[col], width=2 if overview else 3)
        cv.create_rectangle(0, 0, L, H, fill=PANEL, width=0)
        k = 1 if hi - lo <= 16 else 2
        for n in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
            if n % k == 0:
                cv.create_text(L - 6, Y(n), text=E.note_name(n), anchor="e", font=("Segoe UI", 8),
                               fill=MUTED if n % 12 == 0 else "#5d544a")
        if overview:
            cv.create_text(W - 8, 10, text="session overview", anchor="e", fill=MUTED, font=("Segoe UI", 8))

    # -- progress ------------------------------------------------------------------
    def show_progress(self):
        rows = E.daily(E.load_history())
        top = tk.Toplevel(self.root)
        top.title("PitchTrainer — التقدّم")
        top.configure(bg=BG)
        top.geometry("900x700")
        f = ttk.Frame(top, padding=10)
        f.pack(fill="both", expand=True)
        self.progress_win = top
        if not rows:
            ttk.Label(f, text="لسه مفيش جلسات متسجلة. خلّص تمرين وارجع هنا.").pack(pady=40)
            return top
        last = rows[-30:]
        groups = [g for g, _ in E.GROUPS if any(g in r["groups"] for r in last)]
        lg = ttk.Frame(f)
        lg.pack(anchor="w", pady=(0, 4))
        for g, name in E.GROUPS:
            if g in groups:
                i = [x for x, _ in E.GROUPS].index(g)
                sw = tk.Canvas(lg, width=14, height=4, bg=BG, highlightthickness=0)
                sw.create_rectangle(0, 0, 14, 4, fill=SERIES[i], width=0)
                sw.pack(side="left", padx=(8, 3))
                ttk.Label(lg, text=name, style="Hint.TLabel").pack(side="left")
        for title, key, fmt, refs in (("مظبوط (±15c) %  —  خط لكل تمرين", "exact_pct", "%.0f%%",
                                       [(E.BASELINE_EXACT, "base 32%"), (E.GOAL_EXACT, "goal 60%")]),
                                      ("متوسط الانحراف (cents)  —  الأقل أحسن", "avg_cents", "%.0f",
                                       [(E.BASELINE_CENTS, "base 21.7c")])):
            ttk.Label(f, text=title, foreground=GOLD, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            cv = tk.Canvas(f, bg=PANEL, height=165, highlightthickness=0)
            cv.pack(fill="x", pady=(2, 10))
            cv.update_idletasks()
            self.lines(cv, last, key, fmt, groups, refs, pct=(key == "exact_pct"))
        sp = [(i, r["run_nps"]) for i, r in enumerate(last) if r.get("run_nps")]
        if sp:
            ttk.Label(f, text="سرعة العُرَب (نوتة/ث)  —  أسرع عُربة أو تمرين اتغنّى ≥ 80%", foreground=GOLD,
                      font=("Segoe UI", 10, "bold")).pack(anchor="w")
            cv = tk.Canvas(f, bg=PANEL, height=110, highlightthickness=0)
            cv.pack(fill="x", pady=(2, 8))
            cv.update_idletasks()
            W, H, L_ = max(cv.winfo_width(), 600), 110, 44
            hiv = max(v for _, v in sp) * 1.2
            step = (W - L_ - 20) / max(1, len(last))
            Y = lambda v: H - 18 - v / hiv * (H - 28)
            for gv in np.linspace(0, hiv, 3):
                cv.create_line(L_, Y(gv), W - 6, Y(gv), fill="#1d1a16")
                cv.create_text(L_ - 6, Y(gv), text="%.0f" % gv, anchor="e", fill=MUTED, font=("Segoe UI", 8))
            pts = [(L_ + step * (i + 0.5), Y(v)) for i, v in sp]
            if len(pts) > 1:
                cv.create_line(*[c for p_ in pts for c in p_], fill=GOLD, width=2)
            for (x, y), (_, v) in zip(pts, sp):
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=GOLD, outline=PANEL, width=2)
            cv.create_text(pts[-1][0], pts[-1][1] - 10, text="%.1f" % sp[-1][1], fill=TEXT, font=("Segoe UI", 9, "bold"))
        tv = ttk.Treeview(f, columns=("d", "s", "m", "x", "c", "a", "h"), show="headings", height=7)
        for k, t, w in (("d", "Date", 100), ("s", "Sessions", 70), ("m", "Minutes", 70), ("x", "Exact", 70), ("c", "Close", 70),
                        ("a", "Avg cents", 80), ("h", "Highest stable", 110)):
            tv.heading(k, text=t)
            tv.column(k, width=w, anchor="center")
        for r in reversed(rows):
            tv.insert("", "end", values=(r["date"], r["sessions"], "%.0f" % r["minutes"],
                                         "—" if r.get("exact_pct") is None else "%.0f%%" % r["exact_pct"],
                                         "—" if r.get("close_pct") is None else "%.0f%%" % r["close_pct"],
                                         "—" if r.get("avg_cents") is None else "%.1f" % r["avg_cents"],
                                         E.note_label_rounded(r["highest_midi"])))
        tv.pack(fill="both", expand=True)
        return top

    @staticmethod
    def lines(cv, rows, key, fmt, groups, refs, pct):
        W, H = max(cv.winfo_width(), 600), 165
        L, B, T = 44, 24, 12
        allv = [r["groups"][g][key] for r in rows for g in groups if g in r["groups"] and r["groups"][g].get(key) is not None]
        hiv = 100.0 if pct else max([30.0] + [v * 1.15 for v in allv] + [x for x, _ in refs])
        n = len(rows)
        step = (W - L - 70) / max(n, 1)
        Y = lambda v: H - B - v / hiv * (H - B - T)
        for gv in np.linspace(0, hiv, 5):
            cv.create_line(L, Y(gv), W - 6, Y(gv), fill="#1d1a16")
            cv.create_text(L - 6, Y(gv), text=fmt % gv, anchor="e", fill=MUTED, font=("Segoe UI", 8))
        for v, name in refs:
            cv.create_line(L, Y(v), W - 66, Y(v), fill="#5d544a", dash=(4, 4))
            cv.create_text(W - 62, Y(v), text=name, anchor="w", fill=MUTED, font=("Segoe UI", 8))
        for i, r in enumerate(rows):
            if i % max(1, n // 10) == 0 or i == n - 1:
                cv.create_text(L + step * (i + 0.5), H - B + 12, text=r["date"][5:], fill=MUTED, font=("Segoe UI", 8))
        for g in groups:
            col = SERIES[[x for x, _ in E.GROUPS].index(g)]
            pts = [(L + step * (i + 0.5), Y(r["groups"][g][key])) for i, r in enumerate(rows)
                   if g in r["groups"] and r["groups"][g].get(key) is not None]
            if len(pts) > 1:
                cv.create_line(*[c for p in pts for c in p], fill=col, width=2)
            for x, y in pts:
                cv.create_oval(x - 4, y - 4, x + 4, y + 4, fill=col, outline=PANEL, width=2)

    # -- CI autotest -----------------------------------------------------------------
    def shot(self, name):
        """Ask the CI workflow for a screenshot (it polls for *.req files)."""
        if self.autotest:
            try:
                with open(os.path.join(self.shots, name + ".req"), "w") as fh:
                    fh.write(name)
            except OSError:
                pass

    def auto_plans(self):
        return [
            ("calib", {}),
            ("drone", {"maqam": "Bayati", "span": "jins", "start": "D3", "tempo": "fast", "fifth": True}),
            ("callresp", {"maqam": "Saba", "level": "3", "start": "D3", "tempo": "normal", "count": 3}),
            ("song", {}),
            ("onset", {"maqam": "Rast", "start": "C3", "tempo": "fast", "count": 6}),
            ("stability", {"start": "A3", "count": 2}),
            ("leaps", {"start": "C3", "tempo": "fast"}),
            ("ajnas", {"jins": "Sikah", "start": "E3", "tempo": "fast", "blind": True}),
        ]

    def sim_for(self, mode, seed):
        rng = np.random.default_rng(seed)
        offs = {}
        lat = self.autotest["sim_lat"]

        def off(st, k, s):
            return offs.setdefault((st.index, k), float(rng.choice([-30, -18, -8, -3, 0, 4, 9, 22, 40])))

        def make(plan):
            if plan.mode == "calib":
                end = plan.end + 1.0
                tr = np.full(int(end * E.SR), np.nan)
                for c in plan.clicks:
                    a = int((c + lat + 0.015) * E.SR)
                    tr[a:a + int(0.25 * E.SR)] = plan.pitch
                return E.synth_voice(tr, seed=seed)
            end = plan.steps[-1].sing1 + 1.5 + lat
            kw = dict(late=lat, offset_cents=off, scoop=(-45.0, 0.07))
            if mode == "runs":   # legato run, small per-note errors
                kw = dict(late=lat + 0.04, offset_cents=lambda st, k, s: float(rng.choice([-12, -6, 0, 4, 9])), gap=0.0)
            if mode == "stability":
                kw.update(offset_cents=6.0, drift=(-30.0, 1.5), scoop=None)
            tr = E.singer_track(plan.steps, end, **kw)
            self.auto_truth = offs
            return E.synth_voice(tr, vibrato_cents={"stability": 25.0, "runs": 0.0}.get(mode, 12.0), seed=seed, noise=0.006)
        return make

    def auto_next(self):
        plans = self.auto_plans()
        i = self.auto_i = getattr(self, "auto_i", -1) + 1
        if i == len(plans) and not getattr(self, "runs_done", False):
            self.runs_done = True
            self.auto_i -= 1
            self.runs_tab.autotest(self.sim_for("runs", 20), lambda: self.breath_tab.autotest(self.auto_next))
            return
        if i >= len(plans) and not getattr(self, "warm_done", False):
            self.warm_done = True
            self.auto_i -= 1
            self.nb.select(0)
            wt = self.warm_tab
            wt.wv["len"].set("short")
            wt.cbs["len"].current(0)
            wt.fill()
            wt.play()
            self.root.after(9000, lambda: self.shot("15-warmup"))
            def exp():
                wt.stop()
                self.warm_mp3 = wt.export()
                self.root.after(500, self.auto_next)
            self.root.after(11000, exp)
            return
        if i >= len(plans):
            self.nb.select(1)
            win = self.show_progress()
            win.geometry("+%d+%d" % (self.root.winfo_rootx() + 300, self.root.winfo_rooty() + 30))
            self.root.after(1200, lambda: (self.shot("90-progress"), self.root.after(1500, self.auto_write)))
            return
        mode, o = plans[i]
        sim = self.sim_for(mode, i)
        if mode == "calib":
            self.calibrate(sim=sim)
            self.root.after(5000, lambda: self.shot("00-calibration"))
            return
        self.set_combo(self.mode_cb, "mode", mode)
        for k, cb in (("maqam", self.mq_cb), ("span", self.sp_cb), ("start", self.start_cb), ("tempo", self.tmp_cb),
                      ("level", self.lv_cb), ("jins", self.jn_cb)):
            if k in o:
                self.set_combo(cb, k, o[k])
        self.b["fifth"].set(o.get("fifth", True))
        self.b["blind"].set(o.get("blind", False))
        self.b["rec"].set(mode == "drone")
        self.sync()
        if mode == "song":
            if not self.autotest.get("stem"):
                st_, mx_, _ = G.make_test_files(os.path.join(self.shots, "أغنية تجربة"))
                self.autotest.update(stem=st_, mix=mx_)
            self.v["stem"].set(self.autotest["stem"])
            self.v["mixf"].set(self.autotest.get("mix", ""))
            self.stem_lb.configure(text=os.path.basename(self.autotest["stem"]))
            self.mix_lb.configure(text=os.path.basename(self.autotest.get("mix", "")))
            self.song = None
            self.load_song(then=lambda: self.auto_start(mode, o, sim))
            return
        self.auto_start(mode, o, sim)

    def auto_start(self, mode, o, sim):
        try:
            plan = self.make_plan()
        except Exception:
            crash_log(traceback.format_exc())
            raise
        if mode == "song":
            plan = G.plan_song(self.song, 0.0, None)
        if "count" in o:
            plan.steps = plan.steps[:o["count"]]
        if mode == "leaps":
            plan.steps = [st for st in plan.steps if st.label.split()[0] in ("5th", "8ve")][:4]
            for k, st in enumerate(plan.steps):
                st.index = k
            E.layout(plan.steps, gap=0.5, tail=0.7)
        self.start(sim=sim, plan=plan)
        lv = 0.45 * (plan.steps[-1].sing1 if plan.steps else 5)
        self.root.after(int(lv * 1000), lambda: self.shot("%02d-%s-live" % (len(self.auto_results) + 1, mode)))

    def auto_write(self):
        rp = self.day_report()
        out = {"sessions": self.auto_results, "calib": self.calib, "report": rp, "runs": getattr(self, "auto_runs", None),
               "warm_mp3": getattr(self, "warm_mp3", None),
               "history_days": E.daily(E.load_history()), "history_file": E.HISTORY_FILE,
               "sim_lat": self.autotest["sim_lat"]}
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1, default=float)


# ------------------------------------------------------------- «العُرَب» tab
RUNS_DIR = os.path.join(ROOT, "runs")
LEAD = 1.0          # seconds of the recording played before A


class RunsTab:
    """Load a recording, find its runs, practise one (A–B loop, speed, transpose,
    oud note by note), sing it back (DTW score) and progressive drills."""

    def __init__(self, app):
        self.app = app
        a = app
        f = ttk.Frame(a.nb, padding=10)
        a.nb.add(f, text="  العُرَب  ")
        self.f = f
        self.rec = None          # {"path", "audio", "T", "M", "conf", "notes", "maqam"}
        self.runs = []
        self.mixer = None
        self.loop_job = None
        self.result = None
        self.ref = None          # current sing-back reference: notes on the session clock
        cfg = a.cfg
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)
        top = ttk.Frame(f)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(top, text="حمّل تسجيل…", command=self.pick).pack(side="right")
        self.file_lb = ttk.Label(top, text="موال / أغنية طرب / تقسيم — WAV · MP3 · M4A · MP4", style="Hint.TLabel")
        self.file_lb.pack(side="right", padx=8)
        self.rate = tk.DoubleVar(value=cfg.get("run_rate", 4.0))
        self.rate_lb = ttk.Label(top, text="", width=10)
        self.rate_lb.pack(side="left")
        ttk.Scale(top, from_=2.0, to=12.0, variable=self.rate, length=160,
                  command=lambda _v: self.redetect()).pack(side="left", padx=4)
        ttk.Label(top, text="حد السرعة (نوتة/ث)").pack(side="left", padx=(0, 10))
        self.warn_lb = ttk.Label(top, text="", foreground="#e5534b")
        self.warn_lb.pack(side="left", padx=10)
        self.all_v = tk.BooleanVar(value=cfg.get("run_all", False))
        ttk.Checkbutton(top, text="اعرض الكل", variable=self.all_v, command=lambda: self.fill()).pack(side="left", padx=6)

        # ---- runs list (left) + edit
        left = ttk.Frame(f)
        left.grid(row=1, column=0, sticky="nsw", padx=(0, 10))
        self.maq_lb = ttk.Label(left, text="", foreground=GOLD, font=("Segoe UI", 11, "bold"))
        self.maq_lb.pack(anchor="e")
        self.tree = ttk.Treeview(left, columns=("i", "nm", "t", "d", "n", "q", "r", "j"), show="headings", height=11)
        for k, t, w in (("i", "#", 28), ("nm", "الاسم", 72), ("t", "الوقت", 58), ("d", "المدة", 44), ("n", "نوتات", 42),
                        ("q", "مشكوك", 46), ("r", "نوتة/ث", 50), ("j", "الجنس", 92)):
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="center")
        self.tree.pack(fill="y", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.select())
        ed = ttk.Frame(left)
        ed.pack(fill="x", pady=(4, 0))
        self.name_v = tk.StringVar()
        ttk.Entry(ed, textvariable=self.name_v, width=14).pack(side="right")
        ttk.Button(ed, text="سمّي", command=self.rename).pack(side="right", padx=2)
        ttk.Button(ed, text="احذف", command=self.delete).pack(side="right", padx=2)
        ed2 = ttk.Frame(left)
        ed2.pack(fill="x", pady=2)
        ttk.Button(ed2, text="قص على A–B", command=self.trim).pack(side="right", padx=2)
        ttk.Button(ed2, text="ضيف عُربة من A–B", command=self.add).pack(side="right", padx=2)

        # drills
        dr = ttk.LabelFrame(left, text="  تمارين متدرّجة  ", padding=6)
        dr.pack(fill="x", pady=(8, 0))
        self.dv = {"maq": tk.StringVar(value=cfg.get("drill_maq", "Bayati")), "kind": tk.StringVar(value=cfg.get("drill_kind", "turn3")),
                   "start": tk.StringVar(value=cfg.get("drill_start", "D3"))}
        self.dcb = {}
        for r, (k, lab, pairs) in enumerate((("maq", "المقام", [(m, "%s  %s" % (E.MAQAMAT[m]["ar"], m)) for m in RU.DRILL_MAQAMAT]),
                                             ("kind", "الشكل", RU.DRILLS),
                                             ("start", "القرار", [(n, n) for n in E.note_choices(43, E.TOP_LIMIT)]))):
            ttk.Label(dr, text=lab).grid(row=r, column=1, sticky="e")
            cb = ttk.Combobox(dr, state="readonly", width=24, values=[t for _, t in pairs])
            keys = [x for x, _ in pairs]
            cb.current(keys.index(self.dv[k].get()) if self.dv[k].get() in keys else 0)
            cb.bind("<<ComboboxSelected>>", lambda e, k=k, cb=cb, keys=keys: (self.dv[k].set(keys[cb.current()]), self.show_drill()))
            cb.grid(row=r, column=0, sticky="w", pady=2)
            cb.keys = keys
            self.dcb[k] = cb
        self.drill_lb = ttk.Label(dr, text="", style="Hint.TLabel")
        self.drill_lb.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(dr, text="▶ التمرين (عود ثم أنت)", style="Go.TButton", command=self.drill).grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # ---- practice (right)
        right = ttk.Frame(f)
        right.grid(row=1, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self.notes_lb = tk.Text(right, height=2, bg=PANEL, fg=TEXT, relief="flat", font=("Segoe UI", 10), wrap="word")
        self.notes_lb.grid(row=0, column=0, sticky="ew")
        self.notes_lb.tag_configure("r", justify="right")
        self.cv = tk.Canvas(right, bg=PANEL, highlightthickness=0, width=520, height=230)
        self.cv.grid(row=1, column=0, sticky="nsew", pady=4)
        pr = ttk.Frame(right)
        pr.grid(row=2, column=0, sticky="ew")
        self.pv = {"a": tk.StringVar(), "b": tk.StringVar(), "speed": tk.StringVar(value=str(cfg.get("run_speed", "75"))),
                   "semis": tk.StringVar(value=str(cfg.get("run_semis", "0")))}
        for lab, k, w in (("A", "a", 7), ("B", "b", 7)):
            ttk.Label(pr, text=lab).pack(side="right", padx=(6, 2))
            ttk.Entry(pr, textvariable=self.pv[k], width=w).pack(side="right")
        ttk.Label(pr, text="السرعة %").pack(side="right", padx=(10, 2))
        ttk.Combobox(pr, textvariable=self.pv["speed"], values=["50", "60", "75", "90", "100"], width=5).pack(side="right")
        ttk.Label(pr, text="الطبقة (نص تون)").pack(side="right", padx=(10, 2))
        tk.Spinbox(pr, from_=-12, to=12, textvariable=self.pv["semis"], width=4, bg="#1a1714", fg=TEXT,
                   buttonbackground="#2a241d", insertbackground=TEXT).pack(side="right")
        ttk.Button(pr, text="اقترح", command=self.suggest).pack(side="right", padx=4)
        self.loop_v = tk.BooleanVar(value=cfg.get("run_loop", True))
        ttk.Checkbutton(pr, text="لوب", variable=self.loop_v).pack(side="right", padx=6)
        bt = ttk.Frame(right)
        bt.grid(row=3, column=0, sticky="ew", pady=4)
        for txt, cmd in (("▶ اسمع", self.listen), ("♪ العود نوتة نوتة", self.oud_slow), ("● غنّي معاها", lambda: self.sing("along")),
                         ("● غنّي بعدها", lambda: self.sing("after")), ("■ وقّف", self.stop)):
            ttk.Button(bt, text=txt, command=cmd).pack(side="right", padx=2)
        rs = ttk.LabelFrame(right, text="  النتيجة  ", padding=(8, 2))
        rs.grid(row=4, column=0, sticky="ew")
        self.slots = []
        for i, cap in enumerate(("مضبوطة", "قريبة", "فاتت", "متأخرة", "السرعة", "الدرجة", "أحسن سرعة")):
            rs.columnconfigure(i, weight=1)
            ttk.Label(rs, text=cap, style="Hint.TLabel").grid(row=0, column=i, sticky="w")
            v = ttk.Label(rs, text="—", font=("Segoe UI", 12, "bold"))
            v.grid(row=1, column=i, sticky="w")
            self.slots.append(v)
        self.msg = ttk.Label(right, text="", style="Hint.TLabel")
        self.msg.grid(row=5, column=0, sticky="w")
        self.show_rate()
        self.show_drill()

    # -- recording + runs ------------------------------------------------------
    def pick(self):
        p = filedialog.askopenfilename(filetypes=[("Audio / video", "*.wav *.mp3 *.m4a *.mp4 *.flac *.ogg *.aac *.webm"), ("All", "*.*")])
        if p:
            self.load(os.path.normpath(p))

    def load(self, path, then=None):
        self.file_lb.configure(text="بقرا التسجيل وبطلّع الصوت…  " + os.path.basename(path))
        box = {}

        def work():
            try:
                au = G.decode(path)
                T, M, conf = RU.melody(au.mean(1), progress=lambda p: box.__setitem__("p", p))
                box["rec"] = {"path": path, "audio": au, "T": T, "M": M, "conf": conf, "notes": RU.fast_notes(T, M)}
            except Exception as ex:
                box["err"] = str(ex)
                crash_log(traceback.format_exc())

        th = threading.Thread(target=work, daemon=True)
        th.start()

        def wait():
            if th.is_alive():
                self.warn_lb.configure(text="%d%%" % (100 * box.get("p", 0)), foreground=MUTED)
                self.app.root.after(200, wait)
                return
            if "err" in box:
                self.file_lb.configure(text="مش قادر أقرا الملف")
                messagebox.showerror("PitchTrainer", box["err"][:600])
                return
            self.rec = r = box["rec"]
            c = RU.confidence(r["M"], r["conf"])
            low = c["conf"] < 0.6 or c["voiced_pct"] < 25
            self.warn_lb.configure(text=("⚠ ثقة القراية قليلة (%d%%) — النوتات ممكن تبقى غلط" % (100 * c["conf"])) if low
                                   else "ثقة القراية %d%%" % (100 * c["conf"]), foreground="#e5534b" if low else MUTED)
            vocal = [n for n in r["notes"] if n[1] - n[0] >= 0.05]
            r["maqam"] = RU.guess_maqam(vocal)
            self.maq_lb.configure(text="المقام: " + RU.maqam_label(r["maqam"]))
            self.file_lb.configure(text=os.path.basename(path))
            saved = self.load_runs()
            if saved is not None:
                self.runs = saved
                self.fill()
            else:
                self.redetect()
            if then:
                then()
        wait()

    def runs_file(self):
        base = os.path.basename(self.rec["path"])
        return os.path.join(RUNS_DIR, base + ".json")

    def load_runs(self):
        try:
            with open(self.runs_file(), encoding="utf-8") as fh:
                d = json.load(fh)
            if d.get("rate") != round(self.rate.get(), 1):
                return None
            return [RU.run_from(self.rec["notes"], r["t0"], r["t1"], r.get("name", ""), self.rec["T"], self.rec["M"]) for r in d["runs"]]
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save_runs(self):
        if not self.rec:
            return
        os.makedirs(RUNS_DIR, exist_ok=True)
        with open(self.runs_file(), "w", encoding="utf-8") as fh:
            json.dump({"rate": round(self.rate.get(), 1), "runs": [{"t0": r["t0"], "t1": r["t1"], "name": r["name"]} for r in self.runs]},
                      fh, ensure_ascii=False)

    def show_rate(self):
        self.rate_lb.configure(text="%.1f نوتة/ث" % self.rate.get())

    def redetect(self):
        self.rate.set(round(self.rate.get() * 2) / 2)
        self.show_rate()
        self.app.cfg["run_rate"] = self.rate.get()
        if not self.rec:
            return
        self.runs = RU.find_runs(self.rec["notes"], self.rate.get(), T=self.rec["T"], M=self.rec["M"])
        self.fill()
        self.save_runs()

    def jins_of(self, r):
        mq = self.rec.get("maqam") if self.rec else None
        g = RU.guess_maqam(r["notes"], prefer=(mq["tonic"] * 100) % 1200 if mq else None)
        return RU.maqam_label(g) if g else "—"

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        hidden = 0
        first = None
        for i, r in enumerate(self.runs):
            if r.get("music_only") and not self.all_v.get():
                hidden += 1
                continue
            mark = " ♪" if r.get("music_only") else ""
            self.tree.insert("", "end", iid=str(i), values=(i + 1, (r["name"] or "") + mark, G.fmt_t(r["t0"]), "%.1fs" % (r["t1"] - r["t0"]),
                                                            len(r["notes"]), len(r.get("doubt", [])) or "", "%.1f" % r["rate"], self.jins_of(r)))
            first = str(i) if first is None else first
        self.msg.configure(text="%d عُربة%s" % (len(self.runs) - hidden,
                                                 ("   ·   اتخبّى %d (مزيكا من غير صوت ♪ — «اعرض الكل»)" % hidden) if hidden else ""))
        if first is not None:
            self.tree.selection_set(first)

    def cur(self):
        sel = self.tree.selection()
        return (int(sel[0]), self.runs[int(sel[0])]) if sel and int(sel[0]) < len(self.runs) else (None, None)

    def select(self):
        i, r = self.cur()
        if r is None:
            return
        self.pv["a"].set(G.fmt_t(r["t0"]))
        self.pv["b"].set(G.fmt_t(r["t1"]))
        self.name_v.set(r["name"])
        self.suggest()
        self.notes_lb.delete("1.0", "end")
        dz = set(r.get("doubt", []))
        self.notes_lb.insert("end", "  ".join("%s %s%s" % (RU.names(n[2])[1], RU.names(n[2])[0], "؟" if k in dz else "")
                                              for k, n in enumerate(r["notes"])), "r")
        self.result = None
        self.draw()
        self.show_best()

    def ab(self):
        a, b = G.parse_t(self.pv["a"].get()), G.parse_t(self.pv["b"].get())
        if a is None or b is None or b - a < 0.15:
            raise ValueError
        return a, b

    def rename(self):
        i, r = self.cur()
        if r is not None:
            r["name"] = self.name_v.get().strip()
            self.fill()
            self.tree.selection_set(str(i))
            self.save_runs()

    def delete(self):
        i, r = self.cur()
        if r is not None:
            del self.runs[i]
            self.fill()
            self.save_runs()

    def trim(self):
        i, r = self.cur()
        try:
            a, b = self.ab()
        except ValueError:
            return
        nr = RU.run_from(self.rec["notes"], a, b, r["name"] if r else "", self.rec["T"], self.rec["M"]) if self.rec else None
        if nr and r is not None:
            self.runs[i] = nr
            self.fill()
            self.tree.selection_set(str(i))
            self.save_runs()

    def add(self):
        try:
            a, b = self.ab()
        except ValueError:
            return
        nr = RU.run_from(self.rec["notes"], a, b, self.name_v.get().strip(), self.rec["T"], self.rec["M"]) if self.rec else None
        if nr:
            self.runs.append(nr)
            self.runs.sort(key=lambda r: r["t0"])
            self.fill()
            self.tree.selection_set(str(self.runs.index(nr)))
            self.save_runs()

    def speed(self):
        try:
            return max(0.25, min(1.5, float(self.pv["speed"].get().strip().rstrip("%")) / 100.0))
        except ValueError:
            return 1.0

    def semis(self):
        try:
            return int(float(self.pv["semis"].get()))
        except ValueError:
            return 0

    def suggest(self):
        i, r = self.cur()
        if r is not None:
            self.pv["semis"].set(str(RU.suggest_transpose(r["notes"])))

    # -- playback ----------------------------------------------------------------
    def clip(self):
        """The A–B section with LEAD s before it, stretched + transposed, and the
        run's notes on the clip clock."""
        a, b = self.ab()
        sp, st = self.speed(), self.semis()
        a0 = max(0.0, a - LEAD)
        au = self.rec["audio"][int(a0 * E.SR):int((b + 0.25) * E.SR)]
        y = RU.stretch(au, sp, st)
        i, r = self.cur()
        src = self.rec["notes"]
        if r is not None:      # inside the run use its octave-checked notes
            src = [n for n in src if n[1] <= r["t0"] or n[0] >= r["t1"]] + list(r["notes"])
            src.sort()
        notes = [((n[0] - a0) / sp, (n[1] - a0) / sp, n[2] + st) for n in src if n[0] >= a - 1e-6 and n[1] <= b + 1e-6]
        return y * 0.9, notes, sp, st

    def ensure_mixer(self):
        if self.mixer is None:
            self.mixer = Mixer(enabled=not self.app.autotest)
        return self.mixer

    def listen(self):
        if not self.rec or self.app.eng:
            return
        try:
            y, notes, sp, st = self.clip()
        except ValueError:
            return
        except Exception as ex:
            messagebox.showerror("PitchTrainer", str(ex)[:500])
            return
        self.stop()
        self.ensure_mixer().play(y)
        if self.loop_v.get():
            self.loop_job = self.app.root.after(int(len(y) / E.SR * 1000) + 400, self.listen)

    def oud_slow(self):
        i, r = self.cur()
        if r is None or self.app.eng:
            return
        st = self.semis()
        d = 0.45
        segs = [E.Seg(k * d, (k + 1) * d, n[2] + st) for k, n in enumerate(r["notes"])]
        self.stop()
        self.ensure_mixer().play(oud.render_step(E.Step(0, 0, segs)))

    def stop(self):
        if self.loop_job:
            self.app.root.after_cancel(self.loop_job)
            self.loop_job = None
        if self.mixer is not None:
            self.mixer.stop()
            self.mixer = None
        if self.app.eng:
            self.app.stop()

    # -- sing back ---------------------------------------------------------------
    def plan_for(self, clip, notes, how, mode="runs", info=None):
        segs = [E.Seg(n0, n1, min(m, float(E.TOP_LIMIT)), label="run", scored=m <= E.TOP_LIMIT + 0.15) for n0, n1, m in notes]
        dur = len(clip) / E.SR
        if how == "after":
            st = E.Step(0, segs[0].m0, segs, listen=True, listen_len=dur)
            st.dur = max(segs[-1].t1 + 1.2, dur)
            st.listen_len = dur
        else:
            st = E.Step(0, segs[0].m0, segs, listen=False)
            st.dur = dur + 0.8
        st.clip = clip
        E.layout([st], lead=0.8, gap=0.3, tail=0.5)
        st.sing1 = st.sing0 + st.dur
        p = E.Plan(mode, [st], info or {})
        p.end = st.sing1 + 1.0
        return p

    def sing(self, how, sim=None):
        i, r = self.cur()
        if r is None or self.app.eng:
            return
        try:
            y, notes, sp, st = self.clip()
        except ValueError:
            return
        if not notes:
            return
        if max(n[2] for n in notes) > E.TOP_LIMIT + 0.15:
            messagebox.showwarning("PitchTrainer", "فيه نوتات فوق G4 — نزّل الطبقة (اقترح)")
            return
        self.stop()
        name = r["name"] or "%s %s" % (os.path.basename(self.rec["path"]), G.fmt_t(r["t0"]))
        info = {"name": name, "key": "%s@%.2f" % (os.path.basename(self.rec["path"]), r["t0"]), "speed": round(sp, 2),
                "semis": st, "rate": r["rate"], "how": how}
        self.app.start(sim=sim, plan=self.plan_for(y, notes, how, "runs", info))
        self.result = None

    def drill(self, sim=None):
        if self.app.eng:
            return
        key = "%s/%s/%s" % (self.dv["maq"].get(), self.dv["kind"].get(), self.dv["start"].get())
        nps = self.app.cfg.get("drill_nps", {}).get(key, 3.0)
        try:
            notes = RU.drill_notes(self.dv["maq"].get(), self.dv["kind"].get(), E.parse_note(self.dv["start"].get()), nps)
        except E.PlanError as ex:
            messagebox.showwarning("PitchTrainer", "القرار عالي على التمرين ده — أعلى قرار: %s" % str(ex).split("|")[1])
            return
        segs = [E.Seg(a, b, m) for a, b, m in notes]
        y = oud.render_step(E.Step(0, 0, segs))
        name = "%s · %s" % (E.MAQAMAT[self.dv["maq"].get()]["ar"], dict(RU.DRILLS)[self.dv["kind"].get()])
        self.app.cfg.update(drill_maq=self.dv["maq"].get(), drill_kind=self.dv["kind"].get(), drill_start=self.dv["start"].get())
        self.stop()
        self.app.start(sim=sim, plan=self.plan_for(y, notes, "after", "drill", {"name": name, "key": key, "nps": nps, "speed": 1.0, "semis": 0}))

    def show_drill(self):
        key = "%s/%s/%s" % (self.dv["maq"].get(), self.dv["kind"].get(), self.dv["start"].get())
        nps = self.app.cfg.get("drill_nps", {}).get(key, 3.0)
        best = best_speed(E.load_history(), key)
        self.drill_lb.configure(text="السرعة دلوقتي %.1f نوتة/ث%s" % (nps, ("  ·  أحسن %.1f" % best) if best else ""))

    def finished(self, sess, dur):
        st = sess.steps[0]
        ref = [(st.sing0 + g.t0, st.sing0 + g.t1, g.m0) for g in st.segs if g.scored]
        T = np.array(sess.T) - sess.lat
        res = RU.score_sung(ref, T, np.array(sess.M), slack=1.2 + 0.5 * (ref[-1][1] - ref[0][0]))
        self.result = (sess, res, ref)
        info = sess.info
        nps = (info.get("nps") or info.get("rate", 0) * info.get("speed", 1.0))
        e = {"date": datetime.date.today().strftime("%Y-%m-%d"), "time": datetime.datetime.now().strftime("%H:%M"),
             "mode": sess.mode, "duration_s": round(dur, 1), "lat_ms": round(sess.lat * 1000), "blind": self.app.blind}
        e.update({k: info.get(k) for k in ("name", "key", "speed", "semis", "how")})
        e.update(nps=round(nps, 2), n=len(res["notes"]), hits=res["hits"], close=res["close"], missed=res["missed"],
                 late=res["late"], tempo_ratio=res["tempo_ratio"], tempo_score=res["tempo_score"], pitch_score=res["pitch_score"],
                 score=res["score"])
        E.save_session(e)
        if sess.mode == "drill" and res["score"] >= 80:
            d = self.app.cfg.setdefault("drill_nps", {})
            d[info["key"]] = round(info["nps"] * 1.05, 2)
            self.app.save_cfg()
            self.msg.configure(text="برافو — المرة الجاية أسرع 5%%: %.2f نوتة/ث" % d[info["key"]])
        else:
            self.msg.configure(text="اتحفظت في السجل")
        self.show_drill()
        self.show_result(res, e)
        self.draw()
        if self.app.autotest:
            self.app.auto_results.append(dict(e, truth=getattr(self, "truth", None)))
            self.app.shot("%02d-%s-result" % (len(self.app.auto_results), sess.mode))
            if self.auto_then:
                self.app.root.after(1200, self.auto_then)

    def show_result(self, res, e):
        n = max(1, len(res["notes"]))
        vals = ["%d" % res["hits"], "%d" % res["close"], "%d" % res["missed"], "%d" % res["late"],
                "—" if res["tempo_ratio"] is None else "%.0f%%" % (100 * res["tempo_ratio"]), "%.0f%%" % res["score"]]
        best = best_speed(E.load_history(), e.get("key"))
        vals.append("—" if not best else "%.1f n/s" % best)
        cols = [ZONE["green"], ZONE["yellow"], ZONE["red"], TEXT, TEXT, ZONE["green"] if res["score"] >= 80 else TEXT, GOLD]
        for v, t, c in zip(self.slots, vals, cols):
            v.configure(text=t, foreground=c)

    def show_best(self):
        i, r = self.cur()
        if r is None:
            return
        best = best_speed(E.load_history(), "%s@%.2f" % (os.path.basename(self.rec["path"]), r["t0"]))
        for v in self.slots:
            v.configure(text="—", foreground=TEXT)
        self.slots[-1].configure(text="—" if not best else "%.1f n/s" % best, foreground=GOLD)

    # -- drawing -------------------------------------------------------------------
    def live(self):
        a = self.app
        s = a.session
        with a.eng.lock:
            frames = list(s.frames[-3000:])
            now = s.now_a
        ph, st = s.phase_at(now)
        a.phase_lb.configure(text={"lead": "استعد…", "listen": "اسمع  ♪", "gap": "جهّز نفسك…", "sing": "دورك  ●"}.get(ph, ""))
        self.msg.configure(text={"listen": "اسمع ♪", "sing": "دورك ●", "gap": "جهّز نفسك…"}.get(ph, "…"))
        self.draw(frames=None if a.blind else frames, now=now)

    def draw(self, frames=None, now=None):
        cv = self.cv
        cv.delete("all")
        W, H = max(cv.winfo_width(), 200), max(cv.winfo_height(), 120)
        L = 46
        if self.app.eng is not None and self.app.session and self.app.session.mode in ("runs", "drill"):
            st = self.app.session.steps[0]
            ref = [(st.sing0 + g.t0, st.sing0 + g.t1, g.m0) for g in st.segs]
            t_a, t_b = st.sing0 - 0.3, st.sing0 + st.dur
            if self.app.blind:
                cv.create_text(W / 2, H / 2, text="وضع أعمى — الرسم في الآخر", fill=GOLD, font=("Segoe UI", 15, "bold"))
                return
        elif self.result:
            sess, res, ref = self.result
            t_a, t_b = ref[0][0] - 0.4, ref[-1][1] + 0.8
            frames = sess.frames
        else:
            i, r = self.cur()
            if r is None or not self.rec:
                return
            ref = r["notes"]
            t_a, t_b = r["t0"] - 0.4, r["t1"] + 0.4
            T, M = self.rec["T"], self.rec["M"]
            sel = (T >= t_a) & (T <= t_b)
            frames = [(t, None if np.isnan(m) else m, None, None, "ref", 0) for t, m in zip(T[sel], M[sel])]
        ms = [m for _, _, m in ref] + [f[1] for f in (frames or []) if f[1] is not None and t_a <= f[0] <= t_b]
        lo, hi = min(ms) - 1.5, max(ms) + 1.5
        if hi - lo < 7:
            c = (hi + lo) / 2
            lo, hi = c - 3.5, c + 3.5
        X = lambda t: L + (t - t_a) / (t_b - t_a) * (W - L - 6)
        Y = lambda m: H - 8 - (m - lo) / (hi - lo) * (H - 16)
        for n in range(int(np.floor(lo)), int(np.ceil(hi)) + 1):
            cv.create_line(L, Y(n), W, Y(n), fill="#1d1a16")
            cv.create_text(L - 4, Y(n), text=E.note_name(n), anchor="e", fill="#5d544a", font=("Segoe UI", 8))
        if lo < E.TOP_LIMIT + 0.5 < hi + 1:
            cv.create_line(L, Y(E.TOP_LIMIT + 0.5), W, Y(E.TOP_LIMIT + 0.5), fill="#7a2e2a", dash=(4, 4))
        zones = {}
        if self.result:
            zones = {k: r["zone"] for k, r in enumerate(self.result[1]["notes"])}
        zc = {"hit": ZONE["green"], "close": ZONE["yellow"], "missed": ZONE["red"]}
        for k, (a, b, m) in enumerate(ref):
            col = zc.get(zones.get(k), GOLD)
            cv.create_line(X(a), Y(m), X(b), Y(m), fill=col, width=4)
        if frames:
            run = []
            ref_line = frames and frames[0][4] == "ref"
            for f in frames:
                if f[1] is None or not (t_a <= f[0] <= t_b):
                    if len(run) >= 4:
                        cv.create_line(*run, fill="#8a6a3c" if ref_line else "#7fd1ff", width=2)
                    run = []
                    continue
                run += [X(f[0]), Y(f[1])]
            if len(run) >= 4:
                cv.create_line(*run, fill="#8a6a3c" if ref_line else "#7fd1ff", width=2)
        if now is not None:
            cv.create_line(X(now), 4, X(now), H - 4, fill="#3a3128")

    # -- CI ------------------------------------------------------------------------
    def autotest(self, sim, then):
        """CI: a synthetic 'mawwal' with a known run → detect, practise at 75% −2
        semitones, sing after it, then one drill; screenshots on the way."""
        a = self.app
        a.nb.select(2)
        folder = os.path.join(a.shots, "عُرَب تجربة")
        path, truth = make_test_recording(folder)
        self.truth = truth
        self.auto_then = None

        def loaded():
            self.tree.selection_set("0")
            self.select()
            a.root.after(900, lambda: a.shot("08-runs-list"))
            self.pv["speed"].set("75")
            self.pv["semis"].set("-2")
            self.auto_then = after_run
            a.root.after(1500, lambda: self.sing("after", sim=sim))
            a.root.after(5200, lambda: a.shot("09-runs-singing"))

        def after_run():
            self.dv["maq"].set("Hijaz")
            self.dv["kind"].set("turn4")
            self.dv["start"].set("D3")
            self.dcb["maq"].current(self.dcb["maq"].keys.index("Hijaz"))
            self.dcb["kind"].current(self.dcb["kind"].keys.index("turn4"))
            self.dcb["start"].current(self.dcb["start"].keys.index("D3"))
            self.auto_then = done
            self.drill(sim=sim)

        def done():
            self.auto_then = None
            a.auto_runs = {"runs": [{"t0": r["t0"], "t1": r["t1"], "n": len(r["notes"]), "rate": r["rate"],
                                     "notes": [round(n[2], 2) for n in r["notes"]]} for r in self.runs],
                           "maqam": self.rec["maqam"], "rubberband": RU.has_rubberband(),
                           "drill_nps": a.cfg.get("drill_nps", {})}
            then()
        a.b["blind"].set(False)
        self.rate.set(4.0)
        self.load(path, then=loaded)


# ------------------------------------------------------------- «النَفَس» tab
class BreathTab:
    def __init__(self, app):
        self.app = app
        a = app
        f = ttk.Frame(a.nb, padding=10)
        a.nb.add(f, text="  النَفَس  ")
        self.f = f
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        ttk.Label(f, text="⚠  " + BR.SAFETY, foreground="#e8c547", font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="e")
        row = ttk.Frame(f)
        row.grid(row=1, column=0, sticky="ew", pady=6)
        for i in range(3):
            row.columnconfigure(i, weight=1)
        hist = E.load_history()
        # hold
        h = ttk.LabelFrame(row, text="  امسك النوتة  ", padding=8)
        h.grid(row=0, column=2, sticky="nsew", padx=4)
        self.note_v = tk.StringVar(value=a.cfg.get("hold_note") or E.note_name(BR.suggest_note(hist)))
        cb = ttk.Combobox(h, textvariable=self.note_v, state="readonly", width=6, values=E.note_choices(43, E.TOP_LIMIT))
        cb.grid(row=0, column=0, sticky="w")
        ttk.Label(h, text="النوتة").grid(row=0, column=1, sticky="e")
        ttk.Button(h, text="اقترح", command=lambda: self.note_v.set(E.note_name(BR.suggest_note(E.load_history())))).grid(row=0, column=2, padx=4)
        ttk.Button(h, text="▶ ابدأ", style="Go.TButton", command=self.hold).grid(row=1, column=0, columnspan=3, sticky="ew", pady=4)
        self.hold_lb = ttk.Label(h, text="", style="Hint.TLabel", justify="right")
        self.hold_lb.grid(row=2, column=0, columnspan=3, sticky="e")
        # sss
        x = ttk.LabelFrame(row, text="  «سسس» — شهيق · حبس · زفير  ", padding=8)
        x.grid(row=0, column=1, sticky="nsew", padx=4)
        self.pat_v = tk.StringVar(value=a.cfg.get("sss_pattern", "4-4-8"))
        ttk.Combobox(x, textvariable=self.pat_v, state="readonly", width=8, values=[k for k, _ in BR.PATTERNS]).grid(row=0, column=0, sticky="w")
        ttk.Label(x, text="النمط").grid(row=0, column=1, sticky="e")
        self.rounds_v = tk.StringVar(value=str(a.cfg.get("sss_rounds", "3")))
        tk.Spinbox(x, from_=1, to=8, textvariable=self.rounds_v, width=3, bg="#1a1714", fg=TEXT, buttonbackground="#2a241d").grid(row=1, column=0, sticky="w")
        ttk.Label(x, text="مرات").grid(row=1, column=1, sticky="e")
        ttk.Button(x, text="▶ ابدأ", style="Go.TButton", command=self.sss).grid(row=2, column=0, columnspan=2, sticky="ew", pady=4)
        self.sss_lb = ttk.Label(x, text="", style="Hint.TLabel", justify="right")
        self.sss_lb.grid(row=3, column=0, columnspan=2, sticky="e")
        # phrase
        p = ttk.LabelFrame(row, text="  جملة في نَفَس واحد  ", padding=8)
        p.grid(row=0, column=0, sticky="nsew", padx=4)
        ttk.Label(p, text="الجملة = التمرين المختار في «العُرَب» (بسرعة 2.5 نوتة/ث)", style="Hint.TLabel").grid(row=0, column=0, sticky="e")
        ttk.Button(p, text="▶ ابدأ", style="Go.TButton", command=self.phrase).grid(row=1, column=0, sticky="ew", pady=4)
        self.ph_lb = ttk.Label(p, text="", style="Hint.TLabel", justify="right")
        self.ph_lb.grid(row=2, column=0, sticky="e")
        self.cv = tk.Canvas(f, bg=PANEL, highlightthickness=0, height=230)
        self.cv.grid(row=2, column=0, sticky="nsew")
        ttk.Label(f, text="آخر 7 أيام", foreground=GOLD, font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="e", pady=(6, 0))
        self.wk = tk.Canvas(f, bg=PANEL, highlightthickness=0, height=130)
        self.wk.grid(row=4, column=0, sticky="ew")
        self.wk.bind("<Configure>", lambda e: self.week())
        self.refresh()

    def refresh(self):
        hh = E.load_history()
        b = BR.best(hh, "hold", "held_s")
        self.hold_lb.configure(text="أحسن رقم: %s" % ("—" if b is None else "%.1fs" % b))
        b = BR.best(hh, "sss", "hiss_s")
        self.sss_lb.configure(text="أطول «سسس»: %s" % ("—" if b is None else "%.1fs" % b))
        self.ph_lb.configure(text="الهدف النهارده: %d مرات  ·  أحسن: %s" % (BR.phrase_target(hh), BR.best(hh, "phrase", "reps") or "—"))
        self.week()

    def phrase_notes(self):
        rt = self.app.runs_tab
        return RU.drill_notes(rt.dv["maq"].get(), rt.dv["kind"].get(), E.parse_note(rt.dv["start"].get()), 2.5)

    # -- start ---------------------------------------------------------------------
    def hold(self, sim=None):
        if not self.app.eng:
            self.app.start(sim=sim, plan=BR.plan_hold(E.parse_note(self.note_v.get())))

    def sss(self, sim=None):
        if not self.app.eng:
            self.app.cfg["sss_pattern"] = self.pat_v.get()
            try:
                n = max(1, min(8, int(self.rounds_v.get())))
            except ValueError:
                n = 3
            self.app.start(sim=sim, plan=BR.plan_sss(dict(BR.PATTERNS)[self.pat_v.get()], n))

    def phrase(self, sim=None):
        if self.app.eng:
            return
        try:
            notes = self.phrase_notes()
        except E.PlanError:
            messagebox.showwarning("PitchTrainer", "التمرين المختار عالي — غيّر القرار في «العُرَب»")
            return
        self.app.start(sim=sim, plan=BR.plan_phrase(notes, BR.phrase_target(E.load_history())))

    # -- live + results ----------------------------------------------------------------
    def live(self):
        a = self.app
        s = a.session
        with a.eng.lock:
            now = s.now_a
            frames = list(s.frames[-2500:])
            H = list(s.H[-60:])
        cv = self.cv
        cv.delete("all")
        W, H_ = max(cv.winfo_width(), 300), max(cv.winfo_height(), 150)
        if s.mode == "sss":
            ph = [p for p in s.plan.phases if p[1] <= now < p[2]]
            if ph:
                kind, t0, t1, r = ph[0]
                left = t1 - now
                txt = {"in": "شهيق", "hold": "احبس", "out": "زفير «سسس»"}[kind]
                col = {"in": "#3987e5", "hold": GOLD, "out": "#4cc38a"}[kind]
                cv.create_text(W / 2, H_ / 2 - 40, text=txt, fill=col, font=("Segoe UI", 26, "bold"))
                cv.create_text(W / 2, H_ / 2 + 20, text="%d" % int(np.ceil(left)), fill=TEXT, font=("Segoe UI", 40, "bold"))
                cv.create_rectangle(W * 0.2, H_ - 30, W * 0.2 + W * 0.6 * (now - t0) / (t1 - t0), H_ - 18, fill=col, width=0)
                cv.create_text(W - 10, 14, text="%d / %d" % (r + 1, s.plan.phases[-1][3] + 1), anchor="e", fill=MUTED)
            else:
                cv.create_text(W / 2, H_ / 2, text="استريح…", fill=MUTED, font=("Segoe UI", 20, "bold"))
            if H:
                hf = H[-1][2]
                cv.create_rectangle(10, H_ - 30, 10 + 100 * hf, H_ - 18, fill="#4cc38a" if hf > 0.45 else "#5a4a35", width=0)
                cv.create_text(10, H_ - 40, text="sss", anchor="w", fill=MUTED, font=("Segoe UI", 8))
            return
        st = s.steps[0]
        ph, _ = s.phase_at(now)
        a.phase_lb.configure(text={"listen": "اسمع  ♪", "sing": "دورك  ●", "gap": "خد نَفَس…"}.get(ph, ""))
        v = [f for f in frames if f[1] is not None and f[0] >= st.sing0]
        held = (v[-1][0] - v[0][0]) if v else 0.0
        cv.create_text(W - 20, 30, text="%.1f s" % held, anchor="e", fill=TEXT, font=("Segoe UI", 30, "bold"))
        if s.mode == "phrase":
            reps = int((held + 0.15) // s.plan.phrase_s)
            cv.create_text(20, 30, text="%d / %d" % (reps, s.plan.info["target"]), anchor="w", fill=GOLD, font=("Segoe UI", 30, "bold"))
        t_a = max(st.sing0, now - 12)
        ms = [f[1] for f in v] or [st.segs[0].m0]
        lo, hi = min(ms + [st.segs[0].m0]) - 1, max(ms + [st.segs[0].m0]) + 1
        X = lambda t: 40 + (t - t_a) / 12.5 * (W - 60)
        Y = lambda m: H_ - 10 - (m - lo) / (hi - lo) * (H_ - 80)
        if s.mode == "hold":
            y = Y(st.segs[0].m0)
            cv.create_line(40, y, W - 10, y, fill=GOLD, width=2)
        pts = []
        for f in v:
            if f[0] >= t_a:
                pts += [X(f[0]), Y(f[1])]
        if len(pts) >= 4:
            cv.create_line(*pts, fill="#7fd1ff", width=2)

    def finished(self, sess, dur):
        hh = E.load_history()
        e = {"date": datetime.date.today().strftime("%Y-%m-%d"), "time": datetime.datetime.now().strftime("%H:%M"),
             "mode": sess.mode, "duration_s": round(dur, 1)}
        msg = ""
        if sess.mode == "hold":
            st = sess.steps[0]
            r = BR.analyse_hold(np.array(sess.T) - sess.lat, sess.M, sess.D, st.sing0, st.segs[0].m0)
            if r:
                prev = BR.best(hh, "hold", "held_s")
                e.update(r, note=sess.info["note"])
                why = {"pitch": "الطبقة بدأت تقع", "level": "الصوت بدأ يضعف", "shake": "الصوت بدأ يهتز", None: "ثابت للآخر"}[r["why"]]
                msg = "مسكت %.1f ث  ·  ثابت لحد %.1f ث (%s)%s" % (r["held_s"], r["steady_s"], why,
                                                                 "  ·  رقم جديد!" if prev is None or r["held_s"] > prev else "")
        elif sess.mode == "sss":
            rs = BR.analyse_sss(sess.H, sess.plan.phases)
            if rs:
                e.update(pattern=sess.info["pattern"], rounds=rs, hiss_s=max(r_["hiss_s"] for r_ in rs),
                         steady=round(float(np.mean([r_["steady"] for r_ in rs if r_["steady"] is not None] or [0])), 0))
                msg = "  ·  ".join("%d: %.1f/%.0f ث" % (r_["round"], r_["hiss_s"], r_["target_s"]) for r_ in rs) + \
                      "   ·  الثبات %d%%" % e["steady"]
        else:
            st = sess.steps[0]
            r = BR.analyse_phrase(np.array(sess.T) - sess.lat, sess.M, st.sing0, sess.plan.phrase_s)
            e.update(r, target=sess.info["target"])
            msg = "%d مرات في نَفَس واحد (%.1f ث)  ·  الهدف %d%s" % (r["reps"], r["sung_s"], e["target"],
                                                                   "  ✔" if r["reps"] >= e["target"] else "")
        if len(e) > 5:
            E.save_session(e)
        self.app.phase_lb.configure(text="خلصت ✔")
        self.cv.delete("all")
        W = max(self.cv.winfo_width(), 300)
        self.cv.create_text(W / 2, 60, text=msg or "مفيش صوت اتسجّل", fill=TEXT, font=("Segoe UI", 13, "bold"), width=W - 40)
        self.refresh()
        if self.app.autotest:
            self.app.auto_results.append(e)
            self.app.shot("%02d-%s-result" % (len(self.app.auto_results), sess.mode))
            if self.auto_then:
                self.app.root.after(1200, self.auto_then)

    def week(self):
        cv = self.wk
        cv.delete("all")
        W, H = max(cv.winfo_width(), 600), 130
        hh = E.load_history()
        charts = [("النوتة (ث)", BR.weekly(hh, "hold", "held_s"), "#3987e5"), ("«سسس» (ث)", BR.weekly(hh, "sss", "hiss_s"), "#199e70"),
                  ("مرات الجملة", BR.weekly(hh, "phrase", "reps"), GOLD)]
        cw = W / 3
        for i, (title, rows, col) in enumerate(reversed(charts)):
            x0 = i * cw + 10
            cv.create_text(x0 + cw - 20, 10, text=title, anchor="e", fill=MUTED, font=("Segoe UI", 9, "bold"))
            vals = [v for _, v in rows if v is not None]
            top = max(vals + [1]) * 1.15
            bw = (cw - 30) / 7
            for k, (d, v) in enumerate(rows):
                x = x0 + k * bw
                if v is not None:
                    h_ = (H - 40) * v / top
                    cv.create_rectangle(x + 3, H - 18 - h_, x + bw - 3, H - 18, fill=col, width=0)
                    cv.create_text(x + bw / 2, H - 24 - h_, text=("%.0f" % v) if v >= 10 else ("%.1f" % v if isinstance(v, float) else str(v)),
                                   fill=TEXT, font=("Segoe UI", 8))
                cv.create_text(x + bw / 2, H - 8, text=d[8:], fill=MUTED, font=("Segoe UI", 8))

    # -- CI ------------------------------------------------------------------------------
    def autotest(self, then):
        a = self.app
        a.nb.select(3)
        lat = a.autotest["sim_lat"]
        self.auto_then = None

        def hold_sim(plan):
            st = plan.steps[0]
            n = int((st.sing0 + 16) * E.SR)
            tr = np.full(n, np.nan)
            i0, L = int((st.sing0 + 0.3 + lat) * E.SR), int(12 * E.SR)
            u = np.arange(L) / E.SR
            tr[i0:i0 + L] = st.segs[0].m0 + np.where(u > 8, -(u - 8) * 0.2, 0)
            return E.synth_voice(tr, vibrato_cents=15, seed=2)

        def sss_sim(plan):
            return BR.hiss_signal(plan.phases, [5.4, 6.0], lat=lat)

        def phrase_sim(plan):
            st = plan.steps[0]
            k = sum(1 for g in st.segs if g.t1 <= 3 * plan.phrase_s + 1e-6)
            one = E.Step(0, st.root, st.segs[:k], listen=False)
            one.sing0, one.sing1 = st.sing0, st.sing0 + one.dur
            tr = E.singer_track([one], st.sing0 + one.dur + 3, late=lat, gap=0.0)
            return E.synth_voice(tr, vibrato_cents=8, seed=3)

        def step2():
            self.auto_then = step3
            self.pat_v.set("4-0-6")
            self.rounds_v.set("2")
            self.sss(sim=sss_sim)
            a.root.after(3500, lambda: a.shot("12-sss-live"))

        def step3():
            self.auto_then = done
            self.phrase(sim=phrase_sim)
            a.root.after(9000, lambda: a.shot("13-phrase-live"))

        def done():
            self.auto_then = None
            a.root.after(800, lambda: (a.shot("14-breath-week"), a.root.after(1500, then)))

        self.auto_then = step2
        self.note_v.set("G3")
        self.hold(sim=hold_sim)
        a.root.after(7000, lambda: a.shot("11-hold-live"))


# ------------------------------------------------------------- «تسخين» tab
class WarmupTab:
    def __init__(self, app):
        self.app = app
        a = app
        cfg = a.cfg
        f = ttk.Frame(a.nb, padding=10)
        a.nb.insert(0, f, text="  تسخين  ")
        self.f = f
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)
        self.mixer = None
        self.job = None
        self.t0 = None
        self.marks = []
        top = ttk.Frame(f)
        top.grid(row=0, column=0, sticky="e")
        self.wv = {"start": tk.StringVar(value=cfg.get("warm_start", "G2")), "top": tk.StringVar(value=cfg.get("warm_top", "D4")),
                   "len": tk.StringVar(value=cfg.get("warm_len", "normal")), "ref": tk.StringVar(value=cfg.get("warm_ref", "oud"))}
        self.cbs = {}
        for k, lab, pairs in (("start", "القرار", [(n, n) for n in E.note_choices(40, 55)]),
                              ("top", "أعلى نوتة", [(n, n) for n in E.note_choices(55, E.TOP_LIMIT)]),
                              ("len", "الطول", [("short", "قصير (4 دقايق)"), ("normal", "عادي (7 دقايق)")]),
                              ("ref", "صوت المرجع", [("oud", "عود"), ("piano", "بيانو")])):
            ttk.Label(top, text=lab).pack(side="right", padx=(10, 4))
            cb = ttk.Combobox(top, state="readonly", width=14 if k == "len" else 7, values=[t for _, t in pairs])
            keys = [x for x, _ in pairs]
            cb.current(keys.index(self.wv[k].get()) if self.wv[k].get() in keys else 0)
            cb.bind("<<ComboboxSelected>>", lambda e, k=k, cb=cb, keys=keys: (self.wv[k].set(keys[cb.current()]), self.fill()))
            cb.pack(side="right")
            cb.keys = keys
            self.cbs[k] = cb
        self.tree = ttk.Treeview(f, columns=("n", "d", "h"), show="headings", height=6)
        for k, t, w in (("n", "التمرين", 170), ("d", "المدة", 70), ("h", "إزاي", 560)):
            self.tree.heading(k, text=t)
            self.tree.column(k, width=w, anchor="e" if k == "h" else "center")
        self.tree.grid(row=1, column=0, sticky="ew", pady=8)
        self.cv = tk.Canvas(f, bg=PANEL, highlightthickness=0, height=220)
        self.cv.grid(row=2, column=0, sticky="nsew")
        bt = ttk.Frame(f)
        bt.grid(row=3, column=0, sticky="e", pady=8)
        ttk.Button(bt, text="افتح الفولدر", command=lambda: (os.makedirs(WU.out_dir(), exist_ok=True), open_path(WU.out_dir()))).pack(side="right", padx=3)
        ttk.Button(bt, text="نزّل MP3  ⬇", command=self.export).pack(side="right", padx=3)
        ttk.Button(bt, text="■ وقّف", command=self.stop).pack(side="right", padx=3)
        ttk.Button(bt, text="▶ شغّل الروتين", style="Go.TButton", command=self.play).pack(side="right", padx=3)
        self.msg = ttk.Label(f, text="", style="Hint.TLabel")
        self.msg.grid(row=4, column=0, sticky="e")
        self.fill()

    def parts(self):
        return WU.routine(E.parse_note(self.wv["start"].get()), E.parse_note(self.wv["top"].get()), self.wv["len"].get())

    def fill(self):
        self.tree.delete(*self.tree.get_children())
        ps = self.parts()
        for i, p in enumerate(ps):
            self.tree.insert("", "end", iid=str(i), values=(p.name, "%d:%02d" % divmod(int(round(p.dur())), 60), p.how))
        tot = sum(p.dur() for p in ps)
        self.msg.configure(text="الروتين كله %d:%02d — مفيش نوتة فوق %s" % (int(tot) // 60, int(tot) % 60, self.wv["top"].get()))
        self.draw(None)

    def play(self):
        if self.app.eng:
            return
        self.stop()
        y, self.marks = WU.render(self.parts(), self.wv["ref"].get())
        self.mixer = Mixer(enabled=not self.app.autotest)
        self.mixer.play(y)
        self.t0 = time.time()
        self.total = len(y) / E.SR
        self.tick()

    def stop(self):
        if self.job:
            self.app.root.after_cancel(self.job)
            self.job = None
        if self.mixer:
            self.mixer.stop()
            self.mixer = None
        self.t0 = None
        self.draw(None)

    def tick(self):
        if self.t0 is None:
            return
        el = time.time() - self.t0
        if el > self.total:
            self.stop()
            self.msg.configure(text="خلص التسخين ✔ — يلا غنّي")
            return
        self.draw(el)
        self.job = self.app.root.after(200, self.tick)

    def draw(self, el):
        cv = self.cv
        cv.delete("all")
        W, H = max(cv.winfo_width(), 400), max(cv.winfo_height(), 160)
        if el is None:
            cv.create_text(W / 2, H / 2, text="دوس «شغّل الروتين» — قبل كل تمرين سكتة للنَفَس وتسقيف خفيف", fill=MUTED, font=("Segoe UI", 12))
            return
        cur = [(i, p, a, b) for i, (p, a, b) in enumerate(self.marks) if a <= el < b]
        if cur:
            i, p, a, b = cur[0]
            self.tree.selection_set(str(i))
            breath = el < a + WU.BREATH_GAP
            cv.create_text(W - 20, 34, text=p.name, anchor="e", fill=GOLD, font=("Segoe UI", 20, "bold"))
            cv.create_text(W - 20, 74, text="خد نَفَس…" if breath else p.how, anchor="e", fill=TEXT, font=("Segoe UI", 12), width=W - 60)
            cv.create_text(20, 34, text="%d:%02d" % divmod(int(b - el), 60), anchor="w", fill=TEXT, font=("Segoe UI", 20, "bold"))
            cv.create_rectangle(20, H - 50, 20 + (W - 40) * (el - a) / (b - a), H - 40, fill=GOLD, width=0)
        cv.create_rectangle(20, H - 24, 20 + (W - 40) * el / self.total, H - 18, fill="#4cc38a", width=0)
        cv.create_text(W / 2, H - 8, text="%d:%02d / %d:%02d" % (int(el) // 60, int(el) % 60, int(self.total) // 60, int(self.total) % 60),
                       fill=MUTED, font=("Segoe UI", 8))

    def export(self):
        self.msg.configure(text="بعمل الملف…")
        self.app.root.update_idletasks()
        try:
            p = WU.export_mp3(self.parts(), self.wv["ref"].get(), self.wv["len"].get())
        except Exception as ex:
            crash_log(traceback.format_exc())
            messagebox.showerror("PitchTrainer", str(ex)[:500])
            return None
        self.msg.configure(text="اتحفظ: " + p)
        return p


def best_speed(history, key):
    v = [e["nps"] for e in history if e.get("key") == key and e.get("mode") in ("runs", "drill") and (e.get("score") or 0) >= 80]
    return max(v) if v else None


TEST_RUN = [0, 1, 2, 3, 4, 3, 2, 1, 0, 1, 2]      # Ajam degrees, 7 notes/s


def make_test_recording(folder):
    """CI/self-test: a short 'mawwal' (held notes, then a 7 n/s Ajam run on E3,
    then a held tonic) over a keyboard pad — as MP3 through ffmpeg."""
    os.makedirs(folder, exist_ok=True)
    tonic, up = 52.0, E.MAQAMAT["Ajam"]["up"]
    seq = [(0.6, 1.8, tonic), (1.8, 2.8, tonic + 4)]
    t = 2.8
    run = []
    for d in TEST_RUN:
        run.append((t, t + 1 / 7.0, tonic + up[d] / 100.0))
        t += 1 / 7.0
    seq += run + [(t, t + 1.6, tonic)]
    y = RU.synth_run(seq, seed=3, vib=6)
    pad = np.zeros(len(y))
    tt = np.arange(len(y)) / E.SR
    for m in (40.0, 47.0, 52.0):
        f = E.midi_to_hz(m)
        pad += 0.04 * np.sin(2 * np.pi * f * tt) * (1 + 0.4 * np.sin(2 * np.pi * 2 * f * tt))
    wav = os.path.join(folder, "موال تجربة.wav")
    G.write_wav(wav, np.stack([y + pad, y + pad], 1))
    mp3 = os.path.join(folder, "موال تجربة.mp3")
    ff = G.ffmpeg_path()
    if ff:
        kw = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        subprocess.run([ff, "-y", "-v", "error", "-i", wav, "-b:a", "192k", mp3], stdin=subprocess.DEVNULL, **kw)
    return (mp3 if os.path.isfile(mp3) else wav), run


def main():
    autotest = None
    if len(sys.argv) >= 4 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = {"out": sys.argv[2], "shots": sys.argv[3], "sim_lat": 0.12,
                    "stem": sys.argv[4] if len(sys.argv) > 4 else "", "mix": sys.argv[5] if len(sys.argv) > 5 else ""}
    root = tk.Tk()
    root.minsize(980, 680)
    if sys.platform == "win32":
        try:
            root.state("zoomed")
        except tk.TclError:
            pass
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
