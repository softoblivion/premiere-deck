// Module page blocks (docs/BRIDGE_API.md, section 5): cards, table, list, form, grid, actions, text,
// progress, built from the page model `render_host()` returns. Interactive elements are the panel's
// node shapes (button, toggle, select, input, text, progress) drawn with kit controls.
//
// Re-rendering never costs the user their place: blocks are keyed by `id` (or type + index), a block
// whose model is unchanged keeps its DOM, a changed block is rebuilt and the focused control, the
// typed text of a focused input and the scroll position of inner scrollers are put back.

import { Switch, Select } from '../vendor/liquid_ui/js/controls.js';
import { gel } from '../vendor/liquid_ui/js/glass.js';
import { percent } from '../vendor/liquid_ui/js/format.js';
import { call } from './api.js';
import { askConfirm } from './confirm.js';
import { h, toneClass, cellText, cellTone, safeImg, failToast, logOnce, bar, setBar } from './ui.js';

// ---------------------------------------------------------------------------------------------
// Keyed reconciler

function snapshot(el) {
  const snap = { focus: null, scrolls: [] };
  const a = document.activeElement;
  if (a && el.contains(a) && a.dataset && a.dataset.fk) {
    snap.focus = { fk: a.dataset.fk };
    if (a.tagName === 'INPUT') {
      snap.focus.value = a.value;
      try {
        snap.focus.start = a.selectionStart;
        snap.focus.end = a.selectionEnd;
      } catch { /* number inputs have no selection */ }
    }
  }
  for (const s of el.querySelectorAll('[data-sk]')) snap.scrolls.push([s.dataset.sk, s.scrollTop, s.scrollLeft]);
  return snap;
}

function restore(el, snap) {
  for (const [sk, top, left] of snap.scrolls) {
    const s = Array.from(el.querySelectorAll('[data-sk]')).find((n) => n.dataset.sk === sk);
    if (s) {
      s.scrollTop = top;
      s.scrollLeft = left;
    }
  }
  if (snap.focus) {
    const t = Array.from(el.querySelectorAll('[data-fk]')).find((n) => n.dataset.fk === snap.focus.fk);
    if (t) {
      if (t.tagName === 'INPUT' && snap.focus.value != null) {
        t.value = snap.focus.value;
        try {
          if (snap.focus.start != null) t.setSelectionRange(snap.focus.start, snap.focus.end);
        } catch { /* ignore */ }
      }
      t.focus({ preventScroll: true });
    }
  }
}

/** Children of `container` keyed by string; items = [{ key, sig, build: () => ({ el, destroy }) }]. */
export class Keyed {
  constructor(container) {
    this.container = container;
    this.items = new Map();
  }

  update(list) {
    const keep = new Set();
    let prev = null;
    for (const it of list) {
      keep.add(it.key);
      let cur = this.items.get(it.key);
      let snap = null;
      if (!cur || cur.sig !== it.sig) {
        snap = cur ? snapshot(cur.el) : null;
        const built = it.build();
        if (cur) {
          cur.el.replaceWith(built.el);
          cur.destroy && cur.destroy();
        }
        cur = { sig: it.sig, el: built.el, destroy: built.destroy };
        this.items.set(it.key, cur);
      }
      const want = prev ? prev.nextSibling : this.container.firstChild;
      if (cur.el !== want) this.container.insertBefore(cur.el, want);
      if (snap) restore(cur.el, snap);
      prev = cur.el;
    }
    for (const [k, cur] of Array.from(this.items)) {
      if (keep.has(k)) continue;
      cur.el.remove();
      cur.destroy && cur.destroy();
      this.items.delete(k);
    }
  }

  /** Replace the stored signature of an item (its DOM already shows that state). */
  resig(key, sig) {
    const cur = this.items.get(key);
    if (cur) cur.sig = sig;
  }

  destroy() {
    for (const cur of this.items.values()) cur.destroy && cur.destroy();
    this.items.clear();
    this.container.textContent = '';
  }
}

