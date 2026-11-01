// Recording -> notes -> maqam + tonic (quarter-tone aware), BPM, range.
import { trackPitch } from './pitch.js';
import { MAQAMAT, MAQAM_KEYS, hz2midi, mod, nearestDeg, CEILING, spellPc } from './theory.js';

const median = a => { const s = Float64Array.from(a).sort(); const n = s.length; return n ? (n % 2 ? s[n >> 1] : (s[n / 2 - 1] + s[n / 2]) / 2) : NaN; };

export async function analyzeAudio(x, sr, onProgress) {
  const fr = await trackPitch(x, sr, onProgress);
  const curve = framesToMidi(fr);
  const notes = segment(curve, fr.rms, fr.hop);
  return buildAnalysis(notes, curve, fr.hop, x.length / sr);
}

// ---------- frames -> MIDI curve (NaN = unvoiced) ----------
export function framesToMidi({ f, ap, rms }) {
  const n = f.length, p = new Float32Array(n).fill(NaN);
  const sorted = Float32Array.from(rms).sort();
  const ref = sorted[Math.floor(n * 0.95)] || 0, gate = Math.max(0.06 * ref, 2e-4);
  for (let i = 0; i < n; i++) if (rms[i] > gate && ap[i] < 0.3 && f[i] > 60 && f[i] < 1100) p[i] = hz2midi(f[i]);
  // fill single-frame holes
  for (let i = 1; i < n - 1; i++) if (isNaN(p[i]) && !isNaN(p[i - 1]) && !isNaN(p[i + 1]) && Math.abs(p[i - 1] - p[i + 1]) < 1) p[i] = (p[i - 1] + p[i + 1]) / 2;
  // runs
  const runs = [];
  for (let i = 0; i < n;) { if (isNaN(p[i])) { i++; continue; } let j = i; while (j < n && !isNaN(p[j])) j++; runs.push([i, j]); i = j; }
  const out = new Float32Array(n).fill(NaN);
  for (const [a, b] of runs) {
    if (b - a < 5) continue;
    const seg = Array.from(p.subarray(a, b));
    // octave-error fix against a wide local median
    for (let i = 0; i < seg.length; i++) {
      const med = median(seg.slice(Math.max(0, i - 15), i + 16));
      const dv = seg[i] - med;
      if (Math.abs(dv) > 9) seg[i] -= 12 * Math.round(dv / 12);
    }
    for (let i = 0; i < seg.length; i++) out[a + i] = median(seg.slice(Math.max(0, i - 2), i + 3));
  }
  return out;
}

