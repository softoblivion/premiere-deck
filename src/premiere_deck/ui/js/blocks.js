// Module page blocks (docs/BRIDGE_API.md, section 5): cards, table, list, form, grid, actions (buttons or
// tiles), chips, text and progress, built from the page model `render_host()` returns. Interactive elements are the
// panel's node shapes (button, toggle, select, input, text, progress) drawn with kit controls; button fields
// `icon`, `short`, `meta`, `menu`, `variant` and the progress fields `detail`, `cancel` as in the panel.
//
// Re-rendering never costs the user their place: blocks are keyed by `id` (or type + index), a block
// whose model is unchanged keeps its DOM, a changed block is rebuilt and the focused control, the
// typed text of a focused input and the scroll position of inner scrollers are put back.
//
// The window's pane is narrow (430 px): a table with more than four columns leaves its mono columns out, an
// `actions` block whose id is `<table id>_actions` is drawn in that table's title row, the list `last_result` is
// drawn as a tinted result line, and a list with `columns: 2` (or the id `visibility`) lays its rows out in two
// columns grouped by the row's `group` (or the part of its subtitle before " · ").

import { Switch, Select } from '../vendor/liquid_ui/js/controls.js';
import { gel } from '../vendor/liquid_ui/js/glass.js';
import { percent } from '../vendor/liquid_ui/js/format.js';
import { call } from './api.js';
import { askConfirm } from './confirm.js';
import { ic, hasIcon } from './icons.js';
import { h, toneClass, cellText, cellTone, safeImg, failToast, logOnce, bar, setBar, noticeMs, closeOnClick } from './ui.js';

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
// Result line of a module page (the list `last_result`): shown for a limited time after a `result` event of its
// module (success and neutral 3 s, warn and error 10 s), closed at once by a click on it (HOST_UI.md 7).

const resultLines = new Map(); // module id -> { until, timer }
const resultSubs = new Set();

const notifyResultLines = (moduleId) => resultSubs.forEach((fn) => fn(moduleId));

/** A `result` event of `moduleId`: its result line shows again from now on. */
export function noteResult(moduleId, outcome) {
  const prev = resultLines.get(moduleId);
  if (prev) clearTimeout(prev.timer);
  const ms = noticeMs(outcome);
  const entry = { until: Date.now() + ms, timer: setTimeout(() => notifyResultLines(moduleId), ms + 20) };
  resultLines.set(moduleId, entry);
  notifyResultLines(moduleId);
}

export function dismissResult(moduleId) {
  const prev = resultLines.get(moduleId);
  if (!prev) return;
  clearTimeout(prev.timer);
  resultLines.delete(moduleId);
  notifyResultLines(moduleId);
}

const resultLineShown = (moduleId) => {
  const e = resultLines.get(moduleId);
  return !!e && e.until > Date.now();
};