// ---------------------------------------------------------------------------------------------
// Module action helper

/** `act(nodeId, value, row)` for one module: module_action, rejection -> toast. Resolves to the result or null. */
export function makeAct(moduleId) {
  return async (nodeId, value = null, row = null) => {
    try {
      // The module's own toast arrives as a `toast` event; the returned one is not shown twice.
      return (await call('module_action', moduleId, nodeId, value, row)) || { ok: true };
    } catch (err) {
      return failToast(err);
    }
  };
}

// ---------------------------------------------------------------------------------------------
// Nodes

const NODE_KINDS = new Set(['button', 'toggle', 'select', 'input', 'text', 'progress']);
const kindOf = (n) => n.kind ?? n.type;

/** A node button (BRIDGE_API 5: tone accent = the tinted primary; confirm = question sheet first). */
export function buttonEl(node, ctx, { row = null, size = 'btn-s', value = null } = {}) {
  const tone = node.tone;
  const cls = ['btn', size];
  if (tone === 'accent') cls.push('btn-tinted');
  if (tone === 'error' || tone === 'danger') cls.push('btn-danger');
  const b = h('button', cls.join(' '), { type: 'button', 'data-fk': `${node.id ?? ''}|${row ?? ''}`, title: node.hint || null }, h('span', '', { text: node.label ?? '' }));
  if (node.busy) {
    b.classList.add('is-busy');
    b.setAttribute('aria-busy', 'true');
  }
  if (node.disabled) b.disabled = true;
  gel(b);
  b.addEventListener('click', async () => {
    if (b.classList.contains('is-busy') || node.id == null) return;
    if (node.confirm) {
      const yes = await askConfirm({ question: String(node.confirm), origin: b, danger: tone === 'error' || tone === 'danger' });
      if (!yes) return;
    }
    b.classList.add('is-busy');
    b.setAttribute('aria-busy', 'true');
    try {
      await ctx.act(node.id, typeof value === 'function' ? value() : value, row);
    } finally {
      if (b.isConnected && !node.busy) {
        b.classList.remove('is-busy');
        b.removeAttribute('aria-busy');
      }
    }
  });
  return b;
}

function textEl(node) {
  return h('p', `tx ${toneClass(node.tone)}${node.small ? ' tx-small' : ''}`, { text: node.text ?? '' });
}

function progressEl(node) {
  const v = node.value;
  const top = h('div', 'pg-top', null, h('span', '', { text: node.label ?? '' }), v == null ? null : h('span', 'tnum', { text: percent(v) }));
  return h('div', 'pg', null, node.label || v != null ? top : null, bar(v));
}

/** Field spec from a form field, filter or search node; null when it is neither. */
function normField(f) {
  if (!f || typeof f !== 'object') return null;
  const kind = kindOf(f);
  if (f.id != null && NODE_KINDS.has(kind)) {
    if (kind === 'toggle') return { t: 'toggle', nodeId: f.id, key: f.id, label: f.label, hint: f.hint, value: !!f.value, disabled: f.disabled };
    if (kind === 'select') return { t: 'select', nodeId: f.id, key: f.id, label: f.label, hint: f.hint, value: f.value, options: f.options || [], disabled: f.disabled };
    if (kind === 'input') return { t: 'text', nodeId: f.id, key: f.id, label: f.label, hint: f.hint, value: f.value ?? '', placeholder: f.placeholder, submitLabel: f.submit_label, disabled: f.disabled };
    return { t: 'node', node: f };
  }
  if (f.key != null) {
    const base = { nodeId: f.id ?? null, key: f.key, label: f.label ?? f.key, hint: f.hint, value: f.value ?? f.default, disabled: f.disabled || f.available === false, setting: true, action: f.action };
    if (kind === 'bool' || kind === 'toggle') return { ...base, t: 'toggle', value: !!base.value };
    if (kind === 'int' || kind === 'number') return { ...base, t: 'number', value: base.value ?? '' };
    if (kind === 'select') return { ...base, t: 'select', options: f.options || [] };
    return { ...base, t: 'text', value: base.value ?? '', placeholder: f.placeholder };
  }
  return null;
}