// ---------- curve -> notes ----------
export function segment(p, rms, hop) {
  const n = p.length, notes = [];
  let runs = [];
  for (let i = 0; i < n;) { if (isNaN(p[i])) { i++; continue; } let j = i; while (j < n && !isNaN(p[j])) j++; runs.push([i, j]); i = j; }
  // split runs at energy dips (repeated notes without a break in voicing)
  const split = [];
  for (const [a, b] of runs) {
    let s = a;
    for (let i = a + 8; i < b - 8; i++) {
      const r = rms[i];
      if (r > rms[i - 1] || r > rms[i + 1]) continue;
      let mb = 0, ma = 0;
      for (let k = Math.max(s, i - 12); k < i; k++) mb = Math.max(mb, rms[k]);
      for (let k = i + 1; k < Math.min(b, i + 13); k++) ma = Math.max(ma, rms[k]);
      if (r < 0.55 * Math.min(mb, ma) && i - s >= 8) { split.push([s, i]); s = i; i += 6; }
    }
    split.push([s, b]);
  }
  runs = split;
  for (const [a, b] of runs) {
    if (b - a < 6) continue;
    const s = new Float32Array(b - a);
    for (let i = a; i < b; i++) { let t = 0, c = 0; for (let k = Math.max(a, i - 7); k <= Math.min(b - 1, i + 7); k++) { t += p[k]; c++; } s[i - a] = t / c; }
    let segs = [], start = 0, sum = s[0], cnt = 0, nn = 1;
    for (let i = 1; i < s.length; i++) {
      if (Math.abs(s[i] - sum / nn) > 0.5) {
        if (++cnt >= 3) { const cut = i - cnt + 1; segs.push([start, cut]); start = cut; sum = 0; nn = 0; for (let k = cut; k <= i; k++) { sum += s[k]; nn++; } cnt = 0; }
      } else { cnt = 0; sum += s[i]; nn++; }
    }
    segs.push([start, s.length]);
    segs = segs.map(([x, y]) => [x + a, y + a]);
    const stat = ([x, y]) => {
      const t = Math.floor((y - x) * 0.2), v = Array.from(p.subarray(x + t, Math.max(x + t + 1, y - t))).sort((u, w) => u - w);
      return { pitch: median(v), spread: v[Math.floor(v.length * .75)] - v[Math.floor(v.length * .25)] };
    };
    // refine boundaries at the midpoint crossing
    for (let i = 0; i + 1 < segs.length; i++) {
      const A = stat(segs[i]), B = stat(segs[i + 1]), mid = (A.pitch + B.pitch) / 2, up = B.pitch > A.pitch;
      const lo = Math.max(segs[i][0] + 2, segs[i][1] - 10), hi = Math.min(segs[i + 1][1] - 2, segs[i][1] + 10);
      for (let k = lo; k < hi; k++) if (up ? p[k] >= mid : p[k] <= mid) { segs[i][1] = k; segs[i + 1][0] = k; break; }
    }
    // drop glides / blips: hand their time to the following note
    let out = [];
    for (let i = 0; i < segs.length; i++) {
      const sg = segs[i], st = stat(sg);
      if ((sg[1] - sg[0] < 6 || st.spread > 0.6) && segs.length > 1) {
        if (i + 1 < segs.length) segs[i + 1][0] = sg[0]; else if (out.length) out[out.length - 1][1] = sg[1];
        continue;
      }
      out.push(sg);
    }
    // merge neighbours that are the same pitch
    const merged = [];
    for (const sg of out) {
      const L = merged[merged.length - 1];
      if (L && Math.abs(stat(L).pitch - stat(sg).pitch) < 0.4) L[1] = sg[1]; else merged.push([...sg]);
    }
    for (const sg of merged) {
      const st = stat(sg);
      if (!isNaN(st.pitch)) notes.push({ s: +(sg[0] * hop).toFixed(3), d: +((sg[1] - sg[0]) * hop).toFixed(3), c: +st.pitch.toFixed(3) });
    }
  }
  return notes;
}

// ---------- maqam + tonic ----------
const SIG = 22; // cents tolerance of the template match
function clusters(notes) {
  const s = notes.map(n => ({ c: n.c, w: Math.min(n.d, 1.2) })).sort((a, b) => a.c - b.c), cl = [];
  for (const n of s) {
    const L = cl[cl.length - 1];
    if (L && n.c - L.c < 0.35) { L.c = (L.c * L.w + n.c * n.w) / (L.w + n.w); L.w += n.w; } else cl.push({ ...n });
  }
  return cl;
}
const pcDist = (a, b) => { const d = mod(a - b, 12); return Math.min(d, 12 - d) * 100; };

export function detect(notes) {
  if (!notes.length) return [];
  const W = notes.map(n => Math.min(n.d, 1.2)), tw = W.reduce((a, b) => a + b, 0);
  const last = notes[notes.length - 1], first = notes[0];
  const out = [];
  const cl = clusters(notes);
  for (const T of cl) {
    for (const key of MAQAM_KEYS) {
      const m = MAQAMAT[key];
      let fit = 0, onT = 0, below = 0, deep = 0;
      // template fit per pitch cluster (averages out the intonation of repeated notes)
      for (const c of cl) {
        const dc = nearestDeg(m, (c.c - T.c) * 2).err * 50;
        fit += c.w * Math.exp(-dc * dc / (2 * SIG * SIG));
        if (pcDist(c.c, T.c) < 30) onT += c.w;
        if (c.c < T.c - 0.3) below += c.w;
        if (c.c < T.c - 3.6) deep += c.w;
      }
      fit /= tw;
      const gh = T.c + (m.deg[m.gh]) / 2;
      let s = fit + 0.15 * onT / tw - 0.1 * below / tw - 0.4 * deep / tw + m.prior;
      if (pcDist(last.c, T.c) < 30) s += 0.12; else if (pcDist(last.c, gh) < 30) s += 0.05;
      if (pcDist(first.c, T.c) < 30) s += 0.06; else if (pcDist(first.c, gh) < 30) s += 0.02;
      const snapped = m.qtonic ? Math.round(T.c - 0.5) + 0.5 : Math.round(T.c);
      s -= 0.03 * Math.abs(T.c - snapped) / 0.5;
      out.push({ maqam: key, raw: T.c, tonic: snapped, s });
    }
  }
  // keep the best per (maqam, tonic pitch class)
  out.sort((a, b) => b.s - a.s);
  const seen = new Set(), best = [];
  for (const c of out) { const k = c.maqam + mod(Math.round(c.tonic * 2), 24); if (!seen.has(k)) { seen.add(k); best.push(c); } }
  const z = best.reduce((a, c) => a + Math.exp((c.s - best[0].s) / 0.04), 0);
  best.forEach(c => c.p = Math.exp((c.s - best[0].s) / 0.04) / z);
  return best.slice(0, 6);
}

