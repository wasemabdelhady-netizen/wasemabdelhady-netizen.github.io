// «بنك الألحان» UI. Vanilla ES modules, all data in IndexedDB on the phone.
import { analyzeAudio, applyKey, useCandidate, ceilingAdvice, range } from './analyze.js';
import { livePitch } from './pitch.js';
import { MAQAMAT, MAQAM_KEYS, TONIC_CHOICES, keyAr, noteEn, notation, degQ, nearestDeg, mod, CEILING } from './theory.js';
import { toMidi } from './midi.js';
import { renderOud } from './synth.js';
import { makeZip, readZip } from './zip.js';
import { toPhrase, toNotes, continuations, variations } from './develop.js';
import * as DB from './db.js';

const $ = s => document.querySelector(s);
const view = $('#view');
const DEFAULT_TAGS = ['حزين', 'مبهج', 'لايف', 'كوبليه', 'مذهب', 'كورس', 'لزمة'];
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const fmtDur = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, '0')}`;
const fmtDate = t => new Date(t).toLocaleDateString('ar-EG-u-nu-latn', { day: 'numeric', month: 'short', year: 'numeric' });
const uid = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
const keyOf = an => an && an.maqam ? { maqam: an.maqam, tonic: an.tonic } : null;

let ideas = [];
let pending = null; // recording waiting to be saved
async function loadIdeas() { ideas = (await DB.allIdeas()).sort((a, b) => b.created - a.created); }

function toast(msg, ms = 2200) { const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(toast.h); toast.h = setTimeout(() => t.hidden = true, ms); }
function busy(on, text) { $('#busy').hidden = !on; if (text) $('#busyText').textContent = text; }

// ---------- audio ----------
let ctx = null, playing = null;
function audio() {
  if (!ctx) ctx = new (window.AudioContext || window.webkitAudioContext)();
  if (ctx.state === 'suspended') ctx.resume();
  return ctx;
}
function session(type) { try { if (navigator.audioSession) navigator.audioSession.type = type; } catch { } }
function stop() { if (playing) { try { playing.stop(); } catch { } playing = null; } }
function playBuffer(buf) {
  stop();
  const c = audio(), s = c.createBufferSource();
  s.buffer = buf; s.connect(c.destination); s.start();
  s.onended = () => { if (playing === s) playing = null; };
  playing = s;
}
function playNotes(notes) {
  if (!notes || !notes.length) return;
  session('playback');
  const c = audio(), pcm = renderOud(notes, c.sampleRate), b = c.createBuffer(1, pcm.length, c.sampleRate);
  b.getChannelData(0).set(pcm);
  playBuffer(b);
}
const decoded = new Map();
async function playVoice(id, rec) {
  session('playback');
  let b = decoded.get(id);
  if (!b) {
    const a = rec || await DB.getAudio(id);
    if (!a) return toast('مفيش تسجيل صوتي للفكرة دي');
    b = await audio().decodeAudioData(a.buf.slice(0));
    decoded.set(id, b);
  }
  playBuffer(b);
}

// ---------- files out ----------
async function shareFile(blob, name) {
  const file = new File([blob], name, { type: blob.type });
  if (navigator.canShare && navigator.canShare({ files: [file] })) {
    try { await navigator.share({ files: [file], title: name }); return; } catch (e) { if (e.name === 'AbortError') return; }
  }
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob); a.download = name; document.body.appendChild(a); a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
}
const safeName = s => (s || 'idea').replace(/[\\/:*?"<>|]+/g, '-').slice(0, 60);
const midiBlob = (notes, bpm, name) => new Blob([toMidi(notes, { bpm: bpm || 90, name })], { type: 'audio/midi' });

// ---------- recording ----------
const MIMES = ['audio/mp4;codecs=mp4a.40.2', 'audio/mp4', 'audio/aac', 'audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus'];
function pickMime() {
  if (!window.MediaRecorder) return null;
  return MIMES.find(m => MediaRecorder.isTypeSupported && MediaRecorder.isTypeSupported(m)) || '';
}
const extOf = mime => /mp4|aac|m4a/.test(mime) ? 'm4a' : /ogg/.test(mime) ? 'ogg' : 'webm';

const R = { state: 'idle', stream: null, rec: null, chunks: [], t0: 0, raf: 0, line: [], pendingStop: false };
async function startRec() {
  if (R.state !== 'idle') return;
  R.state = 'starting';
  try {
    session('play-and-record');
    const c = audio();
    R.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } });
    const mime = pickMime();
    if (mime === null) throw new Error('MediaRecorder غير مدعوم');
    R.rec = new MediaRecorder(R.stream, mime ? { mimeType: mime } : undefined);
    R.chunks = [];
    R.rec.ondataavailable = e => e.data && e.data.size && R.chunks.push(e.data);
    R.rec.onstop = finishRec;
    R.rec.start(1000);
    const src = c.createMediaStreamSource(R.stream), an = c.createAnalyser();
    an.fftSize = 2048; src.connect(an);
    R.src = src; R.an = an; R.buf = new Float32Array(an.fftSize);
    R.t0 = performance.now(); R.line = []; R.state = 'rec';
    document.querySelector('.rec-btn')?.classList.add('on');
    liveLoop();
    if (R.pendingStop) stopRec();
    R.maxT = setTimeout(stopRec, 5 * 60 * 1000);
  } catch (e) {
    R.state = 'idle';
    cleanupStream();
    toast(e.name === 'NotAllowedError' ? 'محتاج إذن المايك: الإعدادات ← Safari ← الميكروفون' : 'التسجيل مش شغال: ' + e.message, 4000);
  }
}
function stopRec() {
  if (R.state === 'starting') { R.pendingStop = true; return; }
  if (R.state !== 'rec') return;
  R.state = 'stopping'; R.pendingStop = false;
  clearTimeout(R.maxT); cancelAnimationFrame(R.raf);
  document.querySelector('.rec-btn')?.classList.remove('on');
  try { R.rec.requestData && R.rec.requestData(); } catch { }
  R.rec.stop();
}
function cleanupStream() {
  try { R.src && R.src.disconnect(); } catch { }
  if (R.stream) R.stream.getTracks().forEach(t => t.stop());
  R.stream = null; R.src = null;
}
async function finishRec() {
  const mime = (R.rec.mimeType || pickMime() || 'audio/mp4').split(';')[0];
  const blob = new Blob(R.chunks, { type: mime });
  cleanupStream();
  R.state = 'idle';
  session('playback');
  if (blob.size < 2000) return toast('التسجيل قصير قوي');
  busy(true, 'بحلّل…');
  try {
    const buf = await blob.arrayBuffer();
    const ab = await audio().decodeAudioData(buf.slice(0));
    const x = ab.getChannelData(0);
    const an = await analyzeAudio(x, ab.sampleRate, p => $('#busyText').textContent = `بحلّل… ${Math.round(p * 100)}%`);
    if (!an.notes.length) { busy(false); return toast('مالقيتش نغمات واضحة. قرّب من المايك وجرّب تاني', 3500); }
    const id = uid();
    decoded.set(id, ab);
    pending = { id, created: Date.now(), audio: { mime, ext: extOf(mime), buf }, an };
    busy(false);
    location.hash = '#/save';
  } catch (e) {
    busy(false); console.error(e);
    toast('مقدرتش أحلّل التسجيل: ' + e.message, 4000);
  }
}
function liveLoop() {
  const cv = $('#live');
  if (R.state !== 'rec') return;
  R.raf = requestAnimationFrame(liveLoop);
  const t = (performance.now() - R.t0) / 1000;
  $('#timer') && ($('#timer').textContent = fmtDur(t));
  R.an.getFloatTimeDomainData(R.buf);
  const m = livePitch(R.buf, audio().sampleRate);
  R.line.push({ t, m });
  while (R.line.length && R.line[0].t < t - 6) R.line.shift();
  if (m != null && $('#liveNote')) $('#liveNote').textContent = noteEn(Math.round(m * 2) / 2, null, true) + `  ${Math.round((m * 2 - Math.round(m * 2)) * 50) >= 0 ? '+' : ''}${Math.round((m * 2 - Math.round(m * 2)) * 50)}¢`;
  if (cv) drawLive(cv, t);
}
function fitCanvas(cv) {
  const r = cv.getBoundingClientRect(), d = window.devicePixelRatio || 1;
  if (cv.width !== Math.round(r.width * d) || cv.height !== Math.round(r.height * d)) { cv.width = Math.round(r.width * d); cv.height = Math.round(r.height * d); }
  const g = cv.getContext('2d'); g.setTransform(d, 0, 0, d, 0, 0);
  return { g, w: r.width, h: r.height };
}
function drawLive(cv, now) {
  const { g, w, h } = fitCanvas(cv);
  g.direction = 'ltr';
  g.clearRect(0, 0, w, h);
  const vals = R.line.filter(p => p.m != null).map(p => p.m).sort((a, b) => a - b);
  const mid = vals.length ? vals[vals.length >> 1] : 57, lo = mid - 9, hi = mid + 9;
  const Y = m => h - (m - lo) / (hi - lo) * h, X = t => w - (now - t) / 6 * w;
  g.font = '11px system-ui'; g.textAlign = 'left';
  for (let m = Math.ceil(lo); m <= hi; m++) {
    g.strokeStyle = m % 12 === 0 ? 'rgba(224,164,88,.35)' : 'rgba(255,255,255,.06)';
    g.beginPath(); g.moveTo(0, Y(m)); g.lineTo(w, Y(m)); g.stroke();
    if (m % 12 === 0) { g.fillStyle = 'rgba(224,164,88,.6)'; g.fillText(noteEn(m, null, true), 6, Y(m) - 3); }
  }
  g.strokeStyle = '#e0a458'; g.lineWidth = 3; g.lineJoin = 'round'; g.beginPath();
  let pen = false;
  for (const p of R.line) { if (p.m == null) { pen = false; continue; } const x = X(p.t), y = Y(p.m); pen ? g.lineTo(x, y) : g.moveTo(x, y); pen = true; }
  g.stroke();
}

// ---------- piano roll ----------
function drawRoll(cv, an, extra) {
  const { g, w, h } = fitCanvas(cv), key = keyOf(an);
  g.direction = 'ltr';
  g.clearRect(0, 0, w, h);
  const all = [...an.notes, ...(extra || [])];
  if (!all.length) return;
  const end = all.reduce((a, n) => Math.max(a, n.s + n.d), 0) || 1;
  let lo = Math.min(...all.map(n => n.q)) - 1.5, hi = Math.max(...all.map(n => n.q)) + 1.5;
  if (hi - lo < 8) { const c = (hi + lo) / 2; lo = c - 4; hi = c + 4; }
  const L = 40, Y = m => h - 8 - (m - lo) / (hi - lo) * (h - 16), X = t => L + t / end * (w - L - 8);
  g.font = '11px system-ui'; g.textAlign = 'left'; g.textBaseline = 'middle';
  if (key) {
    const m = MAQAMAT[key.maqam];
    for (let i = -14; i < 21; i++) {
      const q = key.tonic + degQ(m, i) / 2;
      if (q < lo || q > hi) continue;
      const ton = mod(i, 7) === 0;
      g.strokeStyle = ton ? 'rgba(224,164,88,.45)' : 'rgba(255,255,255,.07)';
      g.beginPath(); g.moveTo(L, Y(q)); g.lineTo(w, Y(q)); g.stroke();
      g.fillStyle = ton ? '#e0a458' : 'rgba(245,239,230,.5)';
      g.fillText(noteEn(q, key), 4, Y(q));
    }
    if (CEILING < hi) { g.strokeStyle = 'rgba(255,138,101,.7)'; g.setLineDash([4, 4]); g.beginPath(); g.moveTo(L, Y(CEILING)); g.lineTo(w, Y(CEILING)); g.stroke(); g.setLineDash([]); }
  }
  if (an.curve && !extra) {
    g.strokeStyle = 'rgba(245,239,230,.28)'; g.lineWidth = 1.2; g.beginPath();
    let pen = false;
    an.curve.forEach((c, i) => { if (!c) { pen = false; return; } const x = X(i * an.curveHop), y = Y(c / 100); pen ? g.lineTo(x, y) : g.moveTo(x, y); pen = true; });
    g.stroke();
  }
  const bar = (n, col) => { g.fillStyle = col; const x = X(n.s), y = Y(n.q); g.beginPath(); g.roundRect ? g.roundRect(x, y - 4, Math.max(3, X(n.s + n.d) - x), 8, 4) : g.rect(x, y - 4, Math.max(3, X(n.s + n.d) - x), 8); g.fill(); };
  for (const n of an.notes) bar(n, n.q % 1 ? '#7fd1c7' : '#e0a458');
  for (const n of extra || []) bar(n, n.q % 1 ? 'rgba(127,209,199,.75)' : '#f3c98a');
}

// ---------- shared analysis block ----------
function analysisHtml(an) {
  const key = keyOf(an), adv = ceilingAdvice(an), r = an.range;
  const conf = an.manual ? 'يدوي' : `ثقة <span class="ltr">${Math.round((an.conf || 0) * 100)}%</span>`;
  let warn = '';
  if (adv) {
    warn = `<div class="warn">⚠ الفكرة بتوصل لـ <span class="ltr">${noteEn(r.hi, key, true)}</span>، يعني فوق الـ <span class="ltr">G4</span> بـ ${+adv.over.toFixed(1)} تون نصّي.<br>
      نزّلها ${adv.simple.down} نص تون ← <b>${keyAr(an.maqam, adv.simple.tonic)}</b>${adv.home ? `<br>أو المعتاد: <b>${keyAr(an.maqam, adv.home.tonic)}</b> (−${adv.home.down})` : ''}</div>`;
  }
  return `<div class="card">
    <div class="key"><div><b>${key ? keyAr(an.maqam, an.tonic) : 'مقام غير محدد'}</b><small>${conf}</small></div>
      <button class="btn small" data-act="key">تعديل</button></div>
    <div class="stats">
      <div><small>السرعة</small><b>${an.bpm ? '≈ ' + an.bpm : '—'}</b></div>
      <div><small>المساحة</small><b>${r ? noteEn(r.lo, key, true) + '–' + noteEn(r.hi, key, true) : '—'}</b></div>
      <div><small>المدة</small><b>${fmtDur(an.dur || 0)}</b></div>
    </div>
    ${warn}
    <canvas class="roll" id="roll"></canvas>
    <div class="nota" id="nota">${esc(notation(an.notes, key))}</div>
  </div>`;
}

// Key sheet: pick a detected alternative, or set maqam + tonic by hand.
function openKeySheet(an, onApply) {
  const tpc = mod(Math.round(an.tonic * 2), 24);
  const sh = openSheet(`<h3>المقام والقرار</h3>
    <p class="hint" style="text-align:start">اختار من الاحتمالات، أو حدّد بإيدك.</p>
    <div class="tags">${(an.cands || []).slice(0, 4).map((c, i) => `<button class="tag ${c.maqam === an.maqam && c.tonic === an.tonic ? 'on' : ''}" data-cand="${i}">${keyAr(c.maqam, c.tonic)} · ${Math.round(c.p * 100)}%</button>`).join('')}</div>
    <label class="f">المقام<select class="t" id="kM">${MAQAM_KEYS.map(k => `<option value="${k}" ${k === an.maqam ? 'selected' : ''}>${MAQAMAT[k].ar}</option>`).join('')}</select></label>
    <label class="f">القرار<select class="t" id="kT">${TONIC_CHOICES.map(t => `<option value="${t.pcq}" ${t.pcq === tpc ? 'selected' : ''}>${t.ar}  (${t.en})</option>`).join('')}</select></label>
    <div class="row"><button class="btn gold grow" id="kOk">طبّق</button><button class="btn" data-close>إلغاء</button></div>`);
  sh.querySelectorAll('[data-cand]').forEach(b => b.onclick = () => { useCandidate(an, an.cands[+b.dataset.cand]); an.manual = false; closeSheet(); onApply(); });
  sh.querySelector('#kOk').onclick = () => { applyKey(an, $('#kM').value, +$('#kT').value); an.manual = true; closeSheet(); onApply(); };
}
function openSheet(html) {
  const sh = $('#sheet'); sh.innerHTML = html; sh.hidden = false; $('#sheetBg').hidden = false;
  sh.querySelectorAll('[data-close]').forEach(b => b.onclick = closeSheet);
  return sh;
}
function closeSheet() { $('#sheet').hidden = true; $('#sheetBg').hidden = true; }
$('#sheetBg').onclick = closeSheet;

function allTags() {
  const s = new Set(DEFAULT_TAGS);
  ideas.forEach(i => (i.tags || []).forEach(t => s.add(t)));
  return [...s];
}
function tagPicker(selected) {
  return `<div class="tags" id="tagBox">${allTags().map(t => `<button class="tag ${selected.includes(t) ? 'on' : ''}" data-tag="${esc(t)}">${esc(t)}</button>`).join('')}</div>
    <div class="row"><input class="t" id="newTag" placeholder="تاج جديد" style="flex:1;margin:0"><button class="btn small" id="addTag">+ أضف</button></div>`;
}
function bindTagPicker(selected) {
  const box = $('#tagBox');
  const bind = b => b.onclick = () => { const t = b.dataset.tag, i = selected.indexOf(t); i < 0 ? selected.push(t) : selected.splice(i, 1); b.classList.toggle('on'); };
  box.querySelectorAll('[data-tag]').forEach(bind);
  $('#addTag').onclick = () => {
    const t = $('#newTag').value.trim(); if (!t) return;
    if (!selected.includes(t)) selected.push(t);
    if (!box.querySelector(`[data-tag="${CSS.escape(t)}"]`)) { const b = document.createElement('button'); b.className = 'tag on'; b.dataset.tag = t; b.textContent = t; box.appendChild(b); bind(b); }
    $('#newTag').value = '';
  };
}

// ---------- views ----------
function setTitle(t) { $('#title').textContent = t; }
function setNav(n) { document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('on', a.dataset.nav === n)); }

function viewRec() {
  setTitle('بنك الألحان'); setNav('rec');
  view.innerHTML = `<section class="rec">
    <canvas id="live"></canvas>
    <div class="live-note" id="liveNote">—</div>
    <div class="timer" id="timer">0:00</div>
    <button class="rec-btn ${R.state === 'rec' ? 'on' : ''}" id="recBtn" aria-label="تسجيل"><span></span></button>
    <p class="hint">دوسة: يبدأ · دوسة تانية: يقف<br>أو دوس مطوّل وسيب لما تخلص</p>
  </section>`;
  const b = $('#recBtn');
  let press = null;
  b.onpointerdown = e => {
    e.preventDefault();
    if (R.state === 'idle') { press = performance.now(); startRec(); } else press = null;
  };
  b.onpointerup = e => {
    e.preventDefault();
    if (press != null) { if (performance.now() - press > 600) stopRec(); press = null; }
    else if (R.state === 'rec' || R.state === 'starting') stopRec();
  };
  b.oncontextmenu = e => e.preventDefault();
  const cv = $('#live'); R.line = R.line || []; drawLive(cv, 0);
}

function viewSave() {
  if (!pending) { location.hash = '#/'; return; }
  setTitle('فكرة جديدة'); setNav('rec');
  const p = pending, an = p.an, tags = p.tags || (p.tags = []);
  const n = ideas.filter(i => i.kind !== 'gen').length + 1;
  view.innerHTML = `${analysisHtml(an)}
    <div class="row"><button class="btn grow" data-act="voice">▶ صوتي</button><button class="btn grow" data-act="oud">▶ عود</button><button class="btn" data-act="stop">■</button></div>
    <div class="card" style="margin-top:12px">
      <label class="f">الاسم<input class="t" id="fTitle" value="${esc(p.title || 'فكرة ' + n)}"></label>
      <label class="f">ملاحظة<textarea class="t" id="fNote" placeholder="فين جت، إحساسها، كلام…">${esc(p.note || '')}</textarea></label>
      <label class="f">تاجات</label>${tagPicker(tags)}
    </div>
    <div class="row"><button class="btn gold grow" id="save">احفظ</button><button class="btn danger" id="discard">امسح</button></div>`;
  drawRoll($('#roll'), an);
  bindTagPicker(tags);
  bindCommon(an, () => { p.title = $('#fTitle').value; p.note = $('#fNote').value; viewSave(); }, { voice: () => playVoice(p.id, { buf: p.audio.buf }) });
  $('#save').onclick = async () => {
    const idea = { id: p.id, created: p.created, kind: 'rec', title: $('#fTitle').value.trim() || 'فكرة ' + n, note: $('#fNote').value.trim(), tags, star: false, an: p.an, audio: { mime: p.audio.mime, ext: p.audio.ext } };
    await DB.putAudio(p.id, p.audio.mime, p.audio.buf);
    await DB.putIdea(idea);
    DB.persist();
    pending = null; await loadIdeas();
    toast('اتحفظت ✓');
    location.hash = '#/idea/' + idea.id;
  };
  $('#discard').onclick = () => { if (confirm('تمسح التسجيل ده؟')) { pending = null; location.hash = '#/'; } };
}

function bindCommon(an, rerender, acts = {}) {
  view.querySelectorAll('[data-act]').forEach(b => {
    const a = b.dataset.act;
    b.onclick = () => {
      if (a === 'key') openKeySheet(an, rerender);
      else if (a === 'oud') playNotes(an.notes);
      else if (a === 'stop') stop();
      else if (acts[a]) acts[a]();
    };
  });
}

// ---------- library ----------
const F = { text: '', maqam: '', tags: [], bmin: '', bmax: '', star: false };
const normAr = s => String(s || '').toLowerCase().replace(/[ً-ْـ]/g, '').replace(/[أإآ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي');
const stem = t => { t = normAr(t); if (t.startsWith('وال')) t = t.slice(3); else if (t.startsWith('بال') || t.startsWith('فال')) t = t.slice(3); else if (t.startsWith('ال') && t.length > 3) t = t.slice(2); return t.replace(/(ه|ات)$/, '') || t; };
function hay(i) {
  const an = i.an || {}, m = an.maqam ? MAQAMAT[an.maqam] : null;
  return normAr([i.title, i.note, ...(i.tags || []), m && m.ar, m && m.en, an.maqam && keyAr(an.maqam, an.tonic)].join(' '));
}
function filtered() {
  const hs = new Map(ideas.map(i => [i.id, hay(i)]));
  const all = [...hs.values()].join(' ');
  const toks = F.text.split(/\s+/).map(stem).filter(t => t.length > 1 && all.includes(t)); // words that match nothing ("كل", "الأفكار") are ignored
  return ideas.filter(i => {
    const an = i.an || {};
    if (F.maqam && an.maqam !== F.maqam) return false;
    if (F.star && !i.star) return false;
    if (F.tags.length && !F.tags.every(t => (i.tags || []).includes(t))) return false;
    if (F.bmin && !(an.bpm >= +F.bmin)) return false;
    if (F.bmax && !(an.bpm <= +F.bmax)) return false;
    const h = hs.get(i.id);
    return toks.every(t => h.includes(t));
  });
}
function cardHtml(i) {
  const an = i.an || {}, kids = ideas.filter(x => x.parentId === i.id).length;
  return `<div class="card item" data-id="${i.id}">
    <button class="play ${i.kind === 'gen' ? 'alt' : ''}" data-play="${i.id}" aria-label="تشغيل">▶</button>
    <div class="meta">
      <h3>${i.star ? '⭐ ' : ''}${esc(i.title)}</h3>
      <p>${an.maqam ? keyAr(an.maqam, an.tonic) : '—'} · <span class="ltr">${an.bpm ? an.bpm + ' BPM' : ''}</span> · ${fmtDur(an.dur || 0)} · ${fmtDate(i.created)}${kids ? ` · ${kids} تطوير` : ''}${i.kind === 'gen' ? ' · مولّدة' : ''}</p>
      ${(i.tags || []).length ? `<div class="tags">${i.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
    </div></div>`;
}
function bindCards(root) {
  root.querySelectorAll('[data-play]').forEach(b => b.onclick = e => {
    e.stopPropagation();
    const i = ideas.find(x => x.id === b.dataset.play);
    i.kind === 'gen' ? playNotes(i.an.notes) : playVoice(i.id);
  });
  root.querySelectorAll('[data-id]').forEach(c => c.onclick = () => location.hash = '#/idea/' + c.dataset.id);
}
function viewLib() {
  setTitle('المكتبة'); setNav('lib');
  view.innerHTML = `<div class="search">
      <input class="t" id="q" type="search" placeholder="دوّر: حزين بياتي، لزمة، اسم…" value="${esc(F.text)}" style="margin:0">
      <div class="filters">
        <select id="fm"><option value="">كل المقامات</option>${MAQAM_KEYS.map(k => `<option value="${k}" ${F.maqam === k ? 'selected' : ''}>${MAQAMAT[k].ar}</option>`).join('')}</select>
        <input id="bmin" inputmode="numeric" placeholder="BPM من" value="${esc(F.bmin)}">
        <input id="bmax" inputmode="numeric" placeholder="لحد" value="${esc(F.bmax)}">
        <button class="tag ${F.star ? 'on' : ''}" id="fstar">⭐</button>
      </div>
      <div class="tags" id="ftags">${allTags().map(t => `<button class="tag ${F.tags.includes(t) ? 'on' : ''}" data-ft="${esc(t)}">${esc(t)}</button>`).join('')}</div>
    </div>
    <div id="list"></div>`;
  const list = () => {
    const r = filtered();
    $('#list').innerHTML = r.length ? r.map(cardHtml).join('') : `<div class="empty">${ideas.length ? 'مفيش أفكار بالفلتر ده' : 'لسه مفيش أفكار.<br>سجّل أول فكرة من «سجّل».'}</div>`;
    bindCards($('#list'));
  };
  $('#q').oninput = e => { F.text = e.target.value; list(); };
  $('#fm').onchange = e => { F.maqam = e.target.value; list(); };
  $('#bmin').oninput = e => { F.bmin = e.target.value; list(); };
  $('#bmax').oninput = e => { F.bmax = e.target.value; list(); };
  $('#fstar').onclick = e => { F.star = !F.star; e.target.classList.toggle('on'); list(); };
  view.querySelectorAll('[data-ft]').forEach(b => b.onclick = () => { const t = b.dataset.ft, i = F.tags.indexOf(t); i < 0 ? F.tags.push(t) : F.tags.splice(i, 1); b.classList.toggle('on'); list(); });
  list();
}

