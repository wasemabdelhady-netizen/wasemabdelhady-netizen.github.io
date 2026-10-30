"""Reel Maker — simple window around engine.py."""
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
VERSION = "v3.0"


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

VIDEO_TYPES = [("Video", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi"), ("All", "*.*")]
AUDIO_TYPES = [("Audio", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.mp4 *.mov"), ("All", "*.*")]
TEXT_TYPES = [("Text", "*.txt *.lrc"), ("All", "*.*")]


class App:
    def __init__(self, root, container=None):
        self.root = root
        root.title("Reel Maker %s — وسيم" % VERSION)
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        base = ("Segoe UI", 10)
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=base)
        st.configure("TLabel", background="#0a0a0a", foreground="#f5efe6")
        st.configure("Hint.TLabel", foreground="#9a8f80", font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground="#e0a458", font=("Segoe UI", 15, "bold"))
        st.configure("TEntry", fieldbackground="#1a1714", foreground="#f5efe6", insertcolor="#f5efe6")
        st.configure("TCombobox", fieldbackground="#1a1714", foreground="#f5efe6")
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", "#f5efe6")])
        st.configure("TButton", background="#2a241d", foreground="#f5efe6", padding=6)
        st.map("TButton", background=[("active", "#3a3128")])
        st.configure("Go.TButton", background="#e0a458", foreground="#0a0a0a",
                     font=("Segoe UI", 12, "bold"), padding=10)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")

        st.configure("TNotebook", background="#0a0a0a", borderwidth=0)
        st.configure("TNotebook.Tab", background="#1a1714", foreground="#9a8f80", padding=(18, 7),
                     font=("Segoe UI", 11, "bold"))
        st.map("TNotebook.Tab", background=[("selected", "#e0a458")], foreground=[("selected", "#0a0a0a")])

        f = ttk.Frame(container or root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="Reel Maker  ·  ريلز الكلمات", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.v = {k: tk.StringVar() for k in
                  ("bg", "audio", "lyrics", "song", "hook", "closing", "lfont", "hfont", "start", "end")}
        self.v["closing"].set(engine.DEFAULT_CLOSING)
        r = 1
        r = self.file_row(f, r, "فيديو الخلفية  (Background video)", "bg", VIDEO_TYPES)
        r = self.file_row(f, r, "الصوت  (Song / voice clip)", "audio", AUDIO_TYPES)
        r = self.file_row(f, r, "ملف الكلمات  (Lyrics .txt)", "lyrics", TEXT_TYPES)
        r = self.entry_row(f, r, "اسم الأغنية  (Song name)", "song")
        r = self.entry_row(f, r, "الهوك — أول 5 ثواني  (Hook)", "hook")
        r = self.entry_row(f, r, "جملة النهاية — آخر ثانيتين  (Closing)", "closing")

        fonts = engine.font_list()
        ttk.Label(f, text="فونت الكلمات  (Lyrics font)").grid(row=r, column=0, sticky="w", pady=4)
        self.cb_l = ttk.Combobox(f, textvariable=self.v["lfont"], values=fonts, state="readonly")
        self.cb_l.grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        r += 1
        ttk.Label(f, text="فونت الهوك  (Hook font)").grid(row=r, column=0, sticky="w", pady=4)
        self.cb_h = ttk.Combobox(f, textvariable=self.v["hfont"], values=fonts, state="readonly")
        self.cb_h.grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        ttk.Button(f, text="فولدر الفونتات", command=lambda: open_path(engine.FONTS_DIR)).grid(
            row=r, column=2, sticky="ew", pady=4)
        r += 1
        self.v["lfont"].set(pick(fonts, "ArefRuqaa-Bold.ttf", "aref"))
        self.v["hfont"].set(pick(fonts, "Alexandria.ttf", "alexandria"))

        trim = ttk.Frame(f)
        trim.grid(row=r, column=1, sticky="w", padx=6, pady=4)
        ttk.Label(f, text="قص الصوت (اختياري)  (Trim)").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Label(trim, text="من").pack(side="left")
        ttk.Entry(trim, textvariable=self.v["start"], width=8).pack(side="left", padx=(4, 12))
        ttk.Label(trim, text="لحد").pack(side="left")
        ttk.Entry(trim, textvariable=self.v["end"], width=8).pack(side="left", padx=4)
        ttk.Label(trim, text="مثال 0:45 أو 45.5", style="Hint.TLabel").pack(side="left", padx=8)
        r += 1

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(14, 6))
        btns.columnconfigure(0, weight=1)
        self.go = ttk.Button(btns, text="اعمل الريل  ▶", style="Go.TButton", command=self.start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.stop = ttk.Button(btns, text="إلغاء", command=self.cancel.set, state="disabled")
        self.stop.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(btns, text="افتح output", command=lambda: open_path(engine.OUTPUT_DIR)).grid(
            row=0, column=2, padx=(8, 0))
        r += 1

        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew", pady=4)
        r += 1
        self.status = ttk.Label(f, text="جاهز", style="Hint.TLabel")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1
        self.logbox = tk.Text(f, height=9, bg="#141210", fg="#d9cfc0", insertbackground="#f5efe6",
                              relief="flat", font=("Consolas", 9), wrap="word")
        self.logbox.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        f.rowconfigure(r, weight=1)

        self.load()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if not engine.features.check("raqm"):
            self.log("تحذير: RAQM مش شغال — العربي هيطلع غلط. تأكد إن libfribidi-0.dll جنب python.exe")

    # -- widgets ---------------------------------------------------------------
    def file_row(self, f, r, label, key, types):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=58).grid(row=r, column=1, sticky="ew", pady=4, padx=6)

        def browse():
            cur = self.v[key].get()
            p = filedialog.askopenfilename(filetypes=types,
                                           initialdir=os.path.dirname(cur) if cur else None)
            if p:
                self.v[key].set(os.path.normpath(p))
        ttk.Button(f, text="اختار…", command=browse).grid(row=r, column=2, sticky="ew", pady=4)
        return r + 1

    def entry_row(self, f, r, label, key):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        e = ttk.Entry(f, textvariable=self.v[key], width=58, justify="right")
        e.grid(row=r, column=1, columnspan=2, sticky="ew", pady=4, padx=(6, 0))
        return r + 1

    def log(self, msg):
        self.logbox.insert("end", msg + "\n")
        self.logbox.see("end")

    # -- settings ----------------------------------------------------------------
    def load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for k, var in self.v.items():
            if k in data and isinstance(data[k], str):
                if k in ("lfont", "hfont") and data[k] not in engine.font_list():
                    continue
                var.set(data[k])

    def save(self):
        data = read_settings()
        data.update({k: v.get() for k, v in self.v.items()})
        write_settings(data)

    # -- run -----------------------------------------------------------------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        g = {k: v.get().strip() for k, v in self.v.items()}
        try:
            s, e = engine.parse_time(g["start"]), engine.parse_time(g["end"])
        except ValueError as ex:
            messagebox.showerror("Reel Maker", str(ex))
            return
        if not g["lyrics"]:
            if not messagebox.askyesno("Reel Maker", "مفيش ملف كلمات — أكمل من غير كلمات؟"):
                return
        self.save()
        self.cancel.clear()
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.pb["value"] = 0
        job = engine.Job(g["bg"], g["audio"], g["lyrics"], song=g["song"], hook=g["hook"],
                         closing=g["closing"], lyric_font=g["lfont"], hook_font=g["hfont"],
                         start=s, end=e,
                         progress=lambda fr, m="": self.q.put(("p", fr, m)),
                         log=lambda m: self.q.put(("l", m)), cancel=self.cancel)

        def work():
            try:
                out = job.run()
                self.q.put(("done", out))
            except engine.Cancelled:
                self.q.put(("cancel",))
            except Exception as ex:
                crash_log(traceback.format_exc())
                self.q.put(("err", str(ex)))
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
                    self.go.configure(state="normal")
                    self.stop.configure(state="disabled")
                    if m[0] == "done":
                        self.status.configure(text="خلص ✔  " + os.path.basename(m[1][0]))
                        if messagebox.askyesno("Reel Maker", "الريل جاهز ✔\nأفتح فولدر output؟"):
                            open_path(os.path.dirname(m[1][0]))
                    elif m[0] == "cancel":
                        self.status.configure(text="اتلغى")
                        self.pb["value"] = 0
                    else:
                        self.status.configure(text="حصل خطأ")
                        self.log(m[1])
                        messagebox.showerror("Reel Maker", m[1][:1500])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        self.cancel.set()
        self.save()
        self.root.destroy()


def read_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_settings(data):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass


def pick(fonts, preferred, contains):
    if preferred in fonts:
        return preferred
    for f in fonts:
        if contains in f.lower():
            return f
    return fonts[0] if fonts else ""


def open_path(p):
    os.makedirs(p, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(p)
    elif sys.platform == "darwin":
        os.system('open "%s"' % p)
    else:
        os.system('xdg-open "%s" >/dev/null 2>&1 &' % p)


def main():
    autotest = cine_test = None
    if "--autotest-auto" in sys.argv:  # CI: drive the auto tab from a JSON file
        with open(sys.argv[sys.argv.index("--autotest-auto") + 1], encoding="utf-8") as fh:
            autotest = json.load(fh)
    if "--autotest-cine" in sys.argv:  # CI: drive the cinematic tab
        with open(sys.argv[sys.argv.index("--autotest-cine") + 1], encoding="utf-8") as fh:
            cine_test = json.load(fh)
    root = tk.Tk()
    root.report_callback_exception = lambda *exc: crash_log("".join(traceback.format_exception(*exc)))
    root.minsize(900, 720)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    manual = ttk.Frame(nb)
    auto = ttk.Frame(nb)
    cine = ttk.Frame(nb)
    nb.add(manual, text="  يدوي  ")
    nb.add(auto, text="  أوتوماتيك  ")
    nb.add(cine, text="  سينمائي - Cinematic  ")
    app = App(root, manual)
    settings = read_settings()

    def save_auto():
        data = read_settings()
        data["auto"] = settings.get("auto", {})
        data["cine"] = settings.get("cine", {})
        data["tab"] = nb.index("current")
        write_settings(data)
    import autotab
    import cinetab
    tab = autotab.AutoTab(root, auto, settings, save_auto, crash_log, open_path, autotest)
    ctab = cinetab.CineTab(root, cine, settings, save_auto, crash_log, open_path, cine_test)
    if autotest or (settings.get("tab") == 1 and not cine_test):
        nb.select(1)
    if cine_test or (settings.get("tab") == 2 and not autotest):
        nb.select(2)

    def close():
        tab.close()
        ctab.close()
        app.close()  # saves the manual fields (keeps the auto ones) and closes
    root.protocol("WM_DELETE_WINDOW", close)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
