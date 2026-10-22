// Entry point: scene, material, bridge, events, rail, pages and the settings sheet.

import { connect, call, on, goLive, backendMode, reportError } from './api.js';
import { store, notify, watch, sortedModules, moduleById, anyJobRunning } from './store.js';
import { initBackground, setFocused } from '../vendor/liquid_ui/js/background.js';
import { applyTheme } from '../vendor/liquid_ui/js/theme.js';
import { initLight, gel, Lens } from '../vendor/liquid_ui/js/glass.js';
import { SPRINGS, springAnimate, reducedMotion } from '../vendor/liquid_ui/js/motion.js';
import { initToasts, toast } from '../vendor/liquid_ui/js/toast.js';
import { mountUpdateControl } from '../vendor/liquid_ui/js/update.js';
import { icon, appGlyph, pageIcon } from './icons.js';
import { askConfirm } from './confirm.js';
import { h } from './ui.js';
import { mountDeck } from './pages/deck.js';
import { mountModulePage } from './pages/module.js';
import { initSettings, openSettings } from './settings.js';

const THEME_KEY = 'pd.theme';
const PAGE_KEY = 'pd.page';

// ---- theme ------------------------------------------------------------------------------------

/** Apply the confirmed theme and mirror it in localStorage so index.html sets it before the first paint. */
function syncTheme(settings) {
  if (!settings) return;
  const name = applyTheme(settings.theme);
  try {
    localStorage.setItem(THEME_KEY, name);
  } catch {
    /* storage unavailable: the next start begins on the default and switches after connect */
  }
}

// ---- title bar --------------------------------------------------------------------------------

function initTitlebar() {
  document.getElementById('tb-glyph').innerHTML = appGlyph(22);
  const cluster = document.getElementById('tb-cluster');
  new Lens(cluster, 'capsule');
  document.getElementById('btn-settings').innerHTML = icon('gear');
  document.getElementById('win-min').innerHTML = icon('min');
  document.getElementById('win-close').innerHTML = icon('close');
  for (const b of cluster.querySelectorAll('button')) gel(b, { amount: 0.7 });
  const win = { 'win-min': 'window_minimize', 'win-close': 'window_close' };
  for (const [id, method] of Object.entries(win)) {
    document.getElementById(id).addEventListener('click', () => call(method).catch((err) => toast('error', err.message)));
  }
}

function entrance() {
  if (reducedMotion.matches) return;
  const parts = [
    [document.querySelector('.tb-brand'), 0],
    [document.getElementById('ver-pill'), 50],
    [document.getElementById('tb-cluster'), 90],
  ];
  for (const [el, delay] of parts) {
    if (!el) continue;
    const own = !!el.__lens;
    springAnimate(el, [
      { opacity: own ? 0 : 1, transform: 'translateY(-10px) scale(0.96)' },
      { opacity: 1, transform: 'none' },
    ], SPRINGS.bouncy, { delay, fill: 'backwards' });
    if (own) {
      el.__lens.setStrength(0);
      setTimeout(() => el.__lens.animateStrength(1), delay);
    }
  }
}

// ---- update control ---------------------------------------------------------------------------

function initUpdate(state) {
  const ctl = mountUpdateControl({
    pill: document.getElementById('ver-pill'),
    popoverHost: document.getElementById('update-pop'),
    call,
    on,
    options: {
      product: 'Premiere Deck',
      version: state.version || '',
      status: state.update || null,
      settings: state.settings,
      hasActiveWork: anyJobRunning,
      restartWarning: 'Running jobs stop when the app restarts',
      onStatus: (u) => {
        store.update = u;
        notify('update', u);
      },
      onSettings: (s) => {
        store.settings = s;
        notify('settings', s);
      },
    },
  });
  // Settings confirmed elsewhere (the `settings` event, the settings sheet) reach the control.
  watch('settings', (s) => ctl.setSettings(s));
  return ctl;
}

// ---- rail and pages ---------------------------------------------------------------------------

const rail = { items: new Map() };
let current = null; // { id, inst }

function railItem(id) {
  let it = rail.items.get(id);
  if (it) return it;
  const label = h('span', 'ri-label');
  const sub = h('span', 'ri-sub');
  const dot = h('i', 'ri-dot');
  const el = h('button', 'rail-item', { type: 'button', 'data-page': id },
    h('span', 'ri-icon', { html: icon(pageIcon(id)) }), h('span', 'ri-text', null, label, sub), dot);
  gel(el, { amount: 0.5 });
  el.addEventListener('click', () => showPage(id));
  it = { el, label, sub, dot };
  rail.items.set(id, it);
  return it;
}

const RAIL_SUB = { missing: 'Not installed in this build', failed: 'Failed', import_failed: 'Could not be loaded', disabled: 'Turned off', stopped: 'Stopped' };

