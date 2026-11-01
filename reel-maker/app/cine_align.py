"""Cinematic mode — word timing.

    faster-whisper (rough word times)  ->  autoreel.align (lyric lines in order)
    ->  wav2vec2 Arabic CTC forced alignment per chunk of lines (WhisperX's method,
        run with onnxruntime; the model is exported by build/export_align_model.py)
    ->  words that don't align: the line's time split by character length
    ->  <song>.words.json (editable; render reads it back)
"""
import json
import os
import re
import threading
import time
import urllib.request

import numpy as np

import autoreel
import engine

SR = 16000
STRIDE = 320 / float(SR)          # wav2vec2 frame = 20 ms
MODEL_DIR = os.path.join(autoreel.MODELS_DIR, "align-ar")
RELEASE = ("https://github.com/wasemabdelhady-netizen/wasemabdelhady-netizen.github.io"
           "/releases/download/reel-maker-models/")
MODEL_FILES = ("align-ar.onnx", "align-ar.vocab.json")
CHUNK_MAX = 28.0                  # seconds of audio per alignment pass
PAD = 1.2                         # audio around a chunk (ASR times are rough)
MIN_SCORE = -6.0                  # mean log-prob per token below this = word failed
GARBAGE_PEN = 0.3                 # garbage frame cost below the best token (tuned on Wasem's stem)


# ----------------------------------------------------------------------------- download

def model_ready():
    return all(os.path.isfile(os.path.join(MODEL_DIR, f)) for f in MODEL_FILES)


def download(url, dst, progress=None, cancel=None, label=""):
    """Resumable download to dst (via dst.part). progress(fraction, message)."""
    progress = progress or (lambda f, m="": None)
    cancel = cancel or threading.Event()
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    part = dst + ".part"
    have = os.path.getsize(part) if os.path.isfile(part) else 0
    hdr = {"User-Agent": "reelmaker"}
    if have:
        hdr["Range"] = "bytes=%d-" % have
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=60) as r:
        mode = "ab" if (have and r.status == 206) else "wb"
        if mode == "wb":
            have = 0
        total = have + int(r.headers.get("Content-Length") or 0)
        got, last = have, 0
        with open(part, mode) as fh:
            while True:
                if cancel.is_set():
                    raise engine.Cancelled()
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if time.time() - last > 0.3:
                    last = time.time()
                    progress(got / float(total or got), "بنزّل %s… %d / %d MB" % (label, got >> 20, total >> 20))
    if total and os.path.getsize(part) != total:
        raise RuntimeError("التحميل اتقطع (%s) — دوس تاني وهيكمل من مكانه" % os.path.basename(dst))
    os.replace(part, dst)
    return dst


def ensure_model(progress=None, log=None, cancel=None):
    if model_ready():
        return MODEL_DIR
    log = log or (lambda m: None)
    log("بنزّل موديل توقيت الكلمات العربي (~320 ميجا، مرة واحدة بس)…")
    for f in MODEL_FILES[::-1]:
        dst = os.path.join(MODEL_DIR, f)
        if not os.path.isfile(dst):
            download(RELEASE + f, dst, progress, cancel, "موديل التوقيت")
    log("موديل التوقيت اتنزّل ✔")
    return MODEL_DIR


def ensure_whisper(name, progress=None, log=None, cancel=None):
    """faster-whisper model: Hugging Face first (autoreel), then our GitHub mirror."""
    if autoreel.model_ready(name):
        return autoreel.model_dir(name)
    try:
        return autoreel.ensure_model(name, progress, log, cancel)
    except engine.Cancelled:
        raise
    except Exception as ex:
        if name != "small":
            raise
        (log or (lambda m: None))("بجرب نسخة الموديل اللي على GitHub… (%s)" % str(ex).splitlines()[0])
    d = autoreel.model_dir(name)
    for f in ("config.json", "tokenizer.json", "vocabulary.txt", "model.bin"):
        dst = os.path.join(d, f)
        if not os.path.isfile(dst):
            download(RELEASE + "fw-small-" + f, dst, progress, cancel, "موديل السمع")
    with open(os.path.join(d, ".complete"), "w") as fh:
        fh.write("github-mirror")
    return d


