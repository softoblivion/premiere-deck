// Shared page state: what the bridge last confirmed (State and events). Components read from here
// and subscribe to the keys they show; js/main.js is the only writer of the bridge-driven keys.

export const store = {
  version: '',
  fake: false,
  dataDir: '',
  premiere: null,
  panel: null,
  install: null,
  modules: [],
  recent: [],
  activity: [],
  jobs: new Map(), // id -> Job (queued / running only)
  ffmpeg: null,
  settings: null,
  update: null,
  page: 'deck',
};

const subs = new Map();

/** Subscribe to a key; returns an unsubscribe function. */
export function watch(key, fn) {
  if (!subs.has(key)) subs.set(key, new Set());
  subs.get(key).add(fn);
  return () => subs.get(key).delete(fn);
}

export function notify(key, detail) {
  const set = subs.get(key);
  if (!set) return;
  for (const fn of Array.from(set)) {
    try {
      fn(detail);
    } catch (err) {
      console.error(`[ui] store watcher for "${key}" failed`, err);
    }
  }
}

/** Modules sorted by `order`, then title. */
export function sortedModules() {
  return [...store.modules].sort((a, b) => (a.order - b.order) || a.title.localeCompare(b.title));
}

export function moduleById(id) {
  return store.modules.find((m) => m.id === id) || null;
}

/** True while any job is queued or running. */
export function anyJobRunning() {
  for (const j of store.jobs.values()) if (j.state === 'running' || j.state === 'queued') return true;
  return false;
}
