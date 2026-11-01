"""Style-pack picker shared by the three tabs: one click on a pack, «تخصيص» to override
any single part (fonts with live previews, word animation, line in/out, glow), and
«احفظ كباك» to keep the combo as a new pack."""
import base64
import io
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import textfx

CLASSIC = "— الشكل الكلاسيكي —"   # lyric modes only: the v2 look, no pack


def photo(img):
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()).decode("ascii"))


class PackBar:
    def __init__(self, parent, settings, key, save, allow_classic=False, default=None):
        self.settings, self.key, self.save_settings = settings, key, save
        self.allow_classic = allow_classic
        self.frame = ttk.Frame(parent)
        self.var = tk.StringVar()
        self.custom = None  # overrides on top of the chosen pack
        ttk.Label(self.frame, text="الباك").pack(side="right")
        self.cb = ttk.Combobox(self.frame, textvariable=self.var, state="readonly", width=18, justify="right")
        self.cb.pack(side="right", padx=4)
        self.cb.bind("<<ComboboxSelected>>", lambda e: self.changed())
        ttk.Button(self.frame, text="تخصيص…", command=self.customize).pack(side="right", padx=2)
        self.note = ttk.Label(self.frame, text="", style="Hint.TLabel")
        self.note.pack(side="right", padx=4)
        self.refresh()
        st = settings.get(key) or {}
        name = st.get("name") or default or (CLASSIC if allow_classic else textfx.default_pack()["name"])
        if name not in self.cb["values"]:
            name = self.cb["values"][0]
        self.var.set(name)
        self.custom = st.get("custom") or None
        self.show_note()

    def refresh(self):
        names = [p["name"] for p in textfx.all_packs()]
        self.cb["values"] = ([CLASSIC] if self.allow_classic else []) + names

    def changed(self):
        self.custom = None
        self.show_note()
        self.remember()

    def show_note(self):
        self.note.configure(text="(متخصص)" if self.custom else "")

    def remember(self):
        self.settings[self.key] = dict(name=self.var.get(), custom=self.custom)
        self.save_settings()

    def get(self):
        """The pack dict to render with, or None for the classic lyric look."""
        if self.var.get() == CLASSIC:
            return None
        p = textfx.get_pack(self.var.get())
        if self.custom:
            p.update(self.custom)
        return p

    def set(self, name):
        if name in self.cb["values"]:
            self.var.set(name)
            self.changed()

    # -- customize dialog -------------------------------------------------------------
    def customize(self):
        base = self.get() or textfx.default_pack()
        win = tk.Toplevel(self.frame)
        win.title("تخصيص الباك — %s" % base["name"])
        win.configure(bg="#0a0a0a")
        win.transient(self.frame.winfo_toplevel())
        fams = list(textfx.families().keys())
        v = {k: tk.StringVar(value=base.get(k, "")) for k in ("font", "gold_font", "anim", "line_in", "line_out", "glow")}
        names = dict(anim=textfx.ANIMS, line_in=textfx.LINE_IN, line_out=textfx.LINE_OUT,
                     glow={k: k for k in textfx.GLOWS})
        f = ttk.Frame(win, padding=12)
        f.pack(fill="both", expand=True)
        imgs = {}

        def preview(which):
            pk = dict(base, **{k: var.get() for k, var in v.items() if k not in names or var.get()})
            for k in names:  # labels -> ids
                for idv, lab in names[k].items():
                    if v[k].get() == lab:
                        pk[k] = idv
            # each picker previews the whole sample in its own font
            if which == "font":
                pk["gold_font"] = pk["font"]
            else:
                pk["font"] = pk["gold_font"]
            try:
                im = textfx.preview_image(pk, 520, 110)
            except Exception as ex:  # a broken font shows the reason instead of crashing
                im = None
                lbl[which].configure(text=str(ex)[:80], image="")
                return
            imgs[which] = photo(im)
            lbl[which].configure(image=imgs[which], text="")

        lbl = {}
        r = 0
        for key, title in (("font", "فونت الكلمات"), ("gold_font", "فونت الكلمة الدهبي")):
            ttk.Label(f, text=title).grid(row=r, column=1, sticky="e", pady=4)
            cb = ttk.Combobox(f, textvariable=v[key], values=fams, state="readonly", width=22)
            cb.grid(row=r, column=0, sticky="w", pady=4)
            r += 1
            lbl[key] = tk.Label(f, bg="#1a1714", fg="#f5efe6")
            lbl[key].grid(row=r, column=0, columnspan=2, pady=(0, 8))
            cb.bind("<<ComboboxSelected>>", lambda e: (preview("font"), preview("gold_font")))
            r += 1
        for key, title in (("anim", "أنيميشن الكلمة"), ("line_in", "دخول السطر"), ("line_out", "خروج السطر"),
                           ("glow", "التوهج")):
            ttk.Label(f, text=title).grid(row=r, column=1, sticky="e", pady=3)
            labels = list(names[key].values())
            v[key].set(names[key].get(base.get(key), labels[0]))
            cb = ttk.Combobox(f, textvariable=v[key], values=labels, state="readonly", width=22)
            cb.grid(row=r, column=0, sticky="w", pady=3)
            cb.bind("<<ComboboxSelected>>", lambda e: preview("font"))
            r += 1

        def collect():
            out = {}
            for k, var in v.items():
                val = var.get()
                if k in names:
                    val = next((i for i, lab in names[k].items() if lab == val), base.get(k))
                out[k] = val
            return out

        def use():
            ov = {k: val for k, val in collect().items() if val != base.get(k)}
            if self.var.get() == CLASSIC:
                self.var.set(textfx.default_pack()["name"])
            self.custom = ov or None
            self.show_note()
            self.remember()
            win.destroy()

        def save_as():
            name = simpledialog.askstring("احفظ كباك", "اسم الباك الجديد:", parent=win)
            if not name:
                return
            pk = dict(collect(), name=name.strip())
            textfx.save_pack(pk)
            self.refresh()
            self.var.set(pk["name"])
            self.custom = None
            self.show_note()
            self.remember()
            messagebox.showinfo("Reel Maker", "اتحفظ: %s" % pk["name"], parent=win)
            win.destroy()

        b = ttk.Frame(f)
        b.grid(row=r, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(b, text="استخدم", command=use).pack(side="right", padx=4)
        ttk.Button(b, text="احفظ كباك…", command=save_as).pack(side="right", padx=4)
        ttk.Button(b, text="رجّع الباك زي ما هو", command=lambda: (setattr(self, "custom", None), self.show_note(),
                                                                  self.remember(), win.destroy())).pack(side="left")
        preview("font")
        preview("gold_font")
        self.win = win  # tests
        return win
