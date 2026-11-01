// YIN pitch tracker (de Cheveigné & Kawahara 2002). Monophonic voice, cent-level precision.
import { hz2midi } from './theory.js';

export const RATE = 16000;   // analysis rate
export const HOP = 0.01;     // 10 ms frames
const FMIN = 65, FMAX = 1100;

// One YIN estimate on buf[off .. off+W+tauMax). d is a scratch Float32Array(tauMax+1).
export function yin(buf, off, W, sr, tauMax, d, thr = 0.15) {
  const tauMin = Math.max(2, Math.floor(sr / FMAX));
  for (let tau = 1; tau <= tauMax; tau++) {
    let s = 0;
    for (let j = off, e = off + W; j < e; j++) { const v = buf[j] - buf[j + tau]; s += v * v; }
    d[tau] = s;
  }
  d[0] = 1;
  let run = 0;
  for (let tau = 1; tau <= tauMax; tau++) { run += d[tau]; d[tau] = run > 0 ? d[tau] * tau / run : 1; }
  let tau = -1;
  for (let t = tauMin; t <= tauMax; t++) {
    if (d[t] < thr) { while (t + 1 <= tauMax && d[t + 1] < d[t]) t++; tau = t; break; }
  }
  if (tau < 0) { let mn = Infinity; for (let t = tauMin; t <= tauMax; t++) if (d[t] < mn) { mn = d[t]; tau = t; } }
  let tr = tau;
  if (tau > 1 && tau < tauMax) {
    const a = d[tau - 1], b = d[tau], c = d[tau + 1], den = a - 2 * b + c;
    if (den > 0) tr = tau + 0.5 * (a - c) / den;
  }
  return { f: sr / tr, ap: d[tau] };
}

// Windowed-sinc low-pass + linear interpolation down to RATE.
export function resample(x, sr, to = RATE) {
  if (Math.abs(sr - to) < 1) return x;
  const ratio = sr / to, fc = 0.45 / ratio, M = 24, h = new Float32Array(2 * M + 1);
  let hs = 0;
  for (let i = -M; i <= M; i++) {
    const w = 0.42 + 0.5 * Math.cos(Math.PI * i / M) + 0.08 * Math.cos(2 * Math.PI * i / M);
    const s = i === 0 ? 2 * fc : Math.sin(2 * Math.PI * fc * i) / (Math.PI * i);
    hs += h[i + M] = s * w;
  }
  for (let i = 0; i < h.length; i++) h[i] /= hs;
  const n = x.length, y = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0;
    const a = Math.max(0, i - M), b = Math.min(n - 1, i + M);
    for (let j = a; j <= b; j++) s += x[j] * h[j - i + M];
    y[i] = s;
  }
  const out = new Float32Array(Math.floor(n / ratio));
  for (let i = 0; i < out.length; i++) {
    const p = i * ratio, i0 = Math.floor(p), fr = p - i0;
    out[i] = y[i0] * (1 - fr) + (i0 + 1 < n ? y[i0 + 1] : 0) * fr;
  }
  return out;
}

// Full track: returns per-frame f0 (Hz, 0 = none), aperiodicity and RMS.
export async function trackPitch(x, sr, onProgress) {
  const y = resample(x, sr);
  const hop = Math.round(HOP * RATE), W = 512, tauMax = Math.ceil(RATE / FMIN);
  const n = Math.max(0, Math.floor((y.length - W - tauMax) / hop));
  const f = new Float32Array(n), ap = new Float32Array(n), rms = new Float32Array(n), d = new Float32Array(tauMax + 1);
  for (let i = 0; i < n; i++) {
    const off = i * hop;
    let e = 0;
    for (let j = off; j < off + W; j++) e += y[j] * y[j];
    rms[i] = Math.sqrt(e / W);
    if (rms[i] < 1e-4) { ap[i] = 1; continue; }
    const r = yin(y, off, W, RATE, tauMax, d);
    f[i] = r.f; ap[i] = r.ap;
    if (i % 400 === 399) { onProgress && onProgress(i / n); await new Promise(r => setTimeout(r, 0)); }
  }
  return { f, ap, rms, hop: HOP };
}

// Live pitch for the recording display: returns MIDI float or null.
let liveD = null;
export function livePitch(buf, sr) {
  const tauMax = Math.ceil(sr / FMIN), W = Math.min(1024, buf.length - tauMax);
  if (W < 256) return null;
  let e = 0;
  for (let j = 0; j < W; j++) e += buf[j] * buf[j];
  if (Math.sqrt(e / W) < 0.01) return null;
  if (!liveD || liveD.length < tauMax + 1) liveD = new Float32Array(tauMax + 1);
  const r = yin(buf, 0, W, sr, tauMax, liveD);
  return r.ap < 0.2 && r.f > FMIN && r.f < FMAX ? hz2midi(r.f) : null;
}
