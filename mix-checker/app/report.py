"""MixChecker HTML report (Arabic, RTL, self-contained: font and charts inline)."""
import base64
import html
import math
import os
import re
from datetime import datetime

import engine

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = "#e0a458"
REF_COLORS = ["#7fb2ff", "#b48cff", "#5fd3a0"]
BLUE = "#4a8cff"
RED = "#ff5a4f"


def esc(s):
    return html.escape(str(s))


LTR_RUN = re.compile(r"[A-Za-z0-9+\-−][A-Za-z0-9.,+\-−–/%&_ ]*[A-Za-z0-9%]|[A-Za-z0-9]")


def ar(text):
    """Escape Arabic prose and isolate Latin/number runs so '250–500Hz' or '-1 dBTP'
    keep their order inside right-to-left sentences."""
    return LTR_RUN.sub(lambda m: '<bdi dir="ltr">%s</bdi>' % m.group(0), html.escape(str(text)))


def n(v, d=1, plus=False):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    if abs(v) < 0.5 * 10 ** -d:
        v = 0.0
    s = ("%+." if plus else "%.") + str(d) + "f"
    return "<bdi>" + (s % v).replace("-", "−") + "</bdi>"


def font_css():
    p = os.path.join(HERE, "assets", "cairo-subset.woff2")
    try:
        b = base64.b64encode(open(p, "rb").read()).decode()
        return '@font-face{font-family:"Cairo";font-weight:200 1000;src:url(data:font/woff2;base64,%s) format("woff2")}' % b
    except OSError:
        return ""


# ----------------------------------------------------------------------------- charts

def _xlog(f, x0, x1, f0=20.0, f1=20000.0):
    return x0 + (x1 - x0) * (math.log10(f) - math.log10(f0)) / (math.log10(f1) - math.log10(f0))


def _freq_axis(W, H, L, R, T, B, y):
    s = ""
    for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
        x = _xlog(f, L, W - R)
        s += '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="#2b2620"/>' % (x, x, T, H - B)
        s += '<text x="%.1f" y="%d" fill="#9a8f80" font-size="12" text-anchor="middle">%s</text>' % (
            x, H - B + 18, engine.fmt_hz(f))
    for k, name, lo, hi in engine.BANDS:
        x = (_xlog(max(lo, 20), L, W - R) + _xlog(min(hi, 20000), L, W - R)) / 2
        s += '<text x="%.1f" y="%d" fill="#6f665a" font-size="11" text-anchor="middle">%s</text>' % (x, T + 12, name)
        if lo > 20:
            xl = _xlog(lo, L, W - R)
            s += '<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="#3a3128" stroke-dasharray="2 4"/>' % (xl, xl, T, H - B)
    return s


def spectrum_chart(mine, refs, cmp):
    W, H, L, R, T, B = 900, 360, 50, 16, 18, 34
    curves = [(mine.third, GOLD, 3.2, "", mine.name)]
    for i, r in enumerate(refs):
        curves.append((r.third, REF_COLORS[i % 3], 1.6, "", r.name))
    if len(refs) > 1:
        curves.append((cmp["ref_third"], "#f5efe6", 1.6, "6 5", "متوسط الريفرنسات"))
    allv = [v for c in curves for v in c[0]]
    hi = math.ceil((max(allv) + 2) / 6) * 6
    lo = hi - 54
    y = lambda v: T + (H - T - B) * (hi - max(min(v, hi), lo)) / (hi - lo)
    s = '<svg viewBox="0 0 %d %d" class="chart" id="spectrum">' % (W, H)
    for v in range(int(lo), int(hi) + 1, 6):
        s += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#221e19"/>' % (L, W - R, y(v), y(v))
        s += '<text x="%d" y="%.1f" fill="#9a8f80" font-size="11" text-anchor="end">%d</text>' % (L - 6, y(v) + 4, v)
    s += _freq_axis(W, H, L, R, T, B, y)
    for vals, color, w, dash, _ in reversed(curves):
        pts = " ".join("%.1f,%.1f" % (_xlog(fc, L, W - R), y(v)) for fc, v in zip(engine.THIRD_OCT, vals))
        s += '<polyline points="%s" fill="none" stroke="%s" stroke-width="%s" stroke-dasharray="%s" stroke-linejoin="round"/>' % (
            pts, color, w, dash)
    s += "</svg>"
    legend = "".join('<span class="lg"><i style="background:%s"></i>%s</span>' % (c[1], esc(c[4])) for c in curves)
    return s, legend


