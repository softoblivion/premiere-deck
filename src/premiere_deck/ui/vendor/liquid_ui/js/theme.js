// Themes of Premiere Deck (docs/HOST_UI.md 7): Crystal Black (dark, the default) and Crystal Sky
// (light). Every token value lives in src/premiere_deck/ui/themes.json; build/gen_themes.py renders
// it into css/themes.css under `:root[data-theme="<name>"]`, so the first paint needs no JS and
// nothing is derived at runtime. applyTheme() switches the attribute with the sweep (a View
// Transition) and retints the scene; mountThemeControl() renders the two theme cards of the
// settings sheet. The app persists the choice in its settings.

import { retint } from './background.js';
import { gel } from './glass.js';
import { icon } from './icons.js';

/** The themes, in card order (name, label, hue and accent as in ui/themes.json; tests/test_themes.py). */
export const THEMES = [
  { name: 'black', label: 'Crystal Black', scheme: 'dark', hue: '#5f6f8c', accent: '#7563ff' },
  { name: 'sky', label: 'Crystal Sky', scheme: 'light', hue: '#4b93e6', accent: '#5a45e0' },
];

export const DEFAULT_THEME = 'black';

/** Length of the sweep (css/components.css "Theme sweep" holds the same value). */
export const SWEEP_MS = 600;

export function themeByName(name) {
  return THEMES.find((t) => t.name === name) || null;
}

// ---------------------------------------------------------------------------------------------
// Token derivation: the identity and scene tokens the kit derives from a hue / accent pair. The
// dark theme's values start from it; ui/themes.json keeps the result ("derived") and the values
// each theme replaces ("overrides"), and build/gen_themes.py checks the two stay in step.

const rgb = (hex) => {
  const n = parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
};
const hex = (a) => `#${a.map((v) => Math.max(0, Math.min(255, Math.round(v))).toString(16).padStart(2, '0')).join('')}`;
const mix = (a, b, k) => a.map((v, i) => v + (b[i] - v) * k);
const rgba = (a, al) => `rgba(${a.map(Math.round).join(', ')}, ${al})`;

/** Identity and scene tokens for a hue / accent pair. */
export function derive({ hue, accent }) {
  const h = rgb(hue);
  const a = rgb(accent);
  const black = [0, 0, 0];
  const white = [255, 255, 255];
  const light = mix(a, white, 0.22);
  const deep = mix(a, h, 0.42);
  return {
    '--accent': accent,
    '--accent-2': hex(deep),
    '--accent-ink': hex(mix(a, white, 0.56)),
    '--accent-grad': `linear-gradient(135deg, ${hex(light)} 0%, ${accent} 48%, ${hex(deep)} 100%)`,
    '--accent-grad-a': `linear-gradient(135deg, ${rgba(light, 0.74)} 0%, ${rgba(a, 0.74)} 48%, ${rgba(deep, 0.78)} 100%)`,
    '--btn-glow': rgba(a, 0.55),
    '--focus-ring': rgba(mix(a, white, 0.45), 0.95),
    '--glyph-glow': rgba(mix(h, white, 0.2), 0.55),
    '--bg-top': hex(mix(h, black, 0.95)),
    '--bg-mid': hex(mix(h, black, 0.86)),
    '--bg-bottom': hex(mix(h, black, 0.74)),
    '--scene-hue': hue,
    '--scene-accent': accent,
    '--scene-line': hex(mix(h, white, 0.74)),
    '--tint-regular': rgba(mix(h, black, 0.82), 0.3),
    '--tint-sheet': rgba(mix(h, black, 0.88), 0.52),
    '--well': rgba(mix(h, black, 0.92), 0.4),
  };
}

// ---------------------------------------------------------------------------------------------
// Applying a theme

let current = '';
let target = '';
let sweeping = null;
const listeners = new Set();

/** The active theme name (the last one applied, else the root attribute, else the default). */
export function currentTheme() {
  return target || current || document.documentElement.dataset.theme || DEFAULT_THEME;
}

/** Whether applyTheme() animates: the View Transitions API exists and the page is visible. */
export function canSweep() {
  return typeof document.startViewTransition === 'function' && document.visibilityState === 'visible';
}

