// Accuracy test for melody/js analysis on synthesised *singing-like* phrases.
// node tools/melody-test.mjs [--hard] [--n=2]
import { analyzeAudio, applyKey } from '../melody/js/analyze.js';
import { MAQAMAT, MAQAM_KEYS, degQ, midi2hz } from '../melody/js/theory.js';

const HARD = process.argv.includes('--hard');
const SEEDS = +(process.argv.find(a => a.startsWith('--n=')) || '--n=2').slice(4);
const SR = 44100;
let seed = 1;
const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
const gauss = () => Math.sqrt(-2 * Math.log(rnd() + 1e-12)) * Math.cos(2 * Math.PI * rnd());

// Contours (degree indices) + rhythms (beats). Shared by all maqamat, so only the intervals tell them apart.
const CONTOURS = [
  [[0, 1, 2, 3, 2, 1, 0], [1, .5, .5, 1, .5, .5, 2]],
  [[3, 2, 1, 2, 1, 0], [1, 1, .5, .5, 1, 2]],
  [[0, 2, 1, 3, 2, 4, 3, 2, 1, 0], [.5, .5, .5, .5, .5, .5, .5, .5, 1, 2]],
  [[4, 3, 2, 3, 1, 2, 0], [1.5, .5, 1, 1, .5, .5, 2]],
  [[0, 1, 2, 1, 2, 3, 4, 3], [.5, .5, 1, .5, .5, 1, .5, 2]],
  [[2, 1, 0, -1, 0, 1, 2, 1, 0], [.5, .5, 1, .5, .5, .5, .5, 1, 2]],
  [[0, 0, 1, 2, 2, 1, 1, 0], [.5, .5, 1, .5, .5, .5, .5, 2]],
  [[5, 4, 3, 2, 3, 2, 1, 0], [1, .5, .5, 1, .5, .5, 1, 2]],
];
// Typical male-voice tonics (MIDI) per maqam
const TONICS = { rast: [48, 55], bayati: [50, 45], saba: [50, 45], hijaz: [50, 55], nahawand: [48, 55], kurd: [50, 45], sikah: [51.5, 46.5], ajam: [46, 48] };

// Formant-shaped harmonic voice ("aa"), vibrato, jitter, glides, intonation error, breath noise.
function sing(ref, bpm) {
  const beat = 60 / bpm, detune = (rnd() * 2 - 1) * (HARD ? .45 : .3), into = HARD ? .2 : .12;
  const vibD = (HARD ? .5 : .3) + rnd() * .2, vibR = 5 + rnd() * 1.2;
  let t = 0.25; const events = [];
  for (const n of ref) { const dur = n.b * beat * (1 + (rnd() - .5) * .1); events.push({ t, dur, m: n.q + detune + Math.max(-3, Math.min(3, gauss())) * into, q: n.q }); t += dur; }
  const N = Math.ceil((t + 0.4) * SR), x = new Float32Array(N);
  const F = [[700, 130, 1], [1220, 150, .5], [2600, 200, .25], [3300, 250, .1]];
  const gain = f => F.reduce((a, [c, bw, g]) => a + g / (1 + ((f - c) / bw) ** 2), 0) + 0.02;
  let ph = 0, drift = 0;
  const gl = 0.07;
  for (let i = 0; i < N; i++) {
    const ts = i / SR;
    let k = events.findIndex(e => ts >= e.t && ts < e.t + e.dur);
    if (k < 0) continue;
    const e = events[k], into = ts - e.t;
    let m = e.m;
    if (k > 0 && into < gl && events[k - 1].t + events[k - 1].dur >= e.t - 1e-6) { const a = into / gl; m = events[k - 1].m + (e.m - events[k - 1].m) * (a * a * (3 - 2 * a)); }
    drift += (gauss() * 0.02 - drift * 0.001);
    if (into > 0.15) m += vibD * Math.min(1, (into - .15) / .2) * Math.sin(2 * Math.PI * vibR * ts);
    m += drift * 0.05;
    const f0 = midi2hz(m);
    ph += 2 * Math.PI * f0 / SR;
    let s = 0;
    for (let h = 1; h * f0 < 5000; h++) s += gain(h * f0) * Math.sin(h * ph) / h;
    const rel = e.dur - into, sep = events[k].sep;
    let env = Math.min(1, into / 0.03) * Math.min(1, rel / (sep ? 0.07 : 0.015));
    x[i] = s * env * 0.3 + (rnd() - .5) * (HARD ? 0.02 : 0.006);
  }
  return { x, events };
}

