// The compact window (docs/HOST_UI.md 3.1 idle, 3.2 waiting): one column under the title bar. Idle: the glyph,
// "Premiere Deck" with the version pill, the status line, the hero pill "Launch Premiere Pro" and the Recent
// projects card. Waiting: the glyph with two breathing rings, "Waiting for the panel", the status line, the help
// card with the menu path as key caps, three secondary tiles and Recent projects collapsed. `connected` while the
// window is still compact (just before the widen): "Panel connected" with a green ring.

import { Lens, gel } from '../../vendor/liquid_ui/js/glass.js';
import { toast } from '../../vendor/liquid_ui/js/toast.js';
import { call } from '../api.js';
import { store, notify, watch } from '../store.js';
import { ic, glyph } from '../icons.js';
import { askConfirm } from '../confirm.js';
import { h, failToast, isoToEpoch, ago, flash, noticeMs, closeOnClick } from '../ui.js';

const COLLAPSED_KEY = 'pd.recent.collapsed';
const RECENT_ROWS = 3;
const UPDATE_WAIT_MS = 30000;

function readCollapsed() {
  try {
    return localStorage.getItem(COLLAPSED_KEY) === '1';
  } catch {
    return false;
  }
}

function writeCollapsed(v) {
  try {
    localStorage.setItem(COLLAPSED_KEY, v ? '1' : '0');
  } catch { /* storage unavailable: the state lasts for this run */ }
}

/** Premiere's version without the fake marker of `--fake` ("26.0.0 (fake)" -> "26.0.0"). */
export function premiereVersion(p) {
  const v = p && (p.premiere_version || (p.installs || []).find((i) => i.exe === p.exe)?.version);
  return v ? String(v).replace(/\s*\(fake\)$/, '') : '';
}

/**
 * @param {HTMLElement} host  the `#compact` element
 * @param {{pill: HTMLElement, update: object|null, openSettings: Function, addActivity: Function}} o
 */
