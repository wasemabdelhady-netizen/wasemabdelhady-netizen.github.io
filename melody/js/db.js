// IndexedDB: ideas (metadata + analysis) and audio (ArrayBuffer + mime). Everything stays on the phone.
let dbp;
function db() {
  return dbp ||= new Promise((res, rej) => {
    const r = indexedDB.open('melody-bank', 1);
    r.onupgradeneeded = () => { const d = r.result; d.createObjectStore('ideas', { keyPath: 'id' }); d.createObjectStore('audio', { keyPath: 'id' }); };
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
}
async function tx(store, mode, fn) {
  const d = await db();
  return new Promise((res, rej) => {
    const t = d.transaction(store, mode), s = t.objectStore(store), r = fn(s);
    t.oncomplete = () => res(r && r.result); t.onerror = () => rej(t.error); t.onabort = () => rej(t.error);
  });
}
export const allIdeas = () => tx('ideas', 'readonly', s => s.getAll());
export const getIdea = id => tx('ideas', 'readonly', s => s.get(id));
export const putIdea = idea => tx('ideas', 'readwrite', s => s.put(idea));
export const getAudio = id => tx('audio', 'readonly', s => s.get(id));
export const putAudio = (id, mime, buf) => tx('audio', 'readwrite', s => s.put({ id, mime, buf }));
export async function deleteIdea(id) { await tx('ideas', 'readwrite', s => s.delete(id)); await tx('audio', 'readwrite', s => s.delete(id)); }
export async function persist() { try { return navigator.storage && navigator.storage.persist ? await navigator.storage.persist() : false; } catch { return false; } }
