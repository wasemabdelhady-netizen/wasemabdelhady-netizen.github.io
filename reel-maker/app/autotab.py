"""«أوتوماتيك» tab: song + music video + plain lyrics -> sections -> review -> reels."""
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import engine

try:
    import autoreel
    AUTO_ERR = None
except Exception:  # missing package in the bundle: the manual tab still works
    autoreel = None
    AUTO_ERR = traceback.format_exc()

if sys.platform == "win32":
    import winsound

VIDEO_TYPES = [("Video", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi"), ("All", "*.*")]
AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.m4a *.aac *.flac *.ogg *.opus"), ("All", "*.*")]
TEXT_TYPES = [("Text", "*.txt"), ("All", "*.*")]
MODELS = [("small", "small — أسرع (الافتراضي)"), ("medium", "medium — أدق وأبطأ (محتاج رامات 8 جيجا)")]


class Player:
    def __init__(self):
        self.proc = None
        self.file = None

    def play(self, path):
        self.stop()
        self.file = path
        if sys.platform == "win32":
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        else:
            for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path],
                        ["paplay", path], ["aplay", "-q", path]):
                try:
                    self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except OSError:
                    continue

    def stop(self):
        if sys.platform == "win32":
            winsound.PlaySound(None, 0)
        elif self.proc:
            self.proc.kill()
            self.proc = None
        old, self.file = self.file, None
        if old:
            try:
                os.remove(old)
            except OSError:
                pass


