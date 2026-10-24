"""Auto mode: full song + full video + plain lyrics -> several lyric reels.

    lyrics (no times) --faster-whisper--> word times --fuzzy alignment--> line/word times
    line times + repetition --> sections (verse / chorus) --> reels of 12-30 s
    each reel: footage from the music video (synced by audio, or the liveliest
    unused shots), vertical crop that follows the face, then engine.Job renders it.

Everything that touches files goes through ffmpeg or Python's own open(), so
Arabic paths work on Windows.
"""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.request

import numpy as np

import engine

ROOT = engine.ROOT
MODELS_DIR = os.path.join(ROOT, "models")
CACHE_DIR = os.path.join(ROOT, "cache")
MODEL_NAMES = ("small", "medium", "tiny")
HF_HOSTS = ("https://huggingface.co", "https://hf-mirror.com")
MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt",
               "vocabulary.json", "preprocessor_config.json")

MIN_REEL, MAX_REEL = 12.0, 30.0
LEAD = 0.3            # reel starts this much before its first line
GAP_SPLIT = 2.5       # a pause this long between lines starts a new section
SR = 16000


# ----------------------------------------------------------------------------- text

TASHKEEL_RE = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize(s):
    """Spelling-insensitive Arabic: no tashkeel/tatweel, one alef, ى=ي, ة=ه, no punctuation."""
    s = (s or "").replace("*", " ")
    s = TASHKEEL_RE.sub("", s)
    s = re.sub("[إأآٱا]", "ا", s)
    s = (s.replace("ى", "ي").replace("ی", "ي").replace("ئ", "ي").replace("ؤ", "و")
         .replace("ة", "ه").replace("ک", "ك").replace("ء", ""))
    s = s.translate(DIGITS)
    s = re.sub(r"[^\w\s]", " ", s).replace("_", " ")
    return " ".join(s.split()).lower()


def loose(s):
    """normalize() + Egyptian sound swaps (ق→ء, ث→ت, ذ→د, ظ→ض) for matching sung words."""
    s = normalize(s)
    return s.replace("ق", "ا").replace("ث", "ت").replace("ذ", "د").replace("ظ", "ض")


def read_plain_lyrics(path):
    """Lyric lines without times (times, if any, are dropped; empty/# lines skipped)."""
    return [t for _, t in engine.parse_lyrics(engine.read_text(path)) if t]


def letters(w):
    return max(len(normalize(w).replace(" ", "")), 1)


# ----------------------------------------------------------------------------- lines

class Line:
    """One lyric line. start/end/words are in the song's timeline (seconds)."""

    def __init__(self, text, start=0.0, end=0.0, words=None, conf=0.0):
        self.text, self.start, self.end, self.conf = text, start, end, conf
        self.words = words  # list of word start times (one per text.split()) or None

    def shift(self, d):
        self.start += d
        self.end += d
        if self.words:
            self.words = [w + d for w in self.words]

    def set_text(self, text):
        text = " ".join(text.split())
        if len(text.split()) != len(self.text.split()):
            self.words = None
        self.text = text

    def to_json(self):
        return dict(text=self.text, start=self.start, end=self.end, words=self.words, conf=self.conf)

    @classmethod
    def from_json(cls, d):
        return cls(d["text"], d["start"], d["end"], d.get("words"), d.get("conf", 0.0))