# ----------------------------------------------------------------------------- CTC

class Aligner:
    def __init__(self):
        import onnxruntime as ort
        with open(os.path.join(MODEL_DIR, "align-ar.vocab.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
        self.vocab = meta["vocab"]
        self.blank = int(meta.get("blank", 0))
        self.delim = self.vocab.get(meta.get("word_delim", "|"))
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4))
        with open(os.path.join(MODEL_DIR, "align-ar.onnx"), "rb") as fh:  # bytes: Arabic paths
            self.sess = ort.InferenceSession(fh.read(), so, providers=["CPUExecutionProvider"])

    def emissions(self, audio):
        """log-probs [frames, vocab] for float32 mono 16 kHz audio."""
        x = audio.astype(np.float32)
        x = (x - x.mean()) / (x.std() + 1e-7)
        out = []
        step = int(20 * SR)
        for a in range(0, len(x), step):  # 20 s windows keep attention memory small
            seg = x[a:a + step]
            if len(seg) < 400:
                break
            out.append(self.sess.run(None, {"audio": seg[None]})[0][0])
        return np.concatenate(out) if out else np.zeros((0, len(self.vocab)), np.float32)

    def tokens(self, word):
        """Model token ids of one word (letters the model doesn't know are dropped)."""
        w = autoreel.TASHKEEL_RE.sub("", word.replace("*", ""))
        w = w.replace("ـ", "")
        ids = []
        for ch in w:
            if ch in self.vocab:
                ids.append(self.vocab[ch])
            elif ch in "إأآٱ" and "ا" in self.vocab:
                ids.append(self.vocab["ا"])
            elif ch == "ى" and "ي" in self.vocab:
                ids.append(self.vocab["ي"])
        return ids

    def force_align(self, em, words, pen=GARBAGE_PEN):
        """CTC Viterbi over the blank-interleaved token sequence of `words`, with a
        "garbage" label before and after it: singing in the window that isn't these
        words (the previous / next line) lands there instead of being forced onto the
        first or last word. Returns [(start_frame, end_frame, mean_logp) or None] per word."""
        V = em.shape[1]
        em = np.concatenate([em, (em.max(axis=1) - pen)[:, None]], axis=1)
        seq, owner = [V], [-1]
        for wi, w in enumerate(words):
            ids = self.tokens(w)
            if self.delim is not None and len(seq) > 1:
                seq.append(self.delim)
                owner.append(-1)
            for t in ids:
                seq.append(t)
                owner.append(wi)
        seq.append(V)
        owner.append(-1)
        T, L = em.shape[0], len(seq)
        if L <= 2 or T < L:
            return [None] * len(words)
        S = 2 * L + 1
        lab = np.full(S, self.blank, dtype=np.int64)
        lab[1::2] = seq
        own = np.full(S, -1, dtype=np.int64)
        own[1::2] = owner
        skip = np.zeros(S, dtype=bool)  # s may come from s-2 (label differs from s-2's)
        skip[3::2] = np.array(seq[1:]) != np.array(seq[:-1])
        NEG = -1e30
        dp = np.full(S, NEG)
        dp[0] = em[0, lab[0]]
        dp[1] = em[0, lab[1]]
        bp = np.zeros((T, S), dtype=np.int8)  # 0 stay, 1 from s-1, 2 from s-2
        for t in range(1, T):
            stay = dp
            one = np.concatenate([[NEG], dp[:-1]])
            two = np.where(skip, np.concatenate([[NEG, NEG], dp[:-2]]), NEG)
            stack = np.vstack([stay, one, two])
            ch = np.argmax(stack, axis=0)
            dp = stack[ch, np.arange(S)] + em[t, lab]
            bp[t] = ch
        s = S - 1 if dp[S - 1] >= dp[S - 2] else S - 2
        path = np.empty(T, dtype=np.int64)
        for t in range(T - 1, -1, -1):
            path[t] = s
            s -= int(bp[t, s])
        res = [None] * len(words)
        frames = {}
        for t in range(T):
            o = own[path[t]]
            if o >= 0:
                frames.setdefault(o, []).append(t)
        for wi, fr in frames.items():
            lp = em[fr, lab[path[fr]]]
            res[wi] = (fr[0], fr[-1] + 1, float(lp.mean()))
        return res


