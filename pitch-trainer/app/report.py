"""«تقرير اليوم»: a short Markdown summary of today's sessions for the training chat."""
import datetime
import os

import engine as E

DEFAULT_DIR = r"E:\WORKING\clude\تمارين الصوت - Vocal Exercises\التقارير - Reports"
DAYS_AR = ["الاتنين", "التلات", "الأربع", "الخميس", "الجمعة", "السبت", "الحد"]


def report_dir():
    d = os.environ.get("PITCHTRAINER_REPORT_DIR")
    if d:
        return d
    if os.name == "nt" and os.path.isdir("E:\\"):
        return DEFAULT_DIR
    return os.path.join(E.ROOT, "reports")


def day_kind(d):
    wd = d.weekday()          # Mon=0 .. Sun=6
    if wd in (6, 0, 1, 2):
        return "يوم تمرين كامل"
    if wd in (3, 4):
        return "تسخين خفيف"
    return "يوم إضافي"


def pct(v):
    return "—" if v is None else "%.0f%%" % v


def cents(v):
    return "—" if v is None else "%.1fc" % v


def signed(v, unit="c"):
    return "—" if v is None else "%+.1f%s" % (v, unit)


def details(e):
    m = e.get("mode", "basic")
    bits = []
    if m in ("maqam", "drone", "callresp", "onset") and e.get("maqam"):
        bits.append(E.MAQAMAT.get(e["maqam"], {}).get("ar", e["maqam"]))
    if m == "basic" or m == "exercise":
        bits.append(E.EXERCISES.get(e.get("exercise"), {}).get("ar", e.get("exercise", "")))
    if m == "callresp":
        bits.append("مستوى %s" % e.get("level"))
    if m == "ajnas":
        bits.append(E.AJNAS.get(e.get("jins"), {}).get("ar", e.get("jins", "")))
    if m == "song":
        bits.append("%s (%s–%s)" % (e.get("song", ""), e.get("from"), e.get("to")))
    o = e.get("onset")
    if m in ("onset", "leaps") and o:
        bits.append("زحلقة %.0fc / %sms · نزلت مباشرة %s" % (o["slide_c"], "—" if o["slide_ms"] is None else "%.0f" % o["slide_ms"], pct(o["landed_pct"])))
    lp = e.get("leaps")
    if lp:
        bits.append("القفزات صح %s (%s)" % (pct(lp["ok_pct"]), ", ".join("%s %s%%" % (k, v) for k, v in lp["by"].items())))
    sb = e.get("stab")
    if sb:
        vib = "%.1fHz ±%.0fc" % (sb["vib_rate"], sb["vib_width"]) if sb.get("vib_rate") else "مفيش"
        bits.append("drift %s · وقعة آخر النفس %s · فايبراتو %s" % (signed(sb["drift_c"]), signed(sb["end_drop_c"]), vib))
    if e.get("blind"):
        bits.append("أعمى")
    return " · ".join(b for b in bits if b)


def totals(entries):
    a = E._agg(entries)
    return a or {"notes": 0, "exact_pct": None, "close_pct": None, "avg_cents": None}