/** Call `fn(moduleId)` whenever a result line appears or goes away; returns the unsubscribe function. */
export function watchResultLines(fn) {
  resultSubs.add(fn);
  return () => resultSubs.delete(fn);
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

const NODE_KINDS = new Set(['button', 'switch', 'toggle', 'select', 'input', 'text', 'progress']);
const kindOf = (n) => n.kind ?? n.type;
const iconOf = (name) => (name && hasIcon(name) ? ic(name) : '');

/** Run a node button: confirmation sheet first when it has `confirm`, busy state while the call runs. */
async function press(b, node, ctx, row, value) {
  if (b.classList.contains('is-busy') || node.id == null || b.disabled) return;
  const tone = node.tone;
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
}

/** A small menu under `anchor` (a button's `menu`): a pick sends the menu's id with the picked value. */
function openMenu(anchor, menu, ctx, row) {
  document.querySelectorAll('.node-menu').forEach((m) => m.remove());
  const list = h('div', 'select-menu node-menu', { role: 'menu' });
  for (const op of menu.options || []) {
    const on = String(op.value) === String(menu.value);
    const item = h('button', `menu-item select-opt${on ? ' is-on' : ''}`, { type: 'button', role: 'menuitemradio', 'aria-checked': String(on) },
      h('span', '', { text: op.label ?? String(op.value) }));
    item.addEventListener('click', (e) => {
      e.stopPropagation();
      list.remove();
      if (!on) ctx.act(menu.id, op.value, row);
    });
    list.append(item);
  }
  document.body.append(list);
  const r = anchor.getBoundingClientRect();
  const w = Math.max(140, list.offsetWidth);
  list.style.left = `${Math.round(Math.max(8, Math.min(r.right - w, innerWidth - w - 8)))}px`;
  const below = r.bottom + 6 + list.offsetHeight < innerHeight - 8;
  list.style.top = `${Math.round(below ? r.bottom + 6 : r.top - 6 - list.offsetHeight)}px`;
  const close = (e) => {
    if (list.contains(e.target)) return;
    list.remove();
    removeEventListener('pointerdown', close, true);
  };
  setTimeout(() => addEventListener('pointerdown', close, true), 0);
}

function menuBadge(node, ctx, row) {
  const m = node.menu;
  if (!m || m.id == null) return null;
  const label = m.badge ?? (m.options || []).find((o) => String(o.value) === String(m.value))?.label ?? m.value;
  const badge = h('span', 'tile-badge', { role: 'button', tabindex: '0', 'aria-haspopup': 'menu', text: String(label ?? '') });
  const open = (e) => {
    e.stopPropagation();
    e.preventDefault();
    openMenu(badge, m, ctx, row);
  };
  badge.addEventListener('click', open);
  badge.addEventListener('keydown', (e) => (e.key === 'Enter' || e.key === ' ') && open(e));
  return badge;
}

/** A node button (BRIDGE_API 5: tone accent = the tinted primary; confirm = question sheet first; variant icon). */
export function buttonEl(node, ctx, { row = null, size = 'btn-s', value = null } = {}) {
  const tone = node.tone;
  const attrs = { type: 'button', 'data-fk': `${node.id ?? ''}|${row ?? ''}`, 'data-node': node.id ?? null, title: node.hint || null };
  let b;
  if (node.variant === 'icon') {
    b = h('button', 'icon-btn icon-btn-plate icon-btn-s', { ...attrs, 'aria-label': node.label ?? '', title: node.hint || node.label || null },
      h('span', 'ic-hold', { 'data-ic': '', html: iconOf(node.icon) || ic('open') }));
  } else {
    const cls = ['btn', size];
    if (tone === 'accent') cls.push('btn-tinted');
    if (tone === 'error' || tone === 'danger') cls.push('btn-danger');
    if (node.variant === 'ghost') cls.push('btn-ghost');
    const glyphHtml = iconOf(node.icon);
    b = h('button', cls.join(' '), attrs,
      glyphHtml ? h('span', 'ic-hold', { 'data-ic': '', html: glyphHtml }) : null,
      h('span', '', { text: node.label ?? '' }));
  }
  if (node.busy) {
    b.classList.add('is-busy');
    b.setAttribute('aria-busy', 'true');
  }
  if (node.disabled) b.disabled = true;
  gel(b);
  b.addEventListener('click', () => press(b, node, ctx, row, value));
  return b;
}

/** A node button as a glass tile: icon well, short label, one-line meta, optional choice badge. */
export function tileEl(node, ctx, { row = null } = {}) {
  const label = node.short || node.label || '';
  const b = h('button', `ftile${node.busy ? ' is-busy is-running' : ''}${node.tone === 'accent' ? ' is-accent' : ''}`, {
    type: 'button', 'data-fk': `${node.id ?? ''}|${row ?? ''}`, 'data-node': node.id ?? null, title: node.hint || null,
    'aria-label': node.label || label,
  },
  h('span', 'well', { 'data-ic': '', html: node.busy ? '<span class="spinner" aria-hidden="true"></span>' : (iconOf(node.icon) || ic('actions')) }),
  h('b', '', { text: label }),
  node.meta ? h('small', '', { text: String(node.meta) }) : h('small', 'is-empty', { text: '' }),
  menuBadge(node, ctx, row));
  if (node.busy) b.setAttribute('aria-busy', 'true');
  if (node.disabled) b.disabled = true;
  gel(b, { amount: 0.8 });
  b.addEventListener('click', () => press(b, node, ctx, row, null));
  return b;
}

function textEl(node) {
  return h('p', `tx ${toneClass(node.tone)}${node.small ? ' tx-small' : ''}`, { text: node.text ?? '' });
}

/** Progress card: label (with the count), percentage, bar, the current file in mono, and a Stop pill. */
function progressEl(node, ctx) {
  const v = node.value;
  const stop = node.cancel && ctx ? buttonEl({ id: node.cancel, label: 'Stop', icon: 'cross' }, ctx, { size: 'btn-xs' }) : null;
  const top = h('div', 'pg-top', null,
    h('span', 'pg-label', { text: node.label ?? '' }),
    v == null ? null : h('span', 'pg-pct tnum', { text: percent(v) }),
    stop);
  return h('div', `pg${stop ? ' pg-card' : ''}`, null, top, bar(v), node.detail ? h('div', 'pg-detail mono', { text: String(node.detail), title: String(node.detail) }) : null);
}

/** Field spec from a form field, filter or search node; null when it is neither. */
function normField(f) {
  if (!f || typeof f !== 'object') return null;
  const kind = kindOf(f);
  if (f.id != null && NODE_KINDS.has(kind)) {
    if (kind === 'toggle' || kind === 'switch') return { t: 'toggle', nodeId: f.id, key: f.id, label: f.label, hint: f.hint, value: !!f.value, disabled: f.disabled };
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
  if (kind === 'progress') return progressEl(node, ctx);
  if (kind === 'switch') return switchEl(node, ctx, controls, row);
  if (kind === 'toggle' || kind === 'select' || kind === 'input') {
    const spec = normField(node);
    if (spec && spec.t !== 'node') return fieldRow(spec, buildField(spec, { send: async (v) => (await ctx.act(spec.nodeId, v, row)) !== null }, controls));
  }
  logOnce('warning', `Node kind "${kind}" is not supported and was skipped.`);
  return null;
}

/**
 * A `switch` node (PROTOCOL.md 6.1) in a page block: a pill with the node's icon and label and a capsule switch.
 * A click on the capsule sends the node id with the new value; a refused change puts the old value back.
 */
export function switchEl(node, ctx, controls, row = null) {
  const label = node.label || node.short || '';
  const sw = new Switch({
    label,
    checked: !!node.value,
    onChange: async (v) => {
      const ok = (await ctx.act(node.id, v, row)) !== null;
      if (!ok) sw.set(!v);
    },
  });
  sw.el.dataset.fk = `${node.id ?? ''}|${row ?? ''}`;
  sw.setDisabled(!!node.disabled || !!node.busy);
  controls.push(sw);
  const glyphHtml = iconOf(node.icon);
  return h('div', `sw-pill${node.disabled ? ' is-disabled' : ''}`, { 'data-node': node.id ?? null, title: node.hint || null },
    glyphHtml ? h('span', 'ic-hold', { 'data-ic': '', html: glyphHtml }) : null,
    h('span', 'sw-label', { text: label }),
    sw.el);
}

function fieldRow(spec, field) {
  const label = h('div', 'fm-label', null, h('span', '', { text: spec.label ?? spec.key ?? '' }), spec.hint ? h('small', '', { text: spec.hint }) : null);
  return h('div', `fm-row${spec.t === 'text' || spec.t === 'number' ? ' fm-row-wide' : ''}`, null, label,
    h('div', `fm-ctl${spec.t === 'text' || spec.t === 'number' ? (spec.submitLabel ? ' fm-ctl-xwide' : ' fm-ctl-wide') : ''}`, null, field.el));
}

// ---------------------------------------------------------------------------------------------
// Block builders: (block, ctx, controls) -> element

function buildCards(b, ctx, controls) {
  const items = b.items || [];
  const grid = h('div', 'cards');
  for (const it of items) {
    const tile = h('div', `card-tile glass-lite ${toneClass(it.tone)}`, { 'data-rid': it.id }, h('span', 'lg-rim', { 'aria-hidden': 'true' }));
    tile.append(h('div', 'ct-title', null, it.tone && it.tone !== 'neutral' ? h('i', 'dot') : null, h('span', '', { text: it.title ?? '' })));
    tile.append(h('div', 'ct-value tnum', { text: cellText(it.value) }));
    const detail = it.detail ?? it.subtitle;
    if (detail) tile.append(h('div', 'ct-detail', { text: String(detail) }));
    if (it.progress != null) tile.append(bar(it.progress));
    if (it.actions && it.actions.length) {
      tile.append(h('div', 'ct-acts', null, it.actions.map((a) => renderNode(a, ctx, controls, { row: it.id, size: 'btn-xs' }))));
    }
    grid.append(tile);
  }
  if (!items.length) grid.append(h('p', 'empty-line', { text: b.empty || 'Nothing to show.' }));
  return grid;
}

/** Columns shown in the narrow pane: all up to four, else the non-mono ones. */
function visibleColumns(cols) {
  if (cols.length <= 4) return cols;
  const plain = cols.filter((c) => !c.mono);
  return plain.length ? plain : cols.slice(0, 4);
}

/** Text length (header included) up to which a column keeps the width of its text; the others share the rest. */
const FIT_CHARS = 12;

/** Per column: null for a short one (it keeps its text width), else its share of the rest in percent, weighted
 * by the longest text of the column. */
function columnWidths(cols, rows) {
  const len = cols.map((c) => {
    let n = String(c.label ?? c.key ?? '').length;
    for (const r of rows) n = Math.max(n, cellText(r[c.key]).length);
    return n;
  });
  const weights = len.map((n) => (n <= FIT_CHARS ? 0 : Math.min(n, 32)));
  const sum = weights.reduce((x, y) => x + y, 0);
  return weights.map((w) => (w ? Math.round((w / sum) * 1000) / 10 : null));
}

function buildTable(b, ctx, controls) {
  const all = b.columns || [];
  const cols = visibleColumns(all);
  const hidden = all.filter((c) => !cols.includes(c));
  const rows = b.rows || [];
  const hasActs = rows.some((r) => r.actions && r.actions.length);
  const widths = columnWidths(cols, rows);
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
    const extra = hidden.map((c) => cellText(r[c.key])).filter(Boolean).join(' > ');
    const tr = h('tr', '', { 'data-rid': r.id, title: extra || null });
    cols.forEach((c, i) => {
      const v = r[c.key];
      const tone = cellTone(v);
      const td = h('td', `${widths[i] == null ? 'tbl-fit ' : ''}${c.align === 'right' ? 'ta-r ' : ''}${c.mono ? 'mono ' : ''}${tone ? toneClass(tone) : ''}`.trim());
      if (i === 0 && r.tone && r.tone !== 'neutral') td.append(h('i', `dot ${toneClass(r.tone)}`));
      td.append(h('span', 'cell-t', { text: cellText(v) }));
      tr.append(td);
    });
    if (hasActs) tr.append(h('td', 'tbl-acts', null, h('div', 'tbl-acts-in', null, (r.actions || []).map((a) => renderNode(a, ctx, controls, { row: r.id, size: 'btn-xs' })))));
    body.append(tr);
  }
  if (!rows.length) body.append(h('tr', '', null, h('td', 'tbl-empty', { colSpan: cols.length + (hasActs ? 1 : 0), text: b.empty || 'No rows.' })));
  // The columns share the pane by the length of their text and shrink with an ellipsis; the actions column keeps
  // the width of its buttons (a table wider than the pane would clip it).
  const group = h('colgroup', '', null, widths.map((pct) => h('col', '', pct == null ? null : { style: `width:${pct}%` })), hasActs ? h('col', 'tbl-acts-c') : null);
  const table = h('table', 'tbl', null, group, h('thead', '', null, head), body);
  card.append(h('div', 'tbl-scroll', { 'data-sk': 'tbl' }, table));
  return card;
}

function thumbEl(src, cls) {
  const s = safeImg(src);
  return s ? h('img', cls, { src: s, alt: '', draggable: false }) : h('span', `${cls} thumb-ph`, { 'aria-hidden': 'true' });
}

const TONE_ICON = { ok: 'check', warn: 'warn', error: 'cross', accent: 'info', neutral: 'info' };

/** A row's icon: its `icon`, else (rows with a switch) the sprite symbol named like the row id ('_' -> '-'). */
function rowIcon(r) {
  if (r.icon && hasIcon(r.icon)) return r.icon;
  const guess = r.toggle && r.id != null ? String(r.id).replace(/_/g, '-') : null;
  return guess && hasIcon(guess) ? guess : null;
}

function listRow(r, ctx, controls, { compact = false, result = false } = {}) {
  const row = h('div', `lst-row${compact ? ' is-compact' : ''}${result ? ` lst-result ${toneClass(r.tone)}` : ''}`, { 'data-rid': r.id, title: compact && r.subtitle ? r.subtitle : null });
  if (r.thumb) row.append(thumbEl(r.thumb, 'lst-thumb'));
  if (result) row.append(h('span', 'lst-ic', { html: ic(TONE_ICON[r.tone] || 'info') }));
  else if (rowIcon(r)) row.append(h('span', 'lst-ic', { 'data-ic': '', html: iconOf(rowIcon(r)) }));
  else if (r.tone && r.tone !== 'neutral') row.append(h('i', `dot ${toneClass(r.tone)}`));
  const sub = compact ? null : (r.subtitle ? h('div', 'lst-sub', { text: r.subtitle, title: r.subtitle }) : null);
  row.append(h('div', 'lst-main', null, h('div', 'lst-title', { text: r.title ?? '' }), sub));
  if (r.meta) row.append(h('div', 'lst-meta mono', { text: String(r.meta) }));
  if (r.actions && r.actions.length) row.append(h('div', 'lst-acts', null, r.actions.map((a) => renderNode(a, ctx, controls, { row: r.id, size: 'btn-xs' }))));
  if (result) closeOnClick(row, () => dismissResult(ctx.moduleId));
  if (r.toggle && r.toggle.id) {
    // Optional row switch (BRIDGE_API.md 5): sends the toggle's id with the new value and row = the row id.
    const sw = new Switch({
      label: r.title || 'Shown',
      checked: !!r.toggle.value,
      onChange: async (v) => {
        const ok = (await ctx.act(r.toggle.id, v, r.id)) !== null;
        if (!ok) sw.set(!v);
      },
    });
    sw.el.dataset.fk = `${r.toggle.id}|${r.id}`;
    sw.setDisabled(!!r.toggle.disabled);
    controls.push(sw);
    row.classList.toggle('is-off', !r.toggle.value);
    row.append(h('div', `lst-acts lst-toggle${compact ? ' switch-s' : ''}`, null, sw.el));
  }
  return row;
}

function groupOf(r) {
  if (r.group) return String(r.group);
  const sub = String(r.subtitle || '');
  const at = sub.indexOf(' · ');
  return at > 0 ? sub.slice(0, at) : '';
}

function buildList(b, ctx, controls) {
  const rows = b.rows || [];
  const result = b.id === 'last_result';
  const grouped = !result && (Number(b.columns) === 2 || b.id === 'visibility');
  let list;
  if (grouped) {
    list = h('div', 'lst-cols');
    const groups = new Map();
    for (const r of rows) {
      const g = groupOf(r);
      if (!groups.has(g)) groups.set(g, []);
      groups.get(g).push(r);
    }
    for (const [g, items] of groups) {
      list.append(h('div', 'lst-group', null, g ? h('div', 'caption', { text: g }) : null, items.map((r) => listRow(r, ctx, controls, { compact: true }))));
    }
  } else {
    list = h('div', `lst${result ? ' lst-results' : ''}`);
    for (const r of rows) list.append(listRow(r, ctx, controls, { result }));
  }
  if (!rows.length && b.empty) list.append(h('p', 'empty-line', { text: b.empty }));
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
  if (b.tiles) {
    const cols = Math.max(1, Math.min(3, Number(b.columns) || 2));
    const grid = h('div', 'ftiles', { style: `--cols:${cols}` });
    for (const a of b.items || []) {
      const kind = kindOf(a) || 'button';
      if (kind === 'switch') grid.append(switchEl(a, ctx, controls));
      else if (kind === 'button') grid.append(tileEl(a, ctx));
    }
    return grid;
  }
  const row = h('div', 'btn-row');
  for (const a of b.items || []) {
    const el = renderNode(a, ctx, controls);
    if (el) row.append(el);
  }
  return row;
}

/** "142 in the root" -> <b>142</b> in the root. */
function chipText(text) {
  const m = /^([\d.,]+)(\s.*)$/.exec(String(text ?? ''));
  return m ? [h('b', '', { text: m[1] }), m[2]] : [String(text ?? '')];
}

function buildChips(b) {
  const row = h('div', 'chips');
  for (const c of b.items || []) {
    row.append(h('span', `chip ${toneClass(c.tone)}`, { 'data-rid': c.id ?? null },
      c.icon && hasIcon(c.icon) ? h('span', 'chip-ic', { html: ic(c.icon) }) : h('span', 'dot'),
      chipText(c.text)));
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
  chips: buildChips,
  text: (b) => textEl(b),
  progress: (b, ctx) => progressEl(b, ctx),
};

export const BLOCK_TYPES = Object.keys(BUILDERS);

function buildBlock(b, type, ctx, headActions) {
  const controls = [];
  const sec = h('section', `blk blk-${type}`, { 'data-block': b.id ?? null });
  const grouped = type === 'list' && (Number(b.columns) === 2 || b.id === 'visibility');
  const listActs = type === 'list' && Array.isArray(b.actions) ? b.actions : [];
  const acts = [...(headActions ? headActions.items || [] : []), ...listActs];
  if (grouped && ctx.setHeadHint) {
    ctx.setHeadHint(b.title || '');  // the pane head carries the title of a grouped list
  } else if (b.title || acts.length) {
    sec.append(h('div', 'blk-head', null,
      h('h3', 'blk-h', { text: b.title || '' }),
      acts.length ? h('div', 'blk-head-acts', null, acts.map((a) => buttonEl({ ...a, variant: a.variant || 'ghost', icon: a.icon || (a.id && /journal/.test(a.id) ? 'journal' : null) }, ctx, { size: 'btn-xs' }))) : null));
  }
  if (b.hint && type !== 'text' && !grouped) sec.append(h('p', 'blk-hint', { text: b.hint }));
  const body = BUILDERS[type](b, ctx, controls);
  if (body) sec.append(body);
  return { el: sec, destroy: () => controls.forEach((c) => c.destroy && c.destroy()) };
}

/**
 * Render the blocks of a page model into `container`.
 * ctx = { moduleId, act(nodeId, value, row), setHeadHint?(text) }. update(blocks) can be called as often as needed.
 */
export function createBlocks(container, ctx) {
  const keyed = new Keyed(container);
  return {
    update(blocks) {
      const list = [];
      const seen = new Set();
      const all = (blocks || []).filter((b) => b && typeof b === 'object'
        && !((b.type ?? b.kind) === 'list' && b.id === 'last_result' && !resultLineShown(ctx.moduleId)));
      const merged = new Set();
      // An `actions` block named `<table id>_actions` goes into that table's title row.
      const heads = new Map();
      for (const b of all) {
        const type = b.type ?? b.kind;
        if (type === 'actions' && !b.tiles && typeof b.id === 'string' && b.id.endsWith('_actions')) {
          const target = b.id.slice(0, -'_actions'.length);
          if (all.some((x) => (x.type ?? x.kind) === 'table' && x.id === target)) {
            heads.set(target, b);
            merged.add(b);
          }
        }
      }
      if (ctx.setHeadHint) ctx.setHeadHint('');
      all.forEach((b, i) => {
        if (merged.has(b)) return;
        const type = b.type ?? b.kind;
        if (!BUILDERS[type]) {
          logOnce('warning', `Module page block type "${type}" is not supported and was skipped.`);
          return;
        }
        let key = b.id != null ? String(b.id) : `${type}#${i}`;
        if (seen.has(key)) key = `${key}~${i}`;
        seen.add(key);
        const head = heads.get(b.id);
        const grouped = type === 'list' && (Number(b.columns) === 2 || b.id === 'visibility');
        if (grouped && ctx.setHeadHint) ctx.setHeadHint(b.title || '');
        list.push({ key, sig: JSON.stringify([b, head || null]), build: () => buildBlock(b, type, ctx, head) });
      });
      keyed.update(list);
    },
    destroy: () => keyed.destroy(),
  };
}

export { setBar };
