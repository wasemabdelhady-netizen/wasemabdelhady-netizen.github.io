// Develop an idea with music-theory rules (deterministic, no randomness).
// A phrase is [{k, r}]: k = maqam degree index (0 = tonic, 7 = octave, negative = below), r = length in 16ths.
import { MAQAMAT, degQ, nearestDeg, CEILING } from './theory.js';

export function toPhrase(an) {
  const m = MAQAMAT[an.maqam], u = 60 / (an.bpm || 90) / 4, N = an.notes;
  const seq = N.map((n, i) => {
    const ioi = i + 1 < N.length ? N[i + 1].s - n.s : n.d;
    return { k: nearestDeg(m, (n.q - an.tonic) * 2).idx, r: Math.max(0.5, Math.round(ioi / u * 2) / 2) };
  });
  return { m, maqam: an.maqam, tonic: an.tonic, u, seq };
}

export function toNotes(ph, seq, t0 = 0) {
  let t = t0;
  return seq.map((n, i) => {
    const nx = seq[i + 1], pv = seq[i - 1];
    const down = nx ? nx.k < n.k : pv ? pv.k > n.k : false;
    const note = { s: +t.toFixed(3), d: +(n.r * ph.u * 0.92).toFixed(3), q: ph.tonic + degQ(ph.m, n.k, down) / 2 };
    t += n.r * ph.u;
    return note;
  });
}

const sum = s => s.reduce((a, n) => a + n.r, 0);
const shift = (s, d) => s.map(n => ({ k: n.k + d, r: n.r }));
function fitLen(seq, T) {
  const out = [];
  let acc = 0;
  for (const n of seq) {
    if (acc >= T) break;
    const r = Math.min(n.r, T - acc);
    out.push({ k: n.k, r }); acc += r;
  }
  if (acc < T && out.length) out[out.length - 1].r += T - acc;
  return out;
}
function prefix(seq, maxR, minN = 2) {
  const out = []; let acc = 0;
  for (const n of seq) { if (out.length >= minN && acc + n.r > maxR) break; out.push({ ...n }); acc += n.r; }
  return out;
}
// Cadence onto `target` (degree index), placed in the octave nearest `from`, using rhythm cells from the motif.
function cadence(m, formula, target, from, rem, cells) {
  const f = formula.map(k => k + target);
  const o = Math.round((from - f[0]) / 7) * 7;
  const ks = f.map(k => k + o);
  let pre = ks.slice(0, -1).map((k, i) => ({ k, r: cells[i % cells.length] }));
  while (pre.length && sum(pre) > rem - 2) pre.shift();
  return [...pre, { k: ks[ks.length - 1], r: Math.max(2, rem - sum(pre)) }];
}
function nearestOct(target, from) { return target + Math.round((from - target) / 7) * 7; }

// ---------- «كمّل اللحن» ----------
export function continuations(ph, { len = 'same', count = 5 } = {}) {
  const { m, seq } = ph, R = sum(seq), T = len === 'bars2' ? 32 : Math.max(8, Math.round(R));
  const last = seq[seq.length - 1].k;
  const cells = seq.map(n => Math.min(4, Math.max(1, n.r)));
  const half = prefix(seq, T / 2);
  const rh = half.map(n => n.r);
  const cands = [];
  const targets = [[0, 'على القرار'], [m.gh, 'على الغمّاز']];
  for (const [tg, tl] of targets) for (const [ci, cf] of (tg === 0 ? m.cad : [[1, 0], [-1, 0], [2, 1, 0]]).entries()) {
    const form = tg === 0 ? cf : cf; // formulas end on 0; shifted to the target below
    const add = (label, body) => {
      const rem = T - sum(body);
      if (rem < 2) return;
      const from = body.length ? body[body.length - 1].k : last;
      const tk = nearestOct(tg, from);
      const cad = cadence(m, form, tk - (form[form.length - 1]), from, rem, cells);
      cands.push({ label: `${label} · ${tl}`, seq: fitLen([...body, ...cad], T), ci });
    };
    add('تكرار + قفلة', half.map(n => ({ ...n })));
    add('تسلسل نازل', shift(half, -1));
    add('تسلسل طالع', shift(half, 1));
    if (sum(half) * 2 <= T - 2) add('تسلسل مزدوج', [...shift(half, -1), ...shift(half, -2)]);
    add('تنويع إيقاعي', half.map((n, i) => ({ k: n.k, r: rh[rh.length - 1 - i] })));
    const k0 = half[0].k; add('مقلوب + قفلة', half.map(n => ({ k: 2 * k0 - n.k, r: n.r })));
    const tail = seq.slice(-3); add('ذيل الموتيف', shift(tail, -1));
    // stepwise motion from the end of the phrase to the target
    const tk = nearestOct(tg, last), steps = [];
    let k = last, i = 0;
    if (k === tk) steps.push({ k: k + 1, r: cells[0] }, { k, r: cells[1 % cells.length] }, { k: k - 1, r: cells[2 % cells.length] });
    else while (k !== tk && steps.length < 8) { k += k > tk ? -1 : 1; if (k !== tk) steps.push({ k, r: cells[i++ % cells.length] }); }
    add('حركة متدرّجة', steps.slice(0, -0 || undefined));
  }
  return rank(ph, cands, count);
}