/**
 * Switch the theme: sets `data-theme` on the root, retints the scene and tells the listeners.
 * Unknown names fall back to the default. The first call of a page (start-up) is instant; later
 * changes run the sweep where the View Transitions API exists (reduced motion: 1 ms, the CSS),
 * else they are instant too. `{ animate: false }` forces an instant switch. Returns the name.
 */
export function applyTheme(name, { animate = true } = {}) {
  const t = themeByName(name) || themeByName(DEFAULT_THEME);
  const root = document.documentElement;
  if (target === t.name || (!target && current === t.name && root.dataset.theme === t.name)) return t.name;
  const first = !current && !target;
  target = t.name;
  const update = () => {
    root.dataset.theme = t.name;
    current = t.name;
    retint();
    for (const fn of listeners) {
      try {
        fn(t.name);
      } catch (err) {
        console.error(err);
      }
    }
  };
  if (sweeping) sweeping.skipTransition();
  if (first || !animate || !canSweep()) {
    update();
    return t.name;
  }
  let line = null;
  const vt = document.startViewTransition(() => {
    update();
    line = document.createElement('div');
    line.className = 'theme-line';
    line.setAttribute('aria-hidden', 'true');
    document.body.append(line);
  });
  sweeping = vt;
  vt.finished.catch(() => {}).finally(() => {
    if (line) line.remove();
    if (sweeping === vt) sweeping = null;
  });
  return t.name;
}

/** Resolves when the running sweep (if any) has finished. */
export function sweepDone() {
  return sweeping ? sweeping.finished.catch(() => {}) : Promise.resolve();
}

/** Called after every applyTheme() with the new name; returns an unsubscribe function. */
export function onTheme(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// ---------------------------------------------------------------------------------------------
// Settings row: the theme cards

/**
 * Append a "Theme" row with one card per theme to `host` (a `.set-card`). Each card shows a mini
 * preview of its theme (scene, glass card, accent pill) drawn in that theme's tokens; the selected
 * card carries the accent rim. `onChange(name)` is called on a click (the app applies and saves
 * it, then calls set() with the confirmed value). Returns { el, set(name), destroy() }.
 */
export function mountThemeControl(host, {
  value = currentTheme(),
  label = 'Theme',
  hint = 'Window and panel. The panel follows the window.',
  onChange,
} = {}) {
  const row = document.createElement('div');
  row.className = 'set-row set-row-col';
  row.innerHTML = `<div class="set-label"><span></span><small></small></div><div class="theme-cards" role="radiogroup"></div>`;
  row.querySelector('.set-label > span').textContent = label;
  row.querySelector('.set-label small').textContent = hint;
  const group = row.querySelector('.theme-cards');
  group.setAttribute('aria-label', label);
  const cards = THEMES.map((t) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'theme-card';
    b.dataset.name = t.name;
    b.setAttribute('role', 'radio');
    b.innerHTML = `<span class="tc-prev" data-theme-scope="${t.name}" aria-hidden="true"><span class="tc-glass"><span class="tc-line"></span><span class="tc-line"></span></span><span class="tc-pill"></span></span><span class="tc-name"><span></span>${icon('check')}</span>`;
    b.querySelector('.tc-name > span').textContent = t.label;
    b.addEventListener('click', () => {
      if (b.getAttribute('aria-checked') === 'true') return;
      set(t.name);
      onChange && onChange(t.name);
    });
    gel(b);
    group.appendChild(b);
    return b;
  });
  const set = (name) => {
    const on = themeByName(name) ? name : DEFAULT_THEME;
    cards.forEach((b) => {
      const is = b.dataset.name === on;
      b.setAttribute('aria-checked', String(is));
      b.classList.toggle('is-on', is);
      b.tabIndex = is ? 0 : -1;
    });
  };
  group.addEventListener('keydown', (e) => {
    const i = cards.findIndex((b) => b.getAttribute('aria-checked') === 'true');
    let j = -1;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') j = (i + 1) % cards.length;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') j = (i - 1 + cards.length) % cards.length;
    if (j < 0) return;
    e.preventDefault();
    cards[j].focus();
    cards[j].click();
  });
  set(value);
  host.appendChild(row);
  return { el: row, set, destroy: () => row.remove() };
}
