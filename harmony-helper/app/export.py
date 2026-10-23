"""Exports: MIDI files, chord chart, Arabic HTML report; plus the preview synth."""
import base64
import html
import os
import re
import tempfile
import wave

import numpy as np

import engine
import midi

HERE = os.path.dirname(os.path.abspath(__file__))
LINE_FILE = {"third_up": "3rd above", "third_down": "3rd below", "sixth_down": "6th below",
             "octave_up": "octave up", "octave_down": "octave down"}
# key signature (sharps > 0, flats < 0) of each major tonic
KEY_SF = {0: 0, 7: 1, 2: 2, 9: 3, 4: 4, 11: 5, 6: 6, 1: -5, 8: -4, 3: -3, 10: -2, 5: -1}


def safe(s):
    return re.sub(r'[\\/:*?"<>|]+', " ", s).strip() or "song"


def out_folder(song):
    name = safe(song.name) + " - HarmonyHelper"
    d = os.path.join(os.path.dirname(os.path.abspath(song.path)), name)
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".write-test")
        open(probe, "w").close()
        os.remove(probe)
    except OSError:  # song folder not writable
        d = os.path.join(engine.OUTPUT_DIR, name)
        os.makedirs(d, exist_ok=True)
    return d


def key_sig(song):
    major = song.tonic if song.scale == "major" else (song.tonic + 3) % 12
    return KEY_SF[major], 0 if song.scale == "major" else 1


def chord_notes(song):
    out = []
    for c in song.chords:
        if c["root"] is None:
            continue
        r = c["root"]
        third = 3 if c["quality"] == "min" else 4
        root = 48 + r if r < 7 else 36 + r  # keep the voicing around C3–B3
        for m in (root - 12, root, root + third, root + 7):
            out.append((c["start"], c["end"], m, 84 if m != root - 12 else 76))
    return out


def note_events(notes, transpose=0, vel=96):
    return [(n["start"], n["end"], n["midi"] + transpose, vel) for n in notes]


def export_all(song, lines, transpose=0):
    """lines: harmony keys to export. Returns (folder, [files])."""
    d = out_folder(song)
    base = safe(song.name)
    files = []
    ks = key_sig(song)
    chords = {"name": "Chords", "channel": 0, "program": 0, "notes": chord_notes(song)}
    melody = {"name": "Melody (vocal)", "channel": 1, "program": 53, "notes": note_events(song.notes)}
    harm = []
    for i, k in enumerate(lines):
        label = LINE_FILE[k] + ("" if not transpose else " (%+d)" % transpose)
        harm.append((k, {"name": "Harmony " + label, "channel": 2 + i, "program": 52,
                         "notes": note_events(song.harmony(k), transpose, 88)}))
    files.append(midi.write(os.path.join(d, base + " - chords.mid"), song.bpm, [chords], ks))
    if song.notes:
        files.append(midi.write(os.path.join(d, base + " - melody.mid"), song.bpm, [melody], ks))
    for k, tr in harm:
        fn = base + " - harmony " + LINE_FILE[k] + (" %+d" % transpose if transpose else "") + ".mid"
        files.append(midi.write(os.path.join(d, fn), song.bpm, [tr], ks))
    all_tracks = [chords] + ([melody] if song.notes else []) + [tr for _, tr in harm]
    files.append(midi.write(os.path.join(d, base + " - all tracks.mid"), song.bpm, all_tracks, ks))
    chart = os.path.join(d, base + " - chords.txt")
    with open(chart, "w", encoding="utf-8-sig", newline="\r\n") as f:  # BOM: Notepad shows Arabic right
        f.write(engine.chart_text(song))
    files.append(chart)
    files.append(write_report(song, lines, transpose, os.path.join(d, base + " - تقرير.html"), files))
    return d, files


# ----------------------------------------------------------------------------- report

def font_css():
    try:
        b = base64.b64encode(open(os.path.join(HERE, "assets", "cairo-subset.woff2"), "rb").read()).decode()
        return '@font-face{font-family:"Cairo";font-weight:200 1000;src:url(data:font/woff2;base64,%s) format("woff2")}' % b
    except OSError:
        return ""


LTR_RUN = re.compile(r"[A-Za-z0-9+\-−#][A-Za-z0-9.,+\-−–/%&#_ :]*[A-Za-z0-9%#]|[A-Za-z0-9#]")


def ar(text):
    return LTR_RUN.sub(lambda m: '<bdi dir="ltr">%s</bdi>' % m.group(0), html.escape(str(text)))


