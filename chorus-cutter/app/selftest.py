"""Self-test: synthesises a 100 BPM song with a known structure (and its vocal
stem), saves it under Arabic file names, then checks BPM, bar grid, the chorus
pick and the exports.

    python selftest.py <work_dir>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import wave

import numpy as np

import engine

SR = 44100
BPM = 100.0
BEAT = 60.0 / BPM
BAR = 4 * BEAT
LEAD_IN = 0.35
# (name, bars)
FORM = [("intro", 4), ("verse", 8), ("chorus", 8), ("verse", 8), ("chorus", 8),
        ("bridge", 4), ("chorus", 8), ("outro", 4)]
NOTE = {n: i for i, n in enumerate(["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"])}
CHORDS = {"verse": ["A", "F", "C", "G"], "chorus": ["F", "G", "E", "A"],
          "intro": ["A", "F", "A", "F"], "outro": ["A", "F", "A", "A"], "bridge": ["D", "E", "D", "E"]}
MINOR = {"A", "E", "D"}
MELODY = {"verse": [0, 2, 3, 2, 0, -2, 0, 3], "chorus": [7, 7, 5, 4, 5, 7, 9, 7]}


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def hz(semi_from_a4):
    return 440.0 * 2 ** (semi_from_a4 / 12.0)


def tone(f, dur, harm=(1, 0.5, 0.3), vib=0.0, att=0.01, rel=0.08):
    t = np.arange(int(dur * SR)) / SR
    ph = 2 * np.pi * f * t + (vib * np.sin(2 * np.pi * 5.5 * t) / 5.5 if vib else 0)
    y = sum(a * np.sin((k + 1) * ph) for k, a in enumerate(harm))
    env = np.minimum(1, t / att) * np.minimum(1, np.maximum(0, (dur - t) / rel))
    return (y * env).astype(np.float32)


def add(buf, at, sig, gain=1.0):
    i = int(at * SR)
    n = min(len(sig), buf.shape[0] - i)
    if n > 0:
        buf[i:i + n] += gain * sig[:n]


def synth():
    rng = np.random.default_rng(7)
    total_bars = sum(b for _, b in FORM)
    n = int((LEAD_IN + total_bars * BAR + 2.0) * SR)
    L = np.zeros(n, np.float32)
    R = np.zeros(n, np.float32)
    V = np.zeros(n, np.float32)
    kick = tone(1, 0.3, harm=(1,))
    t = np.arange(int(0.3 * SR)) / SR
    kick = (np.sin(2 * np.pi * (45 * t + 75 * (1 - np.exp(-t * 30)) / 30)) * np.exp(-t * 9)).astype(np.float32)
    snare = (rng.standard_normal(int(0.18 * SR)) * np.exp(-np.arange(int(0.18 * SR)) / SR * 22)).astype(np.float32)
    hat = (rng.standard_normal(int(0.05 * SR)) * np.exp(-np.arange(int(0.05 * SR)) / SR * 90)).astype(np.float32)
    hat = np.diff(hat, prepend=0).astype(np.float32)
    bar_i = 0
    starts = {}
    for name, bars in FORM:
        starts.setdefault(name, []).append(LEAD_IN + bar_i * BAR)
        big = name == "chorus"
        for b in range(bars):
            t0 = LEAD_IN + (bar_i + b) * BAR
            root = CHORDS[name][b % 4]
            r = NOTE[root] - 9  # semitones from A
            third = 3 if root in MINOR else 4
            chord_g = 0.16 if big else 0.10
            if name in ("intro", "outro"):
                chord_g = 0.07
            for semi in (r, r + third, r + 7):
                add(L, t0, tone(hz(semi - 12) * 1.003, BAR, harm=(1, 0.6, 0.4, 0.2)), chord_g)
                add(R, t0, tone(hz(semi - 12) * 0.997, BAR, harm=(1, 0.6, 0.4, 0.2)), chord_g)
            bass = tone(hz(r - 36), BAR * 0.95, harm=(1, 0.4))
            add(L, t0, bass, 0.30 if big else 0.22)
            add(R, t0, bass, 0.30 if big else 0.22)
            if name not in ("intro", "outro"):
                for q in range(4):
                    tb = t0 + q * BEAT
                    if q in (0, 2) or (big and q == 3):
                        g = 0.9 if q == 0 else 0.6  # strong downbeat
                        add(L, tb, kick, g)
                        add(R, tb, kick, g)
                    if q in (1, 3):
                        add(L, tb, snare, 0.35 if big else 0.22)
                        add(R, tb, snare, 0.35 if big else 0.22)
                    for e in range(2 if big else 1):
                        add(L, tb + e * BEAT / 2, hat, 0.12)
                        add(R, tb + e * BEAT / 2, hat, 0.08)
            if name in MELODY:
                mel = MELODY[name]
                g = 0.30 if big else 0.16
                for q in range(4):
                    step = mel[(b * 4 + q) % len(mel)]
                    voice = tone(hz(r + 12 + step - 12), BEAT * 0.86, harm=(1, 0.7, 0.45, 0.25, 0.1),
                                 vib=6.0, att=0.03, rel=0.1)
                    add(V, t0 + q * BEAT, voice, g)
        bar_i += bars
    L += V
    R += V
    peak = max(np.abs(L).max(), np.abs(R).max())
    g = 0.89 / peak
    return np.stack([L * g, R * g], 1), np.stack([V * g, V * g], 1), starts


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with open(path, "wb") as fh:  # open() so Arabic paths work everywhere
        w = wave.open(fh, "wb")
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
        w.close()


def info(path):
    r = engine.run_ffmpeg(["-i", path])
    return r.stderr.decode("utf-8", "replace")


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    os.makedirs(d, exist_ok=True)
    mix, voc, starts = synth()
    wav_master = os.path.join(d, "أغنية تجربة - master.wav")
    stem = os.path.join(d, "أغنية تجربة - vocals.wav")
    write_wav(wav_master, mix)
    write_wav(stem, voc)
    mp3_master = os.path.join(d, "أغنية تجربة.mp3")
    r = engine.run_ffmpeg(["-y", "-i", wav_master, "-c:a", "libmp3lame", "-b:a", "256k", mp3_master])
    ok(r.returncode == 0, "sample mp3")
    chorus = starts["chorus"]
    print("     chorus starts at", ["%.2f" % c for c in chorus])

    for label, master, st in (("wav+stem", wav_master, stem), ("mp3 only", mp3_master, None)):
        an = engine.Analysis(master, st, log=lambda m: print("     " + m))
        ok(abs(an.bpm - BPM) < 1.5, "%s: BPM %.2f (expected %.0f)" % (label, an.bpm, BPM))
        for c in chorus:
            dist = np.min(np.abs(an.bars - c))
            ok(dist < 0.07, "%s: bar line at chorus %.2f (off by %.3f s)" % (label, c, dist))
        for length in (20, 10):
            cands = an.candidates(length)
            for i, c in enumerate(cands, 1):
                print("     %d. %6.2f → %6.2f  score %5.1f  loud %3.0f vocal %3.0f repeat %3.0f"
                      % (i, c["start"], c["end"], c["score"], c["loud"], c["vocal"], c["repeat"]))
            ok(len(cands) == 5, "%s %ds: 5 candidates" % (label, length))
            top = cands[0]
            in_chorus = any(cs - 0.1 <= top["start"] and top["end"] <= cs + 8 * BAR + 0.1 for cs in chorus)
            ok(in_chorus, "%s %ds: best clip %.2f–%.2f is inside a chorus" % (label, length, top["start"], top["end"]))
            ok(abs(top["length"] - round(length / BAR) * BAR) < 0.1,
               "%s %ds: clip is %d whole bars (%.2f s)" % (label, length, top["bars"], top["length"]))
            if length == 20:
                hits = sum(1 for c in cands[:3] if min(abs(c["start"] - cs) for cs in chorus) < 0.1)
                ok(hits >= 2, "%s: %d of the top 3 start exactly on a chorus" % (label, hits))

    an = engine.Analysis(wav_master, stem, log=lambda m: None)
    top = an.candidates(20)[0]
    out = os.path.join(d, "output")
    song = engine.song_name_from(wav_master)
    ok(song == "أغنية تجربة", "song name from file: %s" % song)
    start = top["start"] + 0.5  # like pressing +0.5s once
    wav, mp3 = engine.export(wav_master, start, top["length"], song, out)
    vwav, vmp3 = engine.export(stem, start, top["length"], song, out, suffix=" - vocal")
    for p in (wav, mp3, vwav, vmp3):
        ok(os.path.isfile(p), "exported " + os.path.basename(p))
    iw = info(wav)
    ok("pcm_s24le" in iw and "48000 Hz" in iw, "WAV is 24-bit 48 kHz")
    im = info(mp3)
    ok("mp3" in im and "48000 Hz" in im and "320 kb/s" in im, "MP3 is 320k 48 kHz")
    x = engine.decode(wav, 48000, 1)[:, 0]
    ok(abs(len(x) / 48000.0 - top["length"]) < 0.02, "WAV length %.3f s" % (len(x) / 48000.0))
    head = np.abs(x[:24]).max()
    mid = np.sqrt(np.mean(x[len(x) // 2 - 4800:len(x) // 2 + 4800] ** 2))
    tail = np.sqrt(np.mean(x[-2400:] ** 2))
    ok(head < 0.05, "fade-in: first 0.5 ms peak %.4f" % head)
    ok(tail < 0.08 * mid, "fade-out: last 50 ms at %.1f%% of the middle" % (100 * tail / mid))
    pv = engine.preview(wav_master, top["start"], top["length"])
    ok(os.path.getsize(pv) > 100000, "preview WAV for playback")
    os.remove(pv)
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
