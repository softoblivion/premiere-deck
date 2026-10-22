// Deck page (docs/HOST_UI.md 3.1): Premiere status card with the Launch pill, recent projects,
// module cards and the activity feed with running jobs.

import { Lens, gel } from '../../vendor/liquid_ui/js/glass.js';
import { Switch } from '../../vendor/liquid_ui/js/controls.js';
import { toast } from '../../vendor/liquid_ui/js/toast.js';
import { timeAgo, shortDate, percent } from '../../vendor/liquid_ui/js/format.js';
import { call } from '../api.js';
import { store, notify, watch, sortedModules } from '../store.js';
import { icon, pageIcon } from '../icons.js';
import { askConfirm } from '../confirm.js';
import { h, toneClass, failToast, isoToEpoch, bar, setBar } from '../ui.js';

const STATE_HEAD = { not_running: 'Not running', starting: 'Starting', connected: 'Connected', no_panel: 'Panel not loaded' };
const STATE_DOT = { not_running: 'neutral', starting: 'warn', connected: 'ok', no_panel: 'warn' };
const MODULE_STATE = { running: 'Running', stopped: 'Stopped', disabled: 'Turned off', failed: 'Failed', missing: 'Not installed in this build', import_failed: 'Could not be loaded' };

export function mountDeck(host, { navigate }) {
  const offs = [];
  const controls = [];

  // ---- Premiere card --------------------------------------------------------------------------

  const dot = h('i', 'pr-dot');
  const headEl = h('h2', 'pr-state');
  const lineEl = h('p', 'pr-line');
  const fallback = h('div', 'pr-fallback', { hidden: true });

  const hero = h('button', 'hero', { type: 'button', 'data-state': 'empty' },
    h('span', 'pm-fill', { 'aria-hidden': 'true' }, h('span', 'pm-liquid')),
    h('span', 'pm-layer pm-label', null, h('span', '', { html: icon('play') }), h('span', 'pm-text', { text: 'Launch Premiere Pro' })),
    h('span', 'pm-layer pm-spin', null, h('span', 'spinner'), h('span', 'pm-spin-t', { text: 'Starting Premiere Pro' })),
    h('span', 'pm-layer pm-progress', null, h('span', 'pm-ptext tnum'), h('span', 'pm-pmeta tnum')),
    h('span', 'pm-layer pm-done', null, h('span', '', { html: icon('check') }), h('span', 'pm-done-t', { text: 'Premiere Pro is running' })));
  const heroLens = new Lens(hero, 'pill');
  gel(hero, { amount: 0.8 });
  controls.push(heroLens);

  const reason = h('p', 'pr-reason', { hidden: true });
  const openBtn = h('button', 'btn btn-s', { type: 'button' }, h('span', '', { html: icon('folder') }), h('span', '', { text: 'Open project' }));
  gel(openBtn);
  const prCard = h('section', 'flat pr-card', { 'aria-label': 'Premiere Pro' },
    h('div', 'pr-head', null, dot, h('div', 'pr-text', null, headEl, lineEl)),
    fallback, hero, reason, h('div', 'pr-row', null, openBtn));

  const recentList = h('div', 'rc-list', { 'data-sk': 'recent' });
  const recentCard = h('section', 'flat rc-card', { 'aria-label': 'Recent projects' }, h('h3', 'blk-h', { text: 'Recent projects' }), recentList);

  // ---- Modules and activity -------------------------------------------------------------------

  const modsEl = h('div', 'mod-cards');
  const jobsEl = h('div', 'feed-jobs');
  const feedEl = h('div', 'feed-lines');
  const feedBox = h('div', 'feed flat', { 'data-sk': 'feed' }, jobsEl, feedEl);

  const root = h('div', 'deck', null,
    h('div', 'deck-top', null, prCard, recentCard),
    h('section', 'deck-mods', null, h('h3', 'blk-h', { text: 'Modules' }), modsEl),
    h('section', 'deck-act', null, h('h3', 'blk-h', { text: 'Activity' }), feedBox));
  host.append(root);

  // ---- Premiere state -------------------------------------------------------------------------

  let debugOn = false;
  const canLaunch = (p) => !!(p.exe || p.fake || store.fake);

  async function launch(path) {
    try {
      const r = await call('launch_premiere', path ?? null);
      if (r) {
        store.premiere = r;
        notify('premiere', r);
      }
      if (path) refreshRecent();
    } catch (err) {
      failToast(err);
    }
  }

  async function refreshRecent() {
    try {
      store.recent = (await call('recent_projects')) || [];
      notify('recent', store.recent);
    } catch { /* the list stays as it was */ }
  }

  hero.addEventListener('click', () => {
    const p = store.premiere;
    if (!p || hero.dataset.state !== 'ready') return;
    launch(null);
  });
  openBtn.addEventListener('click', async () => {
    try {
      const path = await call('pick_project');
      if (path) launch(path);
    } catch (err) {
      failToast(err);
    }
  });

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
      paintPremiere();
      toast('success', 'Debug mode is on. Restart Premiere Pro to load the panel.');
    } catch (err) {
      failToast(err);
    }
  }

  function paintPremiere() {
    const p = store.premiere;
    if (!p) return;
    const st = p.state;
    dot.className = `pr-dot ${toneClass(STATE_DOT[st])}${st === 'starting' ? ' is-pulse' : ''}`;
    headEl.textContent = STATE_HEAD[st] || st;
    const ver = p.premiere_version ? `Premiere ${p.premiere_version}` : null;
    const installed = (p.installs || []).find((i) => i.exe === p.exe);
    if (st === 'connected') lineEl.textContent = [p.project ? p.project.name : 'No project open', ver].filter(Boolean).join(' · ');
    else if (st === 'starting') lineEl.textContent = 'Waiting for the panel to connect';
    else if (st === 'no_panel') lineEl.textContent = `${ver || 'Premiere Pro'} is running, but the panel has not connected.`;
    else lineEl.textContent = p.exe ? `Premiere Pro${installed && installed.version ? ` ${installed.version}` : ''} found` : 'Premiere Pro was not found';

    // Panel fallback (docs/PANEL.md 4.3).
    fallback.replaceChildren();
    fallback.hidden = st !== 'no_panel';
    if (st !== 'no_panel') debugOn = false;
    if (st === 'no_panel') {
      if (debugOn) {
        fallback.append(h('p', '', { text: 'Debug mode is on. Restart Premiere Pro to load the panel.' }));
      } else if (p.fallback_offer) {
        const b = h('button', 'btn btn-s', { type: 'button' }, h('span', '', { text: "Enable Premiere's extension debug mode" }));
        gel(b);
        b.addEventListener('click', () => enableDebug(b));
        fallback.append(h('p', '', { text: 'Premiere Pro may need extension debug mode to load the panel.' }), b);
      } else {
        const b = h('button', 'btn btn-s', { type: 'button' }, h('span', '', { text: 'Open Settings' }));
        gel(b);
        b.addEventListener('click', () => navigate('settings'));
        fallback.append(h('p', '', { text: 'The panel can be reinstalled in Settings.' }), b);
      }
    }

    // Launch pill.
    let state;
    let why = '';
    if (st === 'connected') state = 'done';
    else if (st === 'starting') state = 'sending';
    else if (st === 'no_panel') state = 'empty';
    else if (!canLaunch(p)) {
      state = 'empty';
      why = 'Premiere Pro was not found on this computer. Choose its location in Settings.';
    } else state = 'ready';
    hero.dataset.state = state;
    hero.disabled = state !== 'ready';
    hero.setAttribute('aria-disabled', String(state !== 'ready'));
    hero.querySelector('.pm-text').textContent = st === 'no_panel' ? 'Premiere Pro is running' : 'Launch Premiere Pro';
    reason.hidden = !why;
    reason.replaceChildren();
    if (why) {
      const b = h('button', 'btn btn-xs btn-ghost', { type: 'button', text: 'Open Settings' });
      gel(b);
      b.addEventListener('click', () => navigate('settings'));
      reason.append(document.createTextNode(`${why} `), b);
    }
    openBtn.disabled = st === 'not_running' && !canLaunch(p);
    paintRecent();
  }

  // ---- Recent projects ------------------------------------------------------------------------

  function paintRecent() {
    const p = store.premiere;
    const list = store.recent || [];
    recentList.replaceChildren();
    if (!list.length) {
      recentList.append(h('p', 'empty-line', { text: 'No recent projects.' }));
      return;
    }
    const blocked = !p || (p.state === 'not_running' && !canLaunch(p));
    for (const r of list.slice(0, 10)) {
      const epoch = isoToEpoch(r.opened_at);
      const when = epoch ? (Date.now() / 1000 - epoch < 345600 ? timeAgo(epoch) : shortDate(epoch)) : '';
      const item = h('button', 'rc-item', { type: 'button', disabled: blocked, title: r.path, 'data-fk': `rc|${r.path}` },
        h('span', 'rc-main', null, h('span', 'rc-name', { text: r.name }), h('span', 'rc-folder mono', { text: r.folder || '' })),
        h('span', 'rc-when tnum', { text: when }));
      item.addEventListener('click', () => launch(r.path));
      recentList.append(item);
    }
  }

  // ---- Module cards ---------------------------------------------------------------------------

  const cards = new Map(); // id -> { el, parts }

  function paintModules() {
    const mods = sortedModules();
    const seen = new Set();
    let prev = null;
    for (const m of mods) {
      seen.add(m.id);
      let c = cards.get(m.id);
      if (!c) {
        const sw = new Switch({
          label: `${m.title} enabled`,
          checked: !!m.enabled,
          onChange: async (v) => {
            try {
              await call('set_module_enabled', m.id, v);
            } catch (err) {
              failToast(err);
              sw.set(!v);
            }
          },
        });
        const title = h('span', 'mc-title');
        const status = h('span', 'mc-status');
        const open = h('button', 'mc-open', { type: 'button' }, h('span', 'mc-icon', { html: icon(pageIcon(m.id)) }), h('span', 'mc-text', null, title, status));
        open.addEventListener('click', () => navigate(m.id));
        const el = h('div', 'mod-card glass-lite', { 'data-mid': m.id }, h('span', 'lg-rim', { 'aria-hidden': 'true' }), open, sw.el);
        c = { el, sw, title, status, open };
        cards.set(m.id, c);
      }
      c.title.textContent = m.title;
      const missing = m.state === 'missing';
      const st = missing ? MODULE_STATE.missing : (m.status && m.status.text) || MODULE_STATE[m.state] || m.state;
      c.status.textContent = st;
      c.status.className = `mc-status ${missing ? '' : toneClass(m.status ? m.status.tone : m.state === 'failed' || m.state === 'import_failed' ? 'error' : 'neutral')}`;
      c.status.title = m.error || st;
      c.el.classList.toggle('is-missing', missing);
      c.sw.set(!!m.enabled);
      c.sw.setDisabled(missing || m.state === 'import_failed');
      const want = prev ? prev.nextSibling : modsEl.firstChild;
      if (c.el !== want) modsEl.insertBefore(c.el, want);
      prev = c.el;
    }
    for (const [id, c] of Array.from(cards)) {
      if (seen.has(id)) continue;
      c.el.remove();
      c.sw.destroy();
      cards.delete(id);
    }
  }

  // ---- Activity and jobs ----------------------------------------------------------------------

  const jobRows = new Map(); // id -> { el, title, meta, bar }

  function paintJobs() {
    const live = Array.from(store.jobs.values()).filter((j) => j.state === 'running' || j.state === 'queued');
    const seen = new Set();
    for (const j of live) {
      seen.add(j.id);
      let r = jobRows.get(j.id);
      if (!r) {
        const title = h('span', 'jb-title');
        const meta = h('span', 'jb-meta tnum');
        const b = bar(j.progress);
        r = { el: h('div', 'jb', null, h('div', 'jb-top', null, title, meta), b), title, meta, bar: b };
        jobRows.set(j.id, r);
        jobsEl.append(r.el);
      }
      r.title.textContent = j.title || 'Job';
      const bits = [];
      if (j.state === 'queued') bits.push('Queued');
      else if (j.label) bits.push(j.label);
      if (j.progress != null && j.state === 'running') bits.push(percent(j.progress));
      r.meta.textContent = bits.join(' · ');
      setBar(r.bar, j.state === 'queued' ? 0 : j.progress);
    }
    for (const [id, r] of Array.from(jobRows)) {
      if (seen.has(id)) continue;
      r.el.remove();
      jobRows.delete(id);
    }
    paintEmptyFeed();
  }

  function paintFeed() {
    feedEl.replaceChildren();
    const titles = new Map(store.modules.map((m) => [m.id, m.title]));
    for (const a of (store.activity || []).slice(0, 20)) {
      const mod = a.module ? titles.get(a.module) || a.module : null;
      feedEl.append(h('div', 'fd', null,
        h('i', `dot ${toneClass(a.tone)}`),
        h('span', 'fd-text', { text: a.text, title: a.text }),
        mod ? h('span', 'fd-mod', { text: mod }) : null,
        h('span', 'fd-when tnum', { text: timeAgo(a.at) })));
    }
    paintEmptyFeed();
  }

  function paintEmptyFeed() {
    let empty = feedBox.querySelector(':scope > .empty-line');
    const none = !jobRows.size && !(store.activity || []).length;
    if (none && !empty) feedBox.append(h('p', 'empty-line', { text: 'No activity yet.' }));
    else if (!none && empty) empty.remove();
  }

  // ---- wiring ---------------------------------------------------------------------------------

  offs.push(watch('premiere', paintPremiere));
  offs.push(watch('recent', paintRecent));
  offs.push(watch('modules', () => {
    paintModules();
    paintFeed();
  }));
  offs.push(watch('activity', paintFeed));
  offs.push(watch('jobs', paintJobs));
  const timer = setInterval(paintFeed, 30000);

  paintPremiere();
  paintModules();
  paintFeed();
  paintJobs();
  refreshRecent();

  return {
    destroy() {
      clearInterval(timer);
      offs.forEach((o) => o && o());
      controls.forEach((c) => c.destroy && c.destroy());
      for (const c of cards.values()) c.sw.destroy();
      root.remove();
    },
  };
}