/**
 * The control of one field. `send(value)` resolves to false when the change was refused (the
 * control then goes back to the last good value); `collect` mode only tracks the value.
 * Returns { el, get(), set(value) }.
 */
export function buildField(spec, { send, collect = false, onEnter = null, live = 0, size = '' } = {}, controls) {
  const fk = `${spec.nodeId ?? spec.key}|`;
  if (spec.t === 'toggle') {
    let good = !!spec.value;
    const sw = new Switch({
      label: spec.label || spec.key,
      checked: good,
      onChange: async (v) => {
        if (collect) return;
        const ok = await send(v);
        if (ok === false) sw.set(good);
        else good = v;
      },
    });
    sw.el.dataset.fk = fk;
    sw.setDisabled(!!spec.disabled);
    controls.push(sw);
    return { el: sw.el, get: () => sw.checked, set: (v) => { good = !!v; sw.set(v); } };
  }
  if (spec.t === 'select') {
    let good = spec.value;
    const sel = new Select({
      label: spec.label || spec.key,
      options: spec.options,
      value: spec.value,
      up: false,
      onChange: async (v) => {
        if (collect) return;
        const ok = await send(v);
        if (ok === false) sel.set(good);
        else good = v;
      },
    });
    sel.btn.dataset.fk = fk;
    if (spec.disabled) sel.btn.disabled = true;
    controls.push({ destroy: () => sel.closeMenu() });
    return { el: sel.el, get: () => sel.value, set: (v) => { good = v; sel.set(v); } };
  }
  // text / number
  const number = spec.t === 'number';
  const input = h('input', `field${size ? ` ${size}` : ''}`, {
    type: number ? 'number' : 'text',
    value: spec.value ?? '',
    placeholder: spec.placeholder || null,
    disabled: !!spec.disabled,
    spellcheck: false,
    autocomplete: 'off',
    'aria-label': spec.label || spec.key || 'Value',
    'data-fk': fk,
  });
  if (number) input.step = '1';
  const read = () => (number ? (input.value.trim() === '' ? null : Number(input.value)) : input.value);
  let sent = input.value;
  const commit = async (force = false) => {
    if (collect) return;
    if (!force && input.value === sent) return;
    const before = sent;
    sent = input.value;
    const ok = await send(read());
    if (ok === false) {
      sent = before;
      if (input.isConnected) input.value = before;
    }
  };
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    if (collect) onEnter && onEnter();
    else commit(!!spec.submitLabel);
  });
  if (!spec.submitLabel) input.addEventListener('change', () => commit());
  if (live) {
    let t = 0;
    input.addEventListener('input', () => {
      clearTimeout(t);
      t = setTimeout(() => commit(), live);
    });
    controls.push({ destroy: () => clearTimeout(t) });
  }
  let el = input;
  if (spec.submitLabel && !collect) {
    const btn = h('button', 'btn btn-s', { type: 'button', text: spec.submitLabel, 'data-fk': `${fk}submit` });
    gel(btn);
    btn.addEventListener('click', () => commit(true));
    el = h('div', 'fm-inline', null, input, btn);
  }
  return { el, get: read, set: (v) => { input.value = v ?? ''; sent = input.value; } };
}

/** Any node as an element (buttons, toggles, selects, inputs, text, progress). */
export function renderNode(node, ctx, controls, { row = null, size = 'btn-s' } = {}) {
  const kind = kindOf(node) || (node.label != null ? 'button' : null);
  if (kind === 'button') return buttonEl(node, ctx, { row, size });
  if (kind === 'text') return textEl(node);
  if (kind === 'progress') return progressEl(node);
  if (kind === 'toggle' || kind === 'select' || kind === 'input') {
    const spec = normField(node);
    if (spec && spec.t !== 'node') return fieldRow(spec, buildField(spec, { send: async (v) => (await ctx.act(spec.nodeId, v, row)) !== null }, controls));
  }
  logOnce('warning', `Node kind "${kind}" is not supported and was skipped.`);
  return null;
}