def diff_chart(cmp):
    W, H, L, R, T, B = 900, 260, 50, 16, 18, 34
    d = cmp["diff_third"]
    m = max(6, math.ceil(max(abs(v) for v in d) / 3) * 3)
    y = lambda v: T + (H - T - B) * (m - max(min(v, m), -m)) / (2 * m)
    s = '<svg viewBox="0 0 %d %d" class="chart" id="diffchart">' % (W, H)
    s += '<rect x="%d" y="%.1f" width="%d" height="%.1f" fill="#e0a45814"/>' % (L, y(1.5), W - L - R, y(-1.5) - y(1.5))
    for v in range(-m, m + 1, 3):
        s += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s"/>' % (L, W - R, y(v), y(v), "#5a5045" if v == 0 else "#221e19")
        s += '<text x="%d" y="%.1f" fill="#9a8f80" font-size="11" text-anchor="end">%+d</text>' % (L - 6, y(v) + 4, v)
    s += _freq_axis(W, H, L, R, T, B, y)
    xs = [_xlog(fc, L, W - R) for fc in engine.THIRD_OCT]
    for sign, color in ((1, GOLD), (-1, BLUE)):
        pts = ["%.1f,%.1f" % (xs[0], y(0))]
        pts += ["%.1f,%.1f" % (x, y(v if v * sign > 0 else 0)) for x, v in zip(xs, d)]
        pts += ["%.1f,%.1f" % (xs[-1], y(0))]
        s += '<polygon points="%s" fill="%s" opacity=".35"/>' % (" ".join(pts), color)
    s += '<polyline points="%s" fill="none" stroke="%s" stroke-width="2.6" stroke-linejoin="round"/>' % (
        " ".join("%.1f,%.1f" % (x, y(v)) for x, v in zip(xs, d)), "#f5efe6")
    for x, v, fc in zip(xs, d, engine.THIRD_OCT):
        if abs(v) >= 1.5:
            s += '<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s"><title>%s Hz: %+.1f dB</title></circle>' % (
                x, y(v), GOLD if v > 0 else BLUE, engine.fmt_hz(fc), v)
    return s + "</svg>"