// Re-quantize the notes to a maqam + tonic. tonicPcq (0..23, optional) = manual tonic.
export function applyKey(an, maqam, tonicPcq) {
  const m = MAQAMAT[maqam];
  let raw = an.raw, tonic;
  if (tonicPcq != null) {
    const pc = tonicPcq / 2, cl = clusters(an.notes);
    const near = cl.filter(c => pcDist(c.c, pc) < 60).sort((a, b) => Math.abs(a.c - an.raw) - Math.abs(b.c - an.raw))[0];
    raw = near ? near.c : an.raw + ((mod(pc - an.raw + 6, 12)) - 6);
    tonic = raw + ((mod(pc - raw + 6, 12)) - 6);
    tonic = Math.round(tonic * 2) / 2;
  } else tonic = m.qtonic ? Math.round(raw - 0.5) + 0.5 : Math.round(raw);
  an.maqam = maqam; an.raw = raw; an.tonic = tonic;
  for (const n of an.notes) {
    const qi = (n.c - raw) * 2, nd = nearestDeg(m, qi);
    n.q = tonic + (nd.err < 0.9 ? nd.q : Math.round(qi)) / 2;
  }
  an.range = range(an.notes);
  return an;
}

export function range(notes) {
  if (!notes.length) return null;
  let lo = Infinity, hi = -Infinity;
  for (const n of notes) { lo = Math.min(lo, n.q); hi = Math.max(hi, n.q); }
  return { lo, hi };
}

// ---------- tempo ----------
export function estimateBpm(notes) {
  if (notes.length < 3) return null;
  const on = notes.map(n => n.s);
  const coh = T => { let re = 0, im = 0; for (const t of on) { re += Math.cos(2 * Math.PI * t / T); im += Math.sin(2 * Math.PI * t / T); } return Math.hypot(re, im) / on.length; };
  let best = null;
  for (let b = 70; b < 140; b += 0.5) {
    const T = 60 / b, sc = coh(T) + 0.7 * coh(T / 2) + 0.3 * coh(T / 4);
    if (!best || sc > best.sc) best = { b, sc };
  }
  return Math.round(best.b);
}

// Suggest a key that keeps the idea under the G4 ceiling.
export function ceilingAdvice(an) {
  if (!an.range || an.range.hi <= CEILING) return null;
  const over = an.range.hi - CEILING, down = Math.ceil(over - 1e-6);
  const m = MAQAMAT[an.maqam];
  const simple = { down, tonic: an.tonic - down };
  let home = null;
  for (const h of m.homes) for (let o = -2; o <= 1; o++) {
    const t = Math.floor(an.tonic / 12) * 12 + h + 12 * o;
    if (t < an.tonic && an.range.hi - an.tonic + t <= CEILING && (!home || t > home.tonic)) home = { down: an.tonic - t, tonic: t };
  }
  return { over, simple, home: home && home.tonic !== simple.tonic ? home : null };
}

export function buildAnalysis(notes, curve, hop, dur) {
  const cands = detect(notes);
  const an = { notes, dur: +dur.toFixed(2), cands, bpm: estimateBpm(notes), conf: cands[0] ? cands[0].p : 0 };
  // compact pitch curve for display: every 20 ms, MIDI*100 (0 = silence)
  an.curve = []; for (let i = 0; i < curve.length; i += 2) an.curve.push(isNaN(curve[i]) ? 0 : Math.round(curve[i] * 100));
  an.curveHop = hop * 2;
  if (cands[0]) useCandidate(an, cands[0]);
  else { an.maqam = null; an.range = null; }
  return an;
}
export function useCandidate(an, c) { an.raw = c.raw; applyKey(an, c.maqam); an.conf = c.p; return an; }