function fieldRow(spec, field) {
  const label = h('div', 'fm-label', null, h('span', '', { text: spec.label ?? spec.key ?? '' }), spec.hint ? h('small', '', { text: spec.hint }) : null);
  return h('div', 'fm-row', null, label, h('div', `fm-ctl${spec.t === 'text' || spec.t === 'number' ? (spec.submitLabel ? ' fm-ctl-xwide' : ' fm-ctl-wide') : ''}`, null, field.el));
}

// ---------------------------------------------------------------------------------------------
// Block builders: (block, ctx, controls) -> element

function buildCards(b, ctx, controls) {
  const items = b.items || [];
  const grid = h('div', 'tiles');
  for (const it of items) {
    const tile = h('div', `tile glass-lite ${toneClass(it.tone)}`, { 'data-rid': it.id }, h('span', 'lg-rim', { 'aria-hidden': 'true' }));
    tile.append(h('div', 'tile-title', null, it.tone && it.tone !== 'neutral' ? h('i', 'dot') : null, h('span', '', { text: it.title ?? '' })));
    tile.append(h('div', 'tile-value tnum', { text: cellText(it.value) }));
    const detail = it.detail ?? it.subtitle;
    if (detail) tile.append(h('div', 'tile-detail', { text: String(detail) }));
    if (it.progress != null) tile.append(bar(it.progress));
    if (it.actions && it.actions.length) {
      tile.append(h('div', 'tile-acts', null, it.actions.map((a) => renderNode(a, ctx, controls, { row: it.id, size: 'btn-xs' }))));
    }
    grid.append(tile);
  }
  if (!items.length) grid.append(h('p', 'empty-line', { text: b.empty || 'Nothing to show.' }));
  return grid;
}

function buildTable(b, ctx, controls) {
  const cols = b.columns || [];
  const rows = b.rows || [];
  const hasActs = rows.some((r) => r.actions && r.actions.length);
  const card = h('div', 'tbl-card');
  const filters = b.filters || [];
  if (filters.length || b.search) {
    const tools = h('div', 'tbl-tools');
    for (const f of filters) {
      const spec = normField(f);
      if (!spec || spec.t !== 'select') continue;
      const field = buildField(spec, { send: async (v) => (await ctx.act(spec.nodeId, v)) !== null }, controls);
      tools.append(h('label', 'tbl-filter', null, spec.label ? h('span', '', { text: spec.label }) : null, field.el));
    }
    if (b.search) {
      const spec = normField(b.search);
      if (spec && spec.t === 'text') {
        spec.submitLabel = null;
        const field = buildField(spec, { send: async (v) => (await ctx.act(spec.nodeId, v)) !== null, live: 400, size: 'tbl-search' }, controls);
        tools.append(h('div', 'tbl-filter tbl-filter-grow', null, field.el));
      }
    }
    card.append(tools);
  }
  const head = h('tr', '', null, cols.map((c) => h('th', c.align === 'right' ? 'ta-r' : '', { scope: 'col', text: c.label ?? c.key ?? '' })), hasActs ? h('th', 'tbl-acts-h', { 'aria-label': 'Actions' }) : null);
  const body = h('tbody');
  for (const r of rows) {
    const tr = h('tr', '', { 'data-rid': r.id });
    cols.forEach((c, i) => {
      const v = r[c.key];
      const tone = cellTone(v);
      const td = h('td', `${c.align === 'right' ? 'ta-r ' : ''}${c.mono ? 'mono ' : ''}${tone ? toneClass(tone) : ''}`.trim());
      if (i === 0 && r.tone && r.tone !== 'neutral') td.append(h('i', `dot ${toneClass(r.tone)}`));
      td.append(h('span', 'cell-t', { text: cellText(v), title: cellText(v) }));
      tr.append(td);
    });
    if (hasActs) tr.append(h('td', 'tbl-acts', null, h('div', 'tbl-acts-in', null, (r.actions || []).map((a) => renderNode(a, ctx, controls, { row: r.id, size: 'btn-xs' })))));
    body.append(tr);
  }
  if (!rows.length) body.append(h('tr', '', null, h('td', 'tbl-empty', { colSpan: cols.length + (hasActs ? 1 : 0), text: b.empty || 'No rows.' })));
  const table = h('table', 'tbl', null, h('thead', '', null, head), body);
  card.append(h('div', 'tbl-scroll', { 'data-sk': 'tbl' }, table));
  return card;
}