def build(history, today=None):
    today = today or datetime.date.today()
    ds = today.strftime("%Y-%m-%d")
    todays = [e for e in history if e.get("date") == ds and e.get("mode") != "calib"]
    before = [e for e in history if e.get("date", "") < ds and e.get("notes")]
    L = []
    L.append("# تقرير التمرين — %s (%s · %s)" % (ds, DAYS_AR[today.weekday()], day_kind(today)))
    L.append("")
    L.append("الأساس: متوسط %.1fc · %.0f%% مظبوط — الهدف: %.0f%% مظبوط" % (E.BASELINE_CENTS, E.BASELINE_EXACT, E.GOAL_EXACT))
    L.append("")
    if not todays:
        L.append("مفيش جلسات متسجلة النهارده.")
        return "\n".join(L) + "\n"
    t = totals(todays)
    mins = sum(e.get("duration_s", 0) for e in todays) / 60.0
    L.append("## الملخص")
    L.append("")
    L.append("| | مظبوط (±15c) | قريب (±35c) | متوسط الانحراف | نوتات | دقايق |")
    L.append("|---|---|---|---|---|---|")
    L.append("| النهارده | %s | %s | %s | %d | %.0f |" % (pct(t["exact_pct"]), pct(t["close_pct"]), cents(t["avg_cents"]), t["notes"], mins))
    if before:
        last_day = max(e["date"] for e in before)
        p = totals([e for e in before if e["date"] == last_day])
        L.append("| آخر مرة (%s) | %s | %s | %s | %d | |" % (last_day, pct(p["exact_pct"]), pct(p["close_pct"]), cents(p["avg_cents"]), p["notes"]))
        if t["exact_pct"] is not None and p["exact_pct"] is not None:
            L.append("| الفرق | %+.0f | %+.0f | %s | | |" % (t["exact_pct"] - p["exact_pct"], t["close_pct"] - p["close_pct"],
                                                       signed((t["avg_cents"] or 0) - (p["avg_cents"] or 0)) if t["avg_cents"] and p["avg_cents"] else "—"))
    L.append("")
    L.append("## التمارين")
    L.append("")
    L.append("| الوقت | التمرين | التفاصيل | مظبوط | قريب | متوسط | مقارنة بآخر مرة |")
    L.append("|---|---|---|---|---|---|---|")
    for e in todays:
        m = e.get("mode", "basic")
        prev = [x for x in history if x.get("mode") == m and (x.get("date", ""), x.get("time", "")) < (e["date"], e.get("time", ""))
                and x.get("exact_pct") is not None]
        cmp_ = "—"
        if prev and e.get("exact_pct") is not None:
            pv = prev[-1]
            cmp_ = "%+.0f%% مظبوط · %s (%s)" % (e["exact_pct"] - pv["exact_pct"],
                                                 signed((e.get("note_avg_cents") or 0) - (pv.get("note_avg_cents") or 0)) if e.get("note_avg_cents") is not None and pv.get("note_avg_cents") is not None else "—",
                                                 pv["date"])
        L.append("| %s | %s | %s | %s | %s | %s | %s |" % (e.get("time", ""), E.MODE_AR.get(m, m), details(e) or "—",
                                                          pct(e.get("exact_pct")), pct(e.get("close_pct")), cents(e.get("note_avg_cents")), cmp_))
    L.append("")
    worst = []
    for e in todays:
        for w in e.get("worst", []):
            worst.append((abs(w["err"]), w, E.MODE_AR.get(e.get("mode"), "")))
    worst.sort(key=lambda x: -x[0])
    if worst:
        L.append("## أسوأ النوتات")
        L.append("")
        L.append("| النوتة | الانحراف | التمرين |")
        L.append("|---|---|---|")
        for _, w, mode in worst[:8]:
            L.append("| %s%s | %+.0fc | %s |" % (w["label"], " (%s)" % w["deg"] if w.get("deg") and w["deg"] not in ("song", "") else "", w["err"], mode))
        L.append("")
    flat = [w["err"] for _, w, _ in worst]
    if flat:
        lo = sum(1 for x in flat if x < 0)
        L.append("الاتجاه: %d من %d من أسوأ النوتات **%s**." % (max(lo, len(flat) - lo), len(flat), "واطية (flat)" if lo >= len(flat) - lo else "عالية (sharp)"))
        L.append("")
    for e in todays:
        if e.get("off_notes"):
            L.append("## الأغنية: %s — الأماكن اللي طلعت فيها" % e.get("song", ""))
            L.append("")
            for o in e["off_notes"][:15]:
                L.append("- %s · %s · %s" % (fmt(o["t"]), o["label"], "مش متغنية" if o["err"] is None else "%+.0fc" % o["err"]))
            L.append("")
    return "\n".join(L) + "\n"


def fmt(t):
    return "%d:%04.1f" % (int(t // 60), t - 60 * int(t // 60))


def write(history, today=None, folder=None):
    today = today or datetime.date.today()
    folder = folder or report_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "%s - تقرير الصوت.md" % today.strftime("%Y-%m-%d"))
    with open(path, "w", encoding="utf-8") as f:
        f.write(build(history, today))
    return path