# ----------------------------------------------------------------------------- lines

def split_by_chars(words, s, e):
    """Fallback: the line's time shared by character length -> [(start, end)]."""
    lens = [max(autoreel.letters(w), 1) for w in words]
    tot = float(sum(lens))
    out, acc = [], 0.0
    for L in lens:
        a = s + (e - s) * acc / tot
        acc += L
        out.append((a, s + (e - s) * acc / tot))
    return out


def chunks(lines, total):
    """Consecutive lines sung close together, up to CHUNK_MAX seconds each."""
    out, cur = [], []
    for i, ln in enumerate(lines):
        if cur:
            first, prev = lines[cur[0]], lines[cur[-1]]
            if ln.start - prev.end > 2.5 or ln.end - first.start > CHUNK_MAX:
                out.append(cur)
                cur = []
        cur.append(i)
    if cur:
        out.append(cur)
    return out


def refine(lines, vocal, total, progress=None, cancel=None, log=None):
    """lines: autoreel.Line list (rough). vocal: float32 16 kHz (stem or master).
    Returns the words.json dict."""
    progress = progress or (lambda f, m="": None)
    cancel = cancel or threading.Event()
    al = Aligner()
    calibrate(al, lines, vocal, total, log)
    words_out = [None] * len(lines)
    groups = chunks(lines, total)
    done = 0
    for gi, g in enumerate(groups):
        if cancel.is_set():
            raise engine.Cancelled()
        a = max(0.0, lines[g[0]].start - PAD)
        b = min(total, lines[g[-1]].end + PAD)
        if gi > 0:  # don't reach back into the previous chunk's last word
            a = max(a, lines[groups[gi - 1][-1]].end - 0.1)
        if gi + 1 < len(groups):
            b = min(b, lines[groups[gi + 1][0]].start + 0.1)
        seg = vocal[int(a * SR):int(b * SR)]
        flat, owner = [], []
        for li in g:
            for w in lines[li].text.split():
                flat.append(w)
                owner.append(li)
        res = [None] * len(flat)
        if len(seg) > SR * 0.5 and flat:
            try:
                em = al.emissions(seg)
                res = al.force_align(em, flat)
            except Exception as ex:
                if log:
                    log("  التوقيت الدقيق فشل في جزء (%s) — هقسّم بالحروف" % ex)
        k = 0
        for li in g:
            ws = lines[li].text.split()
            r = res[k:k + len(ws)]
            k += len(ws)
            words_out[li] = finish_line(lines[li], ws, r, a)
        done += len(g)
        progress(done / float(len(lines)), "بظبط توقيت الكلمات… %d / %d سطر" % (done, len(lines)))
    out_lines = []
    for ln, ws in zip(lines, words_out):
        out_lines.append(dict(text=ln.text, start=round(ws[0]["s"], 3), end=round(ws[-1]["e"], 3),
                              words=ws))
    fix_order(out_lines)
    return out_lines