function thumbEl(src, cls) {
  const s = safeImg(src);
  return s ? h('img', cls, { src: s, alt: '', draggable: false }) : h('span', `${cls} thumb-ph`, { 'aria-hidden': 'true' });
}

function buildList(b, ctx, controls) {
  const rows = b.rows || [];
  const list = h('div', 'lst');
  for (const r of rows) {
    const row = h('div', 'lst-row', { 'data-rid': r.id });
    if (r.thumb) row.append(thumbEl(r.thumb, 'lst-thumb'));
    if (r.tone && r.tone !== 'neutral') row.append(h('i', `dot ${toneClass(r.tone)}`));
    row.append(h('div', 'lst-main', null, h('div', 'lst-title', { text: r.title ?? '' }), r.subtitle ? h('div', 'lst-sub', { text: r.subtitle }) : null));
    if (r.meta) row.append(h('div', 'lst-meta mono', { text: String(r.meta) }));
    if (r.actions && r.actions.length) row.append(h('div', 'lst-acts', null, r.actions.map((a) => renderNode(a, ctx, controls, { row: r.id, size: 'btn-xs' }))));
    if (r.toggle && r.toggle.id) {
      // Optional row switch (BRIDGE_API.md 5): sends the toggle's id with the new value and row = the row id.
      const node = { kind: 'toggle', ...r.toggle, label: '' };  // the row title already names it
      row.append(h('div', 'lst-acts lst-toggle', null, renderNode(node, ctx, controls, { row: r.id })));
    }
    list.append(row);
  }
  if (!rows.length) list.append(h('p', 'empty-line', { text: b.empty || 'Nothing to show.' }));
  return list;
}

function buildForm(b, ctx, controls) {
  const collect = !!b.submit;
  const box = h('div', 'fm');
  const getters = [];
  const submit = b.submit ? buttonEl(b.submit, ctx, { size: 'btn-s', value: () => Object.fromEntries(getters.map(([k, g]) => [k, g()])) }) : null;
  for (const f of b.fields || []) {
    const spec = normField(f);
    if (!spec) {
      logOnce('warning', 'A form field with neither id nor key was skipped.');
      continue;
    }
    if (spec.t === 'node') {
      const el = renderNode(spec.node, ctx, controls);
      if (el) box.append(h('div', 'fm-row fm-row-node', null, el));
      continue;
    }
    const send = async (v) => {
      if (spec.setting) {
        const action = spec.action ?? b.action;
        if (!action) {
          logOnce('warning', `Form field "${spec.key}" has no action.`);
          return false;
        }
        return (await ctx.act(action, { key: spec.key, value: v })) !== null;
      }
      return (await ctx.act(spec.nodeId, v)) !== null;
    };
    const field = buildField(spec, { send, collect, onEnter: () => submit && submit.click() }, controls);
    if (collect) getters.push([spec.key ?? spec.nodeId, field.get]);
    box.append(fieldRow(spec, field));
  }
  if (!box.children.length) box.append(h('p', 'empty-line', { text: 'No fields.' }));
  if (submit) box.append(h('div', 'fm-foot', null, submit));
  return box;
}

