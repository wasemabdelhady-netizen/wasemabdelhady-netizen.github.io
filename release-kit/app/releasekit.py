"""ReleaseKit — simple window around engine.py."""
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

IMAGE_TYPES = [("Image", "*.jpg *.jpeg *.png *.webp *.tif *.tiff *.bmp"), ("All", "*.*")]
VIDEO_TYPES = [("Video", "*.mp4 *.mov *.m4v *.mkv *.webm *.avi"), ("All", "*.*")]
AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.aif *.aiff *.m4a *.aac *.ogg *.opus"), ("All", "*.*")]
PARTS = [("images", "الصور (6 مقاسات)"), ("canvas", "Spotify Canvas"),
         ("teaser", "Teaser 20 ث"), ("visualizer", "YouTube Visualizer")]


class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("ReleaseKit — وسيم")
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=("Segoe UI", 10))
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
        st.configure("TCheckbutton", background="#0a0a0a", foreground="#f5efe6")
        st.map("TCheckbutton", background=[("active", "#0a0a0a")])
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")

        f = ttk.Frame(root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="ReleaseKit  ·  كل مقاسات الإصدار", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.v = {k: tk.StringVar() for k in
                  ("cover", "clip", "audio", "ar", "en", "artist", "tag", "arfont", "enfont", "out")}
        self.v["artist"].set("Wasem Abdelhady")
        self.v["tag"].set(engine.DEFAULT_TAG)
        self.v["out"].set(engine.OUTPUT_DIR)
        r = 1
        r = self.file_row(f, r, "الغلاف — صورة مربعة  (Cover)", "cover", IMAGE_TYPES)
        r = self.file_row(f, r, "الماستر  (Master audio)", "audio", AUDIO_TYPES)
        r = self.file_row(f, r, "فيديو قصير للـ Canvas (اختياري)", "clip", VIDEO_TYPES, clear=True)
        r = self.entry_row(f, r, "اسم الأغنية عربي", "ar", "right")
        r = self.entry_row(f, r, "Song name (English)", "en", "left")
        r = self.entry_row(f, r, "اسم الفنان  (Artist)", "artist", "left")
        r = self.entry_row(f, r, "سطر الستوري (فوق الغلاف)", "tag", "right")

        fonts = engine.font_list()
        ttk.Label(f, text="فونت العربي  (Arabic font)").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v["arfont"], values=fonts, state="readonly").grid(
            row=r, column=1, sticky="ew", pady=4, padx=6)
        r += 1
        ttk.Label(f, text="فونت الإنجليزي  (English font)").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Combobox(f, textvariable=self.v["enfont"], values=fonts, state="readonly").grid(
            row=r, column=1, sticky="ew", pady=4, padx=6)
        ttk.Button(f, text="فولدر الفونتات", command=lambda: open_path(engine.FONTS_DIR)).grid(
            row=r, column=2, sticky="ew", pady=4)
        r += 1
        self.v["arfont"].set(pick(fonts, engine.DEFAULT_AR_FONT, "aref"))
        self.v["enfont"].set(pick(fonts, engine.DEFAULT_EN_FONT, "syne"))

        ttk.Label(f, text="فولدر الحفظ  (Save to)").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v["out"], width=58).grid(row=r, column=1, sticky="ew", pady=4, padx=6)

        def browse_out():
            p = filedialog.askdirectory(initialdir=self.v["out"].get() or engine.OUTPUT_DIR)
            if p:
                self.v["out"].set(os.path.normpath(p))
        ttk.Button(f, text="اختار…", command=browse_out).grid(row=r, column=2, sticky="ew", pady=4)
        r += 1

        ttk.Label(f, text="اعمل إيه").grid(row=r, column=0, sticky="w", pady=4)
        box = ttk.Frame(f)
        box.grid(row=r, column=1, columnspan=2, sticky="w", padx=6)
        self.parts = {}
        for key, label in PARTS:
            var = tk.BooleanVar(value=True)
            self.parts[key] = var
            ttk.Checkbutton(box, text=label, variable=var).pack(side="left", padx=(0, 14))
        r += 1

        btns = ttk.Frame(f)
        btns.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(14, 6))
        btns.columnconfigure(0, weight=1)
        self.go = ttk.Button(btns, text="اعمل الـ Release Kit  ▶", style="Go.TButton", command=self.start)
        self.go.grid(row=0, column=0, sticky="ew")
        self.stop = ttk.Button(btns, text="إلغاء", command=self.cancel.set, state="disabled")
        self.stop.grid(row=0, column=1, padx=(8, 0))
        ttk.Button(btns, text="افتح الفولدر", command=lambda: open_path(self.v["out"].get() or engine.OUTPUT_DIR)).grid(
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

        if autotest:
            for k, val in autotest["fields"].items():
                self.v[k].set(val)
            root.after(800, self.start)
        else:
            self.load()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if not engine.features.check("raqm"):
            self.log("تحذير: RAQM مش شغال — العربي هيطلع غلط. تأكد إن libfribidi-0.dll جنب python.exe")

    # -- widgets ---------------------------------------------------------------
    def file_row(self, f, r, label, key, types, clear=False):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=58).grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        bf = ttk.Frame(f)
        bf.grid(row=r, column=2, sticky="ew")

        def browse():
            cur = self.v[key].get()
            p = filedialog.askopenfilename(filetypes=types, initialdir=os.path.dirname(cur) if cur else None)
            if p:
                self.v[key].set(os.path.normpath(p))
        ttk.Button(bf, text="اختار…", command=browse).pack(side="left", fill="x", expand=True)
        if clear:
            ttk.Button(bf, text="✕", width=3, command=lambda: self.v[key].set("")).pack(side="left", padx=(4, 0))
        return r + 1

    def entry_row(self, f, r, label, key, justify):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=58, justify=justify).grid(
            row=r, column=1, columnspan=2, sticky="ew", pady=4, padx=(6, 0))
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
                if k in ("arfont", "enfont") and data[k] not in engine.font_list():
                    continue
                var.set(data[k])
        for k, var in self.parts.items():
            if isinstance(data.get("part_" + k), bool):
                var.set(data["part_" + k])

    def save(self):
        if self.autotest:
            return
        data = {k: v.get() for k, v in self.v.items()}
        data.update({"part_" + k: v.get() for k, v in self.parts.items()})
        try:
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
        except OSError:
            pass

    # -- run -----------------------------------------------------------------------
    def start(self):
        if self.worker and self.worker.is_alive():
            return
        g = {k: v.get().strip() for k, v in self.v.items()}
        parts = [k for k, v in self.parts.items() if v.get()]
        if not parts:
            messagebox.showerror("ReleaseKit", "اختار حاجة واحدة على الأقل تتعمل")
            return
        self.save()
        self.cancel.clear()
        self.go.configure(state="disabled")
        self.stop.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.pb["value"] = 0
        job = engine.Job(g["cover"], g["audio"], g["ar"], g["en"], g["artist"], clip=g["clip"], tag=g["tag"],
                         ar_font=g["arfont"], en_font=g["enfont"], parts=parts, out_dir=g["out"] or None,
                         progress=lambda fr, m="": self.q.put(("p", fr, m)),
                         log=lambda m: self.q.put(("l", m)), cancel=self.cancel)

        def work():
            try:
                self.q.put(("done", job.run(), job.teaser_start))
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
                        kit, files = m[1]
                        self.status.configure(text="خلص ✔  " + os.path.basename(kit))
                        if self.autotest:
                            self.finish_autotest(kit=kit, files=files, teaser_start=m[2])
                        elif messagebox.askyesno("ReleaseKit", "الـ Release Kit جاهز ✔\n%d ملف\n\nأفتح الفولدر؟" % len(files)):
                            open_path(kit)
                    elif m[0] == "cancel":
                        self.status.configure(text="اتلغى")
                        self.pb["value"] = 0
                    else:
                        self.status.configure(text="حصل خطأ")
                        self.log(m[1])
                        if self.autotest:
                            self.finish_autotest(error=m[1])
                        else:
                            messagebox.showerror("ReleaseKit", m[1][:1500])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finish_autotest(self, kit=None, files=None, teaser_start=None, error=None):
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump({"error": error, "kit": kit, "files": files or [], "teaser_start": teaser_start},
                      fh, ensure_ascii=False)

    def close(self):
        self.cancel.set()
        self.save()
        self.root.destroy()


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
    autotest = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        with open(sys.argv[2], encoding="utf-8-sig") as fh:
            autotest = json.load(fh)
    root = tk.Tk()
    root.minsize(780, 700)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
