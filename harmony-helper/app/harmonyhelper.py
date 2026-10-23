"""HarmonyHelper — window around engine.py / export.py."""
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
    import export
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

AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.aif *.aiff *.m4a *.ogg"), ("All", "*.*")]
KEYS = [(t, sc) for sc in ("major", "minor") for t in range(12)]


class Player:
    def __init__(self):
        self.proc, self.file = None, None

    def play(self, path):
        self.stop()
        self.file = path
        if sys.platform == "win32":
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
            return
        for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path], ["paplay", path], ["aplay", "-q", path]):
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


def open_path(p):
    try:
        if sys.platform == "win32":
            os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("HarmonyHelper — وسيم")
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.song = None
        self.busy = False
        self.player = Player()
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground="#9a8f80", font=("Segoe UI", 9))
        st.configure("Warn.TLabel", foreground="#ffcc66", font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground="#e0a458", font=("Segoe UI", 15, "bold"))
        st.configure("Big.TLabel", foreground="#e0a458", font=("Segoe UI", 13, "bold"))
        st.configure("TEntry", fieldbackground="#1a1714", foreground="#f5efe6", insertcolor="#f5efe6")
        st.configure("TSpinbox", fieldbackground="#1a1714", foreground="#f5efe6", arrowcolor="#f5efe6")
        st.configure("TCombobox", fieldbackground="#1a1714", foreground="#f5efe6")
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", "#f5efe6")])
        st.configure("TButton", background="#2a241d", foreground="#f5efe6", padding=5)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")], foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background="#e0a458", foreground="#0a0a0a", font=("Segoe UI", 11, "bold"), padding=8)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("TCheckbutton", background="#0a0a0a", foreground="#f5efe6")
        st.map("TCheckbutton", background=[("active", "#0a0a0a")])
        st.configure("Treeview", background="#141210", fieldbackground="#141210", foreground="#f5efe6", rowheight=24)
        st.configure("Treeview.Heading", background="#2a241d", foreground="#e0a458")
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")
        st.configure("TLabelframe", background="#0a0a0a", bordercolor="#2b2620")
        st.configure("TLabelframe.Label", background="#0a0a0a", foreground="#e0a458", font=("Segoe UI", 10, "bold"))

        f = ttk.Frame(root, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="HarmonyHelper  ·  كوردات ولحن وهارموني", style="Title.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
        self.v = {k: tk.StringVar() for k in ("song", "stem", "key", "start")}
        self.v["start"].set("0")
        r = 1
        r = self.file_row(f, r, "الأغنية كاملة (WAV / MP3)", "song")
        r = self.file_row(f, r, "Vocal stem (اختياري — للحن والهارموني)", "stem")
        self.go = ttk.Button(f, text="حلّل  ▶", style="Go.TButton", command=self.analyze)
        self.go.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        r += 1
        self.pb = ttk.Progressbar(f, mode="indeterminate")
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1
        self.status = ttk.Label(f, text="اختار الأغنية (والـ stem لو معاك) ودوس «حلّل»", style="Hint.TLabel")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w")
        r += 1

        info = ttk.Frame(f)
        info.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(8, 4))
        self.bpm_l = ttk.Label(info, text="BPM —", style="Big.TLabel")
        self.bpm_l.pack(side="left")
        ttk.Label(info, text="     السلم:").pack(side="left")
        self.key_cb = ttk.Combobox(info, textvariable=self.v["key"], state="disabled", width=14,
                                   values=[engine.key_name(t, sc) for t, sc in KEYS])
        self.key_cb.pack(side="left", padx=6)
        self.key_cb.bind("<<ComboboxSelected>>", lambda e: self.change_key())
        self.tune_l = ttk.Label(info, text="", style="Hint.TLabel")
        self.tune_l.pack(side="left", padx=10)
        r += 1
        self.maqam_l = ttk.Label(f, text="", style="Warn.TLabel", wraplength=860, justify="right")
        self.maqam_l.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1

        cols = ("bar", "time", "daw", "c1", "c2", "c3", "c4", "qt")
        heads = ("بار", "الوقت", "DAW", "1", "2", "3", "4", "")
        widths = (50, 80, 70, 70, 70, 70, 70, 90)
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=9)
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="center")
        self.tree.tag_configure("qt", foreground="#ffcc66")
        self.tree.grid(row=r, column=0, columnspan=3, sticky="nsew", pady=(4, 6))
        f.rowconfigure(r, weight=1)
        r += 1

        hf = ttk.LabelFrame(f, text="  اللحن والهارموني — علّم اللي عايز تصدّره، ودوس ▶ تسمعه مع الأغنية  ", padding=8)
        hf.grid(row=r, column=0, columnspan=3, sticky="ew")
        self.chk = {}
        items = [("chords", "Chords (كوردات)"), ("melody", "Melody (اللحن)")] + [(k, lab) for k, lab, _, _ in engine.LINES]
        self.preview_btns = []
        for i, (k, lab) in enumerate(items):
            var = tk.BooleanVar(value=k in ("chords", "melody", "third_up", "third_down"))
            self.chk[k] = var
            row, col = divmod(i, 2)
            cell = ttk.Frame(hf)
            cell.grid(row=row, column=col, sticky="w", padx=(0, 30), pady=2)
            cb = ttk.Checkbutton(cell, text=lab, variable=var)
            cb.pack(side="left")
            if k in ("chords", "melody"):
                cb.configure(state="disabled")  # always exported
            b = ttk.Button(cell, text="▶", width=3, command=lambda k=k: self.preview(k))
            b.pack(side="left", padx=6)
            self.preview_btns.append(b)
        ctl = ttk.Frame(hf)
        ctl.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(ctl, text="نقل الهارموني (نص تون):").pack(side="left")
        self.tr = tk.IntVar(value=0)
        ttk.Spinbox(ctl, from_=-12, to=12, textvariable=self.tr, width=5).pack(side="left", padx=6)
        ttk.Label(ctl, text="   اسمع من ثانية:").pack(side="left")
        ttk.Entry(ctl, textvariable=self.v["start"], width=7).pack(side="left", padx=6)
        ttk.Button(ctl, text="■ وقّف", command=self.player.stop).pack(side="left", padx=6)
        r += 1
        bot = ttk.Frame(f)
        bot.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        self.exp = ttk.Button(bot, text="صدّر MIDI  ⬇", style="Go.TButton", command=self.export)
        self.exp.pack(side="right")
        self.result = ttk.Label(bot, text="", style="Hint.TLabel")
        self.result.pack(side="left")
        self.sync()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)
        if autotest:
            self.v["song"].set(autotest["song"])
            self.v["stem"].set(autotest.get("stem", ""))
            root.after(500, self.analyze)

    def file_row(self, f, r, label, key):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=3)
        ttk.Entry(f, textvariable=self.v[key], width=60).grid(row=r, column=1, sticky="ew", pady=3, padx=6)
        bf = ttk.Frame(f)
        bf.grid(row=r, column=2, sticky="w")

        def browse():
            cur = self.v[key].get()
            p = filedialog.askopenfilename(filetypes=AUDIO_TYPES, initialdir=os.path.dirname(cur) if cur else None)
            if p:
                self.v[key].set(os.path.normpath(p))
        ttk.Button(bf, text="اختار…", command=browse).pack(side="left")
        if key == "stem":
            ttk.Button(bf, text="✕", width=3, command=lambda: self.v[key].set("")).pack(side="left", padx=(4, 0))
        return r + 1

    def sync(self):
        have = self.song is not None and not self.busy
        for b in self.preview_btns + [self.exp]:
            b.configure(state="normal" if have else "disabled")
        self.go.configure(state="disabled" if self.busy else "normal")
        self.key_cb.configure(state="readonly" if have else "disabled")

    # -- analysis ------------------------------------------------------------------
    def analyze(self):
        song, stem = self.v["song"].get().strip(), self.v["stem"].get().strip() or None
        if not song or not os.path.isfile(song):
            messagebox.showerror("HarmonyHelper", "اختار ملف الأغنية الأول")
            return
        if stem and not os.path.isfile(stem):
            messagebox.showerror("HarmonyHelper", "ملف الـ stem مش موجود")
            return
        self.player.stop()
        self.busy, self.song = True, None
        self.sync()
        self.pb.start(12)

        def work():
            try:
                self.q.put(("done", engine.Song(song, stem, progress=lambda m: self.q.put(("s", m)))))
            except Exception as ex:
                crash_log(traceback.format_exc())
                self.q.put(("err", str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def show(self):
        s = self.song
        self.bpm_l.configure(text="BPM %g" % s.bpm)
        self.v["key"].set(engine.key_name(s.tonic, s.scale))
        self.tune_l.configure(text="%s  ·  دوزان %+.0f cent  ·  %d نوتة%s" % (
            engine.key_name_ar(s.tonic, s.scale), s.tuning, len(s.notes),
            "" if s.melody_from_stem else "  (من غير stem — اللحن تقريبي)"))
        if s.quarter_sections:
            self.maqam_l.configure(text="⚠ احتمال مقام فيه ربع تون — الكوردات والهارموني هنا ممكن ميبقوش دقيقين:  " + "   |   ".join(
                "بار %d%s (%s)" % (q["bar_from"], "" if q["bar_to"] == q["bar_from"] else "–%d" % q["bar_to"], engine.fmt_time(q["start"]))
                for q in s.quarter_sections))
        else:
            self.maqam_l.configure(text="")
        self.tree.delete(*self.tree.get_children())
        for row in engine.chart_rows(s):
            self.tree.insert("", "end", values=(row["bar"], engine.fmt_time(row["time"]), row["daw"], *row["cells"],
                                                "⚠ ربع تون" if row["qt"] else ""), tags=("qt",) if row["qt"] else ())

    def change_key(self):
        if not self.song:
            return
        name = self.v["key"].get()
        for t, sc in KEYS:
            if engine.key_name(t, sc) == name:
                self.song.set_key(t, sc)
        self.show()
        self.status.configure(text="السلم اتغيّر — الهارموني هيمشي على %s" % name)

    # -- preview / export ------------------------------------------------------------
    def preview(self, k):
        try:
            start = float(self.v["start"].get().replace(",", ".") or 0)
        except ValueError:
            start = 0.0
        try:
            path = export.render_preview(self.song, k, start, 30.0, self.tr.get() if k not in ("chords", "melody") else 0)
            self.player.play(path)
            self.result.configure(text="بيشتغل: %s من %s" % (k, engine.fmt_time(start)))
        except Exception as ex:
            crash_log(traceback.format_exc())
            self.result.configure(text="مش قادر أشغّل: %s" % str(ex)[:100])
        return True

    def chosen(self):
        return [k for k, _, _, _ in engine.LINES if self.chk[k].get()]

    def export(self):
        try:
            folder, files = export.export_all(self.song, self.chosen(), self.tr.get())
        except Exception as ex:
            crash_log(traceback.format_exc())
            messagebox.showerror("HarmonyHelper", str(ex)[:1500])
            return None
        self.result.configure(text="اتصدّر ✔  %d ملف في «%s»" % (len(files), os.path.basename(folder)))
        if not self.autotest and messagebox.askyesno("HarmonyHelper", "اتصدّر ✔\n%s\n\nأفتح الفولدر؟" % folder):
            open_path(folder)
        return folder, files

    def poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "s":
                    self.status.configure(text=m[1])
                    continue
                self.busy = False
                self.pb.stop()
                if m[0] == "done":
                    self.song = m[1]
                    self.sync()
                    self.show()
                    self.status.configure(text="خلص ✔ — اسمع الخطوط ▶ وبعدين صدّر")
                    if self.autotest:
                        self.root.after(300, self.run_autotest)
                else:
                    self.sync()
                    self.status.configure(text="حصل خطأ")
                    if self.autotest:
                        self.write_auto({"error": m[1]})
                    else:
                        messagebox.showerror("HarmonyHelper", m[1][:1500])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run_autotest(self):
        self.tr.set(3)
        for k, *_ in engine.LINES:
            self.chk[k].set(True)
        self.v["start"].set("10")
        self.preview("third_up")
        res = self.export()
        s = self.song
        self.write_auto({"error": None, "bpm": s.bpm, "key": engine.key_name(s.tonic, s.scale), "notes": len(s.notes),
                         "maqam": [(q["bar_from"], q["pitches"]) for q in s.quarter_sections],
                         "chords": [c["name"] for c in s.chords][:12], "folder": res[0] if res else None,
                         "files": res[1] if res else []})

    def write_auto(self, data):
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)

    def close(self):
        self.player.stop()
        self.root.destroy()


def main():
    autotest = None
    if len(sys.argv) >= 4 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = {"out": sys.argv[2], "song": sys.argv[3], "stem": sys.argv[4] if len(sys.argv) > 4 else ""}
    root = tk.Tk()
    root.minsize(860, 640)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