function paintRail() {
  const list = document.getElementById('rail-list');
  const order = [{ id: 'deck', title: 'Deck' }, ...sortedModules()];
  const seen = new Set();
  let prev = null;
  for (const m of order) {
    seen.add(m.id);
    const it = railItem(m.id);
    it.label.textContent = m.title;
    const state = m.state;
    it.sub.textContent = RAIL_SUB[state] || '';
    it.sub.hidden = !it.sub.textContent;
    it.dot.className = `ri-dot ${state === 'running' ? 't-ok' : state === 'failed' || state === 'import_failed' ? 't-error' : ''}`;
    it.dot.hidden = !state;
    it.el.classList.toggle('is-missing', state === 'missing');
    it.el.classList.toggle('is-current', current && current.id === m.id);
    if (current && current.id === m.id) it.el.setAttribute('aria-current', 'page');
    else it.el.removeAttribute('aria-current');
    const want = prev ? prev.nextSibling : list.firstChild;
    if (it.el !== want) list.insertBefore(it.el, want);
    prev = it.el;
  }
  for (const [id, it] of Array.from(rail.items)) {
    if (seen.has(id)) continue;
    it.el.remove();
    rail.items.delete(id);
  }
}

function pageExists(id) {
  return id === 'deck' || !!moduleById(id);
}

/** Open a page; the id `settings` opens the settings sheet over the current page instead. */
function showPage(id, { remember = true } = {}) {
  if (id === 'settings') {
    openSettings();
    return;
  }
  if (!pageExists(id)) id = 'deck';
  if (current && current.id === id) return;
  if (current) current.inst.destroy();
  const host = document.getElementById('page');
  host.replaceChildren();
  host.scrollTop = 0;
  let inst;
  if (id === 'deck') inst = mountDeck(host, { navigate: showPage });
  else inst = mountModulePage(host, id);
  current = { id, inst };
  store.page = id;
  host.dataset.page = id;
  if (!reducedMotion.matches) host.firstElementChild && springAnimate(host.firstElementChild, [{ opacity: 0, transform: 'translateY(10px)' }, { opacity: 1, transform: 'none' }], SPRINGS.smooth);
  paintRail();
  if (remember) {
    try {
      localStorage.setItem(PAGE_KEY, id);
    } catch { /* ignore */ }
  }
}

// ---- events -----------------------------------------------------------------------------------

function wireEvents() {
  on('premiere', (p) => {
    if (!p) return;
    store.premiere = p;
    notify('premiere', p);
  });
  on('modules', (ms) => {
    if (!Array.isArray(ms)) return;
    store.modules = ms;
    notify('modules', ms);
  });
  watch('modules', paintRail);
  on('job', (j) => {
    if (!j) return;
    if (j.state === 'running' || j.state === 'queued') store.jobs.set(j.id, j);
    else store.jobs.delete(j.id);
    notify('jobs', store.jobs);
  });
  on('activity', (a) => {
    if (!a) return;
    store.activity = [a, ...store.activity].slice(0, 20);
    notify('activity', store.activity);
  });
  on('settings', (s) => {
    if (!s) return;
    store.settings = s;
    notify('settings', s);
  });
  on('panel', (p) => {
    if (!p) return;
    store.panel = p;
    notify('panel', p);
  });
  on('toast', (t) => t && toast(t.kind || 'info', t.message));
  on('window', (w) => w && setFocused(!!w.focused));
  on('navigate', (n) => n && n.page && showPage(n.page));
  on('confirm', async (c) => {
    if (!c) return;
    const mod = c.module ? moduleById(c.module) : null;
    const yes = await askConfirm({ question: c.question, title: mod ? mod.title : '' });
    try {
      await call('answer_confirm', c.id, yes);
    } catch (err) {
      toast('error', err.message);
    }
  });
}

// ---- boot -------------------------------------------------------------------------------------

async function boot() {
  initBackground(document.getElementById('scene'));
  initLight();
  initToasts();
  initTitlebar();

  let state;
  try {
    state = await connect();
  } catch (err) {
    console.error(err);
    toast('error', `Could not connect to the app: ${err.message}`, { sticky: true });
    return;
  }
  document.documentElement.dataset.backend = backendMode();
  Object.assign(store, {
    version: state.version || '',
    fake: !!state.fake,
    dataDir: state.data_dir || '',
    premiere: state.premiere,
    panel: state.panel,
    install: state.install || null,
    modules: state.modules || [],
    recent: state.recent || [],
    activity: (state.activity || []).slice(0, 20),
    ffmpeg: state.ffmpeg,
    settings: state.settings,
    update: state.update,
  });
  store.jobs = new Map((state.jobs || []).filter((j) => j.state === 'running' || j.state === 'queued').map((j) => [j.id, j]));
  syncTheme(state.settings);
  watch('settings', syncTheme);

  initSettings({ update: initUpdate(state) });
  wireEvents();

  paintRail();
  let start = 'deck';
  try {
    start = localStorage.getItem(PAGE_KEY) || 'deck';
  } catch { /* ignore */ }
  showPage(start, { remember: false });

  if (state.window) setFocused(state.window.focused !== false);
  goLive();
  document.body.classList.add('is-ready');
  entrance();
  const inst = state.install;
  if (inst && (inst.action === 'failed' || inst.action === 'no_source') && inst.message) toast('warning', inst.message);
  if (backendMode() === 'mock') console.info('[ui] running against js/mock.js');
}

addEventListener('error', (e) => reportError(`${e.message} (${e.filename || ''}:${e.lineno || 0})`));
addEventListener('unhandledrejection', (e) => {
  const msg = e.reason && e.reason.message ? e.reason.message : String(e.reason);
  reportError(`unhandled rejection: ${msg}`);
  toast('error', msg);
});

boot();
