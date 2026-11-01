// Oud-like plucked synth: fractional-delay Karplus-Strong (exact quarter tones), short decay, body filter.
import { midi2hz } from './theory.js';

function biquad(x, sr, type, f, Q, gainDb = 0) {
  const A = Math.pow(10, gainDb / 40), w = 2 * Math.PI * f / sr, cs = Math.cos(w), al = Math.sin(w) / (2 * Q);
  let b0, b1, b2, a0, a1, a2;
  if (type === 'peak') { b0 = 1 + al * A; b1 = -2 * cs; b2 = 1 - al * A; a0 = 1 + al / A; a1 = -2 * cs; a2 = 1 - al / A; }
  else { b0 = (1 - cs) / 2; b1 = 1 - cs; b2 = (1 - cs) / 2; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; } // lowpass
  let x1 = 0, x2 = 0, y1 = 0, y2 = 0;
  for (let i = 0; i < x.length; i++) {
    const y = (b0 * x[i] + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0;
    x2 = x1; x1 = x[i]; y2 = y1; y1 = y; x[i] = y;
  }
}

export function renderOud(notes, sr) {
  const end = notes.reduce((a, n) => Math.max(a, n.s + n.d), 0) + 0.8;
  const out = new Float32Array(Math.ceil(end * sr));
  let seed = 12345;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 1073741823.5 - 1; };
  notes.forEach((n, idx) => {
    const f = midi2hz(n.q), P = sr / f, D = P - 0.5;
    const next = notes[idx + 1];
    const stop = Math.min(n.s + n.d + 0.25, next ? next.s + 0.02 : Infinity);
    const len = Math.ceil((stop - n.s + 0.12) * sr), y = new Float32Array(len);
    const t60 = Math.min(2.2, 1.4 * Math.pow(220 / f, 0.35));
    const g = Math.pow(10, -3 / (t60 * f));
    // excitation: one period of filtered noise with a pluck-position comb
    const ex = new Float32Array(Math.ceil(P) + 2);
    let lp = 0;
    for (let i = 0; i < ex.length; i++) { lp += 0.55 * (rnd() - lp); ex[i] = lp; }
    const pos = Math.max(1, Math.round(P * 0.13));
    for (let i = ex.length - 1; i >= pos; i--) ex[i] -= ex[i - pos];
    const rel = Math.floor((stop - n.s) * sr);
    for (let i = 0; i < len; i++) {
      let v = i < ex.length ? ex[i] : 0;
      const r = i - D;
      if (r >= 1) { const i0 = Math.floor(r), fr = r - i0; v += g * 0.5 * ((y[i0] * (1 - fr) + y[i0 + 1] * fr) + (y[i0 - 1] * (1 - fr) + y[i0] * fr)); }
      y[i] = v;
    }
    const o = Math.floor(n.s * sr), relLen = 0.1 * sr;
    for (let i = 0; i < len && o + i < out.length; i++) {
      const env = i < rel ? 1 : Math.max(0, 1 - (i - rel) / relLen);
      out[o + i] += y[i] * env * 0.5;
    }
  });
  biquad(out, sr, 'peak', 220, 1.2, 4);
  biquad(out, sr, 'peak', 2600, 2, -3);
  biquad(out, sr, 'low', 5200, 0.7);
  let pk = 0; for (const v of out) pk = Math.max(pk, Math.abs(v));
  if (pk > 0) for (let i = 0; i < out.length; i++) out[i] *= 0.8 / pk;
  return out;
}
