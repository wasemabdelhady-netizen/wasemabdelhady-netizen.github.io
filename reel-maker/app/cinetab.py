"""«سينمائي - Cinematic» tab: footage + master + lyrics -> analyse -> edit reel list -> render."""
import json
import os
import queue
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import cinematic
    import cine_edit
    from cine_look import GREEN_DEFAULT
    CINE_ERR = None
except Exception:  # missing package in the bundle: the other tabs still work
    cinematic = cine_edit = None
    GREEN_DEFAULT = 0.86
    CINE_ERR = traceback.format_exc()

VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".mts", ".avi")
AUDIO_EXT = (".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a")
STEM_HINTS = ("vocal", "voc", "stem", "acap", "فوكال", "صوت")
VIDEO_TYPES = [("Video", " ".join("*" + e for e in VIDEO_EXT)), ("All", "*.*")]
AUDIO_TYPES = [("Audio", " ".join("*" + e for e in AUDIO_EXT)), ("All", "*.*")]
TEXT_TYPES = [("Text", "*.txt"), ("All", "*.*")]
SRT_TYPES = [("Subtitles", "*.srt"), ("All", "*.*")]


def fmt(t):
    t = max(0.0, float(t))
    return "%d:%05.2f" % (int(t // 60), t - 60 * int(t // 60))


def parse_t(s):
    s = s.strip().replace(",", ".")
    if not s:
        raise ValueError("وقت فاضي")
    sec = 0.0
    for p in s.split(":"):
        sec = sec * 60 + float(p)
    return sec


def eta_text(t0, f):
    if f < 0.03 or f >= 0.999:
        return ""
    left = (time.time() - t0) * (1 - f) / f
    m, s = int(left // 60), int(left % 60)
    return "  —  فاضل تقريباً %d:%02d" % (m, s)


class CineTab:
    def __init__(self, root, parent, settings, save_settings, crash_log, open_path, autotest=None):
        self.root = root
        self.settings = settings.setdefault("cine", {})
        self.save_settings = save_settings
        self.crash_log = crash_log
        self.open_path = open_path
        self.autotest = autotest
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.project = None
        self.sections = []
        self.t0 = time.time()

        st = ttk.Style()
        st.configure("Cine.TButton", background="#e0a458", foreground="#0a0a0a",
                     font=("Segoe UI", 11, "bold"), padding=5)
        st.map("Cine.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("TSpinbox", fieldbackground="#1a1714", foreground="#f5efe6", arrowcolor="#f5efe6")
        f = ttk.Frame(parent, padding=(12, 8))
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="سينمائي  ·  ريلز متقطّعة من لقطات الغُنا", style="Title.TLabel").grid(
            row=0, column=1, columnspan=2, sticky="e", pady=(0, 4))
        ttk.Label(f, text="اسحب الملفات وارميها هنا أو اختارها بالزرار",
                  style="Hint.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        self.v = {k: tk.StringVar() for k in ("footage", "master", "stem", "lyrics", "srt", "song", "out")}
        r = 2
        r = self.file_row(f, r, "الفيديو (Footage)", "footage", VIDEO_TYPES)
        r = self.file_row(f, r, "الماستر WAV", "master", AUDIO_TYPES)
        r = self.file_row(f, r, "الفوكال ستيم (اختياري)", "stem", AUDIO_TYPES)
        r = self.file_row(f, r, "الكلمات .txt", "lyrics", TEXT_TYPES)
        r = self.file_row(f, r, "توقيت تقريبي .srt (اختياري)", "srt", SRT_TYPES)
        r = self.file_row(f, r, "فولدر الريلز", "out", None, folder=True)

        opts = ttk.Frame(f)
        opts.grid(row=r, column=0, columnspan=3, sticky="ew", pady=2)
        self.n_reels = tk.IntVar(value=4)
        self.ig = tk.BooleanVar(value=False)
        self.glare = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="شيل لمعة النضارة", variable=self.glare).pack(side="right", padx=8)
        ttk.Checkbutton(opts, text="نسخة إنستجرام/فيسبوك كمان", variable=self.ig).pack(side="right", padx=8)
        ttk.Spinbox(opts, from_=1, to=12, width=4, textvariable=self.n_reels, justify="center").pack(side="right")
        ttk.Label(opts, text="عدد الريلز").pack(side="right", padx=(12, 4))
        ttk.Entry(opts, textvariable=self.v["song"], justify="right", width=16).pack(side="right", padx=4)
        ttk.Label(opts, text="اسم الأغنية").pack(side="right")
        self.go_an = ttk.Button(opts, text="١ — حلّل  ▶", style="Cine.TButton", command=self.analyze)
        self.go_an.pack(side="left", fill="x", expand=True, padx=(0, 10))
        r += 1

        # green gain of the grade, remembered per footage
        grow = ttk.Frame(f)
        grow.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.green = tk.DoubleVar(value=GREEN_DEFAULT)
        self.green_lbl = ttk.Label(grow, text="%.2f" % GREEN_DEFAULT, width=5)
        self.green_lbl.pack(side="right")
        ttk.Scale(grow, from_=1.00, to=0.80, variable=self.green, orient="horizontal", length=220,
                  command=lambda v: self.green_lbl.configure(text="%.2f" % float(v))).pack(side="right", padx=6)
        ttk.Label(grow, text="تقليل الأخضر").pack(side="right")
        ttk.Button(grow, text="الافتراضي %.2f" % GREEN_DEFAULT, command=lambda: self.set_green(GREEN_DEFAULT)).pack(side="right", padx=6)
        ttk.Label(grow, text="أقل = أخضر أقل في الصورة (v5 = 0.86)", style="Hint.TLabel").pack(side="left")
        r += 1

        # sync + words row
        sync = ttk.Frame(f)
        sync.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        self.off_var = tk.StringVar(value="")
        self.conf_lbl = ttk.Label(sync, text="", style="Hint.TLabel")
        ttk.Button(sync, text="words.json", command=self.open_words).pack(side="left", padx=1)
        ttk.Button(sync, text="أعد التحميل", command=self.reload_words).pack(side="left", padx=1)
        ttk.Button(sync, text="توقيت من الأول", command=lambda: self.analyze(redo=True)).pack(side="left", padx=1)
        self.conf_lbl.pack(side="left", padx=6)
        for d in (0.1, 0.01, -0.01, -0.1):
            ttk.Button(sync, text="%+.2f" % d, width=5, command=lambda d=d: self.nudge(d)).pack(side="right", padx=1)
        ttk.Entry(sync, textvariable=self.off_var, width=9, justify="center").pack(side="right", padx=4)
        ttk.Label(sync, text="الفيديو بيبدأ عند ثانية:").pack(side="right")
        r += 1

        # reels table
        cols = ("on", "name", "start", "end", "len", "style", "first")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=4, selectmode="browse")
        for c, t, w in (("on", "✔", 34), ("name", "الريل", 70), ("start", "من", 70), ("end", "لحد", 70),
                        ("len", "الطول", 55), ("style", "الستايل", 170), ("first", "أول سطر", 300)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="e" if c == "first" else "center", stretch=c == "first")
        self.tree.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=4)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.toggle())
        f.rowconfigure(r, weight=1, minsize=120)
        r += 1
        ed = ttk.Frame(f)
        ed.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.e_start, self.e_end, self.e_style, self.e_hook = (tk.StringVar() for _ in range(4))
        names = [cine_edit.STYLE_NAMES[k] for k in "ABCD"] if cine_edit else []
        ttk.Button(ed, text="طبّق", command=self.apply_row).pack(side="left")
        ttk.Button(ed, text="✔ / ✖", command=self.toggle).pack(side="left", padx=4)
        ttk.Entry(ed, textvariable=self.e_hook, width=16, justify="right").pack(side="right", padx=2)
        ttk.Label(ed, text="هوك IG (اختياري)").pack(side="right")
        ttk.Combobox(ed, textvariable=self.e_style, values=names, state="readonly", width=19).pack(side="right", padx=4)
        ttk.Entry(ed, textvariable=self.e_end, width=8, justify="center").pack(side="right", padx=2)
        ttk.Label(ed, text="لحد").pack(side="right")
        ttk.Entry(ed, textvariable=self.e_start, width=8, justify="center").pack(side="right", padx=2)
        ttk.Label(ed, text="من").pack(side="right")
        r += 1

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(6, 2))
        btns.columnconfigure(0, weight=1)
        self.go_r = ttk.Button(btns, text="٢ — اعمل الريلز  ▶", style="Cine.TButton", command=self.render)
        self.go_r.grid(row=0, column=0, sticky="ew")
        self.go_p = ttk.Button(btns, text="معاينة ٥ ث", command=self.preview)
        self.go_p.grid(row=0, column=1, padx=(8, 0))
        self.stop = ttk.Button(btns, text="إلغاء", command=self.cancel.set, state="disabled")
        self.stop.grid(row=0, column=2, padx=(8, 0))
        ttk.Button(btns, text="افتح فولدر الريلز", command=self.open_out).grid(row=0, column=3, padx=(8, 0))
        r += 1
        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew", pady=2)
        r += 1
        self.status = ttk.Label(f, text="جاهز", style="Hint.TLabel", anchor="e")
        self.status.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1
        self.logbox = tk.Text(f, height=2, bg="#141210", fg="#d9cfc0", relief="flat", font=("Consolas", 9), wrap="word")
        self.logbox.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(2, 0))
        self.logbox.tag_configure("r", justify="right")

        self.load()
        self.enable_dnd(parent)
        for b in (self.go_r, self.go_p):
            b.configure(state="disabled")
        if CINE_ERR:
            self.log("الوضع السينمائي مش شغال — فيه مكتبة ناقصة:\n" + CINE_ERR)
            self.go_an.configure(state="disabled")
        root.after(100, self.poll)
        if autotest:
            root.after(800, self.autotest_step, "analyze")

    # -- widgets ------------------------------------------------------------------
    def file_row(self, f, r, label, key, types, folder=False):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=2)
        e = ttk.Entry(f, textvariable=self.v[key])
        e.grid(row=r, column=1, sticky="ew", pady=2, padx=6)

        def browse():
            cur = self.v[key].get()
            init = os.path.dirname(cur) if cur else (os.path.dirname(self.v["footage"].get()) or None)
            p = filedialog.askdirectory(initialdir=init) if folder else filedialog.askopenfilename(filetypes=types, initialdir=init)
            if p:
                self.set_path(key, os.path.normpath(p))
        ttk.Button(f, text="اختار…", command=browse).grid(row=r, column=2, sticky="ew", pady=2)
        return r + 1

    def green_value(self):
        return round(min(max(float(self.green.get()), 0.80), 1.00), 2)

    def set_green(self, g):
        self.green.set(g)
        self.green_lbl.configure(text="%.2f" % g)

    def load_green(self):
        g = self.settings.get("greens", {}).get(self.v["footage"].get().strip(), GREEN_DEFAULT)
        self.set_green(float(g))

    def set_path(self, key, p):
        self.v[key].set(p)
        if key == "footage":
            self.load_green()
        if key == "footage" and cinematic:
            if not self.v["out"].get():
                self.v["out"].set(cinematic.default_out_dir(p))
            if not self.v["song"].get():
                self.v["song"].set(cinematic.default_song_name(p))

    def enable_dnd(self, widget):
        try:
            from tkinterdnd2 import DND_FILES, TkinterDnD
            TkinterDnD._require(self.root)
            for w in [widget] + list(self.all_children(widget)):
                try:
                    w.drop_target_register(DND_FILES)
                    w.dnd_bind("<<Drop>>", self.on_drop)
                except Exception:
                    pass
        except Exception as ex:
            self.log("السحب والإفلات مش متاح هنا (%s) — استخدم «اختار…»" % ex)

    def all_children(self, w):
        for c in w.winfo_children():
            yield c
            yield from self.all_children(c)

    def on_drop(self, e):
        for p in self.root.tk.splitlist(e.data):
            p = os.path.normpath(p)
            low = os.path.basename(p).lower()
            ext = os.path.splitext(low)[1]
            if ext in VIDEO_EXT:
                self.set_path("footage", p)
            elif ext in AUDIO_EXT:
                if any(h in low for h in STEM_HINTS):
                    self.v["stem"].set(p)
                elif not self.v["master"].get() or "master" in low or "ماستر" in low:
                    self.v["master"].set(p)
                else:
                    self.v["stem"].set(p)
            elif ext == ".txt":
                self.v["lyrics"].set(p)
            elif ext == ".srt":
                self.v["srt"].set(p)
            elif os.path.isdir(p):
                self.v["out"].set(p)
        return e.action

    def log(self, msg):
        self.logbox.insert("end", msg + "\n", "r")
        self.logbox.see("end")

    # -- settings -------------------------------------------------------------------
    def load(self):
        for k, var in self.v.items():
            if isinstance(self.settings.get(k), str):
                var.set(self.settings[k])
        self.n_reels.set(int(self.settings.get("n", 4)))
        self.ig.set(bool(self.settings.get("ig", False)))
        self.glare.set(bool(self.settings.get("glare", False)))
        self.load_green()

    def remember(self):
        self.settings.update({k: v.get() for k, v in self.v.items()})
        self.settings.update(n=self.n_reels.get(), ig=self.ig.get(), glare=self.glare.get())
        if self.v["footage"].get().strip():
            self.settings.setdefault("greens", {})[self.v["footage"].get().strip()] = self.green_value()
        if self.project is not None and self.project.offset is not None:
            self.settings.setdefault("offsets", {})[self.v["footage"].get()] = [self.project.offset, self.project.rate]
        self.save_settings()

    # -- jobs -----------------------------------------------------------------------
    def busy(self, on):
        st = "disabled" if on else "normal"
        self.go_an.configure(state=st)
        has = self.project is not None and bool(self.sections)
        for b in (self.go_r, self.go_p):
            b.configure(state="disabled" if on or not has else "normal")
        self.stop.configure(state="normal" if on else "disabled")

    def run_bg(self, kind, fn):
        if self.worker and self.worker.is_alive():
            return
        self.cancel.clear()
        self.busy(True)
        self.t0 = time.time()
        self.pb["value"] = 0

        def work():
            try:
                self.q.put(("done", kind, fn()))
            except Exception as ex:
                if type(ex).__name__ == "Cancelled":
                    self.q.put(("cancel", kind))
                else:
                    self.crash_log(traceback.format_exc())
                    self.q.put(("err", kind, str(ex)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self.pb["value"] = int(m[1] * 1000)
                    self.status.configure(text=(m[2] or self.status.cget("text").split("  —  ")[0]) + eta_text(self.t0, m[1]))
                elif m[0] == "l":
                    self.log(m[1])
                else:
                    self.busy(False)
                    self.finished(*m)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def make_project(self, keep=True):
        g = {k: v.get().strip() for k, v in self.v.items()}
        offs = self.settings.get("offsets", {})
        off, rate = None, 1.0
        if keep and self.project is not None and self.project.footage == g["footage"]:
            off, rate = self.project.offset, self.project.rate
        elif g["footage"] in offs:
            o = offs[g["footage"]]
            off, rate = (float(o[0]), float(o[1])) if isinstance(o, list) else (float(o), 1.0)
        if not self.off_var.get().strip():  # empty box = sync again automatically
            off, rate = None, 1.0
        p = cinematic.Project(g["footage"], g["master"], g["lyrics"], g["stem"], g["srt"], g["song"], g["out"],
                              n_reels=max(1, int(self.n_reels.get() or 4)), ig=self.ig.get(), deglare=self.glare.get(),
                              green=self.green_value(),
                              progress=lambda f, msg="": self.q.put(("p", f, msg)),
                              log=lambda msg: self.q.put(("l", msg)), cancel=self.cancel, offset=off)
        p.rate = rate
        return p

    def analyze(self, redo=False):
        if not cinematic:
            return
        try:
            p = self.make_project(keep=not redo)
        except Exception as ex:
            messagebox.showerror("Reel Maker", str(ex))
            return
        if redo and self.project:
            p.offset, p.rate = self.project.offset, self.project.rate
        self.project = p
        self.remember()
        self.logbox.delete("1.0", "end")
        self.run_bg("analyze", lambda: p.analyze(redo_words=redo))

    def finished(self, what, kind, result=None):
        if what == "cancel":
            self.status.configure(text="اتلغى")
            return
        if what == "err":
            self.status.configure(text="حصل خطأ — التفاصيل في error.log")
            self.log(result)
            if self.autotest:
                return self.autotest_report(False, result)
            messagebox.showerror("Reel Maker", result[:1500])
            return
        if kind == "analyze":
            self.sections = result
            self.off_var.set("%.3f" % self.project.offset)
            c = self.project.conf
            self.conf_lbl.configure(text="ثقة %d%%" % (c * 100) if c is not None else "متسجّل")
            self.fill()
            self.busy(False)
            self.remember()
            if self.autotest:
                self.root.after(500, self.autotest_step, "render")
        elif kind == "words":
            self.status.configure(text="التوقيت اتقرا ✔")
        elif kind == "preview":
            self.status.configure(text="المعاينة جاهزة ✔")
            if not self.autotest:
                self.open_path_file(result[0]["mp4"])
        elif kind == "render":
            self.status.configure(text="خلص ✔ — %d ملف" % len(result))
            if self.autotest:
                return self.autotest_report(True, None, result)
            if messagebox.askyesno("Reel Maker", "الريلز جاهزة ✔ (%d ملف)\nأفتح الفولدر؟" % len(result)):
                self.open_out()

    # -- table ----------------------------------------------------------------------
    def fill(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.sections):
            self.tree.insert("", "end", iid=str(i), values=(
                "✔" if s.get("checked", True) else "", s["name"], fmt(s["start"]), fmt(s["end"]),
                "%.0f ث" % (s["end"] - s["start"]), cine_edit.STYLE_NAMES[s["style"]], s.get("first", "").replace("*", "")))
        if self.sections:
            self.tree.selection_set("0")

    def sel(self):
        s = self.tree.selection()
        return int(s[0]) if s else None

    def on_select(self, _e=None):
        i = self.sel()
        if i is None:
            return
        s = self.sections[i]
        self.e_start.set(fmt(s["start"]))
        self.e_end.set(fmt(s["end"]))
        self.e_style.set(cine_edit.STYLE_NAMES[s["style"]])
        self.e_hook.set(s.get("hook", ""))

    def apply_row(self):
        i = self.sel()
        if i is None:
            return
        s = self.sections[i]
        try:
            a, b = parse_t(self.e_start.get()), parse_t(self.e_end.get())
        except ValueError:
            messagebox.showerror("Reel Maker", "الوقت مش مفهوم — اكتبه زي 1:05.30 أو 65.3")
            return
        if b - a < 3:
            messagebox.showerror("Reel Maker", "الريل لازم يكون 3 ثواني على الأقل")
            return
        s["start"], s["end"] = a, b
        for k, n in cine_edit.STYLE_NAMES.items():
            if n == self.e_style.get():
                s["style"] = k
        s["hook"] = self.e_hook.get().strip()
        self.fill()
        self.tree.selection_set(str(i))

    def toggle(self):
        i = self.sel()
        if i is None:
            return
        self.sections[i]["checked"] = not self.sections[i].get("checked", True)
        self.fill()
        self.tree.selection_set(str(i))

    def nudge(self, d):
        if not self.project:
            return
        try:
            cur = float(self.off_var.get())
        except ValueError:
            cur = self.project.offset or 0.0
        self.project.offset = round(cur + d, 3)
        self.off_var.set("%.3f" % self.project.offset)
        self.remember()

    def sync_from_entry(self):
        try:
            self.project.offset = float(self.off_var.get())
        except ValueError:
            pass

    # -- actions ----------------------------------------------------------------------
    def ready(self):
        if not self.project or not self.sections:
            messagebox.showinfo("Reel Maker", "دوس «حلّل» الأول")
            return False
        self.apply_row()
        self.sync_from_entry()
        self.project.ig, self.project.deglare = self.ig.get(), self.glare.get()
        self.project.green = self.green_value()
        self.project.out_dir = self.v["out"].get().strip() or self.project.out_dir
        self.project.cancel = self.cancel
        self.remember()
        return True

    def preview(self):
        if not self.ready():
            return
        i = self.sel() or 0
        s = dict(self.sections[i], checked=True)
        self.log("— معاينة ٥ ث: %s" % s["name"])
        self.run_bg("preview", lambda: self.project.render([s], preview=True))

    def render(self):
        if not self.ready():
            return
        n = sum(1 for s in self.sections if s.get("checked", True))
        self.log("— بعمل %d ريل%s…" % (n, " + نسخ إنستجرام/فيسبوك" if self.project.ig else ""))
        self.run_bg("render", lambda: self.project.render(self.sections))

    def open_words(self):
        if self.project and os.path.isfile(self.project.words_path):
            self.open_path_file(self.project.words_path)
        else:
            messagebox.showinfo("Reel Maker", "words.json بيتعمل بعد «حلّل»")

    def reload_words(self):
        if not self.project:
            return
        self.run_bg("words", self.project.reload_words)

    def open_out(self):
        p = self.v["out"].get().strip()
        if p:
            self.open_path(p)

    def open_path_file(self, p):
        try:
            if hasattr(os, "startfile"):
                os.startfile(p)
            else:
                self.open_path(os.path.dirname(p))
        except OSError as ex:
            self.log(str(ex))

    def close(self):
        self.cancel.set()
        try:
            self.remember()
        except Exception:
            pass

    # -- CI ---------------------------------------------------------------------------
    def shot(self, name):
        d = (self.autotest or {}).get("shots")
        if not d:
            return
        try:
            from PIL import ImageGrab
            self.root.update()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            ImageGrab.grab((x, y, x + self.root.winfo_width(), y + self.root.winfo_height())).save(os.path.join(d, name))
        except Exception as ex:
            self.log("screenshot: %s" % ex)

    def autotest_step(self, step):
        at = self.autotest
        if step == "analyze":
            for k in ("footage", "master", "stem", "lyrics", "srt", "song", "out"):
                if at.get(k):
                    self.set_path(k, at[k])
            if at.get("green"):
                self.set_green(float(at["green"]))
            self.shot("cine-1-inputs.png")
            self.analyze()
        elif step == "render":
            self.shot("cine-2-reels.png")
            for i, s in enumerate(self.sections):
                s["checked"] = i == 0
                s["end"] = min(s["end"], s["start"] + at.get("seconds", 6))
            self.fill()
            self.render()

    def autotest_report(self, good, err, result=None):
        self.shot("cine-3-done.png")
        with open(self.autotest["report"], "w", encoding="utf-8") as fh:
            json.dump({"ok": good, "error": err, "reels": [r["mp4"] for r in (result or [])],
                       "offset": self.project.offset if self.project else None}, fh, ensure_ascii=False, indent=1)
        self.root.after(500, self.root.destroy)
