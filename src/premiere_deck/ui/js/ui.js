// Small DOM and formatting helpers shared by the pages.

import { call } from './api.js';
import { toast } from '../vendor/liquid_ui/js/toast.js';

const ATTR_ONLY = /^(data-|aria-)/;

/**
 * Create an element: h('div', 'cls', { text, title, 'data-x': 1, disabled: true }, ...children).
 * Children may be nodes, strings or arrays; null and false are skipped.
 */
export function h(tag, cls = '', attrs = null, ...kids) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === 'text') el.textContent = v;
      else if (k === 'html') el.innerHTML = v;
      else if (ATTR_ONLY.test(k) || k === 'style' || k === 'role' || k === 'for') el.setAttribute(k, v === true ? '' : String(v));
      else el[k] = v;
    }
  }
  for (const k of kids.flat(2)) {
    if (k == null || k === false) continue;
    el.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return el;
}

const TONES = new Set(['ok', 'warn', 'error', 'accent', 'neutral']);

/** CSS class of a tone word (BRIDGE_API: neutral, ok, warn, error, accent); unknown -> neutral. */
export function toneClass(tone) {
  return `t-${TONES.has(tone) ? tone : 'neutral'}`;
}

/** Text of a value that may be `{text, tone}`. */
export function cellText(v) {
  if (v && typeof v === 'object') return String(v.text ?? '');
  return v == null ? '' : String(v);
}

export function cellTone(v) {
  return v && typeof v === 'object' ? v.tone || null : null;
}

/** Only image data URLs, https and http sources are used for thumbnails. */
export function safeImg(src) {
  return typeof src === 'string' && /^(data:image\/|https?:\/\/)/i.test(src) ? src : null;
}

/** Show a rejection of a bridge call as a toast and return null. */
export function failToast(err) {
  toast('error', (err && err.message) || 'Something went wrong.');
  return null;
}

const logged = new Set();
/** Log a UI warning once per distinct message. */
export function logOnce(level, message) {
  const key = `${level}:${message}`;
  if (logged.has(key)) return;
  logged.add(key);
  call('log', level, message).catch(() => {});
}

/** Throttle with a trailing call: at most one run per `ms`. */
export function throttle(fn, ms) {
  let last = 0;
  let timer = 0;
  let args = null;
  const run = () => {
    timer = 0;
    last = performance.now();
    fn(...args);
  };
  const t = (...a) => {
    args = a;
    const wait = ms - (performance.now() - last);
    if (wait <= 0) {
      clearTimeout(timer);
      run();
    } else if (!timer) timer = setTimeout(run, wait);
  };
  t.cancel = () => {
    clearTimeout(timer);
    timer = 0;
  };
  return t;
}

/** Progress bar element (kit .pop-progress look); `value` 0..1, null for indeterminate. */
export function bar(value, cls = '') {
  const indet = value == null || !isFinite(value);
  const el = h('div', `pop-progress bar${indet ? ' is-indet' : ''}${cls ? ` ${cls}` : ''}`, null, h('span'));
  if (!indet) el.style.setProperty('--p', Math.max(0, Math.min(1, value)).toFixed(3));
  return el;
}

export function setBar(el, value) {
  const indet = value == null || !isFinite(value);
  el.classList.toggle('is-indet', indet);
  if (!indet) el.style.setProperty('--p', Math.max(0, Math.min(1, value)).toFixed(3));
}

/** Epoch seconds of an ISO text; 0 when it cannot be parsed. */
export function isoToEpoch(iso) {
  const t = iso ? Date.parse(iso) : NaN;
  return isFinite(t) ? t / 1000 : 0;
}

/** "Just now", "12 min ago", "4 h ago", "Yesterday", "3 days ago", then a short date. Epoch seconds. */
export function ago(epoch) {
  if (!epoch) return '';
  const d = Date.now() / 1000 - epoch;
  if (d < 45) return 'Just now';
  if (d < 3600) return `${Math.max(1, Math.round(d / 60))} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`;
  const days = Math.floor(d / 86400);
  if (days < 2) return 'Yesterday';
  if (days < 7) return `${days} days ago`;
  return new Date(epoch * 1000).toLocaleDateString('en-US', { day: 'numeric', month: 'short' });
}

/** "12:03" (24 h) of an epoch in seconds. */
export function clock(epoch) {
  if (!epoch) return '';
  const t = new Date(epoch * 1000);
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
}

const FLASH_ICON = { ok: 'check', error: 'cross', warn: 'warn', neutral: 'check' };
const FLASH_MS = 2000;

/**
 * The outcome of an action on its control for 2 s (as the panel's tiles): the icon in the control's `[data-ic]`
 * holder turns into a check (mint glow) or a cross (rose glow); controls without an icon only glow.
 * `iconHtml(name)` draws the result icon.
 */
export function flash(el, outcome, iconHtml) {
  if (!el || !el.isConnected) return;
  const tone = FLASH_ICON[outcome] ? outcome : 'neutral';
  clearTimeout(el.__flashT);
  el.querySelectorAll(':scope [data-ic] > .flash-ic').forEach((n) => n.remove());
  const holder = el.querySelector('[data-ic]');
  if (holder && iconHtml) holder.append(h('span', 'flash-ic', { html: iconHtml(FLASH_ICON[tone]) }));
  el.classList.remove('is-flash');
  void el.offsetWidth; // restart the animation
  el.dataset.flash = tone;
  el.classList.add('is-flash');
  el.__flashT = setTimeout(() => {
    el.classList.remove('is-flash');
    delete el.dataset.flash;
    el.querySelectorAll(':scope [data-ic] > .flash-ic').forEach((n) => n.remove());
  }, FLASH_MS);
}