def loudness_chart(mine, refs):
    W, H, L, R, T, B = 900, 280, 50, 16, 14, 34
    series = [(mine, GOLD, 2.8)] + [(r, REF_COLORS[i % 3], 1.4) for i, r in enumerate(refs)]
    tmax = max(t.curve_t[-1] if len(t.curve_t) else 10 for t, _, _ in series)
    vals = [v for t, _, _ in series for v in t.curve if v > -59]
    hi = math.ceil((max(vals + [-6]) + 1) / 3) * 3
    lo = max(-60, math.floor((min(vals + [hi - 18]) - 2) / 3) * 3)
    y = lambda v: T + (H - T - B) * (hi - max(min(v, hi), lo)) / (hi - lo)
    x = lambda t: L + (W - L - R) * t / tmax
    s = '<svg viewBox="0 0 %d %d" class="chart" id="loudchart">' % (W, H)
    for v in range(int(lo), int(hi) + 1, 3):
        s += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="#221e19"/>' % (L, W - R, y(v), y(v))
        s += '<text x="%d" y="%.1f" fill="#9a8f80" font-size="11" text-anchor="end">%d</text>' % (L - 6, y(v) + 4, v)
    step = 30 if tmax > 150 else 15 if tmax > 60 else 5
    t = 0
    while t <= tmax:
        s += '<text x="%.1f" y="%d" fill="#9a8f80" font-size="11" text-anchor="middle">%d:%02d</text>' % (x(t), H - B + 18, t // 60, t % 60)
        t += step
    for tr, color, w in reversed(series):
        pts = " ".join("%.1f,%.1f" % (x(tt), y(v)) for tt, v in zip(tr.curve_t, tr.curve))
        s += '<polyline points="%s" fill="none" stroke="%s" stroke-width="%s" stroke-linejoin="round" opacity="%s"/>' % (
            pts, color, w, "1" if tr is mine else ".8")
        s += '<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" stroke="%s" stroke-dasharray="5 5" opacity=".7"/>' % (
            L, W - R, y(tr.lufs), y(tr.lufs), color)
    s += "</svg>"
    legend = "".join('<span class="lg"><i style="background:%s"></i>%s</span>' % (c, esc(t.name)) for t, c, _ in series)
    return s, legend


# ----------------------------------------------------------------------------- page

CSS = """
:root{--bg:#0a0a0a;--panel:#141210;--panel2:#1b1815;--line:#2b2620;--gold:#e0a458;--text:#f5efe6;--muted:#9a8f80;--red:#ff5a4f;--blue:#4a8cff;--ok:#5fd3a0}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:"Cairo",system-ui,sans-serif;font-size:15.5px;line-height:1.75}
main{max-width:980px;margin:0 auto;padding:22px 16px 60px}
h1{color:var(--gold);font-size:26px;margin:0}
h2{font-size:20px;margin:34px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:18px 0 8px}
.sub{color:var(--muted);font-size:14px}
.badge{display:inline-block;background:var(--gold);color:#0a0a0a;font-weight:800;border-radius:8px;padding:0 10px;font-size:13px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:10px 0}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--gold);font-weight:700;background:var(--panel2)}
td.num{direction:ltr;text-align:left;font-variant-numeric:tabular-nums}
tr.me td{font-weight:800}
.pos{color:var(--gold);font-weight:800}.neg{color:var(--blue);font-weight:800}.bad{color:var(--red);font-weight:800}.okc{color:var(--ok)}
ul.tips{padding:0;margin:0;list-style:none}
ul.tips li{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px;margin:8px 0}
ul.tips li b.d{direction:ltr;display:inline-block}
.note{border-inline-start:4px solid var(--muted);padding:6px 12px;margin:8px 0;background:var(--panel)}
.note.bad{border-color:var(--red);color:var(--text);font-weight:400}.note.warn{border-color:#ffcc66}.note.ok{border-color:var(--ok)}.note.info{border-color:var(--blue)}
.chart{width:100%;height:auto;display:block;direction:ltr;background:var(--panel);border:1px solid var(--line);border-radius:14px}
.legend{display:flex;flex-wrap:wrap;gap:14px;margin:6px 2px 0;font-size:13px}
.lg i{display:inline-block;width:18px;height:4px;border-radius:2px;margin-inline-end:6px;vertical-align:middle}
.summary li{margin:4px 0}
dl.gl{display:grid;grid-template-columns:max-content 1fr;gap:4px 14px;font-size:14px}
dl.gl dt{color:var(--gold);font-weight:700;direction:ltr;text-align:right}
dl.gl dd{margin:0;color:var(--muted)}
footer{color:var(--muted);font-size:13px;margin-top:40px}
"""


def metrics_table(mine, refs, cmp):
    rows = ""
    for t in [mine] + refs:
        tp_cls = ' class="num bad"' if t.tp > -1 else ' class="num"'
        rows += ('<tr class="%s"><td>%s%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td>'
                 '<td%s>%s</td><td class="num">%s</td><td class="num">%s</td></tr>') % (
            "me" if t is mine else "", esc(t.name), ' <span class="badge">تراكك</span>' if t is mine else "",
            n(t.lufs), n(t.st_max), n(t.lra), tp_cls, n(t.tp), n(t.plr), n(t.crest))
    return ('<div class="scroll"><table id="metrics"><thead><tr><th>التراك</th><th>LUFS (Integrated)</th><th>Short-term max</th>'
            '<th>LRA</th><th>True peak (dBTP)</th><th>PLR</th><th>Crest factor</th></tr></thead><tbody>%s</tbody></table></div>' % rows)


def write(mine, refs, cmp, path):
    parts = []
    has_refs = bool(refs)
    # ---- summary
    summ = []
    for i in cmp.get("top", [])[:3]:
        summ.append(ar("%s %s بـ %.1f dB عن الريفرنس (حوالي %s)." % (
            i["name"], "أعلى" if i["diff"] > 0 else "أقل", abs(i["diff"]), i["around"])))
    if cmp["tp_warn"]:
        summ.append('<span class="bad">%s</span>' % ar("الـ True peak فوق -1 dBTP (%.1f dBTP)." % mine.tp))
    if has_refs:
        dl = mine.lufs - cmp["ref_lufs"]
        summ.append(ar("العلو %s الريفرنسات بـ %.1f LU (%.1f مقابل %.1f LUFS)." % (
            "أعلى من" if dl > 0 else "أوطى من", abs(dl), mine.lufs, cmp["ref_lufs"])))
    for kind, text in cmp["stereo"]:
        if kind == "bad":
            summ.append('<span class="bad">%s</span>' % ar(text[:text.index("(")].strip() + "." if "(" in text else text))
    if has_refs and not cmp.get("top"):
        summ.append(ar("التوازن الطيفي قريب من الريفرنسات (مفيش فرق 1.5 dB أو أكتر في أي منطقة)."))
    parts.append('<div class="panel"><h3 style="margin-top:0">أهم حاجات</h3><ul class="summary">%s</ul></div>'
                 % "".join("<li>%s</li>" % s for s in summ))

    # ---- loudness
    parts.append("<h2>1. العلو</h2>")
    parts.append(metrics_table(mine, refs, cmp))
    rows = "".join('<tr><td>%s%s</td><td class="num">%s</td><td class="num %s">%s</td><td style="white-space:normal">%s</td></tr>' % (
        esc(r["name"]), "" if r["official"] else ' <span class="sub">(مفيش رقم رسمي منشور — -14 هدف آمن)</span>',
        n(r["target"], 0), "pos" if r["gain"] < -0.5 else "neg" if r["gain"] > 0.5 else "", n(r["gain"], 1, True), ar(r["what"]))
        for r in cmp["loud_rows"])
    parts.append('<h3>مقارنة بالمنصات</h3><div class="scroll"><table id="platforms"><thead><tr><th>المنصة</th><th>الهدف (LUFS)</th>'
                 '<th>هتتغيّر بـ (dB)</th><th>يعني إيه</th></tr></thead><tbody>%s</tbody></table></div>' % rows)
    for w in cmp["tp_warn"]:
        parts.append('<div class="note bad" id="tp-warning">⚠ %s</div>' % ar(w))
    if has_refs:
        dl = mine.lufs - cmp["ref_lufs"]
        parts.append('<div class="note info">مقارنة بالريفرنسات: تراكك %s LUFS، ومتوسطهم %s LUFS (فرق %s LU). '
                     'الـ PLR (الفرق بين الـ peak والعلو = قد إيه فيه نَفَس) عندك %s مقابل %s عندهم%s</div>' % (
                         n(mine.lufs), n(cmp["ref_lufs"]), n(dl, 1, True), n(mine.plr), n(cmp["ref_plr"]),
                         " — يعني تراكك مضغوط أكتر." if mine.plr < cmp["ref_plr"] - 1.5 else
                         " — يعني تراكك فيه ديناميك أكتر (ومش لازم ده يبقى عيب)." if mine.plr > cmp["ref_plr"] + 1.5 else "."))

    # ---- spectrum
    parts.append("<h2>2. الطيف (الـ EQ balance)</h2>")
    if has_refs:
        sc, lg = spectrum_chart(mine, refs, cmp)
        parts.append('<p class="sub">منحنى 1/3 أوكتاف — كل منطقة كنسبة من الطاقة الكلية (dB)، فالعلو مش بيأثر: بنقارن التوازن بس. الدهبي = تراكك.</p>')
        parts.append(sc + '<div class="legend">%s</div>' % lg)
        parts.append('<h3>الفرق (تراكك − الريفرنس)</h3><p class="sub">فوق الصفر <span class="pos">دهبي</span> = عندك أكتر، '
                     'تحت الصفر <span class="neg">أزرق</span> = عندك أقل. الشريط الفاتح = ±1.5 dB (فرق مش مهم).</p>')
        parts.append(diff_chart(cmp))
        rows = "".join('<tr><td>%s</td><td class="num">%s–%s Hz</td><td class="num">%s</td><td class="num">%s</td><td class="num %s">%s</td></tr>' % (
            i["name"], engine.fmt_hz(i["lo"]), engine.fmt_hz(i["hi"]), n(i["mine"]), n(i["ref"]),
            ("pos" if i["diff"] > 0 else "neg") if i["big"] else "", n(i["diff"], 1, True)) for i in cmp["bands"])
        parts.append('<h3>توازن المناطق</h3><div class="scroll"><table id="bands"><thead><tr><th>المنطقة</th><th>الترددات</th>'
                     '<th>تراكك (dB من الكل)</th><th>الريفرنس</th><th>الفرق</th></tr></thead><tbody>%s</tbody></table></div>' % rows)
        if cmp["top"]:
            tips = "".join('<li><b>%s</b> <b class="d %s">%s dB</b> عن الريفرنس، أكتر حاجة حوالي <bdi>%s</bdi><br>%s</li>' % (
                i["name"], "pos" if i["diff"] > 0 else "neg", n(i["diff"], 1, True), esc(i["around"]), ar(i["advice"]))
                for i in cmp["top"])
            parts.append('<h3>أكبر الفروق وتجرّب إيه</h3><ul class="tips" id="tips">%s</ul>' % tips)
        else:
            parts.append('<div class="note ok">مفيش منطقة فرقها 1.5 dB أو أكتر — التوازن قريب من الريفرنسات.</div>')
        parts.append('<p class="sub">الأرقام دي مقارنة بمتوسط الريفرنسات. لو الريفرنس من ستايل أو توزيع مختلف، الفروق '
                     'هتبقى طبيعية — اختار ريفرنسات قريبة من أغنيتك.</p>')
    else:
        parts.append('<div class="note info">ضيف ريفرنس واحد على الأقل عشان يظهر رسم الطيف والفروق.</div>')

    # ---- stereo
    parts.append("<h2>3. الستيريو</h2>")
    head = "<th>المنطقة</th><th>تراكك side/mid (dB)</th>" + ("<th>الريفرنس</th>" if has_refs else "")
    rows = "".join("<tr><td>%s</td><td class=\"num\">%s</td>%s</tr>" % (
        name, n(mine.side_mid[k]), '<td class="num">%s</td>' % n(cmp["ref_side_mid"][k]) if has_refs else "")
        for k, name, _, _ in engine.BANDS)
    corr_ref = ""
    if has_refs:
        corr_ref = " — الريفرنسات: %s" % "، ".join("%s %s" % (esc(r.name), n(r.corr, 2)) for r in refs)
    parts.append('<p>Correlation (L/R): <b>%s</b>%s<br>تحت 120Hz: correlation <b>%s</b>، side/mid <b>%s dB</b></p>' % (
        n(mine.corr, 2), corr_ref, n(mine.low_corr, 2), n(mine.low_side_mid)))
    parts.append('<div class="scroll"><table id="stereo"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (head, rows))
    parts.append('<p class="sub">side/mid = قد إيه الجوانب عالية مقارنة بالنص. −20 dB = تقريباً مونو، −6 dB = عريض قوي.</p>')
    for kind, text in cmp["stereo"]:
        parts.append('<div class="note %s"%s>%s</div>' % (kind, ' id="low-mono"' if "120Hz" in text else "", ar(text)))

    # ---- dynamics
    parts.append("<h2>4. العلو على طول الأغنية</h2>")
    lc, lg = loudness_chart(mine, refs)
    parts.append('<p class="sub">Short-term LUFS (كل ثانية). الخط المتقطع = الـ Integrated بتاع كل تراك. '
                 'الـ LRA = الفرق بين أهدى وأعلى حتت (عندك %s LU%s).</p>' % (
                     n(mine.lra), "، الريفرنسات %s" % n(cmp["ref_lra"]) if has_refs else ""))
    parts.append(lc + '<div class="legend">%s</div>' % lg)

    # ---- glossary
    parts.append("""<h2>المصطلحات ببساطة</h2><dl class="gl">
<dt>LUFS (Integrated)</dt><dd>متوسط العلو للأغنية كلها زي ما الودن بتحسه — ده الرقم اللي المنصات بتسوّي عليه.</dd>
<dt>Short-term max</dt><dd>أعلى 3 ثواني في الأغنية.</dd>
<dt>LRA</dt><dd>مدى العلو: الفرق بين الحتت الهادية والعالية. رقم صغير = الأغنية كلها على نفس العلو.</dd>
<dt>True peak (dBTP)</dt><dd>أعلى نقطة في الموجة بعد ما تتحوّل لصوت حقيقي. فوق −1 ممكن يعمل تشويش بعد الضغط لـ MP3/AAC.</dd>
<dt>PLR</dt><dd>الـ true peak ناقص الـ LUFS — قد إيه الماستر فيه "نَفَس". رقم صغير = مضغوط قوي.</dd>
<dt>Crest factor</dt><dd>الفرق بين أعلى peak والـ RMS — نفس فكرة الـ PLR بقياس تاني.</dd>
<dt>Correlation</dt><dd>+1 = مونو تماماً، 0 = عريض جداً، سالب = مشكلة phase.</dd>
</dl>""")

    refs_txt = "، ".join("«%s»" % esc(r.name) for r in refs) or "من غير ريفرنس"
    page = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MixChecker — %s</title><style>%s%s</style></head>
<body><main>
<h1>تقرير MixChecker</h1>
<div class="sub">«%s» — مقارنة بـ %s — %s</div>
%s
<footer>اتعمل بـ MixChecker. الأرقام بتساعدك تعرف تدوّر فين — ودانك وسماعاتك هما الحكم.</footer>
</main></body></html>""" % (esc(mine.name), font_css(), CSS, esc(mine.name), refs_txt,
                         datetime.now().strftime("%Y-%m-%d %H:%M"), "\n".join(parts))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path
