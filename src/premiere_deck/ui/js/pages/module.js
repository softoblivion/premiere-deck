// Generic module page: header (title, status, enabled switch), the page model of module_page(id)
// drawn by js/blocks.js, and a "Settings" card built from module_settings(id).

import { Switch } from '../../vendor/liquid_ui/js/controls.js';
import { call, on } from '../api.js';
import { store, watch, moduleById } from '../store.js';
import { createBlocks, makeAct, buildField, Keyed } from '../blocks.js';
import { h, toneClass, failToast, throttle } from '../ui.js';

const STATE_LABEL = {
  running: 'Running',
  stopped: 'Stopped',
  disabled: 'Turned off',
  failed: 'Failed',
  missing: 'Not installed in this build',
  import_failed: 'Could not be loaded',
};
const STATE_TONE = { running: 'ok', stopped: 'neutral', disabled: 'neutral', failed: 'error', missing: 'neutral', import_failed: 'error' };

export function mountModulePage(host, id) {
  let alive = true;
  let info = moduleById(id) || { id, title: id, description: '', state: 'missing', enabled: false, error: null, status: null, has_page: false, has_settings: false };
  let seq = 0;
  let setSeq = 0;
  const controls = [];

  const titleEl = h('h1', 'mp-title');
  const descEl = h('p', 'mp-desc');
  const statusEl = h('span', 'mp-status');
  const stateEl = h('span', 'mp-state');
  const sw = new Switch({ label: 'Module enabled', checked: !!info.enabled, onChange: (v) => setEnabled(v) });
  controls.push(sw);
  const head = h('header', 'mp-head', null,
    h('div', 'mp-head-main', null, titleEl, descEl),
    h('div', 'mp-head-side', null, h('div', 'mp-badges', null, statusEl, stateEl), h('label', 'mp-switch', null, h('span', '', { text: 'Enabled' }), sw.el)));
  const notice = h('div', 'mp-notice', { hidden: true });
  const blocksEl = h('div', 'blocks');
  const settingsBody = h('div', 'mp-set-body');
  const settingsSec = h('section', 'mp-settings', { hidden: true }, h('h3', 'blk-h', { text: 'Settings' }), settingsBody);
  const root = h('div', 'mp', null, head, notice, blocksEl, settingsSec);
  host.append(root);

  const act = makeAct(id);
  const blocks = createBlocks(blocksEl, { moduleId: id, act });
  const settingsKeyed = new Keyed(settingsBody);

  // ---- header and notice --------------------------------------------------------------------

  let pageTitle = null;
  let pageStatus = null;
  let hasPage = false;

  function paintHead() {
    titleEl.textContent = pageTitle || info.title || id;
    descEl.textContent = info.description || '';
    descEl.hidden = !info.description;
    const st = pageStatus || info.status;
    statusEl.hidden = !st || !st.text;
    if (st && st.text) {
      statusEl.className = `mp-status ${toneClass(st.tone)}`;
      statusEl.replaceChildren(h('i', 'dot'), document.createTextNode(st.text));
    }
    stateEl.className = `mp-state ${toneClass(STATE_TONE[info.state])}`;
    stateEl.textContent = STATE_LABEL[info.state] || info.state;
    sw.set(!!info.enabled);
    sw.setDisabled(info.state === 'missing' || info.state === 'import_failed');
    root.classList.toggle('is-missing', info.state === 'missing');
    paintNotice();
  }

  function paintNotice() {
    const lines = [];
    if (info.state === 'missing') lines.push('Not installed in this build.');
    else if (info.state === 'import_failed') lines.push('This module is in the build but could not be loaded.');
    else if (info.state === 'disabled' || (!info.enabled && info.state !== 'running')) lines.push('This module is turned off.');
    else if (info.state === 'failed') lines.push('This module stopped with an error.');
    else if (!hasPage) lines.push('This module has no page yet.');
    notice.replaceChildren();
    if (info.error) notice.append(h('p', 't-error', { text: info.error }));
    for (const l of lines) notice.append(h('p', '', { text: l }));
    notice.hidden = !notice.children.length;
  }

  async function setEnabled(v) {
    try {
      const r = await call('set_module_enabled', id, v);
      if (r && alive) {
        info = r;
        paintHead();
        refresh();
      }
    } catch (err) {
      failToast(err);
      sw.set(!!info.enabled);
    }
  }

  // ---- page model ----------------------------------------------------------------------------

  async function refresh() {
    const my = ++seq;
    try {
      const r = await call('module_page', id);
      if (!alive || my !== seq) return;
      if (r && r.module) info = r.module;
      const page = r && r.page;
      hasPage = !!(page && Array.isArray(page.blocks));
      pageTitle = page && page.title ? page.title : null;
      pageStatus = page && page.status ? page.status : null;
      paintHead();
      blocks.update(hasPage ? page.blocks : []);
    } catch (err) {
      if (!alive || my !== seq) return;
      hasPage = false;
      paintHead();
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
      box.append(h('div', `fm-row${unavailable ? ' is-off' : ''}`, null, label, h('div', `fm-ctl${kind === 'text' || kind === 'number' ? ' fm-ctl-wide' : ''}`, null, field.el)));
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
    on('modules', () => {
      const m = moduleById(id);
      if (m) {
        info = m;
        paintHead();
      }
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

  paintHead();
  refresh();
  refreshSettings();

  return {
    destroy() {
      alive = false;
      refreshThrottled.cancel();
      offs.forEach((o) => o && o());
      blocks.destroy();
      settingsKeyed.destroy();
      controls.forEach((c) => c.destroy && c.destroy());
      root.remove();
    },
  };
}
