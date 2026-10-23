// Entry point: scene, material, icon sprite, bridge, events, the compact and wide layouts, the settings sheet.
//
// The window mode follows the shell (docs/BRIDGE_API.md 4 `window`): `compact` shows idle / waiting
// (js/pages/compact.js), `wide` shows main (js/pages/wide.js). The page never asks for a size; on a `window`
// event with `ms` > 0 it animates its layout in the same time as the native resize (docs/HOST_UI.md 2).

import { connect, call, on, goLive, backendMode, reportError } from './api.js';
import { store, notify, watch, moduleById, anyJobRunning } from './store.js';
import { initBackground, setFocused } from '../vendor/liquid_ui/js/background.js';
import { applyTheme } from '../vendor/liquid_ui/js/theme.js';
import { initLight, gel, Lens } from '../vendor/liquid_ui/js/glass.js';
import { SPRINGS, springAnimate, reducedMotion } from '../vendor/liquid_ui/js/motion.js';
import { initToasts, toast } from '../vendor/liquid_ui/js/toast.js';
import { mountUpdateControl } from '../vendor/liquid_ui/js/update.js';
import { loadSprite, ic, glyph } from './icons.js';
import { askConfirm } from './confirm.js';
import { mountCompact } from './pages/compact.js';
import { mountWide } from './pages/wide.js';
import { initSettings, openSettings, closeSettings } from './settings.js';

const THEME_KEY = 'pd.theme';
const FAKE_KEYS = { 1: 'not_running', 2: 'waiting', 3: 'connected' };

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
  document.getElementById('tb-glyph').innerHTML = glyph(22);
  const cluster = document.getElementById('tb-cluster');
  new Lens(cluster, 'capsule');
  document.getElementById('btn-settings').innerHTML = ic('settings');
  document.getElementById('win-min').innerHTML = ic('minimize');
  document.getElementById('win-close').innerHTML = ic('close');
  for (const b of cluster.querySelectorAll('button')) gel(b, { amount: 0.7 });
  const win = { 'win-min': 'window_minimize', 'win-close': 'window_close' };
  for (const [id, method] of Object.entries(win)) {
    document.getElementById(id).addEventListener('click', () => call(method).catch((err) => toast('error', err.message)));
  }
}

function entrance() {
  if (reducedMotion.matches) return;
  const parts = [
    [document.getElementById('tb-cluster'), 60],
    [document.querySelector('#compact .hero-block'), 0],
  ];
  for (const [el, delay] of parts) {
    if (!el || el.offsetParent === null) continue;
    const own = !!el.__lens;
    springAnimate(el, [
      { opacity: own ? 0 : 1, transform: 'translateY(-8px) scale(0.97)' },
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

// ---- layout modes -------------------------------------------------------------------------------

const ui = { compact: null, wide: null, pill: null };
let modeSeq = 0;

function screenOf() {
  if (store.mode === 'wide') return 'main';
  const st = store.premiere ? store.premiere.state : 'not_running';
  return st === 'not_running' ? 'idle' : 'waiting';
}

function paintScreen() {
  document.documentElement.dataset.screen = screenOf();
}

function placePill(where) {
  const pill = ui.pill;
  if (where === 'titlebar') document.getElementById('tb-pill-slot').append(pill);
  else ui.compact.paint(); // the idle hero block takes it back
}

function showCompact(animate) {
  const root = document.documentElement;
  root.dataset.mode = 'compact';
  document.getElementById('wide').hidden = true;
  document.getElementById('compact').hidden = false;
  ui.wide.reset();
  placePill('hero');
  paintScreen();
  if (animate) ui.compact.enter();
}

/**
 * The `window` event: `wide` switches to the main layout at once and plays the pane's entrance in `ms`; `compact`
 * plays it backwards first, then shows the compact page. `ms` 0 (a show from the tray, focus changes, reduced
 * motion) switches at once.
 */
function applyMode(mode, ms) {
  const root = document.documentElement;
  const animate = ms > 0 && !reducedMotion.matches;
  const seq = ++modeSeq;
  const was = root.dataset.mode;
  store.mode = mode;
  if (mode === 'wide') {
    root.dataset.mode = 'wide';
    document.getElementById('compact').hidden = true;
    document.getElementById('wide').hidden = false;
    placePill('titlebar');
    paintScreen();
    ui.wide.enter(animate && was !== 'wide' ? ms : 0);
    const brand = document.querySelector('.tb-brand');
    if (animate && was !== 'wide') brand.animate([{ opacity: 0, transform: 'translateX(-8px)' }, { opacity: 1, transform: 'none' }], { duration: ms, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    return;
  }
  if (animate && was === 'wide') {
    ui.wide.leave(ms).then(() => {
      if (seq === modeSeq) showCompact(true);
    });
    return;
  }
  showCompact(false);
}

// ---- events -----------------------------------------------------------------------------------

function addActivity(a) {
  const line = { at: Date.now() / 1000, module: null, tone: 'neutral', ...a };
  store.activity = [line, ...store.activity].slice(0, 20);
  notify('activity', store.activity);
}

function navigate(page) {
  if (!page) return;
  if (page === 'settings') {
    openSettings();
    return;
  }
  closeSettings();
  if (page === 'deck') return;
  if (moduleById(page)) ui.wide.showTab(page);
}

function wireEvents() {
  on('premiere', (p) => {
    if (!p) return;
    store.premiere = p;
    notify('premiere', p);
    paintScreen();
  });
  on('modules', (ms) => {
    if (!Array.isArray(ms)) return;
    store.modules = ms;
    notify('modules', ms);
  });
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
  on('result', (r) => r && r.id && ui.wide.flashNode(r.module, r.id, r.outcome));
  on('window', (w) => {
    if (!w) return;
    if (w.focused != null) setFocused(!!w.focused);
    if (w.mode && (w.mode !== store.mode || (w.ms || 0) > 0)) applyMode(w.mode, w.ms || 0);
  });
  on('navigate', (n) => n && navigate(n.page));
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

/** `--fake` and the mock only: Alt+1 / Alt+2 / Alt+3 simulate Premiere not running / waiting / connected. */
function wireFakeKeys() {
  if (!store.fake && backendMode() !== 'mock') return;
  addEventListener('keydown', (e) => {
    const state = e.altKey && !e.ctrlKey && !e.shiftKey ? FAKE_KEYS[e.key] : null;
    if (!state) return;
    e.preventDefault();
    call('fake_premiere', state).catch((err) => toast('error', err.message));
  });
}

// ---- boot -------------------------------------------------------------------------------------

async function boot() {
  await loadSprite();
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

  const update = initUpdate(state);
  initSettings({ update });
  ui.pill = document.getElementById('ver-pill');
  ui.compact = mountCompact(document.getElementById('compact'), { pill: ui.pill, update, openSettings, addActivity });
  ui.wide = mountWide(document.getElementById('wide'));
  wireEvents();
  wireFakeKeys();

  const win = state.window || {};
  applyMode(win.mode === 'wide' ? 'wide' : 'compact', 0);
  setFocused(win.focused !== false);
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
