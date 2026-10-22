// Small modal question sheet (kit Overlay): used by buttons with `confirm`, by the debug-mode and
// panel-removal steps, and by the `confirm` event of a module job. Resolves true for Yes, false for
// No, Esc, the scrim and the close of the overlay. Requests are queued: one sheet at a time.

import { Overlay } from '../vendor/liquid_ui/js/overlay.js';
import { gel } from '../vendor/liquid_ui/js/glass.js';
import { escapeHtml } from '../vendor/liquid_ui/js/format.js';

let overlay = null;
let content = null;
let chain = Promise.resolve();
let settle = null;

function ensure() {
  if (overlay) return;
  const sheet = document.getElementById('confirm-sheet');
  content = sheet.querySelector('.ov-content');
  overlay = new Overlay(sheet, {
    modal: true,
    preset: 'sheet',
    onClose: () => {
      if (settle) settle(false);
    },
  });
}

/**
 * Ask a yes / no question.
 * @param {{question:string, title?:string, yes?:string, no?:string, danger?:boolean, origin?:HTMLElement|null}} o
 * @returns {Promise<boolean>}
 */
export function askConfirm(o) {
  const run = () => new Promise((resolve) => {
    ensure();
    const { question, title = '', yes = 'Yes', no = 'No', danger = false, origin = null } = o;
    content.innerHTML = `
      <div class="cf-body">
        ${title ? `<h2 id="confirm-title" class="cf-title">${escapeHtml(title)}</h2>` : '<span id="confirm-title" class="sr-only">Confirm</span>'}
        <p class="cf-question">${escapeHtml(question)}</p>
        <div class="cf-btns">
          <button type="button" class="btn${danger ? ' ov-focus' : ''}" data-ans="no">${escapeHtml(no)}</button>
          <button type="button" class="btn btn-tinted${danger ? ' btn-danger' : ' ov-focus'}" data-ans="yes">${escapeHtml(yes)}</button>
        </div>
      </div>`;
    content.querySelectorAll('.btn').forEach((b) => gel(b));
    let done = false;
    settle = (v) => {
      if (done) return;
      done = true;
      settle = null;
      resolve(v);
    };
    content.querySelector('[data-ans="yes"]').addEventListener('click', () => {
      settle(true);
      overlay.close();
    });
    content.querySelector('[data-ans="no"]').addEventListener('click', () => {
      settle(false);
      overlay.close();
    });
    overlay.show(origin && origin.isConnected ? origin : null);
  });
  const p = chain.then(run, run);
  // The next request waits for this answer and for the close animation.
  chain = p.then(() => new Promise((r) => setTimeout(r, 260)), () => {});
  return p;
}

/** True while the question sheet is open. */
export function confirmOpen() {
  return !!(overlay && overlay.open);
}