class AutoTab:
    def __init__(self, root, parent, settings, save_settings, crash_log, open_path, autotest=None):
        self.root = root
        self.settings = settings.setdefault("auto", {})
        self.save_settings = save_settings
        self.crash_log = crash_log
        self.open_path = open_path
        self.autotest = autotest
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.project = None
        self.player = Player()
        self.tmp = tempfile.mkdtemp(prefix="reelauto_")
        self.play_t0 = None
        self.sec_vars = []

        st = ttk.Style()
        st.configure("Treeview", background="#141210", fieldbackground="#141210", foreground="#f5efe6",
                     rowheight=28, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background="#2a241d", foreground="#e0a458")
        st.map("Treeview", background=[("selected", "#e0a458")], foreground=[("selected", "#0a0a0a")])
        st.configure("TCheckbutton", background="#0a0a0a", foreground="#f5efe6")
        st.map("TCheckbutton", background=[("active", "#0a0a0a")])
        st.configure("Sec.TFrame", background="#141210")
        st.configure("Sec.TLabel", background="#141210", foreground="#f5efe6")
        st.configure("SecHint.TLabel", background="#141210", foreground="#9a8f80")
        st.configure("Sec.TCheckbutton", background="#141210", foreground="#e0a458", font=("Segoe UI", 10, "bold"))
        st.map("Sec.TCheckbutton", background=[("active", "#141210")])

        outer = ttk.Frame(parent, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        self.title = ttk.Label(outer, text="أوتوماتيك  ·  ريلز كتير من الأغنية مرة واحدة", style="Title.TLabel")
        self.title.grid(row=0, column=0, sticky="e", pady=(0, 8))

        self.stage_box = ttk.Frame(outer)
        self.stage_box.grid(row=1, column=0, sticky="nsew")
        self.stage_box.columnconfigure(0, weight=1)
        self.stage_box.rowconfigure(0, weight=1)
        self.f_in = ttk.Frame(self.stage_box)
        self.f_sec = ttk.Frame(self.stage_box)
        self.f_rev = ttk.Frame(self.stage_box)
        for fr in (self.f_in, self.f_sec, self.f_rev):
            fr.grid(row=0, column=0, sticky="nsew")
        self.build_inputs(self.f_in)
        self.build_sections(self.f_sec)
        self.build_review(self.f_rev)

        bottom = ttk.Frame(outer)
        bottom.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        bottom.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(bottom, maximum=1000)
        self.pb.grid(row=0, column=0, sticky="ew", pady=4)
        self.stop_btn = ttk.Button(bottom, text="إلغاء", command=self.cancel.set, state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(bottom, text="افتح output", command=self.open_out).grid(row=0, column=2, padx=(8, 0))
        self.status = ttk.Label(bottom, text="جاهز", style="Hint.TLabel", anchor="e")
        self.status.grid(row=1, column=0, columnspan=3, sticky="ew")
        self.logbox = tk.Text(bottom, height=7, bg="#141210", fg="#d9cfc0", insertbackground="#f5efe6",
                              relief="flat", font=("Consolas", 9), wrap="word")
        self.logbox.grid(row=2, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        self.logbox.tag_configure("r", justify="right")

        self.show(self.f_in)
        self.load()
        if AUTO_ERR:
            self.log("الوضع الأوتوماتيك مش شغال — فيه مكتبة ناقصة:\n" + AUTO_ERR)
            self.go.configure(state="disabled")
        root.after(100, self.poll)
        if autotest:
            root.after(800, self.autotest_step, "analyze")

    # -- layout ----------------------------------------------------------------
    def show(self, fr):
        fr.tkraise()
        self.current = fr

    def file_row(self, f, r, label, key, types):
        ttk.Label(f, text=label).grid(row=r, column=2, sticky="e", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=58).grid(row=r, column=1, sticky="ew", pady=4, padx=6)

        def browse():
            cur = self.v[key].get()
            start = os.path.dirname(cur) if cur else self.settings.get("dirs", {}).get(key)
            p = filedialog.askopenfilename(filetypes=types, initialdir=start or None)
            if p:
                p = os.path.normpath(p)
                self.v[key].set(p)
                self.settings.setdefault("dirs", {})[key] = os.path.dirname(p)
                if key == "audio" and not self.v["song"].get().strip():
                    self.v["song"].set(song_name_from(p))
        ttk.Button(f, text="اختار…", command=browse).grid(row=r, column=0, sticky="ew", pady=4)
        return r + 1

    def build_inputs(self, f):
        f.columnconfigure(1, weight=1)
        self.v = {k: tk.StringVar() for k in ("audio", "video", "lyrics", "stem", "song", "model", "lfont", "hfont")}
        self.bg_only = tk.BooleanVar(value=False)
        self.reuse = tk.BooleanVar(value=True)
        r = 0
        r = self.file_row(f, r, "الأغنية  (wav / mp3)", "audio", AUDIO_TYPES)
        r = self.file_row(f, r, "الفيديو  (الكليب أو خلفية طويلة)", "video", VIDEO_TYPES)
        r = self.file_row(f, r, "الكلمات  (txt من غير توقيت)", "lyrics", TEXT_TYPES)
        r = self.file_row(f, r, "الفوكال ستيم  (اختياري — أدق)", "stem", AUDIO_TYPES)
        ttk.Label(f, text="اسم الأغنية").grid(row=r, column=2, sticky="e", pady=4)
        self.cb_song = ttk.Combobox(f, textvariable=self.v["song"], justify="right",
                                    values=sorted(self.settings.get("songs", {})))
        self.cb_song.grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        self.cb_song.bind("<<ComboboxSelected>>", lambda e: self.restore_song(self.v["song"].get()))
        r += 1

        ttk.Label(f, text="موديل السمع").grid(row=r, column=2, sticky="e", pady=4)
        self.cb_model = ttk.Combobox(f, values=[m[1] for m in MODELS], state="readonly")
        self.cb_model.grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        self.cb_model.current(0)
        r += 1
        fonts = engine.font_list()
        ttk.Label(f, text="فونت الكلمات").grid(row=r, column=2, sticky="e", pady=4)
        ttk.Combobox(f, textvariable=self.v["lfont"], values=fonts, state="readonly").grid(
            row=r, column=1, sticky="ew", pady=4, padx=6)
        r += 1
        ttk.Label(f, text="فونت الهوك").grid(row=r, column=2, sticky="e", pady=4)
        ttk.Combobox(f, textvariable=self.v["hfont"], values=fonts, state="readonly").grid(
            row=r, column=1, sticky="ew", pady=4, padx=6)
        r += 1
        self.v["lfont"].set(pick(fonts, "ArefRuqaa-Bold.ttf", "aref"))
        self.v["hfont"].set(pick(fonts, "Alexandria.ttf", "alexandria"))

        self.packbar = None
        try:
            import packui
            self.packbar = packui.PackBar(f, self.settings, "pack", self.save_settings, allow_classic=True)
            self.packbar.frame.grid(row=r, column=0, columnspan=3, sticky="e", pady=4)
            r += 1
        except Exception:  # textfx missing: classic look only
            self.packbar = None
        opts = ttk.Frame(f)
        opts.grid(row=r, column=0, columnspan=3, sticky="e", pady=6)
        ttk.Checkbutton(opts, text="استخدم التوقيت المتسجّل لو موجود", variable=self.reuse).pack(side="right", padx=(12, 0))
        ttk.Checkbutton(opts, text="خلفية بس (من غير لقطات الكليب)", variable=self.bg_only).pack(side="right", padx=(12, 0))
        r += 1
        self.go = ttk.Button(f, text="حلّل  ▶", style="Go.TButton", command=self.analyze)
        self.go.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        r += 1
        ttk.Label(f, text="أول مرة بس: البرنامج هينزّل موديل السمع (~500 ميجا) — محتاج نت مرة واحدة.",
                  style="Hint.TLabel").grid(row=r, column=0, columnspan=3, sticky="e")

    def build_sections(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="المقاطع — علّم على الريلز اللي عايزها، واكتب هوك لو حابب (*كلمة* = دهبي)").grid(
            row=0, column=0, sticky="e", pady=(0, 6))
        wrap = ttk.Frame(f)
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.sec_canvas = tk.Canvas(wrap, bg="#0a0a0a", highlightthickness=0)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.sec_canvas.yview)
        self.sec_canvas.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=0, sticky="nsw")
        self.sec_canvas.grid(row=0, column=0, sticky="nsew", padx=(16, 0))
        self.sec_inner = ttk.Frame(self.sec_canvas)
        self.sec_win = self.sec_canvas.create_window(0, 0, window=self.sec_inner, anchor="nw")
        self.sec_inner.bind("<Configure>", lambda e: self.sec_canvas.configure(scrollregion=self.sec_canvas.bbox("all")))
        self.sec_canvas.bind("<Configure>", lambda e: self.sec_canvas.itemconfigure(self.sec_win, width=e.width))
        self.sec_canvas.bind_all("<MouseWheel>", self.wheel)
        btns = ttk.Frame(f)
        btns.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        btns.columnconfigure(1, weight=1)
        ttk.Button(btns, text="رجوع", command=lambda: self.show(self.f_in)).grid(row=0, column=2, sticky="e")
        ttk.Button(btns, text="الكل / ولا حاجة", command=self.toggle_all).grid(row=0, column=0, sticky="w")
        self.next_btn = ttk.Button(btns, text="التالي: راجع الكلمات  ◀", style="Go.TButton", command=self.to_review)
        self.next_btn.grid(row=0, column=1, sticky="ew", padx=8)

    def wheel(self, e):
        if self.current is self.f_sec:
            self.sec_canvas.yview_scroll(int(-e.delta / 120) or (-1 if e.delta > 0 else 1), "units")

    def build_review(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="راجع التوقيت: دوس على سطر يشتغل من عنده — زقّه لو متأخر أو متقدّم، وصلّح الكلام لو غلط (⚠ = راجعه)").grid(
            row=0, column=0, sticky="e", pady=(0, 6))
        cols = ("text", "conf", "end", "start")
        self.tree = ttk.Treeview(f, columns=cols, show="tree headings", selectmode="browse")
        for c, h, w, anc in (("start", "البداية", 80, "center"), ("end", "النهاية", 80, "center"),
                             ("conf", "", 34, "center"), ("text", "الكلام", 420, "e")):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor=anc, stretch=(c == "text"))
        self.tree.heading("#0", text="المقطع")
        self.tree.column("#0", width=150, anchor="e", stretch=False)
        self.tree.tag_configure("sec", foreground="#e0a458", font=("Segoe UI", 10, "bold"))
        self.tree.tag_configure("low", foreground="#f0a070")
        self.tree.tag_configure("playing", background="#3a3128")
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ttk.Scrollbar(f, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Double-1>", lambda e: self.play_line())

        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for txt, cmd in (("▶ اسمع", self.play_line), ("■ وقّف", self.stop_play),
                         ("−0.5s", lambda: self.nudge(-0.5)), ("−0.1s", lambda: self.nudge(-0.1)),
                         ("+0.1s", lambda: self.nudge(0.1)), ("+0.5s", lambda: self.nudge(0.5))):
            ttk.Button(bar, text=txt, command=cmd).pack(side="left", padx=(0, 6))
        self.follow = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="زقّ السطور اللي بعده كمان", variable=self.follow).pack(side="left", padx=8)

        ed = ttk.Frame(f)
        ed.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ed.columnconfigure(1, weight=1)
        self.edit_v = tk.StringVar()
        ttk.Button(ed, text="حفظ الكلام", command=self.save_text).grid(row=0, column=0)
        self.edit = ttk.Entry(ed, textvariable=self.edit_v, justify="right")
        self.edit.grid(row=0, column=1, sticky="ew", padx=6)
        self.edit.bind("<Return>", lambda e: self.save_text())
        ttk.Label(ed, text="الكلام").grid(row=0, column=2)

        btns = ttk.Frame(f)
        btns.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        btns.columnconfigure(1, weight=1)
        ttk.Button(btns, text="رجوع للمقاطع", command=lambda: (self.stop_play(), self.fill_sections(), self.show(self.f_sec))).grid(
            row=0, column=2, sticky="e")
        self.make_btn = ttk.Button(btns, text="تمام، اعمل الريلز  ▶", style="Go.TButton", command=self.render)
        self.make_btn.grid(row=0, column=1, sticky="ew", padx=8)

    # -- helpers ---------------------------------------------------------------
    def log(self, msg):
        self.logbox.insert("end", msg + "\n", "r")
        self.logbox.see("end")

    def open_out(self):
        p = self.project.out_dir if self.project else engine.OUTPUT_DIR
        self.open_path(p if os.path.isdir(p) else engine.OUTPUT_DIR)

    def model_name(self):
        i = self.cb_model.current()
        return MODELS[i if i >= 0 else 0][0]

    def inputs(self):
        return {k: v.get().strip() for k, v in self.v.items() if k != "model"}

    # -- settings --------------------------------------------------------------
    def load(self):
        last = self.settings.get("last", {})
        for k, var in self.v.items():
            if isinstance(last.get(k), str) and k != "model":
                if k in ("lfont", "hfont") and last[k] not in engine.font_list():
                    continue
                var.set(last[k])
        names = [m[0] for m in MODELS]
        if self.settings.get("model") in names:
            self.cb_model.current(names.index(self.settings["model"]))
        self.bg_only.set(bool(self.settings.get("bg_only", False)))

    def remember(self):
        g = self.inputs()
        self.settings["last"] = g
        self.settings["model"] = self.model_name()
        self.settings["bg_only"] = bool(self.bg_only.get())
        if g["song"]:
            songs = self.settings.setdefault("songs", {})
            entry = songs.setdefault(g["song"], {})
            entry.update({k: g[k] for k in ("audio", "video", "lyrics", "stem")})
            if self.project and self.project.sections:
                entry["hooks"] = {s["name"]: s.get("hook", "") for s in self.project.sections if s.get("hook")}
                entry["closings"] = {s["name"]: s["closing"] for s in self.project.sections
                                     if s.get("closing") != engine.DEFAULT_CLOSING}
            self.cb_song.configure(values=sorted(songs))
        self.save_settings()

    def restore_song(self, name):
        entry = self.settings.get("songs", {}).get(name)
        if not entry:
            return
        for k in ("audio", "video", "lyrics", "stem"):
            if isinstance(entry.get(k), str):
                self.v[k].set(entry[k])

    # -- worker plumbing -------------------------------------------------------
    def busy(self, on):
        st = "disabled" if on else "normal"
        for b in (self.go, self.next_btn, self.make_btn):
            b.configure(state=st)
        self.stop_btn.configure(state="normal" if on else "disabled")

    def run_bg(self, kind, fn):
        if self.worker and self.worker.is_alive():
            return
        self.cancel.clear()
        self.busy(True)
        self.pb["value"] = 0

        def work():
            try:
                self.q.put(("done", kind, fn()))
            except engine.Cancelled:
                self.q.put(("cancel", kind))
            except Exception as ex:
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
                    if m[2]:
                        self.status.configure(text=m[2])
                elif m[0] == "l":
                    self.log(m[1])
                else:
                    self.busy(False)
                    if m[0] == "done":
                        self.finished(m[1], m[2])
                    elif m[0] == "cancel":
                        self.status.configure(text="اتلغى")
                        self.pb["value"] = 0
                    else:
                        self.status.configure(text="حصل خطأ")
                        self.log(m[2])
                        if self.autotest:
                            self.autotest_report(False, m[2])
                        else:
                            messagebox.showerror("Reel Maker", m[2][:1500])
        except queue.Empty:
            pass
        self.tick_play()
        self.root.after(100, self.poll)

    # -- analyze ---------------------------------------------------------------
    def analyze(self):
        g = self.inputs()
        if not g["song"] and g["audio"]:
            g["song"] = song_name_from(g["audio"])
            self.v["song"].set(g["song"])
        for key, label in (("audio", "الأغنية"), ("video", "الفيديو"), ("lyrics", "ملف الكلمات")):
            if not g[key] or not os.path.isfile(g[key]):
                messagebox.showerror("Reel Maker", "اختار %s الأول" % label)
                return
        self.remember()
        self.logbox.delete("1.0", "end")
        asr = None
        if self.autotest and self.autotest.get("asr_words"):
            with open(self.autotest["asr_words"], encoding="utf-8") as fh:
                words = json.load(fh)
            asr = lambda path, texts: words
        self.project = autoreel.Project(
            g["audio"], g["video"], g["lyrics"], g["stem"], g["song"], model=self.model_name(),
            bg_only=bool(self.bg_only.get()), lyric_font=g["lfont"], hook_font=g["hfont"],
            progress=lambda f, msg="": self.q.put(("p", f, msg)),
            log=lambda msg: self.q.put(("l", msg)), cancel=self.cancel, asr=asr)
        reuse = bool(self.reuse.get())
        self.run_bg("analyze", lambda: self.project.analyze(reuse_timed=reuse))

    def finished(self, kind, result):
        if kind == "analyze":
            entry = self.settings.get("songs", {}).get(self.project.song, {})
            for s in self.project.sections:
                s["hook"] = entry.get("hooks", {}).get(s["name"], "")
                s["closing"] = entry.get("closings", {}).get(s["name"], engine.DEFAULT_CLOSING)
            self.fill_sections()
            self.show(self.f_sec)
            self.status.configure(text="التحليل خلص ✔ — اختار المقاطع")
            if self.autotest:
                self.root.after(1500, self.autotest_step, "sections")
        elif kind == "render":
            self.remember()
            self.status.configure(text="خلص ✔ — %d ريل في %s" % (len(result), self.project.out_dir))
            if self.autotest:
                self.autotest_report(True, "", result)
            elif messagebox.askyesno("Reel Maker", "الريلز جاهزة ✔ (%d)\nأفتح الفولدر؟" % len(result)):
                self.open_path(self.project.out_dir)

    # -- sections --------------------------------------------------------------
    def fill_sections(self):
        for w in self.sec_inner.winfo_children():
            w.destroy()
        self.sec_vars = []
        pr = self.project
        if not pr:
            return
        self.sec_inner.columnconfigure(0, weight=1)
        for i, s in enumerate(pr.sections):
            a, b = autoreel.reel_span(pr.lines, s["i0"], s["i1"], pr.total, s.get("closing"))
            box = ttk.Frame(self.sec_inner, style="Sec.TFrame", padding=8)
            box.grid(row=i, column=0, sticky="ew", pady=3)
            box.columnconfigure(1, weight=1)
            chk = tk.BooleanVar(value=s.get("checked", False))
            hook = tk.StringVar(value=s.get("hook", ""))
            closing = tk.StringVar(value=s.get("closing", engine.DEFAULT_CLOSING))
            head = ttk.Frame(box, style="Sec.TFrame")
            head.grid(row=0, column=0, columnspan=3, sticky="ew")
            ttk.Checkbutton(head, text=s["name"], variable=chk, style="Sec.TCheckbutton").pack(side="right")
            ttk.Label(head, text="%s → %s   (%.0fs)" % (autoreel.fmt(a, False), autoreel.fmt(b, False), b - a),
                      style="SecHint.TLabel").pack(side="right", padx=12)
            ttk.Label(head, text="«%s»" % pr.lines[s["i0"]].text, style="Sec.TLabel").pack(side="right", padx=6)
            ttk.Label(box, text="الهوك", style="SecHint.TLabel").grid(row=1, column=2, sticky="e", pady=(6, 0))
            ttk.Entry(box, textvariable=hook, justify="right").grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
            ttk.Label(box, text="جملة النهاية", style="SecHint.TLabel").grid(row=2, column=2, sticky="e", pady=(4, 0))
            ttk.Entry(box, textvariable=closing, justify="right").grid(row=2, column=1, sticky="ew", padx=6, pady=(4, 0))
            self.sec_vars.append((s, chk, hook, closing))

    def read_sections(self):
        for s, chk, hook, closing in self.sec_vars:
            s["checked"] = bool(chk.get())
            s["hook"] = hook.get().strip()
            s["closing"] = closing.get().strip()

    def toggle_all(self):
        on = not all(v[1].get() for v in self.sec_vars)
        for v in self.sec_vars:
            v[1].set(on)

    # -- review ----------------------------------------------------------------
    def to_review(self):
        self.read_sections()
        if not any(s["checked"] for s in self.project.sections):
            messagebox.showerror("Reel Maker", "علّم على مقطع واحد على الأقل")
            return
        self.fill_review()
        self.show(self.f_rev)

    def fill_review(self):
        self.tree.delete(*self.tree.get_children())
        pr = self.project
        for k, s in enumerate(pr.sections):
            if not s["checked"]:
                continue
            a, b = autoreel.reel_span(pr.lines, s["i0"], s["i1"], pr.total, s.get("closing"))
            sid = "s%d" % k
            self.tree.insert("", "end", iid=sid, text=s["name"], open=True, tags=("sec",),
                             values=("", "", autoreel.fmt(b), autoreel.fmt(a)))
            for i in range(s["i0"], s["i1"] + 1):
                self.tree.insert(sid, "end", iid="%s-%d" % (sid, i), text=str(i + 1),
                                 values=self.row(i), tags=self.tags(i))

    def row(self, i):
        ln = self.project.lines[i]
        return (ln.text, "⚠" if ln.conf < 0.4 else "✓", autoreel.fmt(ln.end), autoreel.fmt(ln.start))

    def tags(self, i):
        return ("low",) if self.project.lines[i].conf < 0.4 else ()

    def refresh_rows(self):
        pr = self.project
        for sid in self.tree.get_children():
            k = int(sid[1:])
            s = pr.sections[k]
            a, b = autoreel.reel_span(pr.lines, s["i0"], s["i1"], pr.total, s.get("closing"))
            self.tree.item(sid, values=("", "", autoreel.fmt(b), autoreel.fmt(a)))
            for iid in self.tree.get_children(sid):
                i = int(iid.split("-")[1])
                self.tree.item(iid, values=self.row(i), tags=self.tags(i))

    def selected_line(self):
        sel = self.tree.selection()
        if not sel or "-" not in sel[0]:
            return None
        return int(sel[0].split("-")[1])

    def on_select(self, _e=None):
        i = self.selected_line()
        if i is not None:
            self.edit_v.set(self.project.lines[i].text)
            self.play_line()

    def nudge(self, d):
        i = self.selected_line()
        if i is None:
            return
        lines = self.project.lines
        last = len(lines) - 1 if self.follow.get() else i
        lo = lines[i - 1].start + 0.2 if i > 0 else 0.0
        d = max(d, lo - lines[i].start)
        for k in range(i, last + 1):
            lines[k].shift(d)
        if last + 1 < len(lines) and lines[last].end > lines[last + 1].start - 0.05:
            lines[last].end = max(lines[last].start + 0.3, lines[last + 1].start - 0.05)
        if i > 0 and lines[i - 1].end > lines[i].start - 0.05:
            lines[i - 1].end = max(lines[i - 1].start + 0.3, lines[i].start - 0.05)
        lines[i].conf = max(lines[i].conf, 1.0)
        self.refresh_rows()
        self.play_line()

    def save_text(self):
        i = self.selected_line()
        txt = self.edit_v.get().strip()
        if i is None or not txt:
            return
        self.project.lines[i].set_text(txt)
        self.refresh_rows()

    def play_line(self):
        i = self.selected_line()
        if i is None or not self.project:
            return
        t = max(0.0, self.project.lines[i].start - 0.4)
        wav = os.path.join(self.tmp, "play%d.wav" % int(time.time() * 1000))
        r = engine.run_ffmpeg(["-y", "-ss", "%.3f" % t, "-t", "12", "-i", self.project.song_path,
                               "-ac", "2", "-ar", "44100", "-c:a", "pcm_s16le", wav])
        if r.returncode == 0:
            self.player.play(wav)
            self.play_t0 = (time.time(), t)

    def stop_play(self):
        self.player.stop()
        self.play_t0 = None
        for iid in self.playing_rows():
            self.tree.item(iid, tags=self.tags(int(iid.split("-")[1])))

    def playing_rows(self):
        return [c for sid in self.tree.get_children() for c in self.tree.get_children(sid)
                if "playing" in self.tree.item(c, "tags")]

    def tick_play(self):
        if not self.play_t0 or self.current is not self.f_rev:
            return
        t0, at = self.play_t0
        now = at + time.time() - t0
        if now - at > 12:
            self.stop_play()
            return
        for sid in self.tree.get_children():
            for iid in self.tree.get_children(sid):
                i = int(iid.split("-")[1])
                ln = self.project.lines[i]
                tags = self.tags(i) + (("playing",) if ln.start <= now <= ln.end else ())
                if tags != tuple(self.tree.item(iid, "tags") or ()):
                    self.tree.item(iid, tags=tags)

    # -- render ----------------------------------------------------------------
    def render(self):
        self.stop_play()
        pr = self.project
        pr.lines.sort(key=lambda l: l.start)
        pr.cancel = self.cancel
        pr.pack = self.packbar.get() if self.packbar else None
        self.remember()
        self.log("— بعمل %d ريل…" % sum(1 for s in pr.sections if s["checked"]))
        self.run_bg("render", pr.render)

    def close(self):
        self.cancel.set()
        self.player.stop()
        try:
            self.remember()
        except Exception:
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- CI: drive the whole flow and take screenshots -------------------------
    def shot(self, name):
        d = self.autotest.get("shots")
        if not d:
            return
        try:
            from PIL import ImageGrab
            self.root.update()
            x, y = self.root.winfo_rootx(), self.root.winfo_rooty()
            w, h = self.root.winfo_width(), self.root.winfo_height()
            ImageGrab.grab((x, y, x + w, y + h)).save(os.path.join(d, name))
        except Exception as ex:
            self.log("screenshot: %s" % ex)

    def autotest_step(self, step):
        at = self.autotest
        if step == "analyze":
            for k in ("audio", "video", "lyrics", "song"):
                self.v[k].set(at[k])
            self.reuse.set(False)
            self.shot("auto-1-inputs.png")
            self.analyze()
        elif step == "sections":
            self.shot("auto-2-sections.png")
            for s, chk, hook, closing in self.sec_vars:
                chk.set(s["name"] == at.get("section", "الكورس 1"))
                if chk.get():
                    hook.set("لو قلبك *سألك*… هتقوله إيه؟")
            self.to_review()
            first = self.tree.get_children(self.tree.get_children()[0])[0]
            self.tree.selection_set(first)
            self.root.after(800, self.autotest_step, "review")
        elif step == "review":
            self.nudge(0.1)
            self.nudge(-0.1)
            self.stop_play()
            self.shot("auto-3-review.png")
            self.render()

    def autotest_report(self, good, err, result=None):
        self.shot("auto-4-done.png")
        with open(self.autotest["report"], "w", encoding="utf-8") as fh:
            json.dump({"ok": good, "error": err,
                       "reels": [r["mp4"] for r in (result or [])],
                       "sections": [s["name"] for s in self.project.sections] if self.project else []},
                      fh, ensure_ascii=False, indent=1)
        self.root.after(500, self.root.destroy)


def pick(fonts, preferred, contains):
    if preferred in fonts:
        return preferred
    for f in fonts:
        if contains in f.lower():
            return f
    return fonts[0] if fonts else ""


def song_name_from(path):
    import re
    n = os.path.splitext(os.path.basename(path))[0]
    n = re.sub(r"(?i)[\s_\-]*(master|final|mix|mastered|v\d+)$", "", n).strip(" _-")
    return n or os.path.splitext(os.path.basename(path))[0]
