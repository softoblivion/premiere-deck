// Premiere Deck icons: the line icons come from the Liquid UI kit (vendor/liquid_ui/js/icons.js);
// this module adds the page icons and the app glyph.

import { icon, registerIcons } from '../vendor/liquid_ui/js/icons.js';

registerIcons({
  deck: '<rect x="6.5" y="3.5" width="11" height="6.5" rx="2.2"/><rect x="4.5" y="8.5" width="15" height="6.5" rx="2.4"/><rect x="3" y="13.5" width="18" height="7" rx="2.6"/>',
  search: '<circle cx="11" cy="11" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  bolt: '<path d="M13 3.5 5.5 13.5H11l-1 7 7.5-10H12Z"/>',
  module: '<rect x="4" y="4" width="7" height="7" rx="2"/><rect x="13" y="4" width="7" height="7" rx="2"/><rect x="4" y="13" width="7" height="7" rx="2"/><rect x="13" y="13" width="7" height="7" rx="2"/>',
});

export { icon };

/** Rail icon of a page: known module ids get their own, other modules the generic one. */
export function pageIcon(id) {
  return { deck: 'deck', settings: 'gear', files: 'folder', actions: 'bolt', quick_actions: 'bolt', search: 'search' }[id] || 'module';
}

/** The app glyph: three stacked rounded cards (the "deck"). Same drawing as assets/glyph.svg. */
export function appGlyph(size = 22) {
  return `<svg class="app-glyph" width="${size}" height="${size}" viewBox="0 0 64 64" aria-hidden="true">
    <defs>
      <linearGradient id="pd-g-back" x1="8" y1="8" x2="56" y2="40" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#8f7bff"/><stop offset="1" stop-color="#5b49e6"/></linearGradient>
      <linearGradient id="pd-g-mid" x1="8" y1="16" x2="56" y2="48" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#6aa4ff"/><stop offset="1" stop-color="#4a6cf0"/></linearGradient>
      <linearGradient id="pd-g-front" x1="8" y1="26" x2="56" y2="58" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#e3f0ff"/><stop offset="1" stop-color="#9fc2ff"/></linearGradient>
    </defs>
    <rect x="14" y="6" width="36" height="24" rx="7" fill="url(#pd-g-back)" opacity=".75"/>
    <rect x="10" y="16" width="44" height="26" rx="8" fill="url(#pd-g-mid)" opacity=".9"/>
    <rect x="6" y="28" width="52" height="28" rx="9" fill="url(#pd-g-front)"/>
    <rect x="6" y="28" width="52" height="28" rx="9" fill="none" stroke="rgba(255,255,255,.7)" stroke-width="1.4"/>
    <path d="M26 36.5v13l11-6.5Z" fill="#4a3fd1" opacity=".85"/>
  </svg>`;
}
