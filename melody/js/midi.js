// Standard MIDI file writer. Quarter tones -> pitch bend (range ±2 semitones, set by RPN 0).
// Notes that would overlap with a different bend go to their own channel.
const vlq = n => { const b = [n & 0x7f]; while ((n >>= 7)) b.unshift((n & 0x7f) | 0x80); return b; };
const u32 = n => [(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255];
const PPQ = 480;

export function toMidi(notes, { bpm = 90, name = 'Melody', program = 24 } = {}) {
  const tick = s => Math.round(s * bpm / 60 * PPQ);
  const ev = []; // {t, o (order), b: bytes}
  const busy = []; // per channel: end tick
  const chans = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 12, 13, 14, 15];
  const used = new Set();
  for (const n of notes) {
    const q = Math.round(n.q * 2) / 2, key = Math.floor(q), cents = (q - key) * 100;
    const t0 = tick(n.s), t1 = Math.max(t0 + 1, tick(n.s + n.d));
    const ch = chans.find(c => !(busy[c] > t0)) ?? 0;
    busy[ch] = t1; used.add(ch);
    const bend = Math.max(0, Math.min(16383, 8192 + Math.round(cents / 200 * 8192)));
    ev.push({ t: t0, o: 1, b: [0xe0 | ch, bend & 0x7f, bend >> 7] });
    ev.push({ t: t0, o: 2, b: [0x90 | ch, key, 90] });
    ev.push({ t: t1, o: 0, b: [0x80 | ch, key, 0] });
  }
  const head = [];
  const mpq = Math.round(60e6 / bpm);
  head.push([0xff, 0x51, 3, (mpq >> 16) & 255, (mpq >> 8) & 255, mpq & 255]);
  const nm = Array.from(new TextEncoder().encode(name)).slice(0, 120);
  head.push([0xff, 0x03, ...vlq(nm.length), ...nm]);
  for (const c of used) {
    head.push([0xc0 | c, program]);
    head.push([0xb0 | c, 101, 0], [0xb0 | c, 100, 0], [0xb0 | c, 6, 2], [0xb0 | c, 38, 0], [0xb0 | c, 101, 127], [0xb0 | c, 100, 127]);
  }
  ev.sort((a, b) => a.t - b.t || a.o - b.o);
  const trk = [];
  for (const h of head) trk.push(0, ...h);
  let last = 0;
  for (const e of ev) { trk.push(...vlq(e.t - last), ...e.b); last = e.t; }
  trk.push(0, 0xff, 0x2f, 0);
  return new Uint8Array([0x4d, 0x54, 0x68, 0x64, ...u32(6), 0, 0, 0, 1, PPQ >> 8, PPQ & 255,
    0x4d, 0x54, 0x72, 0x6b, ...u32(trk.length), ...trk]);
}
