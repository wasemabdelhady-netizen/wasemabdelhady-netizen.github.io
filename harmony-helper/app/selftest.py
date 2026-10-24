"""Self-test against a synthetic song with known tempo, key, chords, melody and
a quarter-tone section (see testsong.py).

    python selftest.py <work_dir>
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # embedded Python skips it
import wave

import numpy as np

import engine
import export
import midi
import player
import testsong


def ok(cond, msg):
    print(("OK   " if cond else "FAIL ") + msg, flush=True)
    if not cond:
        sys.exit(1)


def main():
    d = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "selftest-عربي")
    os.makedirs(d, exist_ok=True)
    mix, voc, chords, notes = testsong.synth()
    song_p = os.path.join(d, "أغنية تجربة.wav")
    stem_p = os.path.join(d, "أغنية تجربة - vocal.wav")
    testsong.write_wav(song_p, mix)
    testsong.write_wav(stem_p, voc)
    mp3_p = os.path.join(d, "أغنية تجربة.mp3")
    ok(engine.run_ffmpeg(["-y", "-i", song_p, "-b:a", "256k", mp3_p]).returncode == 0, "sample mp3")

    s = engine.Song(song_p, stem_p, progress=lambda m: print("     " + m))
    ok(abs(s.bpm - testsong.BPM) < 0.05, "BPM %.2f (true %.0f)" % (s.bpm, testsong.BPM))
    ok((s.tonic, s.scale) == (2, "minor"), "key %s (true D minor)" % engine.key_name(s.tonic, s.scale))
    ok(abs(s.first_downbeat - testsong.LEAD) < 0.06, "bar 1 at %.3f s (true %.2f)" % (s.first_downbeat, testsong.LEAD))
    good = sum(s.beat_chords[int(np.argmin(np.abs(s.beats - t)))] == lab for t, lab in chords)
    ok(good / len(chords) >= 0.9, "chords right on %d/%d beats (%.0f%%)" % (good, len(chords), 100 * good / len(chords)))
    names = [c["name"] for c in s.chords]
    ok("Bb" in names and "A" in names and "Gm" in names, "chord names use flats in D minor and keep the major V (A): %s" % names[:9])
    hits, onset = 0, []
    for nt in notes:
        allow = {nt["midi"]} if nt["cents"] == 0 else {nt["midi"], nt["midi"] - 1}
        c = [x for x in s.notes if abs(x["start"] - nt["start"]) < 0.1 and x["midi"] in allow]
        if c:
            hits += 1
            onset.append(abs(c[0]["start"] - nt["start"]))
    ok(hits / len(notes) >= 0.95, "melody: %d/%d notes right (pitch + onset within 100 ms)" % (hits, len(notes)))
    ok(len(s.notes) <= len(notes) * 1.08, "melody: no extra notes (%d detected)" % len(s.notes))
    ok(np.median(onset) < 0.04, "melody onsets: median error %.0f ms" % (1000 * np.median(onset)))
    ok(abs(s.tuning) < 10, "tuning offset %+.1f cents" % s.tuning)
    qt_true = [nt for nt in notes if nt["cents"]]
    ok(len(s.quarter_sections) == 1, "one quarter-tone section found")
    q = s.quarter_sections[0]
    ok(all(q["start"] - 0.2 <= nt["start"] <= q["end"] for nt in qt_true) and q["bar_from"] == qt_true[0]["bar"],
       "quarter-tone section at bar %d (%s) — true bar %d" % (q["bar_from"], engine.fmt_time(q["start"]), qt_true[0]["bar"]))
    ok("E half-flat" in q["pitches"][0], "named %s" % q["pitches"][0])

    # harmony theory in D minor (D E F G A Bb C)
    probe = [{"start": 0, "end": 1, "midi": m, "cents": 0} for m in (62, 64, 69, 70, 61)]
    h = lambda st, oc=0: [n["midi"] for n in engine.harmony_line(probe, 2, "minor", st, oc)]
    ok(h(2) == [65, 67, 72, 74, 64], "3rd above: D→F, E→G, A→C, Bb→D, C#→E  %s" % h(2))
    ok(h(-2) == [58, 60, 65, 67, 57], "3rd below: D→Bb, E→C, A→F, Bb→G, C#→A  %s" % h(-2))
    ok(h(-5) == [53, 55, 60, 62, 52], "6th below: D→F, E→G, A→C, Bb→D  %s" % h(-5))
    ok(h(None, 12) == [74, 76, 81, 82, 73] and h(None, -12) == [50, 52, 57, 58, 49], "octaves ±12")

    # export
    lines = ["third_up", "third_down", "sixth_down", "octave_up"]
    folder, files = export.export_all(s, lines, transpose=-2)
    ok(os.path.basename(folder) == "أغنية تجربة - HarmonyHelper" and os.path.dirname(folder) == d,
       "export folder named after the song, next to it")
    ok(len(files) == 9, "%d files: chords, melody, 4 harmony, all tracks, chart, report" % len(files))
    ppq, tempo, trs = midi.read(os.path.join(folder, "أغنية تجربة - melody.mid"))
    ok(tempo == int(round(60e6 / s.bpm)) and ppq == 480, "melody.mid tempo %d µs/beat (= %.1f BPM)" % (tempo, 60e6 / tempo))
    mel = trs[1]["notes"]
    ok(len(mel) == len(s.notes), "melody.mid has %d notes" % len(mel))
    first = s.notes[0]
    ok(abs(mel[0][0] - first["start"] * s.bpm / 60 * 480) <= 1 and mel[0][2] == first["midi"],
       "melody.mid aligned to the tempo grid (tick %d = %.3f s)" % (mel[0][0], first["start"]))
    _, _, trs = midi.read(os.path.join(folder, "أغنية تجربة - harmony 3rd above -2.mid"))
    expect = [n["midi"] - 2 for n in s.harmony("third_up")]
    ok([n[2] for n in trs[1]["notes"]] == expect, "3rd-above file = harmony transposed −2")
    _, _, trs = midi.read(os.path.join(folder, "أغنية تجربة - all tracks.mid"))
    ok([t["name"] for t in trs][1:3] == ["Chords", "Melody (vocal)"] and len(trs) == 7, "all tracks: %s" % [t["name"] for t in trs])
    ch = trs[1]["notes"]
    ok(len(ch) == 4 * sum(1 for c in s.chords if c["root"] is not None), "chords track: block chords (4 notes each)")
    chart = open(os.path.join(folder, "أغنية تجربة - chords.txt"), encoding="utf-8-sig").read()
    ok("بار   1" in chart and "Bb" in chart and "~ربع تون" in chart, "chord chart with bar numbers and the quarter-tone mark")
    rep = open([f for f in files if f.endswith(".html")][0], encoding="utf-8").read()
    ok('id="maqam-warning"' in rep and "ربع تون" in rep and 'id="chart"' in rep and "1.1.1" in rep, "report: maqam warning, chart, DAW steps")

    # preview
    wav = export.render_preview(s, "third_up", start=10, length=8)
    ok(os.path.getsize(wav) > 8 * 44100 * 4 * 0.9, "preview WAV (song + harmony line)")
    os.remove(wav)

    # volumes: «علو الأغنية» / «علو الخط»
    bed, line, limit = export.preview_parts(s, "third_up", start=10, length=8)
    pcm = lambda b: np.frombuffer(b, "<i2").reshape(-1, 2) / 32767.0
    full = pcm(player.LiveMix(bed, line, limit).render())
    ok(0.5 < np.abs(full).max() <= 0.951, "100%% / 100%%: peak %.3f (≤ 0.95)" % np.abs(full).max())
    alone = pcm(player.LiveMix(bed, line, limit, song_vol=0.0).render())
    quiet = np.abs(line) < 1e-9
    ok(quiet.any() and np.abs(alone[quiet]).max() < 1e-4, "song 0%: silent where the line is silent — the line alone")
    ok(np.abs(alone[:, 1] - line * limit).max() < 1e-3 and np.abs(alone[:, 0] - 0.8 * line * limit).max() < 1e-3,
       "song 0%: output = the line")
    song_only = pcm(player.LiveMix(bed, line, limit, line_vol=0.0).render())
    ok(np.abs(song_only - bed * limit).max() < 1e-3, "line 0%: output = the song")
    half = pcm(player.LiveMix(bed, line, limit, song_vol=0.5, line_vol=0.25).render())
    ok(np.abs(half[:, 1] - (0.5 * bed[:, 1] + 0.25 * line) * limit).max() < 1e-3, "50% / 25% mix")
    # live: change the song volume in the middle of playback
    m = player.LiveMix(bed, line, limit)
    first = pcm(b"".join(m.next_chunk() for _ in range(20)))
    m.song_vol = 0.0
    ramp = pcm(m.next_chunk())
    after = pcm(b"".join(m.next_chunk() for _ in range(20)))
    n0, n1 = 20 * player.CHUNK, 21 * player.CHUNK
    ok(np.abs(first - full[:n0]).max() < 1e-3, "live: plays at 100% before the change")
    ok(np.abs(after[:, 1] - line[n1:n1 + len(after)] * limit).max() < 1e-3, "live: song gone one chunk (~46 ms) after the change")
    step = np.abs(np.diff(ramp[:, 1])).max()
    ok(step < 0.1, "live: the change ramps over the chunk (largest step %.4f) — no click" % step)
    wav = export.render_preview(s, "third_up", start=10, length=8, song_vol=0.0, line_vol=1.0)
    with open(wav, "rb") as fh:
        wr = wave.open(fh)
        x = pcm(wr.readframes(wr.getnframes()))
    os.remove(wav)
    ok(len(x) == len(line) and np.abs(x[quiet]).max() < 1e-4, "preview WAV at song 0%: the line alone")
    if sys.platform == "win32":
        import ctypes
        ok(ctypes.sizeof(player.WAVEFORMATEX) == 18 and ctypes.sizeof(player.WAVEHDR) == 48, "waveOut structs (18 / 48 bytes)")
        try:
            pl = player.Player()
            pl.play(bed[:44100], line[:44100], limit)
            backend = pl.backend
            pl.set_volume(0.0, 1.0)
            import time
            time.sleep(1.3)
            pl.stop()
            print("     playback backend: %s" % backend)
        except Exception as ex:  # no audio device on the CI machine is fine
            print("     playback not testable here: %s" % ex)

    # without a stem, and an MP3
    s2 = engine.Song(mp3_p, None)
    ok(abs(s2.bpm - testsong.BPM) < 1.0 and (s2.tonic, s2.scale) == (2, "minor"), "MP3 without stem: BPM %.1f, %s" % (
        s2.bpm, engine.key_name(s2.tonic, s2.scale)))
    good = sum(s2.beat_chords[int(np.argmin(np.abs(s2.beats - t)))] == lab for t, lab in chords)
    ok(good / len(chords) >= 0.85, "MP3 without stem: chords %.0f%%" % (100 * good / len(chords)))
    ok(not s2.melody_from_stem, "without stem: melody marked as approximate")
    print("ALL GOOD", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
