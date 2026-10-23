// Icons of the host window: the sprite assets/icons.svg (symbols `i-<name>` on a 24 px grid and the glyph
// `glyph-pr`, shared in names with the panel's sprite) inlined into the page once at start, plus the kit's own
// line icons (vendor/liquid_ui/js/icons.js) that the kit controls draw.

import { icon as kitIcon } from '../vendor/liquid_ui/js/icons.js';

let loaded = false;

/** Fetch assets/icons.svg and put it at the top of <body>, so `<use href="#...">` and its gradients resolve. */
export async function loadSprite() {
  if (loaded) return;
  try {
    const res = await fetch(new URL('../assets/icons.svg', import.meta.url));
    const holder = document.createElement('div');
    holder.innerHTML = await res.text();
    const svg = holder.querySelector('svg');
    if (svg) {
      svg.id = 'sprite';
      document.body.prepend(svg);
      loaded = true;
    }
  } catch (err) {
    console.error('[ui] icon sprite not loaded', err);
  }
}

/** True when the sprite has the symbol `i-<name>`. */
export function hasIcon(name) {
  return !!(name && document.getElementById(`i-${name}`));
}

/** Inline SVG of one sprite icon; unknown names fall back to the kit's icon of that name (or `info`). */
export function ic(name, cls = '') {
  if (!hasIcon(name)) return kitIcon(KIT_FALLBACK[name] || name, cls);
  return `<svg class="ic${cls ? ` ${cls}` : ''}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}

const KIT_FALLBACK = { gear: 'gear', min: 'min', x: 'x' };

/** The Premiere-style glyph (symbol `glyph-pr`): a violet rounded square with a "Pr" mark. */
export function glyph(size = 64, cls = '') {
  return `<svg class="pr-glyph${cls ? ` ${cls}` : ''}" width="${size}" height="${size}" viewBox="0 0 64 64" aria-hidden="true"><use href="#glyph-pr"/></svg>`;
}

/** Tab / activity icon of a module id: known ids get their own, other modules a neutral one. */
export function moduleIcon(id) {
  return { files: 'folder', actions: 'actions', quick_actions: 'actions' }[id] || 'info';
}

/** Short tab label of a module (the module title when no shorter one is known). */
export function moduleLabel(m) {
  return { actions: 'Actions', quick_actions: 'Actions' }[m.id] || m.title || m.id;
}

export { kitIcon as icon };