// --wav=FILE: write one Bayati-on-D phrase as a 16-bit WAV (used as a fake mic in the browser test)
const WAV = (process.argv.find(a => a.startsWith('--wav=')) || '').slice(6);
if (WAV) {
  const m = MAQAMAT.bayati, [cont, rh] = CONTOURS[0];
  const ref = cont.map((k, i) => ({ q: 50 + degQ(m, k) / 2, b: rh[i] }));
  const { x } = sing(ref, 90);
  const pad = new Float32Array(x.length + SR); pad.set(x, SR / 2);
  const buf = Buffer.alloc(44 + pad.length * 2);
  buf.write('RIFF', 0); buf.writeUInt32LE(36 + pad.length * 2, 4); buf.write('WAVEfmt ', 8); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20); buf.writeUInt16LE(1, 22);
  buf.writeUInt32LE(SR, 24); buf.writeUInt32LE(SR * 2, 28); buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34); buf.write('data', 36); buf.writeUInt32LE(pad.length * 2, 40);
  pad.forEach((v, i) => buf.writeInt16LE(Math.max(-32767, Math.min(32767, Math.round(v * 2 * 32767))), 44 + i * 2));
  (await import('fs')).writeFileSync(WAV, buf);
  process.exit(0);
}
const maqamCorrect = {}, conf = {}, cm = {};
let total = 0, okM = 0, okT = 0, okBoth = 0, top3 = 0, notesRef = 0, notesOk = 0, qtRef = 0, qtOk = 0, bpmOk = 0;
const t0 = Date.now();
const ONLY = (process.argv.find(a => a.startsWith('--only=')) || '').slice(7);
for (const key of MAQAM_KEYS) {
  if (ONLY && !ONLY.split(',').includes(key)) continue;
  const m = MAQAMAT[key];
  for (const tonic of TONICS[key]) for (const [ci, [cont, rh]] of CONTOURS.entries()) for (let s = 0; s < SEEDS; s++) {
    seed = 1 + ((MAQAM_KEYS.indexOf(key) * 1000 + tonic * 37 + ci * 101 + s * 7919) * 2654435761) % 2147483646; for (let w = 0; w < 30; w++) rnd();
    const ref = cont.map((k, i) => ({ q: tonic + degQ(m, k, i > 0 && k < cont[i - 1]) / 2, b: rh[i] }));
    // repeated notes get a short articulation gap
    const bpm = 75 + Math.floor(rnd() * 45);
    const { x, events } = sing(ref, bpm);
    events.forEach((e, i) => { if (i > 0 && ref[i].q === ref[i - 1].q) { events[i - 1].dur -= 0.06; } });
    const an = await analyzeAudio(x, SR);
    total++;
    const c0 = an.cands[0];
    const mOK = c0 && c0.maqam === key, tOK = c0 && ((c0.tonic - tonic) % 12 + 12) % 12 === 0;
    okM += mOK; okT += tOK; okBoth += mOK && tOK;
    if (an.cands.slice(0, 3).some(c => c.maqam === key && ((c.tonic - tonic) % 12 + 12) % 12 === 0)) top3++;
    if (!(mOK && tOK) && process.argv.includes('-v')) console.log('MISS', key, tonic, 'contour', ci, '->', an.cands.slice(0, 3).map(c => `${c.maqam}@${c.tonic} ${(c.p * 100).toFixed(0)}%`).join(' | '));
    if (!(mOK && tOK) && process.argv.includes('-vv')) console.log('   ref', ref.map(r => r.q).join(' '), '\n   sung', events.map(e => (e.m - events[events.length-1].m).toFixed(2)).join(' '), '\n   got', an.notes.map(n => (n.c - an.raw).toFixed(2) + '/' + n.d.toFixed(2)).join(' '));
    cm[key] = cm[key] || {}; const got = c0 ? c0.maqam : '-'; cm[key][got] = (cm[key][got] || 0) + 1;
    if (Math.abs(an.bpm - bpm) / bpm < 0.06) bpmOk++;
    // note accuracy with the correct key applied (tracking quality)
    an.raw = tonic + (an.raw - Math.round(an.raw));
    const an2 = applyKey({ ...an, notes: an.notes.map(n => ({ ...n })) }, key, ((tonic * 2) % 24 + 24) % 24);
    events.forEach((e, i) => {
      notesRef++;
      const hit = an2.notes.find(n => n.s < e.t + e.dur && n.s + n.d > e.t && Math.min(n.s + n.d, e.t + e.dur) - Math.max(n.s, e.t) > 0.5 * Math.min(n.d, e.dur));
      const ok = hit && Math.abs(hit.q - ref[i].q) < 0.01;
      notesOk += !!ok;
      if (ref[i].q % 1) { qtRef++; qtOk += !!ok; }
    });
  }
}
const pct = (a, b) => (100 * a / b).toFixed(1) + '%';
console.log(`${HARD ? 'HARD' : 'NORMAL'} set — ${total} phrases, ${((Date.now() - t0) / 1000).toFixed(1)}s`);
console.log(`maqam ${pct(okM, total)} · tonic ${pct(okT, total)} · maqam+tonic ${pct(okBoth, total)} · correct in top 3 ${pct(top3, total)}`);
console.log(`notes ${pct(notesOk, notesRef)} · quarter-tone notes kept ${pct(qtOk, qtRef)} · BPM ±6% ${pct(bpmOk, total)}`);
console.log('confusion (row = sung, col = detected):');
console.log('          ' + MAQAM_KEYS.map(k => k.slice(0, 5).padStart(6)).join(''));
for (const k of MAQAM_KEYS) console.log(k.padEnd(10) + MAQAM_KEYS.map(g => String(cm[k][g] || 0).padStart(6)).join(''));