CSS = """
:root{--bg:#0a0a0a;--panel:#141210;--panel2:#1b1815;--line:#2b2620;--gold:#e0a458;--text:#f5efe6;--muted:#9a8f80;--warn:#ffcc66}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);font-family:"Cairo",system-ui,sans-serif;font-size:15.5px;line-height:1.75}
main{max-width:960px;margin:0 auto;padding:22px 16px 60px}
h1{color:var(--gold);font-size:26px;margin:0}
h2{font-size:20px;margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted);font-size:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-top:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 14px}
.card b{display:block;font-size:22px;color:var(--gold)}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{color:var(--gold);background:var(--panel2)}
td.c{direction:ltr;text-align:left;font-family:ui-monospace,Consolas,monospace;font-weight:700}
tr.qt td{background:rgba(255,204,102,.08)}
.warn{border-inline-start:4px solid var(--warn);background:var(--panel);padding:8px 14px;margin:8px 0}
.ok{border-inline-start:4px solid #5fd3a0;background:var(--panel);padding:8px 14px;margin:8px 0}
ul{padding-inline-start:20px}
code{direction:ltr;unicode-bidi:isolate;background:var(--panel2);padding:0 6px;border-radius:6px}
"""


def write_report(song, lines, transpose, path, files):
    k_en = engine.key_name(song.tonic, song.scale)
    parts = []
    parts.append('<div class="cards"><div class="card">BPM<b><bdi>%.1f</bdi></b></div>'
                 '<div class="card">المقام / السلم<b><bdi>%s</bdi></b><span class="sub">%s</span></div>'
                 '<div class="card">ضبط الدوزان<b><bdi>%+.0f cents</bdi></b><span class="sub">عن 440Hz</span></div>'
                 '<div class="card">نوتات اللحن<b><bdi>%d</bdi></b><span class="sub">%s</span></div></div>' % (
                     song.bpm, html.escape(k_en), html.escape(engine.key_name_ar(song.tonic, song.scale)),
                     song.tuning, len(song.notes), "من الـ vocal stem" if song.melody_from_stem else "من غير stem — تقريبي"))
    second = song.key_scores[1]
    parts.append('<p class="sub">%s</p>' % ar("الاحتمال التاني للسلم: %s. لو حاسس إن ده الصح، غيّره من البرنامج قبل التصدير."
                                               % engine.key_name(second[1], second[2])))
    # maqam
    parts.append("<h2>تحذير المقامات (ربع التون)</h2>")
    if song.quarter_sections:
        for q in song.quarter_sections:
            parts.append('<div class="warn" id="maqam-warning">⚠ %s</div>' % ar(
                "من %s لحد %s (بار %d%s): احتمال مقام فيه ربع تون — الكوردات والهارموني هنا ممكن ميبقوش دقيقين. "
                "النوتات: %s." % (engine.fmt_time(q["start"]), engine.fmt_time(q["end"]), q["bar_from"],
                                  "" if q["bar_to"] == q["bar_from"] else "–%d" % q["bar_to"], "، ".join(q["pitches"]))))
        parts.append('<p class="sub">%s</p>' % ar(
            "الـ MIDI مفيهوش ربع تون: النوتات دي اتقرّبت لأقرب نص تون. لو الحتة دي بياتي أو صبا أو سيكا، "
            "الأفضل تغنّي الهارموني فيها بودنك أو تستخدم pitch bend / microtuning في الـ DAW."))
    else:
        parts.append('<div class="ok">%s</div>' % ar("مفيش نوتات بعيدة عن السلم الغربي بأكتر من 35 cent — مفيش ربع تون واضح."))
    # chords
    parts.append("<h2>الكوردات</h2>")
    parts.append('<p class="sub">%s</p>' % ar(
        "بار 1 = أول ضربة قوية (عند %s). عمود DAW = مكان البار لو حطيت الأغنية على 1.1.1 وتمبو المشروع %.1f. "
        "«.» = نفس الكورد مكمّل." % (engine.fmt_time(song.first_downbeat), song.bpm)))
    rows = []
    for ln in engine.chart_text(song).splitlines()[4:]:
        m = re.match(r"بار\s+(-?\d+)\s+(\S+)\s+DAW (\S+)\s+\|(.*)\|(.*)$", ln)
        if not m:
            continue
        qt = bool(m.group(5).strip())
        rows.append('<tr class="%s"><td>%s</td><td class="c">%s</td><td class="c">%s</td><td class="c">%s</td><td>%s</td></tr>' % (
            "qt" if qt else "", m.group(1), m.group(2), m.group(3), html.escape(m.group(4).strip()), "⚠ ربع تون" if qt else ""))
    parts.append('<div class="scroll"><table id="chart"><thead><tr><th>بار</th><th>الوقت</th><th>DAW</th><th>الكوردات (4 ضربات)</th><th></th></tr></thead>'
                 '<tbody>%s</tbody></table></div>' % "".join(rows))
    # harmony
    parts.append("<h2>خطوط الهارموني</h2>")
    names = {k: lab for k, lab, _, _ in engine.LINES}
    if lines:
        parts.append("<ul>%s</ul>" % "".join("<li>%s</li>" % ar(names[k]) for k in lines))
        parts.append('<p class="sub">%s</p>' % ar(
            "الهارموني ماشي على سلم %s (diatonic): يعني التالتة ممكن تبقى كبيرة أو صغيرة حسب مكانها في السلم. %s"
            % (k_en, "كلها متحرّكة %+d نص تون." % transpose if transpose else "")))
    else:
        parts.append('<p class="sub">مفيش خطوط هارموني اتصدّرت.</p>')
    parts.append("<h2>الملفات</h2><ul>%s</ul>" % "".join("<li><code>%s</code></li>" % html.escape(os.path.basename(f)) for f in files))
    parts.append("<h2>تدخّلهم الـ DAW إزاي</h2><ol>"
                 "<li>%s</li><li>%s</li><li>%s</li><li>%s</li></ol>" % (
                     ar("اعمل مشروع جديد وخلّي التمبو %.1f و 4/4." % song.bpm),
                     ar("حط الأغنية على أول المشروع بالظبط (1.1.1)."),
                     ar("اسحب ملفات الـ .mid على تراكات MIDI جديدة — برضه على 1.1.1 — وحط عليها أي آلة (بيانو، pad، choir)."),
                     ar("ملف «all tracks» فيه كل حاجة في ملف واحد، والملفات المنفصلة أسهل لو عايز تراك واحد بس.")))
    page = ('<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1"><title>HarmonyHelper — %s</title>'
            "<style>%s%s</style></head><body><main><h1>HarmonyHelper</h1><div class=\"sub\">«%s»</div>%s"
            '<p class="sub" style="margin-top:40px">الأرقام دي تحليل آلي — ودنك هي الحكم.</p></main></body></html>') % (
        html.escape(song.name), font_css(), CSS, html.escape(song.name), "\n".join(parts))
    with open(path, "w", encoding="utf-8") as f:
        f.write(page)
    return path