// ---------- idea detail + develop ----------
const DEV = { mode: 'cont', len: 'same', opts: null, forId: null };
async function viewIdea(id) {
  const i = ideas.find(x => x.id === id);
  if (!i) { location.hash = '#/lib'; return; }
  setTitle(i.title); setNav('lib');
  const an = i.an, parent = i.parentId && ideas.find(x => x.id === i.parentId), kids = ideas.filter(x => x.parentId === i.id);
  if (DEV.forId !== id) { DEV.forId = id; DEV.opts = null; }
  view.innerHTML = `
    <div class="row" style="margin-top:0;align-items:center"><div style="flex:1;color:var(--muted);font-size:14px">${fmtDate(i.created)}${parent ? ` · من «<a href="#/idea/${parent.id}" style="color:var(--gold)">${esc(parent.title)}</a>»` : ''}</div>
      <button class="btn small ${i.star ? 'on' : ''}" id="star">${i.star ? '⭐ مميزة' : '☆ ميّز'}</button></div>
    ${i.note ? `<p style="margin:10px 0;line-height:1.8">${esc(i.note)}</p>` : ''}
    ${(i.tags || []).length ? `<div class="tags" style="margin-bottom:10px">${i.tags.map(t => `<span class="tag">${esc(t)}</span>`).join('')}</div>` : ''}
    ${analysisHtml(an)}
    <div class="row">${i.kind !== 'gen' ? '<button class="btn grow" data-act="voice">▶ صوتي</button>' : ''}<button class="btn grow" data-act="oud">▶ عود</button><button class="btn" data-act="stop">■</button></div>
    <div class="row"><button class="btn grow" id="midi">⬇ MIDI</button><button class="btn grow" id="copy">⧉ النوتة</button>${i.kind !== 'gen' ? '<button class="btn grow" id="wav">⬇ الصوت</button>' : ''}</div>
    <h2>طوّر الفكرة</h2>
    <div class="seg"><button data-mode="cont" class="${DEV.mode === 'cont' ? 'on' : ''}">كمّل اللحن</button><button data-mode="vary" class="${DEV.mode === 'vary' ? 'on' : ''}">التنويع</button></div>
    ${DEV.mode === 'cont' ? `<div class="row"><button class="tag ${DEV.len === 'same' ? 'on' : ''}" data-len="same">نفس طول الجملة</button><button class="tag ${DEV.len === 'bars2' ? 'on' : ''}" data-len="bars2">مازورتين</button></div>` : ''}
    <div class="row"><button class="btn gold grow" id="gen">${DEV.mode === 'cont' ? 'اقترح ٥ تكملات' : 'اعمل التنويعات'}</button></div>
    <div id="opts" style="margin-top:12px"></div>
    ${kids.length ? `<h2>التطويرات المحفوظة</h2><div id="kids">${kids.map(cardHtml).join('')}</div>` : ''}
    <h2>تعديل</h2>
    <div class="card">
      <label class="f">الاسم<input class="t" id="eTitle" value="${esc(i.title)}"></label>
      <label class="f">ملاحظة<textarea class="t" id="eNote">${esc(i.note || '')}</textarea></label>
      <label class="f">تاجات</label>${tagPicker(i.tags || (i.tags = []))}
      <div class="row"><button class="btn gold grow" id="eSave">احفظ التعديل</button><button class="btn danger" id="del">امسح الفكرة</button></div>
    </div>`;
  drawRoll($('#roll'), an);
  bindTagPicker(i.tags);
  const save = async () => { await DB.putIdea(i); await loadIdeas(); };
  bindCommon(an, async () => { DEV.opts = null; await save(); viewIdea(id); }, { voice: () => playVoice(i.id) });
  $('#star').onclick = async () => { i.star = !i.star; await save(); viewIdea(id); };
  $('#midi').onclick = () => shareFile(midiBlob(an.notes, an.bpm, i.title), safeName(i.title) + '.mid');
  $('#copy').onclick = async () => { try { await navigator.clipboard.writeText(notation(an.notes, keyOf(an))); toast('النوتة اتنسخت'); } catch { toast('مقدرتش أنسخ'); } };
  $('#wav') && ($('#wav').onclick = async () => { const a = await DB.getAudio(i.id); a && shareFile(new Blob([a.buf], { type: a.mime }), safeName(i.title) + '.' + (i.audio?.ext || extOf(a.mime))); });
  view.querySelectorAll('[data-mode]').forEach(b => b.onclick = () => { DEV.mode = b.dataset.mode; DEV.opts = null; viewIdea(id); });
  view.querySelectorAll('[data-len]').forEach(b => b.onclick = () => { DEV.len = b.dataset.len; DEV.opts = null; viewIdea(id); });
  $('#gen').onclick = () => {
    if (!an.maqam || an.notes.length < 2) return toast('محتاج مقام ونغمتين على الأقل');
    const ph = toPhrase(an);
    const list = DEV.mode === 'cont' ? continuations(ph, { len: DEV.len }) : variations(ph);
    const base = toNotes(ph, ph.seq), baseEnd = ph.seq.reduce((a, n) => a + n.r, 0) * ph.u;
    DEV.opts = list.map(o => {
      const notes = toNotes(ph, o.seq, DEV.mode === 'cont' ? baseEnd : 0);
      return { ...o, notes, full: DEV.mode === 'cont' ? [...base, ...notes] : notes };
    });
    renderOpts();
  };
  $('#kids') && bindCards($('#kids'));
  $('#eSave').onclick = async () => { i.title = $('#eTitle').value.trim() || i.title; i.note = $('#eNote').value.trim(); await save(); toast('اتحفظ ✓'); viewIdea(id); };
  $('#del').onclick = async () => {
    if (!confirm(`تمسح «${i.title}»؟${kids.length ? ' (التطويرات بتاعتها هتفضل)' : ''}`)) return;
    await DB.deleteIdea(i.id); await loadIdeas(); location.hash = '#/lib';
  };
  function renderOpts() {
    const key = keyOf(an), box = $('#opts');
    if (!DEV.opts) { box.innerHTML = ''; return; }
    box.innerHTML = DEV.opts.map((o, k) => `<div class="card opt">
      <h4>${k + 1}. ${esc(o.label)}${o.score != null ? `<small>${o.score}</small>` : ''}</h4>
      <div class="nota">${DEV.mode === 'cont' ? `<span style="opacity:.55">${esc(notation(an.notes, key))}</span>  |  ` : ''}${esc(notation(o.notes, key))}</div>
      <div class="row"><button class="btn small grow" data-op="play" data-k="${k}">▶ ${DEV.mode === 'cont' ? 'مع الأصل' : 'اسمع'}</button>
        ${DEV.mode === 'cont' ? `<button class="btn small" data-op="solo" data-k="${k}">▶ لوحدها</button>` : ''}
        <button class="btn small" data-op="star" data-k="${k}">⭐</button><button class="btn small" data-op="save" data-k="${k}">احفظ</button><button class="btn small" data-op="midi" data-k="${k}">MIDI</button></div></div>`).join('');
    box.querySelectorAll('[data-op]').forEach(b => b.onclick = async () => {
      const o = DEV.opts[+b.dataset.k], op = b.dataset.op;
      if (op === 'play') playNotes(o.full);
      else if (op === 'solo') playNotes(o.notes.map(n => ({ ...n, s: n.s - o.notes[0].s })));
      else if (op === 'midi') shareFile(midiBlob(o.full, an.bpm, `${i.title} - ${o.label}`), safeName(`${i.title} - ${o.label}`) + '.mid');
      else {
        const notes = o.full.map(n => ({ ...n, c: n.q }));
        const end = notes.reduce((a, n) => Math.max(a, n.s + n.d), 0);
        const child = { id: uid(), created: Date.now(), kind: 'gen', parentId: i.id, title: `${i.title} — ${o.label.split(' · ')[0]}`, note: o.label, tags: [...(i.tags || [])], star: op === 'star',
          an: { notes, maqam: an.maqam, tonic: an.tonic, raw: an.tonic, bpm: an.bpm, dur: +end.toFixed(2), range: range(notes), cands: [{ maqam: an.maqam, tonic: an.tonic, raw: an.tonic, p: 1 }], conf: 1, manual: an.manual } };
        await DB.putIdea(child); await loadIdeas();
        toast(op === 'star' ? 'اتحفظت ⭐' : 'اتحفظت كتطوير');
        b.disabled = true; b.textContent = '✓';
      }
    });
  }
  renderOpts();
}

