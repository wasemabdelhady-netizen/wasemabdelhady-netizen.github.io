"""OudLofiMaker — window around engine.py."""
import json
import os
import queue
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ROOT = os.path.dirname(HERE)
SETTINGS = os.path.join(ROOT, "settings.json")


def crash_log(text):
    try:
        with open(os.path.join(ROOT, "error.log"), "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import engine
except Exception:
    crash_log(traceback.format_exc())
    raise

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.opus *.aif *.aiff"), ("All", "*.*")]
BG_TYPES = [("Image / Video", "*.jpg *.jpeg *.png *.webp *.bmp *.mp4 *.mov *.m4v *.mkv *.webm *.avi"),
            ("All", "*.*")]
CORNER_LABELS = [("تحت شمال  (bottom-left)", "bottom-left"), ("تحت يمين  (bottom-right)", "bottom-right"),
                 ("فوق شمال  (top-left)", "top-left"), ("فوق يمين  (top-right)", "top-right")]
TEXTURE_LABELS = [("من غير  (none)", "none"), ("صوت أسطوانة  (vinyl crackle)", "vinyl"),
                  ("مطر  (rain)", "rain"), ("أسطوانة + مطر  (vinyl + rain)", "vinyl+rain"),
                  ("ملف من عندي  (my own file)", "file")]
APP = "OudLofiMaker"


class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("OudLofiMaker — وسيم")
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.tracks = {}   # tree item id -> dict(path, ar, en, dur)
        self.drag = None

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground="#9a8f80", font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground="#e0a458", font=("Segoe UI", 15, "bold"))
        st.configure("TEntry", fieldbackground="#1a1714", foreground="#f5efe6", insertcolor="#f5efe6")
        st.configure("TCombobox", fieldbackground="#1a1714", foreground="#f5efe6")
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", "#f5efe6")])
        st.configure("TButton", background="#2a241d", foreground="#f5efe6", padding=6)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")],
               foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background="#e0a458", foreground="#0a0a0a",
                     font=("Segoe UI", 12, "bold"), padding=10)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")
        st.configure("Horizontal.TScale", background="#0a0a0a", troughcolor="#1a1714")
        st.configure("Treeview", background="#141210", fieldbackground="#141210", foreground="#f5efe6",
                     rowheight=26, borderwidth=0)
        st.map("Treeview", background=[("selected", "#5a4428")])
        st.configure("Treeview.Heading", background="#2a241d", foreground="#e0a458",
                     font=("Segoe UI", 9, "bold"))
        st.configure("TLabelframe", background="#0a0a0a", bordercolor="#2b2620")
        st.configure("TLabelframe.Label", background="#0a0a0a", foreground="#e0a458",
                     font=("Segoe UI", 10, "bold"))

        f = ttk.Frame(root, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="OudLofiMaker  ·  فيديو عود طويل لليوتيوب", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))

        # -- tracks ------------------------------------------------------------------
        tf = ttk.LabelFrame(f, text="  التراكات — بالترتيب (اسحب أو استخدم ▲ ▼)  ", padding=8)
        tf.grid(row=1, column=0, sticky="nsew")
        f.rowconfigure(1, weight=1)
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        cols = ("n", "ar", "en", "dur", "file")
        self.tree = ttk.Treeview(tf, columns=cols, show="headings", height=7, selectmode="browse")
        for c, label, w, anchor in (("n", "#", 36, "center"), ("ar", "العنوان بالعربي", 200, "e"),
                                    ("en", "English title", 200, "w"), ("dur", "المدة", 64, "center"),
                                    ("file", "الملف", 260, "w")):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor=anchor, stretch=c in ("ar", "en", "file"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<ButtonPress-1>", self.drag_start)
        self.tree.bind("<B1-Motion>", self.drag_move)
        self.tree.bind("<ButtonRelease-1>", lambda e: setattr(self, "drag", None))
        self.tree.bind("<Delete>", lambda e: self.remove())

        bf = ttk.Frame(tf)
        bf.grid(row=0, column=2, sticky="n", padx=(8, 0))
        self.track_btns = []
        for text, cmd in (("ضيف تراكات…", self.add_dialog), ("▲  فوق", lambda: self.shift(-1)),
                          ("▼  تحت", lambda: self.shift(1)), ("شيل", self.remove), ("امسح الكل", self.clear)):
            b = ttk.Button(bf, text=text, command=cmd)
            b.pack(fill="x", pady=2)
            self.track_btns.append(b)

        ef = ttk.Frame(tf)
        ef.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        ef.columnconfigure(1, weight=1)
        ef.columnconfigure(3, weight=1)
        self.v_ar, self.v_en = tk.StringVar(), tk.StringVar()
        ttk.Label(ef, text="عنوان التراك المختار  (عربي)").grid(row=0, column=0, sticky="w")
        self.e_ar = ttk.Entry(ef, textvariable=self.v_ar, justify="right")
        self.e_ar.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Label(ef, text="English").grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.e_en = ttk.Entry(ef, textvariable=self.v_en)
        self.e_en.grid(row=0, column=3, sticky="ew", padx=6)
        self.v_ar.trace_add("write", lambda *a: self.edit_title())
        self.v_en.trace_add("write", lambda *a: self.edit_title())
        self.total_lbl = ttk.Label(tf, text="", style="Hint.TLabel")
        self.total_lbl.grid(row=2, column=0, columnspan=3, sticky="w", pady=(6, 0))

        # -- settings ----------------------------------------------------------------
        sf = ttk.Frame(f)
        sf.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        sf.columnconfigure(1, weight=1)
        self.v = {k: tk.StringVar() for k in ("bg", "mix_ar", "mix_en", "corner", "texture", "texfile")}
        self.vol = tk.IntVar(value=40)
        r = 0
        ttk.Label(sf, text="الخلفية — صورة أو فيديو قصير").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(sf, textvariable=self.v["bg"]).grid(row=r, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(sf, text="اختار…", command=lambda: self.browse("bg", BG_TYPES)).grid(row=r, column=2, sticky="ew")
        r += 1
        ttk.Label(sf, text="اسم الفيديو  (عربي)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(sf, textvariable=self.v["mix_ar"], justify="right").grid(
            row=r, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=3)
        r += 1
        ttk.Label(sf, text="Video title  (English)").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(sf, textvariable=self.v["mix_en"]).grid(row=r, column=1, columnspan=2, sticky="ew",
                                                          padx=(6, 0), pady=3)
        r += 1
        ttk.Label(sf, text="مكان اسم التراك").grid(row=r, column=0, sticky="w", pady=3)
        self.cb_corner = ttk.Combobox(sf, values=[a for a, _ in CORNER_LABELS], state="readonly", width=28)
        self.cb_corner.grid(row=r, column=1, sticky="w", padx=6, pady=3)
        r += 1
        ttk.Label(sf, text="طبقة lo-fi").grid(row=r, column=0, sticky="w", pady=3)
        tx = ttk.Frame(sf)
        tx.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6, pady=3)
        tx.columnconfigure(3, weight=1)
        self.cb_tex = ttk.Combobox(tx, values=[a for a, _ in TEXTURE_LABELS], state="readonly", width=28)
        self.cb_tex.grid(row=0, column=0, sticky="w")
        self.cb_tex.bind("<<ComboboxSelected>>", lambda e: self.sync())
        ttk.Label(tx, text="  الصوت").grid(row=0, column=1)
        self.scale = ttk.Scale(tx, from_=0, to=100, variable=self.vol, length=200,
                               command=lambda v: self.vol_lbl.configure(text="%d%%" % float(v)))
        self.scale.grid(row=0, column=2, padx=6)
        self.vol_lbl = ttk.Label(tx, text="40%", width=5)
        self.vol_lbl.grid(row=0, column=3, sticky="w")
        r += 1
        self.texrow = ttk.Frame(sf)
        self.texrow.grid(row=r, column=1, columnspan=2, sticky="ew", padx=6)
        self.texrow.columnconfigure(0, weight=1)
        ttk.Entry(self.texrow, textvariable=self.v["texfile"]).grid(row=0, column=0, sticky="ew")
        ttk.Button(self.texrow, text="ملف الـ texture…",
                   command=lambda: self.browse("texfile", AUDIO_TYPES)).grid(row=0, column=1, padx=(6, 0))
        r += 1
        ttk.Label(sf, text="آخر الوصف (لينكات، هاشتاجات…)").grid(row=r, column=0, sticky="nw", pady=3)
        self.footer = tk.Text(sf, height=3, bg="#1a1714", fg="#f5efe6", insertbackground="#f5efe6",
                              relief="flat", font=("Segoe UI", 10), wrap="word")
        self.footer.grid(row=r, column=1, columnspan=2, sticky="ew", padx=(6, 0), pady=3)
        r += 1

        # -- run -----------------------------------------------------------------------
        btns = ttk.Frame(f)
        btns.grid(row=3, column=0, sticky="ew", pady=(10, 4))
        btns.columnconfigure(0, weight=1)
        self.go = ttk.Button(btns, text="اعمل الفيديو  ▶", style="Go.TButton", command=self.start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.stop = ttk.Button(btns, text="إلغاء", command=self.cancel.set, state="disabled")
        self.stop.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(btns, text="افتح output", command=lambda: open_path(engine.OUTPUT_DIR)).grid(
            row=0, column=2, padx=(8, 0))
        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.grid(row=4, column=0, sticky="ew", pady=4)
        self.status = ttk.Label(f, text="جاهز", style="Hint.TLabel")
        self.status.grid(row=5, column=0, sticky="w")
        self.logbox = tk.Text(f, height=7, bg="#141210", fg="#d9cfc0", insertbackground="#f5efe6",
                              relief="flat", font=("Consolas", 9), wrap="word")
        self.logbox.grid(row=6, column=0, sticky="nsew", pady=(6, 0))

        self.cb_corner.current(0)
        self.cb_tex.current(1)
        self.load()
        self.sync()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if not engine.features.check("raqm"):
            self.log("تحذير: RAQM مش شغال — العربي هيطلع غلط. تأكد إن libfribidi-0.dll جنب python.exe")
        if autotest:
            self.clear()
            self.add_paths(autotest["tracks"])
            self.v["bg"].set(autotest["bg"])
            self.v["mix_ar"].set("عود وليل")
            self.v["mix_en"].set("Oud Night · Lo-Fi")
            self.cb_tex.current(1)
            self.vol.set(45)
            root.after(1500, self.start)

    # -- tracks --------------------------------------------------------------------
    def add_dialog(self):
        paths = filedialog.askopenfilenames(filetypes=AUDIO_TYPES)
        if paths:
            self.add_paths([os.path.normpath(p) for p in paths])

    def add_paths(self, paths, titles=None):
        for k, p in enumerate(paths):
            ar, en = titles[k] if titles else engine.default_titles(p)
            iid = self.tree.insert("", "end", values=("", ar, en, "…", os.path.basename(p)))
            self.tracks[iid] = {"path": p, "ar": ar, "en": en, "dur": None}
            threading.Thread(target=self.probe_dur, args=(iid, p), daemon=True).start()
        self.renumber()

    def probe_dur(self, iid, path):
        try:
            d = engine.probe(path)[0]
        except Exception:
            d = -1
        self.q.put(("dur", iid, d))

    def renumber(self):
        known = []
        for i, iid in enumerate(self.tree.get_children(), 1):
            self.tree.set(iid, "n", i)
            known.append(self.tracks[iid]["dur"])
        n = len(known)
        if not n:
            self.total_lbl.configure(text="ضيف التراكات (WAV / MP3) — بيتعملهم crossfade ٣ ثواني ويتظبط الصوت على -14 LUFS")
        elif all(d and d > 0 for d in known):
            tot = sum(known) - engine.XFADE * (n - 1)
            self.total_lbl.configure(text="%d تراك — مدة الفيديو حوالي %s" % (n, engine.fmt_time(tot, tot >= 3600)))
        else:
            self.total_lbl.configure(text="%d تراك" % n)

    def selected(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def on_select(self, _=None):
        iid = self.selected()
        self._loading = True
        self.v_ar.set(self.tracks[iid]["ar"] if iid else "")
        self.v_en.set(self.tracks[iid]["en"] if iid else "")
        self._loading = False

    def edit_title(self):
        iid = self.selected()
        if not iid or getattr(self, "_loading", False):
            return
        self.tracks[iid]["ar"], self.tracks[iid]["en"] = self.v_ar.get(), self.v_en.get()
        self.tree.set(iid, "ar", self.v_ar.get())
        self.tree.set(iid, "en", self.v_en.get())

    def shift(self, d):
        iid = self.selected()
        if iid:
            self.tree.move(iid, "", max(0, self.tree.index(iid) + d))
            self.tree.see(iid)
            self.renumber()

    def remove(self):
        iid = self.selected()
        if iid:
            nxt = self.tree.next(iid) or self.tree.prev(iid)
            self.tree.delete(iid)
            del self.tracks[iid]
            if nxt:
                self.tree.selection_set(nxt)
            self.on_select()
            self.renumber()

    def clear(self):
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.tracks.clear()
        self.on_select()
        self.renumber()

    def drag_start(self, e):
        self.drag = self.tree.identify_row(e.y) or None

    def drag_move(self, e):
        if not self.drag:
            return
        target = self.tree.identify_row(e.y)
        if target and target != self.drag:
            self.tree.move(self.drag, "", self.tree.index(target))
            self.renumber()

    # -- misc ------------------------------------------------------------------------
    def browse(self, key, types):
        cur = self.v[key].get()
        p = filedialog.askopenfilename(filetypes=types, initialdir=os.path.dirname(cur) if cur else None)
        if p:
            self.v[key].set(os.path.normpath(p))

    def sync(self):
        if TEXTURE_LABELS[self.cb_tex.current()][1] == "file":
            self.texrow.grid()
        else:
            self.texrow.grid_remove()

    def log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")

    def state(self):
        return {"bg": self.v["bg"].get(), "mix_ar": self.v["mix_ar"].get(), "mix_en": self.v["mix_en"].get(),
                "corner": CORNER_LABELS[self.cb_corner.current()][1],
                "texture": TEXTURE_LABELS[self.cb_tex.current()][1], "texfile": self.v["texfile"].get(),
                "vol": int(self.vol.get()), "footer": self.footer.get("1.0", "end").strip(),
                "tracks": [{k: self.tracks[i][k] for k in ("path", "ar", "en")} for i in self.tree.get_children()]}

    def load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        for k in ("bg", "mix_ar", "mix_en", "texfile"):
            if isinstance(d.get(k), str):
                self.v[k].set(d[k])
        for i, (_, key) in enumerate(CORNER_LABELS):
            if d.get("corner") == key:
                self.cb_corner.current(i)
        for i, (_, key) in enumerate(TEXTURE_LABELS):
            if d.get("texture") == key:
                self.cb_tex.current(i)
        if isinstance(d.get("vol"), int):
            self.vol.set(d["vol"])
            self.vol_lbl.configure(text="%d%%" % d["vol"])
        if isinstance(d.get("footer"), str):
            self.footer.insert("1.0", d["footer"])
        tr = [t for t in d.get("tracks") or [] if isinstance(t, dict) and os.path.isfile(t.get("path", ""))]
        if tr:
            self.add_paths([t["path"] for t in tr], [(t.get("ar", ""), t.get("en", "")) for t in tr])

    def save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                json.dump(self.state(), fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # -- run -------------------------------------------------------------------------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        s = self.state()
        if not s["tracks"]:
            return self.fail("ضيف تراك واحد على الأقل.")
        if not s["bg"].strip():
            return self.fail("اختار الخلفية (صورة أو فيديو).")
        self.save()
        self.cancel.clear()
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        for b in self.track_btns:
            b.configure(state="disabled")
        self.logbox.delete("1.0", "end")
        self.pb["value"] = 0
        job = engine.Job([engine.Track(t["path"], t["ar"], t["en"]) for t in s["tracks"]], s["bg"].strip(),
                         s["mix_ar"], s["mix_en"], s["corner"], s["texture"], s["texfile"].strip(), s["vol"],
                         s["footer"], progress=lambda fr, m="": self.q.put(("p", fr, m)),
                         log=lambda m: self.q.put(("l", m)), cancel=self.cancel)
        self.job = job

        def work():
            try:
                self.q.put(("done", job.run()))
            except engine.Cancelled:
                self.q.put(("cancel",))
            except Exception as ex:
                crash_log(traceback.format_exc())
                self.q.put(("err", str(ex)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def fail(self, msg):
        if self.autotest:
            self.finish_autotest(error=msg)
        else:
            messagebox.showerror(APP, msg)

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
                elif m[0] == "dur":
                    if m[1] in self.tracks:
                        self.tracks[m[1]]["dur"] = m[2]
                        self.tree.set(m[1], "dur", engine.fmt_time(m[2]) if m[2] > 0 else "؟")
                        self.renumber()
                else:
                    self.go.configure(state="normal")
                    self.stop.configure(state="disabled")
                    for b in self.track_btns:
                        b.configure(state="normal")
                    if m[0] == "done":
                        self.status.configure(text="خلص ✔  " + os.path.basename(m[1][0]))
                        for p in m[1]:
                            self.log(p)
                        if self.autotest:
                            self.finish_autotest(files=list(m[1]))
                        elif messagebox.askyesno(APP, "الفيديو جاهز ✔\nومعاه الوصف والصورة المصغّرة.\n\nأفتح فولدر output؟"):
                            open_path(os.path.dirname(m[1][0]))
                    elif m[0] == "cancel":
                        self.status.configure(text="اتلغى")
                        self.pb["value"] = 0
                    else:
                        self.status.configure(text="حصل خطأ")
                        self.log(m[1])
                        self.fail(m[1][:1500])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finish_autotest(self, files=None, error=None):
        data = {"error": error, "files": files or []}
        job = getattr(self, "job", None)
        if job is not None and not error:
            data.update(total=job.total, starts=job.starts, lufs=job.audio_lufs, tp=job.audio_tp,
                        order=[t.path for t in job.tracks])
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def close(self):
        self.cancel.set()
        self.save()
        self.root.destroy()


def open_path(p):
    os.makedirs(p, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(p)
    elif sys.platform == "darwin":
        os.system('open "%s"' % p)
    else:
        os.system('xdg-open "%s" >/dev/null 2>&1 &' % p)


def main():
    autotest = None
    if len(sys.argv) >= 5 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = {"out": sys.argv[2], "bg": sys.argv[3], "tracks": sys.argv[4:]}
    root = tk.Tk()
    root.minsize(940, 780)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