function score(ph, c) {
  const s = c.seq, ks = s.map(n => n.k), m = ph.m;
  const lo = Math.min(...ph.seq.map(n => n.k)), hi = Math.max(...ph.seq.map(n => n.k));
  let sc = 0;
  const end = ks[ks.length - 1];
  if (((end % 7) + 7) % 7 === 0) sc += 3; else if (((end - m.gh) % 7 + 7) % 7 === 0) sc += 1.5;
  if (s[s.length - 1].r >= 4) sc += 1;
  let steps = 0, rep = 0;
  for (let i = 1; i < ks.length; i++) {
    const d = Math.abs(ks[i] - ks[i - 1]);
    if (d <= 1) steps++;
    if (d > 4) sc -= 1;
    if (d === 0 && i > 1 && ks[i - 1] === ks[i - 2]) rep++;
  }
  sc += 2 * steps / Math.max(1, ks.length - 1) - 0.7 * rep;
  const join = Math.abs(ks[0] - ph.seq[ph.seq.length - 1].k);
  sc -= 0.5 * Math.max(0, join - 2);
  for (const k of ks) {
    if (k < lo - 2 || k > hi + 2) sc -= 0.5;
    if (ph.tonic + degQ(m, k) / 2 > CEILING) sc -= 2;
  }
  const inJins = ks.filter(k => k >= -1 && k <= m.gh + 1).length / ks.length;
  sc += inJins;
  const same = ks.length === ph.seq.length && ks.every((k, i) => k === ph.seq[i].k);
  if (same) sc -= 3;
  return sc;
}
function dist(a, b) {
  const x = a.map(n => n.k), y = b.map(n => n.k);
  const D = Array.from({ length: x.length + 1 }, (_, i) => [i, ...Array(y.length).fill(0)]);
  for (let j = 1; j <= y.length; j++) D[0][j] = j;
  for (let i = 1; i <= x.length; i++) for (let j = 1; j <= y.length; j++)
    D[i][j] = Math.min(D[i - 1][j] + 1, D[i][j - 1] + 1, D[i - 1][j - 1] + (x[i - 1] === y[j - 1] ? 0 : 1));
  let rd = 0; for (let i = 0; i < Math.min(a.length, b.length); i++) rd += a[i].r !== b[i].r;
  return D[x.length][y.length] + 0.3 * rd;
}
function rank(ph, cands, count) {
  cands.forEach(c => c.score = score(ph, c));
  cands.sort((a, b) => b.score - a.score);
  const out = [];
  for (const c of cands) {
    if (out.every(o => dist(o.seq, c.seq) >= 2)) out.push(c);
    if (out.length >= count) break;
  }
  return out.map(c => ({ label: c.label, seq: c.seq, score: +c.score.toFixed(2) }));
}

// ---------- «التنويع» ----------
export function variations(ph) {
  const s = ph.seq, out = [];
  // rhythmic: equal pairs -> dotted (3:1); otherwise even out
  let rv = [], changed = false;
  for (let i = 0; i < s.length; i++) {
    if (i + 1 < s.length && s[i].r === s[i + 1].r && s[i].r >= 1) { rv.push({ k: s[i].k, r: s[i].r * 1.5 }, { k: s[i + 1].k, r: s[i].r * 0.5 }); i++; changed = true; }
    else rv.push({ ...s[i] });
  }
  if (!changed) { const avg = sum(s) / s.length; rv = s.map(n => ({ k: n.k, r: avg })); }
  out.push({ label: 'تنويع إيقاعي', seq: rv });
  out.push({ label: 'تسلسل طالع (درجة)', seq: shift(s, 1) });
  out.push({ label: 'تسلسل نازل (درجة)', seq: shift(s, -1) });
  const k0 = s[0].k;
  out.push({ label: 'مقلوب', seq: s.map(n => ({ k: 2 * k0 - n.k, r: n.r })) });
  out.push({ label: 'معكوس', seq: s.slice().reverse().map(n => ({ ...n })) });
  // Eastern ornaments: turn on long notes, grace note after a leap, mordent on medium notes
  const orn = [];
  s.forEach((n, i) => {
    const prev = s[i - 1], lastNote = i === s.length - 1;
    if (!lastNote && n.r >= 4) orn.push({ k: n.k + 1, r: .5 }, { k: n.k, r: .5 }, { k: n.k - 1, r: .5 }, { k: n.k, r: n.r - 1.5 });
    else if (prev && Math.abs(n.k - prev.k) >= 2 && n.r >= 1) orn.push({ k: n.k + 1, r: .5 }, { k: n.k, r: n.r - .5 });
    else if (!lastNote && n.r >= 2) orn.push({ k: n.k, r: .5 }, { k: n.k + 1, r: .5 }, { k: n.k, r: n.r - 1 });
    else orn.push({ ...n });
  });
  out.push({ label: 'زخارف شرقية', seq: orn });
  return out;
}

// Phase 2 (not built yet): «الرد» answer phrase, «النقلة» modulation (see MAQAMAT[x].rel), «على الكلام» lyric rhythm.
export const PHASE2 = { answer: null, modulate: null, lyrics: null };