export function mountCompact(host, { pill, update, openSettings, addActivity }) {
  // ---- hero block -----------------------------------------------------------------------------
  const glyphWrap = h('div', 'glyph-wrap', null, h('span', 'ring'), h('span', 'ring'), h('span', 'glyph-in', { html: glyph(84) }));
  const pillSlot = h('span', 'pill-slot');
  const appName = h('div', 'app-name', null, h('span', '', { text: 'Premiere Deck' }), pillSlot);
  const stateTitle = h('h1', 'state-title');
  const statusText = h('span', 'status-text');
  const status = h('div', 'status-line t-neutral', null, h('span', 'dot'), statusText);
  const heroBlock = h('div', 'hero-block', null, glyphWrap, appName, stateTitle, status);

  // ---- launch pill (idle) ---------------------------------------------------------------------
  const heroText = h('span', 'pm-text', { text: 'Launch Premiere Pro' });
  const hero = h('button', 'hero', { type: 'button', 'data-state': 'ready' },
    h('span', 'pm-layer pm-label', null, h('span', 'ic-hold', { html: ic('launch') }), heroText),
    h('span', 'pm-layer pm-spin', null, h('span', 'spinner'), h('span', 'pm-spin-t', { text: 'Starting Premiere Pro' })));
  const heroLens = new Lens(hero, 'pill');
  gel(hero, { amount: 0.6 });

  // ---- help card and tiles (waiting) ----------------------------------------------------------
  const keys = h('div', 'path', { 'aria-label': 'Window, Extensions, Premiere Deck' },
    h('span', 'key', { text: 'Window' }), h('span', 'sep', { html: ic('chevron') }),
    h('span', 'key', { text: 'Extensions' }), h('span', 'sep', { html: ic('chevron') }),
    h('span', 'key', { text: 'Premiere Deck' }));
  const fallback = h('div', 'help-fallback', { hidden: true });
  const help = h('div', 'help', null, h('p', '', { text: 'Open the panel once; Premiere keeps it in the workspace.' }), keys, fallback);
  // The hint card and the debug-mode offer are notices: a click outside their buttons closes them until the screen
  // changes (HOST_UI.md 7). The offer closes on its own; the rest of the card closes with the card.
  let helpClosed = false;
  let fallbackClosed = false;
  closeOnClick(help, () => {
    helpClosed = true;
    help.hidden = true;
  });
  closeOnClick(fallback, (e) => {
    e.stopPropagation();
    fallbackClosed = true;
    fallback.hidden = true;
  });

  const tile = (icon, label) => {
    const b = h('button', 'wtile', { type: 'button' }, h('span', 'wt-ic', { 'data-ic': '', html: ic(icon) }), h('span', 'wt-label', { text: label }));
    gel(b, { amount: 0.8 });
    return b;
  };
  const tPanel = tile('panel-update', 'Check panel update');
  const tUpdate = tile('update', 'Check for updates');
  const tReinstall = tile('reinstall', 'Reinstall panel');
  const tiles = h('div', 'tiles3', null, tPanel, tUpdate, tReinstall);
  const tileResult = h('div', 'tile-result', { 'aria-live': 'polite' });
  let resultTimer = 0;
  const clearResult = () => {
    clearTimeout(resultTimer);
    resultTimer = 0;
    tileResult.replaceChildren();
  };
  // A result line goes away after 3 s (10 s for warn and error) and on a click (HOST_UI.md 7).
  closeOnClick(tileResult, clearResult);

  // ---- recent projects ------------------------------------------------------------------------
  let collapsedIdle = readCollapsed();
  let collapsedWaiting = true;
  const count = h('span', 'count');
  const pickBtn = h('button', 'icon-btn icon-btn-s rh-pick', { type: 'button', 'aria-label': 'Choose project', title: 'Choose project', html: ic('folder') });
  const chevBtn = h('button', 'icon-btn icon-btn-s', { type: 'button', 'aria-label': 'Collapse', html: ic('chevron', 'chev') });
  gel(pickBtn);
  gel(chevBtn);
  const head = h('div', 'recent-head', null, h('span', 'rh-ic', { html: ic('recent') }), h('span', 'caption', { text: 'Recent projects' }), count, pickBtn, chevBtn);
  const rows = h('div', 'recent-rows');
  const recent = h('section', 'recent', { 'aria-label': 'Recent projects' }, head, rows);
  const recentLens = new Lens(recent, 'panel');

  const home = h('div', 'home', null, heroBlock, hero, help, tiles, tileResult, recent);
  host.append(home);

  // ---- behaviour ------------------------------------------------------------------------------

  const screen = () => {
    const st = store.premiere ? store.premiere.state : 'not_running';
    return st === 'not_running' ? 'idle' : 'waiting';
  };
  const canLaunch = (p) => !!(p && (p.exe || p.fake || store.fake));
  let launching = false;

  async function launch(path) {
    if (launching) return;
    launching = true;
    if (!path) hero.dataset.state = 'sending';
    try {
      const r = await call('launch_premiere', path ?? null);
      if (r) {
        store.premiere = r;
        notify('premiere', r);
      }
      refreshRecent();
    } catch (err) {
      failToast(err);
    } finally {
      launching = false;
      paint();
    }
  }

  // The list is re-read on every `premiere` event (a --project launch handed over by a second instance records the
  // project before the state is emitted); a read already under way absorbs the events that arrive meanwhile.
  let recentInflight = false;
  let recentAgain = false;
  async function refreshRecent() {
    if (recentInflight) {
      recentAgain = true;
      return;
    }
    recentInflight = true;
    try {
      store.recent = (await call('recent_projects')) || [];
      notify('recent', store.recent);
    } catch { /* the list stays as it was */ } finally {
      recentInflight = false;
      if (recentAgain) {
        recentAgain = false;
        refreshRecent();
      }
    }
  }

  hero.addEventListener('click', () => {
    const p = store.premiere;
    if (hero.dataset.state === 'sending') return;
    if (!canLaunch(p)) {
      openSettings();
      return;
    }
    launch(null);
  });

  pickBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const path = await call('pick_project');
      if (path) launch(path);
    } catch (err) {
      failToast(err);
    }
  });

  const toggleRecent = () => {
    if (screen() === 'idle') {
      collapsedIdle = !collapsedIdle;
      writeCollapsed(collapsedIdle);
    } else collapsedWaiting = !collapsedWaiting;
    paintRecent();
  };
  chevBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleRecent();
  });
  head.addEventListener('click', (e) => {
    if (recent.classList.contains('is-collapsed') && !e.target.closest('button')) toggleRecent();
  });

  // ---- waiting tiles --------------------------------------------------------------------------

  function outcome(btn, tone, text) {
    flash(btn, tone, ic);
    tileResult.className = `tile-result is-notice t-${tone}`;
    tileResult.replaceChildren(h('span', 'tr-ic', { html: ic(tone === 'ok' ? 'check' : tone === 'warn' ? 'warn' : 'cross') }), h('span', '', { text }));
    clearTimeout(resultTimer);
    resultTimer = setTimeout(clearResult, noticeMs(tone));
    addActivity({ text, tone, module: null });
  }

  async function busy(btn, fn) {
    if (btn.classList.contains('is-busy')) return;
    btn.classList.add('is-busy');
    btn.setAttribute('aria-busy', 'true');
    try {
      await fn();
    } finally {
      btn.classList.remove('is-busy');
      btn.removeAttribute('aria-busy');
    }
  }

  tPanel.addEventListener('click', () => busy(tPanel, async () => {
    try {
      const r = await call('panel_check_update');
      if (r && r.updated) outcome(tPanel, 'ok', `Panel updated · ${r.installed || 'none'} to ${r.bundled}`);
      else outcome(tPanel, 'ok', `Panel is up to date · ${(r && (r.installed || r.bundled)) || ''}`.replace(/ · $/, ''));
    } catch (err) {
      outcome(tPanel, 'error', err.message || 'The panel could not be checked.');
    }
  }));

  /** Resolve with the first update status that is no longer `checking` (or the last one after the wait). */
  function settledUpdate() {
    return new Promise((resolve) => {
      let off = null;
      const done = (u) => {
        clearTimeout(timer);
        off && off();
        resolve(u);
      };
      const timer = setTimeout(() => done(store.update), UPDATE_WAIT_MS);
      off = watch('update', (u) => {
        if (u && u.stage !== 'checking') done(u);
      });
    });
  }

  tUpdate.addEventListener('click', () => busy(tUpdate, async () => {
    if (!update) {
      outcome(tUpdate, 'error', 'Updates are not available.');
      return;
    }
    const wait = settledUpdate();
    const first = await update.check();
    const u = first && first.stage && first.stage !== 'checking' ? first : await wait;
    const stage = u && u.stage;
    if (stage === 'available' || stage === 'downloading' || stage === 'ready') {
      outcome(tUpdate, 'ok', `Premiere Deck ${u.latest} is available`);
      if (!update.isOpen()) update.openSheet();
    } else if (stage === 'up_to_date' || stage === 'idle') {
      outcome(tUpdate, 'ok', 'Premiere Deck is up to date');
    } else {
      outcome(tUpdate, 'error', (u && u.message) || 'The update check failed.');
    }
  }));

  tReinstall.addEventListener('click', () => busy(tReinstall, async () => {
    try {
      const r = await call('panel_reinstall');
      if (r) {
        store.panel = r;
        notify('panel', r);
      }
      const res = r && r.last_result;
      if (res && (res.action === 'failed' || res.action === 'no_source')) outcome(tReinstall, 'error', res.message || 'The panel could not be installed.');
      else outcome(tReinstall, 'ok', `Panel reinstalled · ${(r && r.installed_version) || ''}`.replace(/ · $/, ''));
    } catch (err) {
      outcome(tReinstall, 'error', err.message || 'The panel could not be installed.');
    }
  }));

  // ---- debug-mode fallback (PANEL.md 4.3) -----------------------------------------------------

  let debugOn = false;
  async function enableDebug(btn) {
    const yes = await askConfirm({
      title: 'Extension debug mode',
      question: "Premiere Deck will set PlayerDebugMode for Adobe's extension runtime (CSXS.9 to CSXS.12) in your user registry so Premiere Pro can load the panel. It can be turned off again in Settings.",
      yes: 'Enable',
      origin: btn,
    });
    if (!yes) return;
    try {
      const r = await call('panel_debug_mode', true);
      store.panel = { ...(store.panel || {}), debug_mode: r };
      notify('panel', store.panel);
      debugOn = true;
      paint();
      toast('success', 'Debug mode is on. Restart Premiere Pro to load the panel.');
    } catch (err) {
      failToast(err);
    }
  }

  function paintFallback(p) {
    fallback.replaceChildren();
    const offer = p && p.state === 'waiting' && (p.fallback_offer || debugOn);
    fallback.hidden = !offer || fallbackClosed;
    if (!offer) return;
    if (debugOn) {
      fallback.append(h('p', 't-ok', { text: 'Debug mode is on. Restart Premiere Pro to load the panel.' }));
      return;
    }
    const b = h('button', 'btn btn-xs', { type: 'button' }, h('span', '', { text: "Enable Premiere's extension debug mode" }));
    gel(b);
    b.addEventListener('click', () => enableDebug(b));
    fallback.append(h('p', '', { text: 'The panel did not load. Premiere Pro may need extension debug mode.' }), b);
  }

  // ---- paint ----------------------------------------------------------------------------------

  function paintRecent() {
    const p = store.premiere;
    const list = (store.recent || []).slice(0, RECENT_ROWS);
    const scr = screen();
    const collapsed = scr === 'idle' ? collapsedIdle : collapsedWaiting;
    recent.classList.toggle('is-collapsed', collapsed);
    chevBtn.setAttribute('aria-label', collapsed ? 'Expand' : 'Collapse');
    chevBtn.setAttribute('aria-expanded', String(!collapsed));
    count.textContent = collapsed && store.recent && store.recent.length ? String(Math.min(store.recent.length, RECENT_ROWS)) : '';
    const blocked = !canLaunch(p) && (!p || p.state === 'not_running');
    pickBtn.disabled = blocked;
    rows.replaceChildren();
    if (!list.length) {
      rows.append(h('p', 'empty-line', { text: 'No recent projects yet.' }));
      return;
    }
    for (const r of list) {
      const epoch = isoToEpoch(r.opened_at);
      const row = h('button', 'recent-row', { type: 'button', disabled: blocked, title: r.path },
        h('b', '', { text: r.name }), h('small', '', { text: r.folder || '' }), h('time', '', { text: epoch ? ago(epoch) : '' }));
      row.addEventListener('click', () => launch(r.path));
      rows.append(row);
    }
  }

  let lastScreen = null;
  function paint() {
    const p = store.premiere || { state: 'not_running' };
    const st = p.state;
    const scr = screen();
    const connected = st === 'connected';
    home.dataset.screen = scr;
    home.classList.toggle('is-connected', connected);
    glyphWrap.classList.toggle('is-ok', connected);
    const ver = premiereVersion(p);
    if (scr === 'idle') {
      const found = canLaunch(p);
      status.className = `status-line ${found ? 't-neutral' : 't-warn'}`;
      statusText.textContent = found ? 'Premiere Pro is not running' : 'Premiere Pro was not found';
      if (!launching) hero.dataset.state = 'ready';
      heroText.textContent = found ? 'Launch Premiere Pro' : 'Choose Premiere Pro';
      if (pill.parentElement !== pillSlot) pillSlot.append(pill);
    } else {
      stateTitle.textContent = connected ? 'Panel connected' : 'Waiting for the panel';
      status.className = `status-line ${connected ? 't-ok' : 't-warn'}`;
      if (connected) statusText.textContent = [p.project ? p.project.name : 'No project open', ver ? `Premiere Pro ${ver}` : 'Premiere Pro'].join(' · ');
      else statusText.textContent = st === 'starting' ? 'Premiere Pro is starting' : `Premiere Pro${ver ? ` ${ver}` : ''} is running`;
    }
    if (st !== 'waiting') debugOn = false;
    help.hidden = helpClosed;
    paintFallback(p);
    if (scr !== lastScreen) {
      helpClosed = false;
      fallbackClosed = false;
      if (scr === 'waiting') collapsedWaiting = true;
      if (lastScreen) clearResult();
      lastScreen = scr;
    }
    paintRecent();
  }

  const offs = [watch('premiere', () => {
    paint();
    refreshRecent();
  }), watch('recent', paintRecent)];
  paint();
  refreshRecent();

  return {
    paint,
    /** Entrance of the compact page (after the window narrowed). */
    enter() {
      home.animate([{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }], { duration: 220, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)' });
    },
    destroy() {
      offs.forEach((o) => o && o());
      heroLens.destroy();
      recentLens.destroy();
      home.remove();
    },
  };
}
