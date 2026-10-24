"""ChorusCutter — window around engine.py."""
import json
import os
import queue
import subprocess
import sys
import threading
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # embedded Python skips the script folder
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
    import winsound
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.m4a *.aif *.aiff *.ogg"), ("All", "*.*")]


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
                pass  # still open by the player; temp folder cleans up


class App:
    def __init__(self, root, autotest=None):
        self.root = root
        self.autotest = autotest
        root.title("ChorusCutter %s — وسيم" % engine.VERSION)
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.analysis = None
        self.cands = []
        self.offsets = {}
        self.player = Player()
        self.summary = ""
        self.busy = False

        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground="#9a8f80", font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground="#e0a458", font=("Segoe UI", 15, "bold"))
        st.configure("TEntry", fieldbackground="#1a1714", foreground="#f5efe6", insertcolor="#f5efe6")
        st.configure("TButton", background="#2a241d", foreground="#f5efe6", padding=6)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")],
               foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background="#e0a458", foreground="#0a0a0a",
                     font=("Segoe UI", 11, "bold"), padding=8)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("TRadiobutton", background="#0a0a0a", foreground="#f5efe6")
        st.map("TRadiobutton", background=[("active", "#0a0a0a")])
        st.configure("TCheckbutton", background="#0a0a0a", foreground="#f5efe6")
        st.map("TCheckbutton", background=[("active", "#0a0a0a")])
        st.configure("Treeview", background="#141210", fieldbackground="#141210", foreground="#f5efe6",
                     rowheight=28, font=("Segoe UI", 10))
        st.configure("Treeview.Heading", background="#2a241d", foreground="#e0a458")
        st.map("Treeview", background=[("selected", "#e0a458")], foreground=[("selected", "#0a0a0a")])
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")

        f = ttk.Frame(root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="ChorusCutter  ·  أحسن حتة للريل", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.v = {k: tk.StringVar() for k in ("master", "stem", "song", "start")}
        self.length = tk.IntVar(value=20)
        self.vocal_export = tk.BooleanVar(value=False)
        r = 1
        r = self.file_row(f, r, "الماستر  (WAV / MP3)", "master")
        r = self.file_row(f, r, "Vocal stem (اختياري)", "stem")
        ttk.Label(f, text="اسم الأغنية  (Song name)").grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v["song"], justify="right").grid(
            row=r, column=1, columnspan=2, sticky="ew", pady=4, padx=(6, 0))
        r += 1

        ttk.Label(f, text="طول الكليب  (Length)").grid(row=r, column=0, sticky="w", pady=6)
        lf = ttk.Frame(f)
        lf.grid(row=r, column=1, sticky="w", padx=6)
        for L in engine.LENGTHS:
            ttk.Radiobutton(lf, text="%ds" % L, value=L, variable=self.length,
                            command=self.refresh).pack(side="left", padx=(0, 14))
        self.go = ttk.Button(f, text="دوّر  ▶", style="Go.TButton", command=self.analyze)
        self.go.grid(row=r, column=2, sticky="ew", pady=6)
        r += 1

        self.pb = ttk.Progressbar(f, maximum=1000)
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(4, 2))
        r += 1
        self.status = ttk.Label(f, text="اختار الماستر ودوس «دوّر»", style="Hint.TLabel")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        cols = ("n", "start", "end", "len", "on", "score", "loud", "vocal", "repeat")
        heads = ("#", "من", "لحد", "الطول", "بتبدأ على", "السكور", "علو الصوت", "غُنا", "تكرار")
        widths = (36, 90, 80, 80, 90, 70, 80, 60, 60)
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=5, selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(8, 4))
        self.tree.bind("<<TreeviewSelect>>", lambda e: (self.show_start(), self.play()))
        f.rowconfigure(r, weight=1)
        r += 1

        bar = ttk.Frame(f)
        bar.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        self.btns = []
        for txt, cmd in (("▶ شغّل", self.play), ("■ وقّف", self.player.stop),
                         ("−0.5s", lambda: self.nudge(-0.5)), ("+0.5s", lambda: self.nudge(0.5))):
            b = ttk.Button(bar, text=txt, command=cmd)
            b.pack(side="left", padx=(0, 6))
            self.btns.append(b)
        # type the start directly (e.g. 0:43.3) — same length
        ttk.Label(bar, text="  البداية:").pack(side="left")
        self.start_e = ttk.Entry(bar, textvariable=self.v["start"], width=8, justify="center")
        self.start_e.pack(side="left", padx=(4, 4))
        self.start_e.bind("<Return>", lambda e: self.set_start())
        self.start_e.bind("<KP_Enter>", lambda e: self.set_start())
        b = ttk.Button(bar, text="روح ▶", command=self.set_start)
        b.pack(side="left")
        self.btns.append(b)
        self.exp = ttk.Button(bar, text="Export  ⬇", style="Go.TButton", command=self.export)
        self.exp.pack(side="right")
        self.vchk = ttk.Checkbutton(bar, text="صدّر كمان نفس الحتة من الـ stem", variable=self.vocal_export)
        self.vchk.pack(side="right", padx=10)
        r += 1
        ttk.Button(f, text="افتح output", command=lambda: open_path(engine.OUTPUT_DIR)).grid(
            row=r, column=2, sticky="e", pady=(10, 0))
        self.result = ttk.Label(f, text="", style="Hint.TLabel")
        self.result.grid(row=r, column=0, columnspan=2, sticky="w", pady=(10, 0))

        self.v["stem"].trace_add("write", lambda *a: self.sync())
        self.load()
        self.sync()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if autotest:
            self.v["master"].set(autotest["master"])
            self.v["stem"].set(autotest.get("stem", ""))
            self.v["song"].set(engine.song_name_from(autotest["master"]))
            root.after(500, self.analyze)

    # -- widgets ---------------------------------------------------------------
    def file_row(self, f, r, label, key):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=60).grid(row=r, column=1, sticky="ew", pady=4, padx=6)

        def browse():
            cur = self.v[key].get()
            p = filedialog.askopenfilename(filetypes=AUDIO_TYPES,
                                           initialdir=os.path.dirname(cur) if cur else None)
            if p:
                self.v[key].set(os.path.normpath(p))
                if key == "master":
                    self.v["song"].set(engine.song_name_from(p))
                    self.v["stem"].set("")
        ttk.Button(f, text="اختار…", command=browse).grid(row=r, column=2, sticky="ew", pady=4)
        return r + 1

    def sync(self):
        has = bool(self.cands) and not self.busy
        for b in self.btns + [self.exp, self.start_e]:
            b.configure(state="normal" if has else "disabled")
        stem_ok = bool(self.v["stem"].get().strip())
        self.vchk.configure(state="normal" if stem_ok else "disabled")
        if not stem_ok:
            self.vocal_export.set(False)
        self.go.configure(state="disabled" if self.busy else "normal")

    # -- settings ----------------------------------------------------------------
    def load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("length") in engine.LENGTHS:
                self.length.set(data["length"])
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as fh:
                json.dump({"length": self.length.get()}, fh)
        except OSError:
            pass

    # -- analysis ------------------------------------------------------------------
    def analyze(self):
        master = self.v["master"].get().strip()
        stem = self.v["stem"].get().strip() or None
        if not master or not os.path.isfile(master):
            messagebox.showerror("ChorusCutter", "اختار ملف الماستر الأول")
            return
        if stem and not os.path.isfile(stem):
            messagebox.showerror("ChorusCutter", "ملف الـ vocal stem مش موجود")
            return
        if not self.v["song"].get().strip():
            self.v["song"].set(engine.song_name_from(master))
        self.player.stop()
        self.busy = True
        self.cands = []
        self.tree.delete(*self.tree.get_children())
        self.result.configure(text="")
        self.sync()

        def work():
            try:
                an = engine.Analysis(master, stem,
                                     progress=lambda fr, m="": self.q.put(("p", fr, m)),
                                     log=lambda m: self.q.put(("s", m)))
                self.q.put(("done", an))
            except Exception as ex:
                crash_log(traceback.format_exc())
                self.q.put(("err", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        self.save()
        if not self.analysis:
            return
        self.player.stop()
        self.cands = self.analysis.candidates(self.length.get())
        self.offsets = {}
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(self.cands):
            self.tree.insert("", "end", iid=str(i), values=self.row(i))
        self.sync()
        if self.cands:
            an = self.analysis
            self.status.configure(text="BPM %.1f   |   %d bars   |   %d جملة غُنا   |   vocal: %s   |   دوس على أي سطر يشتغل" % (
                an.bpm, len(an.bars) - 1, len(an.phrases), "stem" if an.vocal_source == "stem" else "estimate"))
        else:
            self.status.configure(text="الأغنية أقصر من طول الكليب")

    def row(self, i):
        c = self.cands[i]
        s = c["start"] + self.offsets.get(i, 0.0)
        off = self.offsets.get(i, 0.0)
        mark = "" if not off else (" (%+.1f)" % off)
        L = c["length"]
        # real length; ↑ = grew past the chosen length to finish a phrase
        ln = "%.1fs%s" % (L, " ↑" if L > c["target"] + 0.05 else "")
        return (i + 1, engine.fmt_time(s) + mark, engine.fmt_time(s + L), ln,
                "أول كلمة" if c["on_word"] else "مازورة", "%.0f" % c["score"], "%.0f" % c["loud"],
                "%.0f" % c["vocal"], "%.0f" % c["repeat"])

    def selected(self):
        sel = self.tree.selection()
        if not sel and self.cands:
            self.tree.selection_set("0")
            sel = ("0",)
        if not sel:
            return None, None
        i = int(sel[0])
        c = self.cands[i]
        start = max(0.0, c["start"] + self.offsets.get(i, 0.0))
        length = min(c["length"], self.analysis.duration - start)
        return i, (start, length)

    def show_start(self):
        i, w = self.selected()
        if w is not None:
            self.v["start"].set(engine.fmt_time(w[0]))

    def set_start(self):
        """Typed start (0:43.3 / 43.3): same length, clamped to the song."""
        i, w = self.selected()
        if w is None:
            return
        try:
            t = engine.parse_time(self.v["start"].get())
        except ValueError:
            self.result.configure(text="اكتب البداية كده: 0:43.3  أو  43.3")
            return
        c = self.cands[i]
        t = max(0.0, min(t, self.analysis.duration - c["length"]))
        self.offsets[i] = t - c["start"]
        self.tree.item(str(i), values=self.row(i))
        self.v["start"].set(engine.fmt_time(t))
        self.play()

    def play(self):
        i, w = self.selected()
        if w is None:
            return
        try:
            path = engine.preview(self.v["master"].get().strip(), *w, fade=self.cands[i]["fade"])
            self.player.play(path)
            self.result.configure(text="بيشتغل من %s لحد %s" % (engine.fmt_time(w[0]), engine.fmt_time(w[0] + w[1])))
        except Exception as ex:
            self.result.configure(text="مش قادر أشغّل الصوت: %s" % str(ex)[:120])

    def nudge(self, d):
        i, w = self.selected()
        if w is None:
            return
        c = self.cands[i]
        off = self.offsets.get(i, 0.0) + d
        off = max(-c["start"], min(off, self.analysis.duration - c["start"] - c["length"]))
        self.offsets[i] = round(off, 2)
        self.tree.item(str(i), values=self.row(i))
        self.show_start()
        self.play()

    def export(self):
        i, w = self.selected()
        if w is None:
            return
        song = self.v["song"].get().strip() or engine.song_name_from(self.v["master"].get())
        try:
            fade = self.cands[i]["fade"]
            out = list(engine.export(self.v["master"].get().strip(), w[0], w[1], song, fade=fade))
            if self.vocal_export.get() and self.v["stem"].get().strip():
                out += engine.export(self.v["stem"].get().strip(), w[0], w[1], song, suffix=" - vocal", fade=fade)
        except Exception as ex:
            crash_log(traceback.format_exc())
            messagebox.showerror("ChorusCutter", str(ex)[:1500])
            return
        names = [os.path.basename(p) for p in out]
        self.result.configure(text="اتصدّر ✔  " + "  |  ".join(n for n in names if n.endswith(".wav")))
        if self.autotest:
            with open(self.autotest["report"], "w", encoding="utf-8") as fh:
                json.dump({"bpm": self.analysis.bpm, "files": out, "start": w[0], "length": w[1],
                           "song": song, "typed": self.autotest.get("typed"),
                           "phrases": len(self.analysis.phrases),
                           "cands": [[c["start"], c["length"], c["score"], c["on_word"]] for c in self.cands]},
                          fh, ensure_ascii=False)
            return
        if messagebox.askyesno("ChorusCutter", "اتصدّر ✔\n" + "\n".join(names) + "\n\nأفتح فولدر output؟"):
            open_path(os.path.dirname(out[0]))

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "p":
                    self.pb["value"] = int(m[1] * 1000)
                    if m[2]:
                        self.status.configure(text=m[2])
                elif m[0] == "s":
                    self.summary = m[1]
                elif m[0] == "done":
                    self.busy = False
                    self.analysis = m[1]
                    self.pb["value"] = 1000
                    self.refresh()
                    if self.autotest and self.cands:
                        self.tree.selection_set("0")
                        self.nudge(0.5)
                        # then type a start like a user would: first word + 1.3 s, written m:ss.s
                        typed = round(self.cands[0]["start"] + 1.3, 1)
                        self.autotest["typed"] = typed
                        self.v["start"].set("%d:%04.1f" % (typed // 60, typed % 60))
                        self.set_start()
                        self.vocal_export.set(bool(self.v["stem"].get()))
                        self.export()
                elif m[0] == "err":
                    self.busy = False
                    self.pb["value"] = 0
                    self.sync()
                    self.status.configure(text="حصل خطأ")
                    if self.autotest:
                        with open(self.autotest["report"], "w", encoding="utf-8") as fh:
                            json.dump({"error": m[1]}, fh, ensure_ascii=False)
                    else:
                        messagebox.showerror("ChorusCutter", m[1][:1500])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def close(self):
        self.player.stop()
        self.save()
        self.root.destroy()


def open_path(p):
    os.makedirs(p, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(p)
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    autotest = None
    if len(sys.argv) >= 4 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = {"master": sys.argv[2], "report": sys.argv[3],
                    "stem": sys.argv[4] if len(sys.argv) > 4 else ""}
    root = tk.Tk()
    root.minsize(820, 600)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
