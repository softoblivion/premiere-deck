// The main state in the wide window (docs/HOST_UI.md 3.3): the left column (Premiere card at the top, the activity
// strip at the bottom, growing upwards) and the right pane, a glass panel showing one module whose content is laid
// out at a fixed 430 px so it never reflows while the window widens. The module tabs (kit Segmented, module icon
// and title) sit in the title bar's centre slot (`tabsHost`).

import { Lens } from '../../vendor/liquid_ui/js/glass.js';
import { Segmented } from '../../vendor/liquid_ui/js/controls.js';
import { store, watch, sortedModules, moduleById } from '../store.js';
import { ic, glyph, moduleIcon, moduleLabel } from '../icons.js';
import { mountModuleTab } from './module.js';
import { premiereVersion } from './compact.js';
import { h, clock } from '../ui.js';

const TAB_KEY = 'pd.tab';
const ACTIVITY_LINES = 20;
const EASE = 'cubic-bezier(0.2, 0.8, 0.2, 1)';
const SLIDE_PX = 22;

const TONE_ICON = { ok: 'check', warn: 'warn', error: 'cross' };

function rememberedTab() {
  try {
    return localStorage.getItem(TAB_KEY);
  } catch {
    return null;
  }
}

/** @param {{tabsHost: HTMLElement}} o  the title bar's centre slot */
export function mountWide(host, { tabsHost }) {
  // ---- left column ----------------------------------------------------------------------------
  const prState = h('div', 'pr-state', null, h('span', 'dot'), h('span', 'pr-state-t'));
  const prVer = h('div', 'pr-ver');
  const prName = h('div', 'pr-name');
  const prPath = h('div', 'pr-path');
  const prCard = h('section', 'pr-card', { 'aria-label': 'Premiere Pro' },
    h('div', 'pr-top', null, h('span', 'pr-glyph-hold', { html: glyph(36) }), h('div', 'pr-top-t', null, prState, prVer)),
    prName, prPath);
  const prLens = new Lens(prCard, 'panel');

  const actRows = h('div', 'act-rows');
  const activity = h('section', 'activity', { 'aria-label': 'Activity' },
    h('div', 'caption act-cap', null, h('span', 'act-cap-ic', { html: ic('activity') }), h('span', '', { text: 'Activity' })), actRows);
  const left = h('aside', 'left', null, prCard, activity);

  // ---- right pane -----------------------------------------------------------------------------
  const tabsHold = h('div', 'tabs-hold');
  const headHint = h('span', 'pane-hint');
  const scroller = h('div', 'pane-scroll', { 'data-sk': 'pane' });
  const paneHead = h('div', 'pane-head', { hidden: true }, h('span', 'spacer'), headHint);
  const paneIn = h('div', 'pane-in', null, paneHead, scroller);
  tabsHost.append(tabsHold);
  const pane = h('section', 'pane', { 'aria-label': 'Modules' }, paneIn);
  const paneLens = new Lens(pane, 'panel');

  const grid = h('div', 'main-grid', null, left, pane);
  host.append(grid);

  // ---- Premiere card --------------------------------------------------------------------------
  function paintPremiere() {
    const p = store.premiere || {};
    const connected = p.state === 'connected';
    prState.className = `pr-state ${connected ? 't-ok' : 't-warn'}`;
    prState.querySelector('.pr-state-t').textContent = connected ? 'Connected' : p.state === 'not_running' ? 'Not running' : 'Panel closed';
    const ver = premiereVersion(p);
    prVer.textContent = ver ? `Premiere Pro ${ver}` : 'Premiere Pro';
    prName.textContent = p.project ? p.project.name : 'No project open';
    prPath.textContent = p.project ? p.project.root || '' : 'Open a project in Premiere Pro';
    prPath.title = p.project ? p.project.root || '' : '';
  }

  // ---- activity strip: newest at the bottom, as many of the last 20 as fit ----------------------
  function paintActivity() {
    const titles = new Map(store.modules.map((m) => [m.id, m.title]));
    const lines = (store.activity || []).slice(0, ACTIVITY_LINES).reverse();
    actRows.replaceChildren();
    for (const a of lines) {
      const icon = TONE_ICON[a.tone] || (a.module ? moduleIcon(a.module) : 'info');
      const mod = a.module ? titles.get(a.module) || a.module : '';
      actRows.append(h('div', `act-row t-${a.tone && TONE_ICON[a.tone] ? a.tone : 'neutral'}`, { title: mod ? `${mod} · ${a.text}` : a.text },
        h('span', 'act-ic', { html: ic(icon) }), h('span', 'act-t', { text: a.text }), h('time', '', { text: clock(a.at) })));
    }
    activity.classList.toggle('is-empty', !lines.length);
    if (!lines.length) actRows.append(h('p', 'act-empty', { text: 'No activity yet.' }));
  }

  // ---- tabs -----------------------------------------------------------------------------------
  let seg = null;
  let segSig = '';
  let current = null; // mounted tab

  function tabIds() {
    return sortedModules().map((m) => m.id);
  }

  function pickTab() {
    const ids = tabIds();
    for (const want of [store.tab, rememberedTab()]) if (want && ids.includes(want)) return want;
    return ids[0] || null;
  }

  function paintTabs() {
    const mods = sortedModules();
    const sig = JSON.stringify(mods.map((m) => [m.id, moduleLabel(m)]));
    const active = pickTab();
    if (sig !== segSig) {
      segSig = sig;
      if (seg) seg.destroy();
      tabsHold.replaceChildren();
      seg = null;
      if (mods.length) {
        seg = new Segmented({
          label: 'Modules',
          cls: 'tabs',
          options: mods.map((m) => ({ value: m.id, label: moduleLabel(m) })),
          value: active,
          onChange: (v) => selectTab(v),
        });
        seg.opts.forEach((b) => b.insertAdjacentHTML('afterbegin', ic(moduleIcon(b.dataset.v))));
        tabsHold.append(seg.el);
      }
    }
    if (active !== (current && current.id)) selectTab(active, { fromSeg: false });
  }

  function setHeadHint(text) {
    headHint.textContent = text || '';
    paneHead.hidden = !text;
  }

  function selectTab(id, { fromSeg = true } = {}) {
    if (!id) return;
    store.tab = id;
    try {
      localStorage.setItem(TAB_KEY, id);
    } catch { /* ignore */ }
    if (seg && !fromSeg && seg.value !== id) seg.set(id);
    if (current && current.id === id) return;
    if (current) current.destroy();
    scroller.replaceChildren();
    scroller.scrollTop = 0;
    setHeadHint('');
    current = mountModuleTab(scroller, id, { setHeadHint });
  }

  /** The strip exists only while the setting `show_activity` is on (HOST_UI.md 3.3). */
  function paintActivityVisibility() {
    const on = !!(store.settings && store.settings.show_activity);
    activity.hidden = !on;
    left.classList.toggle('has-activity', on);
  }

  const offs = [
    watch('premiere', paintPremiere),
    watch('activity', paintActivity),
    watch('settings', paintActivityVisibility),
    watch('modules', () => {
      paintTabs();
      paintActivity();
    }),
  ];

  paintPremiere();
  paintActivityVisibility();
  paintActivity();

  let anim = null;
  const stop = () => {
    if (anim) anim.cancel();
    anim = null;
  };

  return {
    /** Show a module's tab (the `navigate` event); remembered while the window is compact. */
    showTab(id) {
      if (!moduleById(id)) return;
      store.tab = id;
      if (!host.hidden) paintTabs();
    },
    /** A `result` event: morph the control in the visible tab. */
    flashNode(moduleId, nodeId, outcome) {
      if (current && current.id === moduleId) current.flashNode(nodeId, outcome);
    },
    /** Enter main: the left column is final at once; the pane's content fades in and slides 22 px from the right. */
    enter(ms) {
      stop();
      paintTabs();
      paintPremiere();
      paintActivity();
      if (!ms) return Promise.resolve();
      anim = paneIn.animate([{ opacity: 0, transform: `translateX(${SLIDE_PX}px)` }, { opacity: 1, transform: 'none' }], { duration: ms, easing: EASE, fill: 'backwards' });
      left.animate([{ opacity: 0.6 }, { opacity: 1 }], { duration: Math.round(ms * 0.6), easing: EASE });
      return anim.finished.catch(() => {}).finally(() => (anim = null));
    },
    /** Leave main: the same motion backwards; resolves when it ended (the window has narrowed by then). */
    leave(ms) {
      stop();
      if (!ms) return Promise.resolve();
      anim = paneIn.animate([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: `translateX(${SLIDE_PX}px)` }], { duration: ms, easing: EASE, fill: 'forwards' });
      return anim.finished.then(() => true, () => false).finally(() => {
        anim = null;
      });
    },
    reset() {
      stop();
      paneIn.getAnimations().forEach((a) => a.cancel());
    },
    destroy() {
      stop();
      offs.forEach((o) => o && o());
      if (current) current.destroy();
      prLens.destroy();
      paneLens.destroy();
      grid.remove();
      tabsHold.remove();
    },
  };
}
