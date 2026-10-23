"""Tiny Standard MIDI File (type 1) writer and reader — no dependencies."""
import struct

PPQ = 480


def _vlq(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _track(events):
    """events: (tick, order, bytes). order sorts note-offs before note-ons on the same tick."""
    data, last = b"", 0
    for tick, _, ev in sorted(events, key=lambda e: (e[0], e[1])):
        data += _vlq(tick - last) + ev
        last = tick
    data += _vlq(0) + b"\xff\x2f\x00"
    return b"MTrk" + struct.pack(">I", len(data)) + data


def _meta_name(name):
    b = name.encode("utf-8")
    return b"\xff\x03" + _vlq(len(b)) + b


def write(path, bpm, tracks, key=None):
    """tracks: list of dicts {name, channel, program, notes: [(start_s, end_s, midi, velocity)]}.
    Times in seconds are placed on a constant-tempo grid at `bpm`, so the file lines up with the
    audio when both start at bar 1 of a project at that tempo."""
    tempo = int(round(60_000_000 / bpm))
    t0 = [(0, 0, _meta_name("Tempo")), (0, 0, b"\xff\x51\x03" + tempo.to_bytes(3, "big")),
          (0, 0, b"\xff\x58\x04\x04\x02\x18\x08")]
    if key is not None:  # key signature: sharps/flats count, major/minor
        sf, mi = key
        t0.append((0, 0, b"\xff\x59\x02" + struct.pack("b", sf) + bytes([mi])))
    chunks = [_track(t0)]
    tick = lambda s: max(0, int(round(s * bpm / 60.0 * PPQ)))
    for tr in tracks:
        ch = tr.get("channel", 0) & 0x0F
        ev = [(0, 0, _meta_name(tr["name"])), (0, 1, bytes([0xC0 | ch, tr.get("program", 0) & 0x7F]))]
        for s, e, m, v in tr["notes"]:
            if not 0 <= m <= 127:
                continue
            a, b = tick(s), max(tick(s) + 1, tick(e))
            ev.append((a, 3, bytes([0x90 | ch, m, max(1, min(127, v))])))
            ev.append((b, 2, bytes([0x80 | ch, m, 0])))
        chunks.append(_track(ev))
    head = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), PPQ)
    with open(path, "wb") as f:
        f.write(head + b"".join(chunks))
    return path


def read(path):
    """Returns (ppq, tempo_us, [ {name, notes:[(start_tick, end_tick, midi, vel)]} ])."""
    data = open(path, "rb").read()
    assert data[:4] == b"MThd"
    _, fmt, ntr, ppq = struct.unpack(">IHHH", data[4:14])
    pos, tracks, tempo = 14, [], None
    for _ in range(ntr):
        assert data[pos:pos + 4] == b"MTrk"
        ln = struct.unpack(">I", data[pos + 4:pos + 8])[0]
        p, end, t, run = pos + 8, pos + 8 + ln, 0, None
        tr = {"name": "", "notes": []}
        on = {}
        while p < end:
            d = 0
            while True:
                b = data[p]; p += 1
                d = (d << 7) | (b & 0x7F)
                if b < 0x80:
                    break
            t += d
            st = data[p]
            if st == 0xFF:
                typ, p = data[p + 1], p + 2
                ln2 = 0
                while True:
                    b = data[p]; p += 1
                    ln2 = (ln2 << 7) | (b & 0x7F)
                    if b < 0x80:
                        break
                body = data[p:p + ln2]; p += ln2
                if typ == 0x03:
                    tr["name"] = body.decode("utf-8", "replace")
                elif typ == 0x51:
                    tempo = int.from_bytes(body, "big")
                continue
            if st & 0x80:
                run = st; p += 1
            hi = run & 0xF0
            if hi in (0xC0, 0xD0):
                p += 1
                continue
            a, b = data[p], data[p + 1]; p += 2
            if hi == 0x90 and b > 0:
                on[a] = (t, b)
            elif hi in (0x80, 0x90):
                if a in on:
                    s, v = on.pop(a)
                    tr["notes"].append((s, t, a, v))
        tracks.append(tr)
        pos = end
    return ppq, tempo, tracks
