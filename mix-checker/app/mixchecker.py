"""MixChecker — window around engine.py / report.py."""
import json
import os
import pathlib
import queue
import sys
import threading
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
    import engine
    import report
except Exception:
    crash_log(traceback.format_exc())
    raise

if sys.platform == "win32":
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

AUDIO_TYPES = [("Audio", "*.wav *.mp3 *.flac *.aif *.aiff *.m4a *.ogg"), ("All", "*.*")]


def open_file(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)
        else:
            webbrowser.open(pathlib.Path(path).as_uri())
    except Exception:
        pass


class App:
    def __init__(self, root, autotest=None):
        self.root, self.autotest = root, autotest
        root.title("MixChecker — وسيم")
        root.configure(bg="#0a0a0a")
        self.q = queue.Queue()
        self.busy = False
        self.result = None
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background="#0a0a0a", foreground="#f5efe6", font=("Segoe UI", 10))
        st.configure("Hint.TLabel", foreground="#9a8f80", font=("Segoe UI", 9))
        st.configure("Title.TLabel", foreground="#e0a458", font=("Segoe UI", 15, "bold"))
        st.configure("Big.TLabel", foreground="#f5efe6", font=("Segoe UI", 11, "bold"))
        st.configure("TEntry", fieldbackground="#1a1714", foreground="#f5efe6", insertcolor="#f5efe6")
        st.configure("TCombobox", fieldbackground="#1a1714", foreground="#f5efe6")
        st.map("TCombobox", fieldbackground=[("readonly", "#1a1714")], foreground=[("readonly", "#f5efe6")])
        st.configure("TButton", background="#2a241d", foreground="#f5efe6", padding=6)
        st.map("TButton", background=[("active", "#3a3128"), ("disabled", "#17130f")], foreground=[("disabled", "#5d544a")])
        st.configure("Go.TButton", background="#e0a458", foreground="#0a0a0a", font=("Segoe UI", 11, "bold"), padding=8)
        st.map("Go.TButton", background=[("active", "#f0bb70"), ("disabled", "#5a4a35")])
        st.configure("Horizontal.TProgressbar", background="#e0a458", troughcolor="#1a1714")
        st.configure("TLabelframe", background="#0a0a0a", bordercolor="#2b2620")
        st.configure("TLabelframe.Label", background="#0a0a0a", foreground="#e0a458", font=("Segoe UI", 10, "bold"))

        f = ttk.Frame(root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="MixChecker  ·  قارن المكس بالريفرنس", style="Title.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
        self.v = {k: tk.StringVar() for k in ("mix", "ref1", "ref2", "ref3", "mref")}
        r = 1
        r = self.file_row(f, r, "المكس / الماستر بتاعك", "mix")
        for i in (1, 2, 3):
            r = self.file_row(f, r, "ريفرنس %d%s" % (i, "" if i == 1 else " (اختياري)"), "ref%d" % i, clear=True)

        self.go = ttk.Button(f, text="حلّل واعمل التقرير  ▶", style="Go.TButton", command=self.analyze)
        self.go.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        r += 1
        self.pb = ttk.Progressbar(f, maximum=100, mode="determinate")
        self.pb.grid(row=r, column=0, columnspan=3, sticky="ew")
        r += 1
        self.status = ttk.Label(f, text="اختار المكس وريفرنس واحد على الأقل", style="Hint.TLabel")
        self.status.grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 6))
        r += 1
        self.summary = tk.Text(f, height=9, bg="#141210", fg="#f5efe6", relief="flat", font=("Segoe UI", 10),
                               wrap="word", insertbackground="#f5efe6")
        self.summary.grid(row=r, column=0, columnspan=3, sticky="nsew")
        self.summary.tag_configure("rtl", justify="right")
        f.rowconfigure(r, weight=1)
        r += 1
        self.open_btn = ttk.Button(f, text="افتح التقرير", command=self.open_report, state="disabled")
        self.open_btn.grid(row=r, column=2, sticky="e", pady=(8, 0))
        r += 1

        qm = ttk.LabelFrame(f, text="  Quick master — للديمو والريلز بس (مش ماستر نهائي)  ", padding=10)
        qm.grid(row=r, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        qm.columnconfigure(1, weight=1)
        ttk.Label(qm, text="ماتش على ريفرنس:").grid(row=0, column=0, sticky="w")
        self.mref_cb = ttk.Combobox(qm, textvariable=self.v["mref"], state="readonly", values=[])
        self.mref_cb.grid(row=0, column=1, sticky="ew", padx=8)
        self.qm_btn = ttk.Button(qm, text="اعمل Quick master", command=self.quick_master)
        self.qm_btn.grid(row=0, column=2)
        ttk.Label(qm, text="بيستخدم Matchering: بيقلّد علو و EQ الريفرنس. بيطلع WAV 24-bit 48k و MP3 320k جنب المكس.",
                  style="Hint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        for k in ("ref1", "ref2", "ref3"):
            self.v[k].trace_add("write", lambda *a: self.sync())
        self.sync()
        root.after(100, self.poll)
        if autotest:
            self.v["mix"].set(autotest["mix"])
            for i, p in enumerate(autotest["refs"][:3], 1):
                self.v["ref%d" % i].set(p)
            root.after(500, self.analyze)

    # -- widgets ---------------------------------------------------------------
    def file_row(self, f, r, label, key, clear=False):
        ttk.Label(f, text=label).grid(row=r, column=0, sticky="w", pady=4)
        ttk.Entry(f, textvariable=self.v[key], width=62).grid(row=r, column=1, sticky="ew", pady=4, padx=6)
        bf = ttk.Frame(f)
        bf.grid(row=r, column=2, sticky="ew")

        def browse():
            cur = self.v[key].get()
            p = filedialog.askopenfilename(filetypes=AUDIO_TYPES, initialdir=os.path.dirname(cur) if cur else None)
            if p:
                self.v[key].set(os.path.normpath(p))
        ttk.Button(bf, text="اختار…", command=browse).pack(side="left")
        if clear:
            ttk.Button(bf, text="✕", width=3, command=lambda: self.v[key].set("")).pack(side="left", padx=(4, 0))
        return r + 1

    def refs(self):
        return [self.v[k].get().strip() for k in ("ref1", "ref2", "ref3") if self.v[k].get().strip()]

    def sync(self):
        names = [os.path.basename(p) for p in self.refs()]
        self.mref_cb.configure(values=names)
        if self.v["mref"].get() not in names:
            self.v["mref"].set(names[0] if names else "")
        state = "disabled" if self.busy else "normal"
        self.go.configure(state=state)
        self.qm_btn.configure(state="normal" if names and not self.busy else "disabled")

    def say(self, text):
        self.summary.insert("end", text + "\n", "rtl")
        self.summary.see("end")

    # -- actions -----------------------------------------------------------------
    def check_inputs(self, need_ref):
        mix = self.v["mix"].get().strip()
        if not mix or not os.path.isfile(mix):
            messagebox.showerror("MixChecker", "اختار ملف المكس الأول")
            return None
        for p in self.refs():
            if not os.path.isfile(p):
                messagebox.showerror("MixChecker", "الملف ده مش موجود:\n" + p)
                return None
        if need_ref and not self.refs():
            messagebox.showerror("MixChecker", "اختار ريفرنس واحد على الأقل")
            return None
        return mix

    def run(self, kind, fn):
        self.busy = True
        self.sync()
        self.pb.configure(mode="indeterminate")
        self.pb.start(12)

        def work():
            try:
                self.q.put(("done", kind, fn()))
            except Exception as ex:
                crash_log(traceback.format_exc())
                self.q.put(("err", kind, str(ex)))
        threading.Thread(target=work, daemon=True).start()

    def analyze(self):
        mix = self.check_inputs(need_ref=False)
        if not mix:
            return
        refs = self.refs()
        self.summary.delete("1.0", "end")
        if not refs:
            self.say("مفيش ريفرنس — التقرير هيبقى فيه العلو والستيريو بس.")

        def job():
            m, rs, cmp = engine.analyze(mix, refs, progress=lambda s: self.q.put(("s", s)))
            path = engine.report_path(mix)
            try:
                report.write(m, rs, cmp, path)
            except OSError:  # track folder not writable
                os.makedirs(engine.OUTPUT_DIR, exist_ok=True)
                path = report.write(m, rs, cmp, os.path.join(engine.OUTPUT_DIR, os.path.basename(path)))
            return m, rs, cmp, path
        self.run("analyze", job)

    def quick_master(self):
        mix = self.check_inputs(need_ref=True)
        if not mix:
            return
        name = self.v["mref"].get()
        ref = next((p for p in self.refs() if os.path.basename(p) == name), self.refs()[0])

        def job():
            try:
                return engine.quick_master(mix, ref, log=lambda s: self.q.put(("s", s)))
            except OSError:
                return engine.quick_master(mix, ref, out_dir=engine.OUTPUT_DIR, log=lambda s: self.q.put(("s", s)))
        self.say("— Quick master على «%s» —" % os.path.basename(ref))
        self.run("master", job)

    def open_report(self):
        if self.result:
            open_file(self.result[3])

    def show(self, m, rs, cmp, path):
        self.summary.delete("1.0", "end")
        self.say("تراكك: %.1f LUFS  |  True peak %.1f dBTP  |  LRA %.1f LU  |  PLR %.1f" % (m.lufs, m.tp, m.lra, m.plr))
        if rs:
            self.say("الريفرنسات: %.1f LUFS في المتوسط" % cmp["ref_lufs"])
        for w in cmp["tp_warn"]:
            self.say("⚠ True peak فوق -1 dBTP")
        for i in cmp.get("top", [])[:4]:
            self.say("• %s %+.1f dB (حوالي %s)" % (i["name"], i["diff"], i["around"]))
        for kind, text in cmp["stereo"]:
            if kind == "bad":
                self.say("⚠ " + text.split("(")[0].strip())
        self.say("")
        self.say("التقرير اتحفظ: " + path)

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "s":
                    self.status.configure(text=msg[1].strip()[:110])
                    continue
                self.busy = False
                self.pb.stop()
                self.pb.configure(mode="determinate", value=100 if msg[0] == "done" else 0)
                self.sync()
                kind = msg[1]
                if msg[0] == "err":
                    self.status.configure(text="حصل خطأ")
                    self.say("خطأ: " + msg[2])
                    if self.autotest:
                        self.finish_autotest(error=msg[2])
                    else:
                        messagebox.showerror("MixChecker", msg[2][:1500])
                elif kind == "analyze":
                    self.result = msg[2]
                    self.open_btn.configure(state="normal")
                    self.status.configure(text="خلص ✔ — التقرير جنب المكس")
                    self.show(*msg[2])
                    if self.autotest:
                        self.root.after(200, self.quick_master)
                    else:
                        open_file(msg[2][3])
                else:
                    wav, mp3 = msg[2]
                    self.status.configure(text="Quick master جاهز ✔")
                    self.say("Quick master (للديمو والريلز):")
                    self.say(wav)
                    self.say(mp3)
                    if self.autotest:
                        self.finish_autotest(files=[wav, mp3])
                    elif messagebox.askyesno("MixChecker", "الـ Quick master جاهز ✔\nده للديمو والريلز — مش ماستر نهائي.\n\nأفتح الفولدر؟"):
                        open_file(os.path.dirname(wav))
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def finish_autotest(self, files=None, error=None):
        data = {"error": error, "files": files or []}
        if self.result:
            m, rs, cmp, path = self.result
            data.update(report=path, lufs=m.lufs, tp=m.tp, top=[(i["band"], i["diff"]) for i in cmp.get("top", [])])
        with open(self.autotest["out"], "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)


def main():
    autotest = None
    if len(sys.argv) >= 4 and sys.argv[1] == "--autotest":  # used by the Windows CI test
        autotest = {"out": sys.argv[2], "mix": sys.argv[3], "refs": sys.argv[4:]}
    root = tk.Tk()
    root.minsize(820, 640)
    App(root, autotest)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        crash_log(traceback.format_exc())
        raise
