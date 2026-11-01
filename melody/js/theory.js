// Maqam theory: scale templates, spelling, degree helpers.
// Pitches are MIDI numbers as floats; a quarter tone is a .5 step.
// "q units" = quarter tones (50 cents); an octave is 24 q.

// deg: the 7 degrees above the tonic, in q units (never rounded to Western notes).
// desc: alternative degree used when the melody descends (e.g. Rast B½b -> Bb).
// gh: index of the ghammaz (dominant) degree. homes: usual tonic pitch classes (semitones, C=0).
// cad: cadence formulas as degree indices ending on the tonic (0); negative = below the tonic.
// rel: related maqamat for modulation (Phase 2: «النقلة»).
export const MAQAMAT = {
  rast:     { ar: 'راست',   en: 'Rast',     deg: [0, 4, 7, 10, 14, 18, 21], desc: { 6: 20 }, gh: 4, homes: [0, 7, 5],
              cad: [[2, 1, 0], [1, -1, 0], [3, 2, 1, 0], [4, 3, 2, 1, 0]], rel: ['bayati', 'nahawand', 'sikah', 'ajam'], prior: .02 },
  bayati:   { ar: 'بياتي',  en: 'Bayati',   deg: [0, 3, 6, 10, 14, 16, 20], gh: 3, homes: [2, 9, 7],
              cad: [[3, 2, 1, 0], [1, 2, 1, 0], [2, 1, 0], [-1, 1, 0]], rel: ['rast', 'saba', 'hijaz', 'nahawand'], prior: .02 },
  saba:     { ar: 'صبا',    en: 'Saba',     deg: [0, 3, 6, 8, 14, 16, 20], gh: 2, homes: [2, 9, 7],
              cad: [[2, 1, 0], [3, 2, 1, 0], [1, 2, 1, 0]], rel: ['bayati', 'hijaz'], prior: .01 },
  hijaz:    { ar: 'حجاز',   en: 'Hijaz',    deg: [0, 2, 8, 10, 14, 16, 20], gh: 3, homes: [2, 7, 9],
              cad: [[2, 1, 0], [1, 0, -1, 0], [3, 2, 1, 0]], rel: ['bayati', 'rast', 'nahawand'], prior: .015 },
  nahawand: { ar: 'نهاوند', en: 'Nahawand', deg: [0, 4, 6, 10, 14, 16, 22], desc: { 6: 20 }, gh: 4, homes: [0, 7, 2],
              cad: [[1, -1, 0], [2, 1, 0], [3, 2, 1, 0], [4, 3, 2, 1, 0]], rel: ['hijaz', 'kurd', 'rast'], prior: .015 },
  kurd:     { ar: 'كرد',    en: 'Kurd',     deg: [0, 2, 6, 10, 14, 16, 20], gh: 3, homes: [2, 9, 4],
              cad: [[1, 0], [2, 1, 0], [-1, 0], [3, 2, 1, 0]], rel: ['bayati', 'nahawand', 'hijaz'], prior: .01 },
  sikah:    { ar: 'سيكاه',  en: 'Sikah',    deg: [0, 3, 7, 11, 14, 17, 21], gh: 2, homes: [3.5, 10.5], qtonic: true,
              cad: [[1, 0], [2, 1, 0], [3, 2, 1, 0]], rel: ['rast', 'bayati'], prior: .005 },
  ajam:     { ar: 'عجم',    en: 'Ajam',     deg: [0, 4, 8, 10, 14, 18, 22], gh: 4, homes: [10, 0, 5],
              cad: [[-1, 0], [2, 1, 0], [4, 3, 2, 1, 0]], rel: ['rast', 'nahawand'], prior: .01 },
};
export const MAQAM_KEYS = Object.keys(MAQAMAT);

export const mod = (a, n) => ((a % n) + n) % n;
export const hz2midi = f => 69 + 12 * Math.log2(f / 440);
export const midi2hz = m => 440 * Math.pow(2, (m - 69) / 12);
export const CEILING = 67; // G4: Wasem's top note

// Degree index (any integer, 7 = octave) -> q units above the tonic.
export function degQ(m, idx, descending = false) {
  const o = Math.floor(idx / 7), i = mod(idx, 7);
  const q = descending && m.desc && m.desc[i] != null ? m.desc[i] : m.deg[i];
  return o * 24 + q;
}

// Nearest degree to an interval given in q units (float). Returns {idx, q, err} (err in q units).
export function nearestDeg(m, qi) {
  const o = Math.floor(qi / 24);
  let best = null;
  for (let idx = (o - 1) * 7; idx <= (o + 2) * 7; idx++) {
    for (const d of [false, true]) {
      if (d && !(m.desc && m.desc[mod(idx, 7)] != null)) continue;
      const q = degQ(m, idx, d), err = Math.abs(qi - q);
      if (!best || err < best.err) best = { idx, q, err };
    }
  }
  return best;
}

// ---------- spelling ----------
const NAT = [0, 4, 8, 10, 14, 18, 22];
const EN = 'CDEFGAB';
export const AR_NOTES = ['دو', 'ري', 'مي', 'فا', 'صول', 'لا', 'سي'];
const ACC = { '-2': '♭', '-1': '♭½', 0: '', 1: '♯½', 2: '♯' };
const ACC_AR = { '-2': ' بيمول', '-1': ' نص بيمول', 0: '', 1: ' نص دييز', 2: ' دييز' };
const norm = a => { a = mod(a, 24); return a > 12 ? a - 24 : a; };

// Pitch class in q units (0..23) -> {L, acc}
export function spellPc(pcq) {
  pcq = mod(Math.round(pcq), 24);
  for (const a of [0, -1, -2, 2, 1]) for (let L = 0; L < 7; L++) if (norm(pcq - NAT[L]) === a) return { L, acc: a };
  return { L: 0, acc: 0 };
}

// Spell a note inside a key {tonic (midi), maqam}: letters follow the scale degrees.
export function spellNote(q, key) {
  const pcq = mod(Math.round(q * 2), 24);
  let sp = null;
  if (key && key.maqam) {
    const m = MAQAMAT[key.maqam];
    const t = spellPc(key.tonic * 2);
    const nd = nearestDeg(m, (q - key.tonic) * 2);
    if (nd.err < 0.01) {
      const L = mod(t.L + nd.idx, 7), acc = norm(pcq - NAT[L]);
      if (Math.abs(acc) <= 2) sp = { L, acc };
    }
  }
  if (!sp) sp = spellPc(pcq);
  const nat = (Math.round(q * 2) - sp.acc) / 2;
  sp.oct = Math.floor(Math.round(nat) / 12) - 1;
  return sp;
}
export const nameEn = (sp, oct = false) => EN[sp.L] + ACC[sp.acc] + (oct ? sp.oct : '');
export const nameAr = sp => AR_NOTES[sp.L] + ACC_AR[sp.acc];
export const noteEn = (q, key, oct) => nameEn(spellNote(q, key), oct);
export const noteAr = (q, key) => nameAr(spellNote(q, key));
export const keyAr = (maqam, tonic) => `${MAQAMAT[maqam].ar} على ${nameAr(spellPc(tonic * 2))}`;

// Text notation: `D  E♭½  F  G`
export const notation = (notes, key) => notes.map(n => noteEn(n.q, key)).join('  ');

// The 24 tonic choices for the manual picker.
export const TONIC_CHOICES = Array.from({ length: 24 }, (_, pcq) => {
  const sp = spellPc(pcq);
  return { pcq, en: nameEn(sp), ar: nameAr(sp) };
});