// ---------- backup ----------
async function exportBackup() {
  busy(true, 'بجهّز النسخة…');
  try {
    const enc = new TextEncoder(), files = [], lines = [];
    for (const i of ideas) {
      if (i.kind !== 'gen') { const a = await DB.getAudio(i.id); if (a) files.push({ name: `audio/${i.id}.${i.audio?.ext || extOf(a.mime)}`, data: new Uint8Array(a.buf) }); }
      if (i.an && i.an.notes.length) files.push({ name: `midi/${safeName(i.title)}-${i.id}.mid`, data: toMidi(i.an.notes, { bpm: i.an.bpm || 90, name: i.title }) });
      lines.push(`${i.title}\t${i.an?.maqam ? keyAr(i.an.maqam, i.an.tonic) : ''}\t${notation(i.an?.notes || [], keyOf(i.an))}`);
    }
    files.unshift({ name: 'ideas.json', data: enc.encode(JSON.stringify({ app: 'melody-bank', v: 1, exported: new Date().toISOString(), ideas }, null, 1)) });
    files.push({ name: 'notation.txt', data: enc.encode(lines.join('\n')) });
    const d = new Date(), name = `melody-bank-${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}.zip`;
    busy(false);
    await shareFile(makeZip(files), name);
  } catch (e) { busy(false); toast('مقدرتش أعمل النسخة: ' + e.message, 4000); }
}
async function importBackup(file) {
  busy(true, 'باسترجع…');
  try {
    const entries = await readZip(await file.arrayBuffer()), dec = new TextDecoder();
    const js = entries.find(e => e.name === 'ideas.json');
    if (!js) throw new Error('الملف مش نسخة من بنك الألحان');
    const data = JSON.parse(dec.decode(js.data));
    let n = 0;
    for (const i of data.ideas || []) {
      const a = entries.find(e => e.name.startsWith(`audio/${i.id}.`));
      if (a) await DB.putAudio(i.id, i.audio?.mime || 'audio/mp4', a.data.buffer.slice(a.data.byteOffset, a.data.byteOffset + a.data.byteLength));
      await DB.putIdea(i); n++;
    }
    await loadIdeas(); busy(false); toast(`رجعت ${n} فكرة ✓`); route();
  } catch (e) { busy(false); toast('الاسترجاع فشل: ' + e.message, 4000); }
}
$('#menuBtn').onclick = () => {
  const sh = openSheet(`<h3>النسخة الاحتياطية</h3>
    <p class="hint" style="text-align:start;line-height:1.8">كل الأفكار محفوظة على الموبايل ده بس. صدّر نسخة (.zip فيها الصوت وJSON وMIDI) واحفظها على Google Drive أو الكمبيوتر.</p>
    <div class="row"><button class="btn gold grow" id="bx">⬇ صدّر نسخة</button><button class="btn grow" id="bi">⬆ استرجع نسخة</button></div>
    <p class="hint" style="text-align:start;margin-top:14px">${ideas.length} فكرة · <span id="quota"></span></p>
    <div class="row"><button class="btn ghost grow" data-close>قفل</button></div>`);
  sh.querySelector('#bx').onclick = () => { closeSheet(); exportBackup(); };
  sh.querySelector('#bi').onclick = () => { closeSheet(); $('#importFile').click(); };
  navigator.storage?.estimate?.().then(e => { const q = sh.querySelector('#quota'); if (q) q.textContent = `مستخدم ${(e.usage / 1048576).toFixed(1)} ميجا`; });
};
$('#importFile').onchange = e => { const f = e.target.files[0]; e.target.value = ''; if (f) importBackup(f); };

// ---------- router ----------
function route() {
  stop(); closeSheet();
  const h = location.hash || '#/';
  if (h.startsWith('#/idea/')) viewIdea(decodeURIComponent(h.slice(7)));
  else if (h === '#/lib') viewLib();
  else if (h === '#/save') viewSave();
  else viewRec();
  window.scrollTo(0, 0);
}
window.addEventListener('hashchange', route);

(async () => {
  await loadIdeas();
  route();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(() => { });
})();

// debug hook for automated tests
window.__mb = { DB, loadIdeas, get ideas() { return ideas; } };