# ----------------------------------------------------------------------------- preview

PSR = 44100


def _voice(f, dur, kind):
    t = np.arange(int(dur * PSR)) / PSR
    if kind == "pad":
        y = sum(a * np.sin(2 * np.pi * f * (k + 1) * t) for k, a in enumerate((1, .35, .15)))
        env = np.minimum(1, t / 0.05) * np.minimum(1, np.maximum(0, (dur - t) / 0.12))
    else:  # soft "ooh": a few harmonics with a little vibrato
        ph = 2 * np.pi * f * t + 0.004 * f * np.sin(2 * np.pi * 5.2 * t) / 5.2 * 2 * np.pi
        y = sum(a * np.sin((k + 1) * ph) for k, a in enumerate((1, .45, .25, .12)))
        env = np.minimum(1, t / 0.03) * np.minimum(1, np.maximum(0, (dur - t) / 0.06))
    return y * env


def render_preview(song, what, start=0.0, length=30.0, transpose=0):
    """what: 'melody', 'chords' or a harmony key. Song in the middle, the line on top. Returns a temp WAV."""
    start = max(0.0, min(start, song.duration - 1))
    length = min(length, song.duration - start)
    r = engine.run_ffmpeg(["-ss", "%.3f" % start, "-t", "%.3f" % length, "-i", song.path, "-vn", "-ac", "2",
                           "-ar", str(PSR), "-f", "f32le", "-"])
    bed = np.frombuffer(r.stdout, "<f4").reshape(-1, 2).astype(np.float64) if r.returncode == 0 else np.zeros((int(length * PSR), 2))
    n = len(bed)
    line = np.zeros(n)
    if what == "chords":
        evs = chord_notes(song)
        kind, tr = "pad", 0
    else:
        evs = note_events(song.notes if what == "melody" else song.harmony(what), 0 if what == "melody" else transpose)
        kind, tr = "voice", 0
    for s, e, m, v in evs:
        if e < start or s > start + length:
            continue
        a = max(s, start)
        sig = _voice(440 * 2 ** ((m + tr - 69) / 12.0), e - a, kind) * (v / 127.0)
        i = int((a - start) * PSR)
        k = min(len(sig), n - i)
        if k > 0:
            line[i:i + k] += sig[:k]
    if np.abs(line).max() > 0:
        line *= 0.35 / np.abs(line).max()
    peak = max(np.abs(bed).max(), 1e-9)
    out = 0.55 * bed / peak
    out[:, 0] += line * 0.8
    out[:, 1] += line
    out /= max(1.0, np.abs(out).max() / 0.95)
    fd, path = tempfile.mkstemp(prefix="harmony_", suffix=".wav")
    os.close(fd)
    with open(path, "wb") as fh:
        w = wave.open(fh, "wb")
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(PSR)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())
        w.close()
    return path
