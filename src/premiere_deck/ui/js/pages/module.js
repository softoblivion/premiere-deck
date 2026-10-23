// One module tab of the main state (docs/HOST_UI.md 3.3): the page model of module_page(id) drawn by js/blocks.js
// inside the right pane, the page's `subtitle` (one mono line, the Files root) with its `header_actions`, a
// notice for a module that is not running or missing, and a "Settings" card built from module_settings(id).
// The tab names the module, so the page title is not repeated.

import { call, on } from '../api.js';
import { store, watch, moduleById } from '../store.js';
import { createBlocks, makeAct, buildField, buttonEl, Keyed, noteResult, watchResultLines } from '../blocks.js';
import { ic } from '../icons.js';
import { h, failToast, throttle, flash, closeOnClick } from '../ui.js';

const STATE_LABEL = {
  running: 'Running',
  stopped: 'Stopped',
  disabled: 'Turned off',
  failed: 'Failed',
  missing: 'Not installed in this build',
  import_failed: 'Could not be loaded',
};

/**
 * @param {HTMLElement} host  the scroller of the pane
 * @param {string} id  module id
 * @param {{setHeadHint?: (text: string) => void}} o
 */
export function mountModuleTab(host, id, { setHeadHint = null } = {}) {
  let alive = true;
  let info = moduleById(id) || { id, title: id, description: '', state: 'missing', enabled: false, error: null, status: null, has_page: false, has_settings: false };
  let seq = 0;
  let setSeq = 0;

  const rootRow = h('div', 'root-row', { hidden: true });
  const notice = h('div', 'mp-notice', { hidden: true });
  const blocksEl = h('div', 'blocks');
  const settingsBody = h('div', 'mp-set-body');
  const settingsSec = h('section', 'blk mp-settings', { hidden: true }, h('div', 'blk-head', null, h('h3', 'blk-h', { text: 'Settings' })), settingsBody);
  const root = h('div', 'mp', { 'data-module': id }, rootRow, notice, blocksEl, settingsSec);
  host.append(root);

  const act = makeAct(id);
  const ctx = { moduleId: id, act, setHeadHint };
  const blocks = createBlocks(blocksEl, ctx);
  const settingsKeyed = new Keyed(settingsBody);
  const headControls = [];
  let hasPage = false;
  let headSig = '';
  let pageBlocks = [];
  let noticeDismissed = ''; // signature of the module notice the user closed

  // ---- subtitle line and notice -------------------------------------------------------------

  function paintRoot(page) {
    const sub = page && page.subtitle ? String(page.subtitle) : '';
    const acts = (page && Array.isArray(page.header_actions)) ? page.header_actions : [];
    const sig = JSON.stringify([sub, acts]);
    if (sig === headSig) return;
    headSig = sig;
    rootRow.replaceChildren();
    rootRow.hidden = !sub && !acts.length;
    if (rootRow.hidden) return;
    rootRow.append(
      h('span', 'root-ic', { html: ic('folder') }),
      h('span', 'path', { text: sub, title: sub }),
      ...acts.map((a) => buttonEl({ variant: 'icon', ...a }, ctx)));
  }

  function paintNotice() {
    const lines = [];
    if (info.state === 'missing') lines.push('Not installed in this build.');
    else if (info.state === 'import_failed') lines.push('This module is in the build but could not be loaded.');
    else if (info.state === 'disabled' || (!info.enabled && info.state !== 'running')) lines.push('This module is turned off. Turn it on in Settings, Modules.');
    else if (info.state === 'failed') lines.push('This module stopped with an error.');
    else if (!hasPage) lines.push('This module has no page yet.');
    notice.replaceChildren();
    if (lines.length || info.error) {
      notice.append(h('div', 'mp-notice-head', null, h('b', '', { text: info.title || id }), h('span', 'mp-state', { text: STATE_LABEL[info.state] || info.state })));
    }
    if (info.error) notice.append(h('p', 't-error', { text: info.error }));
    for (const l of lines) notice.append(h('p', '', { text: l }));
    const sig = notice.textContent;
    notice.hidden = !notice.children.length || (!!sig && sig === noticeDismissed);
    root.classList.toggle('is-missing', info.state === 'missing');
  }
  // The notice closes on a click and stays closed until its text changes (HOST_UI.md 7).
  closeOnClick(notice, () => {
    noticeDismissed = notice.textContent;
    notice.hidden = true;
  });

  // ---- page model ----------------------------------------------------------------------------

  async function refresh() {
    const my = ++seq;
    try {
      const r = await call('module_page', id);
      if (!alive || my !== seq) return;
      if (r && r.module) info = r.module;
      const page = r && r.page;
      hasPage = !!(page && Array.isArray(page.blocks));
      paintRoot(hasPage ? page : null);
      paintNotice();
      pageBlocks = hasPage ? page.blocks : [];
      blocks.update(pageBlocks);
    } catch (err) {
      if (!alive || my !== seq) return;
      hasPage = false;
      paintNotice();
      notice.append(h('p', 't-error', { text: err.message }));
      notice.hidden = false;
    }
  }

  // ---- settings card -------------------------------------------------------------------------

  let model = null;

  function buildSettings(m) {
    const sctl = [];
    const box = h('div', 'fm');
    for (const f of m.fields) {
      const unavailable = f.available === false;
      const kind = f.kind === 'bool' ? 'toggle' : f.kind === 'int' ? 'number' : f.kind === 'select' ? 'select' : 'text';
      const spec = {
        t: kind, key: f.key, nodeId: `set:${f.scope}:${f.key}`, label: f.label || f.key, hint: f.hint, disabled: unavailable,
        value: kind === 'toggle' ? !!f.value : (f.value ?? f.default ?? ''), options: f.options || [], placeholder: f.kind === 'path' ? 'Folder or file path' : null,
      };
      let field;
      const send = async (v) => {
        try {
          const stored = await call('set_module_setting', id, f.key, v, f.scope);
          f.value = stored;
          if (model) settingsKeyed.resig('settings', JSON.stringify(model));
          if (stored !== undefined && stored !== v && field) field.set(stored);
          return true;
        } catch (err) {
          failToast(err);
          return false;
        }
      };
      field = buildField(spec, { send }, sctl);
      const note = unavailable ? 'Open a project in Premiere Pro' : f.hint;
      const label = h('div', 'fm-label', null,
        h('span', '', null, spec.label, f.scope === 'project' ? h('em', 'scope-tag', { text: 'Project' }) : null),
        note ? h('small', unavailable ? 't-warn' : '', { text: note }) : null);
      const wide = kind === 'text' || kind === 'number';
      box.append(h('div', `fm-row${unavailable ? ' is-off' : ''}${wide ? ' fm-row-wide' : ''}`, null, label, h('div', `fm-ctl${wide ? ' fm-ctl-wide' : ''}`, null, field.el)));
    }
    return { el: box, destroy: () => sctl.forEach((c) => c.destroy && c.destroy()) };
  }

  async function refreshSettings() {
    if (!info.has_settings || info.state === 'missing') {
      settingsSec.hidden = true;
      return;
    }
    const my = ++setSeq;
    try {
      const r = await call('module_settings', id);
      if (!alive || my !== setSeq) return;
      const fields = (r && r.fields) || [];
      settingsSec.hidden = !fields.length;
      if (!fields.length) {
        settingsKeyed.update([]);
        return;
      }
      const next = { fields, project: r.project ?? null };
      const sig = JSON.stringify(next);
      if (model && JSON.stringify(model) === sig) return;
      model = next;
      settingsKeyed.update([{ key: 'settings', sig, build: () => buildSettings(model) }]);
    } catch {
      if (alive && my === setSeq) settingsSec.hidden = true;
    }
  }

  // ---- wiring --------------------------------------------------------------------------------

  let lastProject = store.premiere && store.premiere.project ? store.premiere.project.path : null;
  const refreshThrottled = throttle(() => {
    if (!alive) return;
    refresh();
    refreshSettings();
  }, 150);
  const offs = [
    // A result line appears with a `result` event of this module and goes away after its time or on a click.
    watchResultLines((moduleId) => {
      if (alive && moduleId === id) blocks.update(pageBlocks);
    }),
    on('modules', () => {
      const m = moduleById(id);
      if (m) info = m;
      refreshThrottled();
    }),
    watch('premiere', (p) => {
      const key = p && p.project ? p.project.path : null;
      if (key !== lastProject) {
        lastProject = key;
        refreshThrottled();
      }
    }),
  ];

  paintNotice();
  refresh();
  refreshSettings();

  return {
    id,
    /** The outcome of an action on its control (a `result` event of this module). */
    flashNode(nodeId, outcome) {
      noteResult(id, outcome);
      const el = Array.from(root.querySelectorAll('[data-node]')).find((n) => n.dataset.node === nodeId);
      if (el) flash(el, outcome, ic);
    },
    refresh: () => refreshThrottled(),
    destroy() {
      alive = false;
      refreshThrottled.cancel();
      offs.forEach((o) => o && o());
      blocks.destroy();
      settingsKeyed.destroy();
      headControls.forEach((c) => c.destroy && c.destroy());
      root.remove();
    },
  };
}
