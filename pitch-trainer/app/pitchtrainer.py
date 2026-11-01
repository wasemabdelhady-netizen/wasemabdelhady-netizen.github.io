"""PitchTrainer — window around engine/session/analysis (live mic pitch, exercises,
maqam, drone, call & response, song, onset, stability, leaps, ajnas)."""
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
    from tkinter import filedialog, messagebox, ttk
    import numpy as np
    import analysis as A
    import engine as E
    import oud
    import report
    import session as S
    import song as G
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
            if st.listen:
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

        outer = ttk.Frame(root, padding=10)
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
        d = {k: v.get() for k, v in self.v.items()}
        d.update({k: v.get() for k, v in self.b.items()})
        if self.cfg.get("lat_ms") is not None:
            d["lat_ms"] = self.cfg["lat_ms"]
        self.cfg = d
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False)
        except OSError:
            pass

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
        self.session = S.Session(plan, lat)
        rec = None
        self.wav = None
        if self.b["rec"].get() and plan.mode != "calib":
            self.wav = E.recording_path("%s %s" % (plan.mode, self.v["start"].get()))
            rec = E.WavRecorder(self.wav)
        self.blind = self.b["blind"].get() and plan.mode not in ("calib", "free")
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
            cv = tk.Canvas(f, bg=PANEL, height=200, highlightthickness=0)
            cv.pack(fill="x", pady=(2, 10))
            cv.update_idletasks()
            self.lines(cv, last, key, fmt, groups, refs, pct=(key == "exact_pct"))
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
        W, H = max(cv.winfo_width(), 600), 200
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
            if mode == "stability":
                kw.update(offset_cents=6.0, drift=(-30.0, 1.5), scoop=None)
            tr = E.singer_track(plan.steps, end, **kw)
            self.auto_truth = offs
            return E.synth_voice(tr, vibrato_cents=25.0 if mode == "stability" else 12.0, seed=seed, noise=0.006)
        return make

    def auto_next(self):
        plans = self.auto_plans()
        i = self.auto_i = getattr(self, "auto_i", -1) + 1
        if i >= len(plans):
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
        out = {"sessions": self.auto_results, "calib": self.calib, "report": rp,
               "history_days": E.daily(E.load_history()), "history_file": E.HISTORY_FILE,
               "sim_lat": self.autotest["sim_lat"]}
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=1, default=float)


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