function buildGrid(b, ctx, controls) {
  const items = b.items || [];
  const cols = Math.max(1, Math.min(6, Number(b.columns) || 4));
  const grid = h('div', `gr${b.busy ? ' is-busy' : ''}`, { style: `--cols:${cols}` });
  for (const it of items) {
    const first = (it.actions || []).find((a) => a && a.id != null);
    const media = h('div', 'gr-media', null, thumbEl(it.thumb, 'gr-img'), it.badge ? h('span', 'chip-badge', { text: String(it.badge) }) : null);
    const card = h('div', 'gr-item', { 'data-rid': it.id }, media);
    const info = h('div', 'gr-info', null, h('div', 'gr-title', { text: it.title ?? '', title: it.title ?? '' }), it.subtitle ? h('div', 'gr-sub', { text: it.subtitle, title: it.subtitle }) : null);
    card.append(info);
    if (it.bar != null) card.append(bar(it.bar));
    if (it.times && it.times.length) {
      const chips = h('div', 'gr-times');
      for (const t of it.times) {
        const chip = h('button', 'tchip', { type: 'button', 'data-fk': `t${t.seconds}|${it.id}`, disabled: !first, title: first ? `${first.label ?? 'Open'} at ${t.label ?? t.seconds}` : null }, h('span', '', { text: t.label ?? String(t.seconds) }));
        if (t.bar != null) chip.append(h('i', 'tchip-bar', { style: `--b:${Math.max(0, Math.min(1, t.bar))}` }));
        gel(chip, { amount: 0.6 });
        chip.addEventListener('click', () => first && ctx.act(first.id, t.seconds, it.id));
        chips.append(chip);
      }
      card.append(chips);
    }
    if (it.actions && it.actions.length) card.append(h('div', 'gr-acts', null, it.actions.map((a) => renderNode(a, ctx, controls, { row: it.id, size: 'btn-xs' }))));
    grid.append(card);
  }
  const wrap = h('div', 'gr-wrap');
  if (!items.length) wrap.append(h('p', 'empty-line', { text: b.empty || 'Nothing to show.' }));
  else wrap.append(grid);
  if (b.busy) wrap.append(h('div', 'gr-busy', { 'aria-busy': 'true' }, h('span', 'spinner', { 'aria-hidden': 'true' })));
  return wrap;
}

function buildActions(b, ctx, controls) {
  const row = h('div', 'act-row');
  for (const a of b.items || []) {
    const el = renderNode(a, ctx, controls);
    if (el) row.append(el);
  }
  return row;
}

const BUILDERS = {
  cards: buildCards,
  table: buildTable,
  list: buildList,
  form: buildForm,
  grid: buildGrid,
  actions: buildActions,
  text: (b) => textEl(b),
  progress: (b) => progressEl(b),
};

export const BLOCK_TYPES = Object.keys(BUILDERS);

function buildBlock(b, type, ctx) {
  const controls = [];
  const sec = h('section', `blk blk-${type}`, { 'data-block': b.id ?? null });
  if (b.title) sec.append(h('h3', 'blk-h', { text: b.title }));
  if (b.hint && type !== 'text') sec.append(h('p', 'blk-hint', { text: b.hint }));
  const body = BUILDERS[type](b, ctx, controls);
  if (body) sec.append(body);
  return { el: sec, destroy: () => controls.forEach((c) => c.destroy && c.destroy()) };
}

/**
 * Render the blocks of a page model into `container`.
 * ctx = { moduleId, act(nodeId, value, row) }. update(blocks) can be called as often as needed.
 */
export function createBlocks(container, ctx) {
  const keyed = new Keyed(container);
  return {
    update(blocks) {
      const list = [];
      const seen = new Set();
      (blocks || []).forEach((b, i) => {
        if (!b || typeof b !== 'object') return;
        const type = b.type ?? b.kind;
        if (!BUILDERS[type]) {
          logOnce('warning', `Module page block type "${type}" is not supported and was skipped.`);
          return;
        }
        let key = b.id != null ? String(b.id) : `${type}#${i}`;
        if (seen.has(key)) key = `${key}~${i}`;
        seen.add(key);
        list.push({ key, sig: JSON.stringify(b), build: () => buildBlock(b, type, ctx) });
      });
      keyed.update(list);
    },
    destroy: () => keyed.destroy(),
  };
}

export { setBar };