def fmt(t, ms=True):
    t = max(0.0, t)
    m = int(t // 60)
    return ("%d:%05.2f" % (m, t - 60 * m)) if ms else ("%d:%02d" % (m, int(t - 60 * m)))


def fmt_file(t):
    """1m07 — Windows file names can't hold ':'"""
    t = max(0.0, t)
    return "%dm%02d" % (int(t // 60), int(t % 60))


def save_timed(lines, path, song=""):
    """'[mm:ss.xx] line' file the manual mode reads; an empty timed line ends a line
    that is followed by a pause."""
    out = ["# %s — توقيت أوتوماتيك (Reel Maker)" % song if song else "# توقيت أوتوماتيك (Reel Maker)",
           "# تقدر تعدّل الأوقات هنا وتستخدم الملف في الوضع اليدوي"]
    for i, ln in enumerate(lines):
        out.append("[%02d:%05.2f] %s" % (int(ln.start // 60), ln.start % 60, ln.text))
        nxt = lines[i + 1].start if i + 1 < len(lines) else None
        if nxt is None or nxt - ln.end > 1.0:
            out.append("[%02d:%05.2f]" % (int(ln.end // 60), ln.end % 60))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    return path


def load_timed(path, texts=None):
    """Read a timed file back into Lines (ends from the next time). If `texts` is given
    the file must have the same lines, otherwise None."""
    entries = engine.parse_lyrics(engine.read_text(path))
    if not entries or any(t is None for t, _ in entries):
        return None
    lines = []
    for i, (t, txt) in enumerate(entries):
        if not txt:
            continue
        nxt = entries[i + 1][0] if i + 1 < len(entries) else t + 4.0
        lines.append(Line(txt, t, max(t + 0.4, min(nxt - 0.05, t + 9.0)), None, 1.0))
    if texts is not None and [normalize(l.text) for l in lines] != [normalize(t) for t in texts]:
        return None
    return lines


# ----------------------------------------------------------------------------- alignment

def asr_tokens(words):
    """ASR words [(text, start, end), ...] -> [(loose_token, start, end)] (multi-word
    items are split evenly)."""
    out = []
    for w in words:
        txt, a, b = w[0], float(w[1]), float(w[2])
        parts = loose(txt).split()
        if not parts:
            continue
        step = (b - a) / len(parts)
        for k, p in enumerate(parts):
            out.append((p, a + k * step, a + (k + 1) * step))
    return out


def similarity(lyr, asr):
    """Matrix of fuzzy similarity 0..1 between lyric and ASR tokens."""
    from rapidfuzz import fuzz, process
    if not lyr or not asr:
        return np.zeros((len(lyr), len(asr)), dtype=np.float32)
    return process.cdist(lyr, asr, scorer=fuzz.ratio, dtype=np.float32) / 100.0


def nw_align(sim, gap_lyr=-0.35, gap_asr=-0.25, thr=0.55):
    """Needleman-Wunsch over the similarity matrix. Returns {lyric_idx: asr_idx}
    for pairs similar enough to trust. Monotonic: repeated choruses map in order."""
    n, m = sim.shape
    score = ((sim - thr) * 2.0).tolist()
    prev = [j * gap_asr for j in range(m + 1)]
    ptr = [bytearray(m + 1) for _ in range(n + 1)]  # 0 diag, 1 up (skip lyric), 2 left (skip asr)
    for j in range(1, m + 1):
        ptr[0][j] = 2
    for i in range(1, n + 1):
        cur = [0.0] * (m + 1)
        cur[0] = i * gap_lyr
        row, p = score[i - 1], ptr[i]
        p[0] = 1
        for j in range(1, m + 1):
            d = prev[j - 1] + row[j - 1]
            u = prev[j] + gap_lyr
            le = cur[j - 1] + gap_asr
            if d >= u and d >= le:
                cur[j] = d
            elif u >= le:
                cur[j] = u
                p[j] = 1
            else:
                cur[j] = le
                p[j] = 2
        prev = cur
    pairs = {}
    i, j = n, m
    while i > 0 or j > 0:
        k = ptr[i][j]
        if i > 0 and j > 0 and k == 0:
            if sim[i - 1, j - 1] >= thr - 0.05:
                pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif i > 0 and (k == 1 or j == 0):
            i -= 1
        else:
            j -= 1
    return pairs


def word_dur(w):
    return min(max(0.12 * letters(w), 0.25), 1.2)


def align(texts, words, total=None):
    """texts: lyric lines (ground truth). words: ASR [(text, start, end), ...].
    Returns [Line] with start/end/word times; repeated lines map to each occurrence in order."""
    toks = []  # (line, word_idx, raw word, loose)
    for li, t in enumerate(texts):
        for wi, w in enumerate(t.split()):
            toks.append((li, wi, w, loose(w).replace(" ", "")))
    asr = asr_tokens(words)
    total = total or (asr[-1][2] + 5.0 if asr else 10.0 * max(len(texts), 1))
    keyed = [k for k, t in enumerate(toks) if t[3]]
    sim = similarity([toks[k][3] for k in keyed], [a[0] for a in asr])
    pairs = nw_align(sim) if len(keyed) and len(asr) else {}
    st = [None] * len(toks)
    en = [None] * len(toks)
    for ki, aj in pairs.items():
        k = keyed[ki]
        st[k], en[k] = asr[aj][1], asr[aj][2]
    matched = [s is not None for s in st]

    # unmatched runs: words of an anchored line hug their anchor, whole lines share the rest
    k = 0
    N = len(toks)
    while k < N:
        if st[k] is not None:
            k += 1
            continue
        a = k
        while k < N and st[k] is None:
            k += 1
        b = k  # run = a..b-1
        left_t = en[a - 1] if a > 0 else 0.0
        right_t = st[b] if b < N else total
        if right_t < left_t:
            right_t = left_t
        left_line = toks[a - 1][0] if a > 0 else None
        right_line = toks[b][0] if b < N else None
        run = list(range(a, b))
        head = [x for x in run if toks[x][0] == left_line]
        tail = [x for x in run if toks[x][0] == right_line and toks[x][0] != left_line]
        mid = [x for x in run if x not in head and x not in tail]
        span = right_t - left_t
        need = sum(word_dur(toks[x][2]) for x in run) or 1.0
        squeeze = min(1.0, span / need) if span > 0 else 0.0
        t = left_t
        for x in head:
            d = word_dur(toks[x][2]) * squeeze
            st[x], en[x] = t, t + d
            t += d
        t2 = right_t
        for x in reversed(tail):
            d = word_dur(toks[x][2]) * squeeze
            st[x], en[x] = t2 - d, t2
            t2 -= d
        if mid:
            lo, hi = t, t2
            groups = []
            for x in mid:
                if groups and toks[groups[-1][-1]][0] == toks[x][0]:
                    groups[-1].append(x)
                else:
                    groups.append([x])
            weights = [sum(letters(toks[x][2]) for x in g) for g in groups]
            tw = float(sum(weights))
            acc = 0.0
            for g, wgt in zip(groups, weights):
                g0 = lo + (hi - lo) * acc / tw
                acc += wgt
                g1 = lo + (hi - lo) * acc / tw
                natural = sum(word_dur(toks[x][2]) for x in g)
                if natural < g1 - g0:  # sit in the middle of the share at a natural pace
                    pad = (g1 - g0 - natural) / 2
                    g0, g1 = g0 + pad, g1 - pad
                sc = (g1 - g0) / natural if natural else 0
                tt = g0
                for x in g:
                    d = word_dur(toks[x][2]) * sc
                    st[x], en[x] = tt, tt + d
                    tt += d

    lines = []
    for li, txt in enumerate(texts):
        idx = [k for k in range(N) if toks[k][0] == li]
        if not idx:
            lines.append(Line(txt, 0.0, 0.0, None, 0.0))
            continue
        ws = [st[k] for k in idx]
        for q in range(1, len(ws)):
            ws[q] = max(ws[q], ws[q - 1])
        conf = sum(1 for k in idx if matched[k]) / float(len(idx))
        lines.append(Line(txt, ws[0], max(en[idx[-1]], ws[-1] + 0.2), ws, conf))
    tidy(lines, total)
    return lines


def tidy(lines, total=None):
    """Keep lines in order and non-overlapping; fill lines that had no words."""
    for i, ln in enumerate(lines):
        if ln.end <= ln.start:
            prev_end = lines[i - 1].end if i else 0.0
            ln.start, ln.end = prev_end + 0.1, prev_end + 0.1 + 0.15 * letters(ln.text)
            ln.words = None
    for i in range(1, len(lines)):
        p, ln = lines[i - 1], lines[i]
        if ln.start < p.start + 0.3:
            ln.shift(p.start + 0.3 - ln.start)
        if p.end > ln.start - 0.05:
            p.end = max(p.start + 0.3, ln.start - 0.05)
    if total:
        for ln in lines:
            ln.end = min(ln.end, total)
    return lines


# ----------------------------------------------------------------------------- sections

def repeat_groups(lines, thr=85):
    """Group id per line; lines that read the same (fuzzy) share a group."""
    from rapidfuzz import fuzz
    keys = [loose(l.text) for l in lines]
    gid, reps = [], []
    for k in keys:
        g = -1
        for j, r in enumerate(reps):
            if k and fuzz.ratio(k, r) >= thr:
                g = j
                break
        if g < 0:
            reps.append(k)
            g = len(reps) - 1
        gid.append(g)
    return gid


def find_blocks(lines):
    """Split lines into blocks of the same kind: pauses and chorus/verse changes cut."""
    if not lines:
        return []
    gid = repeat_groups(lines)
    count = {}
    for g in gid:
        count[g] = count.get(g, 0) + 1
    rep = [count[g] >= 2 for g in gid]
    # runs of the same flag, cut by long pauses
    runs = []
    for i in range(len(lines)):
        pause = i and lines[i].start - lines[i - 1].end > GAP_SPLIT
        if not runs or pause or rep[i] != runs[-1]["rep"]:
            runs.append(dict(rep=rep[i], idx=[i], pause=bool(pause) or not runs))
        else:
            runs[-1]["idx"].append(i)
    # a lone line that flips the flag joins its neighbour (no pause between them)
    merged = []
    for r in runs:
        if merged and len(r["idx"]) == 1 and not r["pause"]:
            merged[-1]["idx"] += r["idx"]
            continue
        if merged and len(merged[-1]["idx"]) == 1 and not r["pause"] and not merged[-1].get("lead"):
            prev = merged.pop()
            r = dict(r, idx=prev["idx"] + r["idx"], pause=prev["pause"])
        merged.append(r)
    blocks = []
    for r in merged:
        reps = sum(1 for i in r["idx"] if rep[i])
        kind = "chorus" if reps * 2 > len(r["idx"]) else "verse"
        if blocks and blocks[-1]["kind"] == kind and not r["pause"]:
            blocks[-1]["idx"] += r["idx"]
        else:
            blocks.append(dict(kind=kind, idx=list(r["idx"])))
    return [(b["kind"], b["idx"][0], b["idx"][-1]) for b in blocks]


def tail_for(closing):
    return 0.6 + (engine.CLOSING_SECONDS if closing else 0.0)


def reel_span(lines, i0, i1, total, closing=engine.DEFAULT_CLOSING):
    s = max(0.0, lines[i0].start - LEAD)
    e = lines[i1].end + tail_for(closing)
    if e - s < MIN_REEL:
        e = s + MIN_REEL
    if total:
        e = min(e, total)
        if e - s < MIN_REEL:
            s = max(0.0, e - MIN_REEL)
    return s, e


def make_sections(lines, total, closing=engine.DEFAULT_CLOSING):
    """Blocks -> reel candidates of 12-30 s snapped to whole lines, labelled in Arabic."""
    blocks = find_blocks(lines)
    tail = tail_for(closing)

    def length(a, b):
        return lines[b].end + tail - max(0.0, lines[a].start - LEAD)

    chunks = []  # (kind, block_no, i0, i1)
    for bno, (kind, b0, b1) in enumerate(blocks):
        a = b0
        while a <= b1:
            b = a
            while b + 1 <= b1 and length(a, b + 1) <= MAX_REEL:
                b += 1
            # too short: borrow neighbouring lines sung without a pause; otherwise the
            # reel runs on into the music (reel_span pads it to MIN_REEL)
            a0 = a
            while length(a0, b) < MIN_REEL:
                if (b + 1 < len(lines) and lines[b + 1].start - lines[b].end < GAP_SPLIT
                        and length(a0, b + 1) <= MAX_REEL):
                    b += 1
                elif (a0 > 0 and lines[a0].start - lines[a0 - 1].end < GAP_SPLIT
                        and length(a0 - 1, b) <= MAX_REEL):
                    a0 -= 1
                else:
                    break
            chunks.append((kind, bno, a0, b))
            a = b + 1
    # labels
    n_ch = len({bno for k, bno, _, _ in chunks if k == "chorus"})
    names, seen_blocks, cnt = [], {}, {"chorus": 0, "verse": 0}
    for kind, bno, a, b in chunks:
        if bno not in seen_blocks:
            cnt[kind] += 1
            seen_blocks[bno] = cnt[kind]
        no = seen_blocks[bno]
        if kind == "chorus":
            base = "الكورس الأخير" if (n_ch >= 2 and no == n_ch) else "الكورس %d" % no
        else:
            base = "كوبليه %d" % no
        names.append(base)
    out = []
    for (kind, bno, a, b), base in zip(chunks, names):
        parts = [c for c, nm in zip(chunks, names) if nm == base]
        name = base if len(parts) == 1 else "%s - جزء %d" % (base, parts.index((kind, bno, a, b)) + 1)
        out.append(dict(name=name, kind=kind, i0=a, i1=b, hook="", closing=closing))
    first_verse = next((s for s in out if s["kind"] == "verse"), None)
    for s in out:
        s["checked"] = s["kind"] == "chorus" or s is first_verse
    return out


# ----------------------------------------------------------------------------- audio

def decode(path, sr=SR, start=None, dur=None):
    args = []
    if start is not None:
        args += ["-ss", "%.3f" % start]
    if dur is not None:
        args += ["-t", "%.3f" % dur]
    r = engine.run_ffmpeg(args + ["-i", path, "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"])
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("مش قادر أقرا الصوت:\n%s\n%s" % (path, r.stderr.decode("utf-8", "replace")[-500:]))
    return np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)


def energy(x, sr, hop=0.05):
    n = int(sr * hop)
    k = len(x) // n
    if k == 0:
        return np.zeros(0, dtype=np.float32)
    f = x[:k * n].reshape(k, n)
    return np.log10((f * f).mean(axis=1) + 1e-7).astype(np.float32)


def find_offset(song_path, video_path, hop=0.05):
    """Seconds to add to a song time to land on the same moment in the video
    (video has the song as its soundtrack), or None when they don't match."""
    try:
        a = energy(decode(song_path, 8000), 8000, hop)
        b = energy(decode(video_path, 8000), 8000, hop)
    except RuntimeError:
        return None
    if len(a) < 100 or len(b) < 100:
        return None
    # onsets (positive energy change) are robust to level/EQ differences
    da = np.maximum(np.diff(a), 0)
    db = np.maximum(np.diff(b), 0)
    da = (da - da.mean()) / (da.std() + 1e-9)
    db = (db - db.mean()) / (db.std() + 1e-9)
    n = 1
    while n < len(da) + len(db):
        n *= 2
    c = np.fft.irfft(np.fft.rfft(db, n) * np.conj(np.fft.rfft(da, n)), n)
    # lag L: db[t + L] ~ da[t]; lags are 0..len(db)-1 and negative wrapped at the end
    lags = np.concatenate([np.arange(0, len(db)), np.arange(-len(da) + 1, 0)])
    vals = np.concatenate([c[:len(db)], c[n - len(da) + 1:]])
    L = int(lags[int(np.argmax(vals))])
    # correlation coefficient over the overlap at that lag
    if L >= 0:
        x, y = da[:max(0, min(len(da), len(db) - L))], db[L:L + len(da)]
    else:
        x, y = da[-L:-L + len(db)], db[:len(da) + L]
    m = min(len(x), len(y))
    if m < 200:
        return None
    r = float(np.corrcoef(x[:m], y[:m])[0, 1])
    return (L * hop, r) if r > 0.35 else None


# ----------------------------------------------------------------------------- ASR

def model_dir(name):
    return os.path.join(MODELS_DIR, name)


def model_ready(name):
    return os.path.isfile(os.path.join(model_dir(name), ".complete"))


def _open(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "reelmaker"}, **(headers or {})))
    return urllib.request.urlopen(req, timeout=60)


def ensure_model(name, progress=None, log=None, cancel=None):
    """Download Systran/faster-whisper-<name> into models/<name>/ (once; resumes)."""
    progress = progress or (lambda f, m="": None)
    log = log or (lambda m: None)
    cancel = cancel or threading.Event()
    d = model_dir(name)
    if model_ready(name):
        return d
    os.makedirs(d, exist_ok=True)
    repo = "Systran/faster-whisper-%s" % name
    err = None
    for host in HF_HOSTS:
        try:
            files = None
            try:
                with _open("%s/api/models/%s?blobs=true" % (host, repo)) as r:
                    info = json.loads(r.read().decode("utf-8"))
                files = [(s["rfilename"], s.get("size") or 0) for s in info.get("siblings", [])
                         if s["rfilename"] in MODEL_FILES]
            except Exception:
                pass
            if not files:
                files = [(f, 0) for f in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")]
            total = float(sum(s for _, s in files)) or 1.0
            done = 0
            log("بنزّل موديل الكلام «%s» من %s (مرة واحدة بس)…" % (name, host.split("//")[1]))
            for fname, size in files:
                dst = os.path.join(d, fname)
                if os.path.isfile(dst) and (not size or os.path.getsize(dst) == size):
                    done += size
                    continue
                part = dst + ".part"
                have = os.path.getsize(part) if os.path.isfile(part) else 0
                hdr = {"Range": "bytes=%d-" % have} if have else {}
                url = "%s/%s/resolve/main/%s" % (host, repo, fname)
                with _open(url, hdr) as r:
                    mode = "ab" if (have and r.status == 206) else "wb"
                    if mode == "wb":
                        have = 0
                    if not size:
                        size = have + int(r.headers.get("Content-Length") or 0)
                        total = max(total, done + size)
                    with open(part, mode) as fh:
                        got = have
                        last = 0
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
                                progress((done + got) / total,
                                         "بنزّل الموديل… %d / %d MB" % ((done + got) >> 20, int(total) >> 20))
                if size and os.path.getsize(part) != size:
                    raise RuntimeError("التحميل اتقطع (%s)" % fname)
                os.replace(part, dst)
                done += size
            for need in ("config.json", "model.bin", "tokenizer.json"):
                if not os.path.isfile(os.path.join(d, need)):
                    raise RuntimeError("ملف ناقص في الموديل: %s" % need)
            with open(os.path.join(d, ".complete"), "w") as fh:
                fh.write(repo)
            log("الموديل اتنزّل ✔")
            return d
        except engine.Cancelled:
            raise
        except Exception as ex:  # try the mirror
            err = ex
            log("التحميل من %s فشل: %s" % (host, ex))
    raise RuntimeError("مش قادر أنزّل موديل الكلام — اتأكد من النت وجرّب تاني.\n%s" % err)


def _ascii_path(p):
    try:
        p.encode("ascii")
        return p
    except UnicodeEncodeError:
        pass
    if engine.IS_WIN:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(1024)
            if ctypes.windll.kernel32.GetShortPathNameW(p, buf, 1024):
                buf.value.encode("ascii")
                return buf.value
        except Exception:
            pass
    return None


def load_model(name):
    from faster_whisper import WhisperModel
    d = model_dir(name)
    kw = dict(device="cpu", compute_type="int8", cpu_threads=max(1, os.cpu_count() or 4))
    p = _ascii_path(d)
    if p:
        return WhisperModel(p, **kw)
    # Arabic folder: CTranslate2 can't open it, so hand it the files from memory
    files = {}
    for f in MODEL_FILES:
        fp = os.path.join(d, f)
        if os.path.isfile(fp):
            with open(fp, "rb") as fh:
                files[f] = fh.read()
    return WhisperModel(name, files=files, **kw)


def transcribe(audio, model_name, prompt="", progress=None, cancel=None, log=None):
    """audio: float32 mono 16 kHz. Returns [(word, start, end, prob), ...]."""
    progress = progress or (lambda f, m="": None)
    cancel = cancel or threading.Event()
    model = load_model(model_name)
    total = len(audio) / float(SR)
    segs, _ = model.transcribe(audio, language="ar", task="transcribe", beam_size=5,
                               word_timestamps=True, vad_filter=False,
                               condition_on_previous_text=False,
                               hallucination_silence_threshold=2.0,
                               initial_prompt=prompt or None)
    words = []
    for seg in segs:
        if cancel.is_set():
            raise engine.Cancelled()
        for w in seg.words or []:
            if w.word.strip():
                words.append((w.word.strip(), float(w.start), float(w.end), float(w.probability)))
        progress(min(1.0, seg.end / max(total, 1.0)), "بسمع الأغنية… %s / %s" % (fmt(seg.end, False), fmt(total, False)))
        if log:
            log("  %s  %s" % (fmt(seg.start, False), seg.text.strip()))
    return words


def file_key(*paths):
    h = hashlib.sha1()
    for p in paths:
        if p:
            st = os.stat(p)
            h.update(("%s|%d|%d" % (os.path.abspath(p), st.st_size, int(st.st_mtime))).encode("utf-8"))
    return h.hexdigest()[:16]


# ----------------------------------------------------------------------------- video analysis

def video_size(path):
    r = engine.run_ffmpeg(["-i", path])
    err = r.stderr.decode("utf-8", "replace")
    m = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    if not m:
        raise RuntimeError("مش لاقي فيديو في الملف:\n%s" % path)
    w, h = int(m.group(1)), int(m.group(2))
    rot = re.search(r"rotation of (-?\d+(?:\.\d+)?)", err) or re.search(r"rotate\s*:\s*(-?\d+)", err)
    if rot and abs(abs(float(rot.group(1))) - 90) < 1:
        w, h = h, w
    return w, h


def gray_frames(path, w, h, fps, start=None, dur=None, vf_pre=""):
    args = []
    if start is not None:
        args += ["-ss", "%.3f" % start]
    if dur is not None:
        args += ["-t", "%.3f" % dur]
    vf = "%sfps=%s,scale=%d:%d:flags=area,format=gray" % (vf_pre, fps, w, h)
    r = engine.run_ffmpeg(args + ["-i", path, "-an", "-vf", vf, "-f", "rawvideo", "-"])
    if r.returncode != 0:
        raise RuntimeError("ffmpeg مش قادر يقرا الفيديو:\n%s" % r.stderr.decode("utf-8", "replace")[-500:])
    n = len(r.stdout) // (w * h)
    return np.frombuffer(r.stdout[:n * w * h], dtype=np.uint8).reshape(n, h, w)


def find_bars(frames, thr=24, limit=0.3):
    """Letterbox / pillarbox from low-res frames -> (top, bottom, left, right) fractions."""
    if not len(frames):
        return 0.0, 0.0, 0.0, 0.0
    rows = np.percentile(frames.mean(axis=2), 95, axis=0)
    cols = np.percentile(frames.mean(axis=1), 95, axis=0)

    def edge(prof):
        k = 0
        while k < len(prof) * limit and prof[k] < thr:
            k += 1
        return k / float(len(prof))
    return edge(rows), edge(rows[::-1]), edge(cols), edge(cols[::-1])


class VideoInfo:
    def __init__(self, path, log=None):
        self.path = path
        self.dur, has_v, self.has_audio = engine.probe(path)
        if not has_v:
            raise RuntimeError("الملف ده مفيهوش فيديو:\n%s" % path)
        self.w, self.h = video_size(path)
        self.fps = 2.0 if self.dur <= 900 else 0.5
        fr = gray_frames(path, 160, 90, self.fps)
        t, b, l, r = find_bars(fr)
        # active picture in source pixels (even numbers for the encoder)
        self.ax = int(self.w * l) // 2 * 2
        self.ay = int(self.h * t) // 2 * 2
        self.aw = (int(self.w * (1 - l - r)) // 2 * 2) or self.w
        self.ah = (int(self.h * (1 - t - b)) // 2 * 2) or self.h
        y0, y1 = int(90 * t), max(int(90 * (1 - b)), int(90 * t) + 1)
        x0, x1 = int(160 * l), max(int(160 * (1 - r)), int(160 * l) + 1)
        act = fr[:, y0:y1, x0:x1].astype(np.float32)
        self.bright = act.mean(axis=(1, 2)) if len(act) else np.zeros(0)
        diff = np.abs(np.diff(act, axis=0)).mean(axis=(1, 2)) if len(act) > 1 else np.zeros(0)
        self.motion = np.concatenate([[0.0], diff]) if len(act) else np.zeros(0)
        med = float(np.median(diff)) if len(diff) else 0.0
        self.cuts = np.where(self.motion > max(25.0, 4 * med))[0] / self.fps
        valid = self.bright >= 20  # black frames
        n = len(valid)
        edge = max(1, int(n * 0.05))
        for sl in (slice(0, edge), slice(n - edge, n)):  # title cards / credits
            if n and self.bright[sl].mean() < 50:
                valid[sl] = False
        self.valid = valid
        if log:
            log("الفيديو: %s — %dx%d، الصورة الفعلية %dx%d%s" % (
                fmt(self.dur, False), self.w, self.h, self.aw, self.ah,
                " (فيه شرايط سودا هتتشال)" if (self.aw, self.ah) != (self.w, self.h) else ""))


def pick_window(info, length, used, margin=1.0):
    """Start time of the liveliest valid window of `length` s that doesn't reuse footage."""
    n = len(info.valid)
    k = max(1, int(round(length * info.fps)))
    if n <= k:
        return 0.0
    v = np.concatenate([[0], np.cumsum(info.valid.astype(np.float32))])
    mo = np.concatenate([[0], np.cumsum(np.minimum(info.motion, 40.0))])
    starts = np.arange(0, n - k + 1)
    vf = (v[starts + k] - v[starts]) / k
    act = (mo[starts + k] - mo[starts]) / k
    score = (act + 2.0) * vf ** 4
    cut_set = set(int(round(c * info.fps)) for c in info.cuts)
    score = score * np.array([1.08 if s in cut_set else 1.0 for s in starts])
    t0 = starts / info.fps
    overlap = np.zeros(len(starts))
    for a, b in used:
        lo = np.maximum(t0, a - margin)
        hi = np.minimum(t0 + length, b + margin)
        overlap += np.maximum(hi - lo, 0)
    free = overlap <= 0
    if free.any():
        best = int(np.argmax(np.where(free, score, -1)))
    else:
        best = int(np.argmax(score - overlap * 1000.0))
    return float(t0[best])


# ----------------------------------------------------------------------------- face crop

_cascade = None


def cascade():
    global _cascade
    if _cascade is None:
        import cv2
        p = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        with open(p, "rb") as fh:  # bytes: the package may sit in an Arabic folder
            xml = fh.read().decode("utf-8")
        fs = cv2.FileStorage(xml, cv2.FILE_STORAGE_READ | cv2.FILE_STORAGE_MEMORY)
        c = cv2.CascadeClassifier()
        if not c.read(fs.getFirstTopLevelNode()):
            raise RuntimeError("Haar cascade didn't load")
        _cascade = c
    return _cascade


def crop_geometry(aw, ah):
    """9:16 window inside the active picture -> (cw, ch, horizontal_follow)."""
    if aw * 16 > ah * 9:
        cw = int(ah * 9 / 16) // 2 * 2
        return cw, ah // 2 * 2, True
    ch = min(ah, int(aw * 16 / 9)) // 2 * 2
    return aw // 2 * 2, ch, False


def crop_x(cx, aw, cw):
    """Left edge of the crop that centres normalized x `cx`, kept inside the picture."""
    return float(min(max(cx * aw - cw / 2.0, 0.0), aw - cw))


def smooth_track(cxs, fps, default=0.5, window=1.6):
    """Fill gaps (hold / interpolate), drop outliers (median 5), then moving average."""
    n = len(cxs)
    if n == 0:
        return np.zeros(0)
    known = [i for i, c in enumerate(cxs) if c is not None]
    if not known:
        return np.full(n, default)
    xs = np.interp(np.arange(n), known, [cxs[i] for i in known])
    if n >= 5:
        pad = np.pad(xs, 2, mode="edge")
        xs = np.median(np.stack([pad[i:i + n] for i in range(5)]), axis=0)
    k = max(1, int(round(window * fps)))
    if k > 1:
        for _ in range(2):
            pad = np.pad(xs, (k // 2, k - 1 - k // 2), mode="edge")
            xs = np.convolve(pad, np.ones(k) / k, mode="valid")
    return xs


def face_centres(frames, prev=None):
    """Normalized x of the main face per frame (None when there isn't one)."""
    cc = cascade()
    out = []
    for img in frames:
        h, w = img.shape
        faces = cc.detectMultiScale(img, scaleFactor=1.1, minNeighbors=5,
                                    minSize=(max(24, int(h * 0.08)),) * 2)
        if len(faces) == 0:
            out.append(None)
            continue
        best, bs = None, -1e9
        for x, y, fw, fh in faces:
            cx = (x + fw / 2.0) / w
            s = fw * fh / float(w * h) - (abs(cx - prev) * 0.08 if prev is not None else 0)
            if s > bs:
                best, bs = cx, s
        prev = best
        out.append(best)
    return out


def x_expr(times, xs, step=0.5):
    """ffmpeg expression for a piecewise-linear crop x(t) (keyframes every `step`)."""
    if len(xs) == 0:
        return "0"
    T = float(times[-1]) if len(times) else 0.0
    keys = np.arange(0.0, T + step, step)
    kx = np.interp(keys, times, xs)
    kx = np.round(kx).astype(int)
    if len(keys) == 1 or np.all(kx == kx[0]):
        return str(int(kx[0]))
    expr = str(int(kx[-1]))
    for i in range(len(keys) - 2, -1, -1):
        a, b = int(kx[i]), int(kx[i + 1])
        seg = str(a) if a == b else "%d+%d*(t-%.2f)/%.2f" % (a, b - a, keys[i], step)
        expr = "if(lt(t\\,%.2f)\\,%s\\,%s)" % (keys[i + 1], seg, expr)
    return expr


def face_follow(info, start, dur, log=None):
    """Crop filter (without scale) for the window [start, start+dur]."""
    cw, ch, horiz = crop_geometry(info.aw, info.ah)
    y = info.ay + (info.ah - ch) // 2 // 2 * 2
    if not horiz or info.aw - cw < 8:
        return "crop=%d:%d:%d:%d" % (cw, ch, info.ax + (info.aw - cw) // 2 // 2 * 2, y), 0
    fps = 3
    ow = 480
    oh = max(2, int(ow * info.ah / float(info.aw)) // 2 * 2)
    pre = "crop=%d:%d:%d:%d," % (info.aw, info.ah, info.ax, info.ay)
    try:
        frames = gray_frames(info.path, ow, oh, fps, start, dur, pre)
        cxs = face_centres(frames)
    except Exception as ex:
        if log:
            log("  تتبع الوش مشتغلش (%s) — هقص من النص" % ex)
        cxs = []
    found = sum(1 for c in cxs if c is not None)
    if found < max(2, 0.15 * len(cxs)):  # no face, or a few false hits
        return "crop=%d:%d:%d:%d" % (cw, ch, info.ax + (info.aw - cw) // 2 // 2 * 2, y), 0
    sm = smooth_track(cxs, fps)
    times = np.arange(len(sm)) / float(fps)
    xs = np.array([info.ax + crop_x(c, info.aw, cw) for c in sm])
    return "crop=w=%d:h=%d:x=%s:y=%d" % (cw, ch, x_expr(times, xs), y), found / float(len(cxs))


def make_clip(info, start, dur, dst, log=None):
    crop, face_share = face_follow(info, start, dur, log)
    vf = ("%s,scale=%d:%d:flags=lanczos,setsar=1,fps=%d,tpad=stop_mode=clone:stop_duration=2,format=yuv420p"
          % (crop, engine.W, engine.H, engine.FPS))
    r = engine.run_ffmpeg(["-y", "-ss", "%.3f" % start, "-t", "%.3f" % dur, "-i", info.path, "-an",
                           "-vf", vf, "-t", "%.3f" % dur, "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", dst])
    if r.returncode != 0:
        raise RuntimeError("ffmpeg وقع في قص الفيديو:\n%s" % r.stderr.decode("utf-8", "replace")[-700:])
    return face_share


# ----------------------------------------------------------------------------- project

class Project:
    """State of one auto run: inputs, timed lines, sections. Used by the GUI and selftest."""

    def __init__(self, song_path, video_path, lyrics_path, stem_path="", song="",
                 model="small", bg_only=False, lyric_font="ArefRuqaa-Bold.ttf",
                 hook_font="Alexandria.ttf", out_root=None, progress=None, log=None,
                 cancel=None, asr=None):
        self.song_path, self.video_path, self.lyrics_path = song_path, video_path, lyrics_path
        self.stem_path = stem_path or ""
        self.song = (song or "").strip() or os.path.splitext(os.path.basename(song_path))[0]
        self.model, self.bg_only = model, bg_only
        self.lyric_font, self.hook_font = lyric_font, hook_font
        self.out_root = out_root or engine.OUTPUT_DIR
        self.progress = progress or (lambda f, m="": None)
        self.log = log or (lambda m: print(m, flush=True))
        self.cancel = cancel or threading.Event()
        self.asr = asr  # optional callable(audio_path, texts) -> words (tests)
        self.lines, self.sections = [], []
        self.total = 0.0
        self.offset = None

    def check_inputs(self):
        for p, label in ((self.song_path, "الأغنية"), (self.video_path, "الفيديو"),
                         (self.lyrics_path, "ملف الكلمات")):
            if not p or not os.path.isfile(p):
                raise RuntimeError("اختار %s الأول" % label)
        if self.stem_path and not os.path.isfile(self.stem_path):
            raise RuntimeError("ملف الفوكال ستيم مش موجود:\n%s" % self.stem_path)
        if not engine.features.check("raqm"):
            raise RuntimeError("RAQM مش شغال — الحروف العربي هتطلع مقطّعة.")

    @property
    def timed_path(self):
        return os.path.join(os.path.dirname(self.lyrics_path), "%s - timed.txt" % engine.safe_name(self.song))

    @property
    def out_dir(self):
        return os.path.join(self.out_root, engine.safe_name(self.song))

    def stage(self, a, b):
        return lambda f, m="": self.progress(a + (b - a) * max(0.0, min(1.0, f)), m)

    # -- analysis ---------------------------------------------------------------
    def analyze(self, reuse_timed=False):
        self.check_inputs()
        texts = read_plain_lyrics(self.lyrics_path)
        if not texts:
            raise RuntimeError("ملف الكلمات فاضي")
        self.total, _, has_a = engine.probe(self.song_path)
        if not has_a:
            raise RuntimeError("ملف الأغنية مفيهوش صوت")
        self.log("الأغنية: %s — الكلمات: %d سطر" % (fmt(self.total, False), len(texts)))
        lines = load_timed(self.timed_path, texts) if reuse_timed and os.path.isfile(self.timed_path) else None
        if lines:
            self.log("استخدمت التوقيت المتسجّل: %s" % os.path.basename(self.timed_path))
        else:
            words = self.recognize(texts)
            self.progress(0.8, "بظبط الكلمات على الصوت…")
            lines = align(texts, words, self.total)
            low = sum(1 for l in lines if l.conf < 0.4)
            self.log("التوقيت: %d سطر — %d سطر محتاج مراجعة (⚠)" % (len(lines), low))
        self.lines = lines
        save_timed(lines, self.timed_path, self.song)
        self.log("اتحفظ: %s" % self.timed_path)
        if self.cancel.is_set():
            raise engine.Cancelled()
        self.progress(0.85, "بقسّم الأغنية…")
        self.sections = make_sections(lines, self.total)
        self.log("المقاطع: " + "، ".join(s["name"] for s in self.sections))
        self.progress(0.88, "بحلل الفيديو…")
        self.analyze_video()
        self.progress(1.0, "التحليل خلص ✔")
        return self.sections

    def recognize(self, texts):
        src = self.stem_path or self.song_path
        if self.asr:
            return self.asr(src, texts)
        key = "%s-%s-%s" % (file_key(src), self.model, "stem" if self.stem_path else "mix")
        cache = os.path.join(CACHE_DIR, "asr-%s.json" % key)
        if os.path.isfile(cache):
            try:
                with open(cache, encoding="utf-8") as fh:
                    words = json.load(fh)
                self.log("استخدمت نتيجة السمع المتسجّلة من قبل")
                return words
            except (OSError, ValueError):
                pass
        if not model_ready(self.model):
            ensure_model(self.model, self.stage(0.0, 0.25), self.log, self.cancel)
        self.progress(0.25, "بحمّل الموديل…")
        self.log("بسمع %s بموديل %s (ممكن ياخد كام دقيقة)…" % ("الفوكال ستيم" if self.stem_path else "الأغنية", self.model))
        audio = decode(src)
        # no lyric prompt on purpose: Whisper tends to "hear" a prompt over the intro music
        words = transcribe(audio, self.model, "", self.stage(0.27, 0.8), self.cancel, self.log)
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(words, fh, ensure_ascii=False)
        return words

    def analyze_video(self):
        self.vinfo = VideoInfo(self.video_path, self.log)
        if self.bg_only:
            self.log("الفيديو: خلفية بس (loop + قص من النص)")
            return
        self.offset = None
        if self.vinfo.has_audio:
            res = find_offset(self.song_path, self.video_path)
            if res:
                self.offset = res[0]
                self.log("الفيديو ماشي مع الأغنية (فرق %+.2f ث، تطابق %.2f) — كل ريل هياخد نفس اللحظة من الكليب" % res)
        if self.offset is None and abs(self.vinfo.dur - self.total) <= max(2.0, 0.03 * self.total):
            self.offset = 0.0
            self.log("طول الفيديو قد الأغنية — هعتبره متزامن")
        if self.offset is None:
            self.log("الفيديو مش متزامن — هختار أحلى لقطات من غير تكرار")

    # -- render -----------------------------------------------------------------
    def reel_lines(self, sec, s, e):
        """Lines of a section for engine.Job: (start, end, text, word_starts); a short
        breath holds the line until the next one."""
        idx = list(range(sec["i0"], sec["i1"] + 1))
        out = []
        for k, i in enumerate(idx):
            ln = self.lines[i]
            ts = ln.start - 0.15
            te = ln.end + 0.6
            if k + 1 < len(idx):
                nxt = self.lines[idx[k + 1]].start - 0.15
                te = nxt - 0.05 if nxt - ln.end < GAP_SPLIT else min(te, nxt - 0.05)
            ws = [w - 0.1 for w in ln.words] if ln.words else None
            out.append((ts, te, ln.text, ws))
        return out

    def render(self, sections=None):
        secs = [s for s in (sections or self.sections) if s.get("checked")]
        if not secs:
            raise RuntimeError("اختار مقطع واحد على الأقل")
        os.makedirs(self.out_dir, exist_ok=True)
        save_timed(self.lines, self.timed_path, self.song)
        results, used = [], []
        n = len(secs)
        with tempfile.TemporaryDirectory(prefix="autoreel_") as tmp:
            for k, sec in enumerate(secs):
                if self.cancel.is_set():
                    raise engine.Cancelled()
                a, b = k / float(n), (k + 1) / float(n)
                prog = self.stage(a, b)
                closing = sec.get("closing", engine.DEFAULT_CLOSING)
                s, e = reel_span(self.lines, sec["i0"], sec["i1"], self.total, closing)
                dur = e - s
                self.log("— %s  (%s → %s)" % (sec["name"], fmt(s, False), fmt(e, False)))
                prog(0.0, "ريل %d من %d: %s — بجهز الفيديو…" % (k + 1, n, sec["name"]))
                bg = self.video_path
                vi = self.vinfo
                bg_crop = None
                if (vi.aw, vi.ah) != (vi.w, vi.h):
                    bg_crop = "crop=%d:%d:%d:%d" % (vi.aw, vi.ah, vi.ax, vi.ay)
                if not self.bg_only:
                    vs = None
                    if self.offset is not None:
                        vs = s + self.offset
                        if vs < 0 or vs + dur > vi.dur + 0.5:
                            vs = None
                    if vs is None:
                        if vi.dur < dur + 1.0:
                            self.log("  الفيديو أقصر من الريل — هيتعاد loop")
                        else:
                            vs = pick_window(vi, dur + 0.5, used)
                    if vs is not None:
                        vs = max(0.0, min(vs, vi.dur - dur - 0.05)) if vi.dur > dur else 0.0
                        used.append((vs, vs + dur))
                        bg, bg_crop = os.path.join(tmp, "clip%d.mp4" % k), None
                        share = make_clip(vi, vs, dur + 0.6, bg, self.log)
                        self.log("  فيديو من %s%s" % (fmt(vs, False),
                                 "، الوش في %d%% من اللقطات" % (share * 100) if share else "، قص من النص"))
                base = "%s - %s (%s-%s)" % (engine.safe_name(self.song), sec["name"], fmt_file(s), fmt_file(e))
                hook = sec.get("hook", "").strip()
                job = engine.Job(bg, self.song_path, None, song=self.song, hook=hook, closing=closing,
                                 lyric_font=self.lyric_font, hook_font=self.hook_font,
                                 start=s, end=e, out_dir=self.out_dir,
                                 progress=lambda f, m="": prog(0.1 + 0.9 * f, "ريل %d من %d: %s — %s" % (k + 1, n, sec["name"], m)),
                                 log=lambda m: None, cancel=self.cancel,
                                 lines=self.reel_lines(sec, s, e), out_name=base,
                                 thumb_text=hook or self.lines[sec["i0"]].text, bg_crop=bg_crop)
                mp4, jpg = job.run()
                results.append(dict(name=sec["name"], start=s, end=e, hook=hook, mp4=mp4, jpg=jpg,
                                    first=self.lines[sec["i0"]].text))
                self.log("  ✔ %s" % os.path.basename(mp4))
        self.write_summary(results)
        self.progress(1.0, "خلص ✔ — %d ريل" % len(results))
        return results

    def write_summary(self, results):
        p = os.path.join(self.out_dir, "reels.txt")
        out = ["%s — %d ريل" % (self.song, len(results)), ""]
        for r in results:
            out.append("%s\t%s → %s (%.0f ث)" % (r["name"], fmt(r["start"]), fmt(r["end"]), r["end"] - r["start"]))
            out.append("  أول سطر: %s" % r["first"])
            out.append("  الهوك: %s" % (r["hook"] or "—"))
            out.append("  %s" % os.path.basename(r["mp4"]))
            out.append("")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out))
        return p