def calibrate(al, lines, vocal, total, log=None):
    """Rough times may all be off by the same amount (an old .srt, a different intro):
    align a few lines with a wide window and shift every line by the median error."""
    errs = []
    picks = sorted(range(len(lines)), key=lambda i: -len(lines[i].text))[:4]
    for i in sorted(picks):
        ln = lines[i]
        a, b = max(0.0, ln.start - 4.0), min(total, ln.end + 4.0)
        seg = vocal[int(a * SR):int(b * SR)]
        ws = ln.text.split()
        if len(seg) < SR or not ws:
            continue
        res = al.force_align(al.emissions(seg), ws)
        ok = [r for r in res if r is not None and r[2] >= MIN_SCORE]
        if len(ok) >= max(1, len(ws) // 2) and res[0] is not None:
            errs.append(a + res[0][0] * STRIDE - ln.start)
    if len(errs) >= 2:
        d = float(np.median(errs))
        if abs(d) > 0.25:
            for ln in lines:
                ln.shift(d)
            if log:
                log("  التوقيت التقريبي كان مزحوح %+.2f ث — اتظبط" % d)


def finish_line(line, ws, res, t0):
    """Word dicts for one line from CTC results (None/low score -> char split)."""
    good = [r is not None and r[2] >= MIN_SCORE for r in res]
    times = [((t0 + r[0] * STRIDE), (t0 + r[1] * STRIDE)) if g else None for r, g in zip(res, good)]
    if not any(good):
        s, e = line.start, max(line.end, line.start + 0.3)
        times = split_by_chars(ws, s, e)
        return [dict(w=w, s=round(a, 3), e=round(b, 3), ok=False) for w, (a, b) in zip(ws, times)]
    # fill failed words between aligned neighbours by character length
    n = len(ws)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 else max(line.start, times[j][0] - 0.25 * (j - i)) if j < n else line.start
        right = times[j][0] if j < n else max(left + 0.3 * (j - i), line.end)
        if right - left < 0.12 * (j - i):
            right = left + 0.12 * (j - i)
        for q, ab in zip(range(i, j), split_by_chars(ws[i:j], left, right)):
            times[q] = ab
        i = j
    out = []
    for q, (w, (a, b)) in enumerate(zip(ws, times)):
        out.append(dict(w=w, s=round(a, 3), e=round(max(b, a + 0.08), 3), ok=bool(good[q])))
    return out


def fix_order(lines):
    """Monotonic word starts; a line never starts before the previous one ends."""
    last = 0.0
    for ln in lines:
        for w in ln["words"]:
            w["s"] = round(max(w["s"], last), 3)
            w["e"] = round(max(w["e"], w["s"] + 0.08), 3)
            last = w["s"] + 0.04
        ln["start"], ln["end"] = ln["words"][0]["s"], ln["words"][-1]["e"]
        last = max(last, ln["end"] - 0.02)


# ----------------------------------------------------------------------------- words.json

def key_index(text):
    """Index of the *marked* gold word, or -1."""
    for i, w in enumerate(text.split()):
        if w.startswith("*") and w.endswith("*") and len(w) > 2:
            return i
    return -1


def save_words(path, song, lines, source):
    data = dict(song=song, source=source,
                note="عدّل s/e لأي كلمة (بالثواني على الماستر) وبعدين دوس «أعد التحميل». "
                     "ok=false يعني الكلمة اتقسمت بالحروف ومحتاجة مراجعة.",
                lines=[dict(text=l["text"], key=key_index(l["text"]), start=l["start"], end=l["end"],
                            words=l["words"]) for l in lines])
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return path


def load_words(path, texts=None):
    """Read words.json. With `texts`, return None if the lines don't match the lyrics."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    lines = data.get("lines") or []
    for ln in lines:
        ws = ln.get("words") or []
        if len(ws) != len(ln["text"].split()):
            raise RuntimeError("في words.json: عدد الكلمات مش مظبوط في السطر:\n%s" % ln["text"])
        for w in ws:
            w["s"], w["e"] = float(w["s"]), float(w["e"])
        ln["start"], ln["end"] = ws[0]["s"], ws[-1]["e"]
        ln["key"] = key_index(ln["text"]) if ln.get("key") is None else int(ln["key"])
    if texts is not None:
        norm = autoreel.normalize
        if [norm(l["text"]) for l in lines] != [norm(t) for t in texts]:
            return None
    return lines


def clean_text(w):
    return re.sub(r"^\*|\*$", "", w)
