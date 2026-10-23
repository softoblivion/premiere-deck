// Settings sheet (docs/HOST_UI.md 3.4): opened from the gear in the title bar, grows out of it (kit Overlay).
// Groups in this order: Appearance (the two theme cards), Behaviour, Updates (the host version and the kit update rows, the
// panel's installed and bundled versions with Check now and Reinstall), Premiere, then below the fold Panel,
// Modules, Tools, Data, About. Every change is saved at once through set_settings (or the panel / module / ffmpeg
// calls); a refusal restores the control and shows a toast. The values are refreshed each time the sheet opens.

import { Switch } from '../vendor/liquid_ui/js/controls.js';
import { gel } from '../vendor/liquid_ui/js/glass.js';
import { Overlay } from '../vendor/liquid_ui/js/overlay.js';
import { applyTheme, mountThemeControl, DEFAULT_THEME } from '../vendor/liquid_ui/js/theme.js';
import { toast } from '../vendor/liquid_ui/js/toast.js';
import { call } from './api.js';
import { store, notify, watch, sortedModules } from './store.js';
import { icon, ic } from './icons.js';
import { askConfirm } from './confirm.js';
import { h, failToast } from './ui.js';

const REPO_URL = 'https://github.com/softoblivion/premiere-deck';
const MODULE_STATE = { running: 'Running', stopped: 'Stopped', disabled: 'Turned off', failed: 'Failed', missing: 'Not installed in this build', import_failed: 'Could not be loaded' };

let overlay = null;
let gear = null;
let built = false;
let scroller = null;

/** Open the sheet from the gear (no-op while open). */
export function openSettings() {
  if (!overlay) return;
  refresh();
  if (scroller) scroller.scrollTop = 0;
  overlay.show(gear);
}

export function closeSettings() {
  if (overlay) overlay.close();
}

/** Fresh values from the shell; the cards repaint through the store watchers. */
function refresh() {
  call('panel_status').then((r) => { if (r) { store.panel = r; notify('panel', r); } }).catch(() => {});
  call('ffmpeg_status').then((r) => { if (r) { store.ffmpeg = r; notify('ffmpeg', r); } }).catch(() => {});
  call('get_settings').then((r) => { if (r) { store.settings = r; notify('settings', r); } }).catch(() => {});
  call('modules').then((r) => { if (Array.isArray(r)) { store.modules = r; notify('modules', r); } }).catch(() => {});
}

/**
 * Wire the gear and build the sheet.
 * @param {{update: object|null}} o  the kit update control (its `mountSettings` fills the host's update rows)
 */
export function initSettings({ update }) {
  if (built) return;
  built = true;
  const sheet = document.getElementById('settings-sheet');
  gear = document.getElementById('btn-settings');
  overlay = new Overlay(sheet, { modal: true, preset: 'sheet' });
  build(sheet.querySelector('.ov-content'), update);
  gear.addEventListener('click', () => {
    if (overlay.open) overlay.close();
    else openSettings();
  });
}

// ---- small builders -------------------------------------------------------------------------------

function button(label, { cls = '', onClick, disabled = false, title = null, glyph = null } = {}) {
  const b = h('button', `btn btn-s ${cls}`.trim(), { type: 'button', disabled, title },
    glyph ? h('span', 'ic-hold', { 'data-ic': '', html: ic(glyph) }) : null, h('span', '', { text: label }));
  gel(b);
  if (onClick) {
    b.addEventListener('click', async () => {
      if (b.classList.contains('is-busy')) return;
      b.classList.add('is-busy');
      b.setAttribute('aria-busy', 'true');
      try {
        await onClick(b);
      } finally {
        if (b.isConnected) {
          b.classList.remove('is-busy');
          b.removeAttribute('aria-busy');
        }
      }
    });
  }
  return b;
}

function row(label, small, ctl, cls = '') {
  return h('div', `set-row ${cls}`.trim(), null,
    h('div', 'set-label', null, h('span', '', { text: label }), small == null ? null : (small.nodeType ? small : h('small', '', { text: small }))),
    ctl ? h('div', 'set-ctl', null, ctl) : null);
}

/** A stacked row: label (and optional controls) on top, a wide body below, an optional hint. */
function colRow(label, body, { ctl = null, hint = null } = {}) {
  return h('div', 'set-row set-row-col', null,
    h('div', 'set-top', null, h('div', 'set-label', null, h('span', '', { text: label })), ctl ? h('div', 'set-ctl', null, ctl) : null),
    body,
    hint ? h('div', 'set-hint', { text: hint }) : null);
}

function group(title, card) {
  return h('section', 'set-group', null, h('h3', '', { text: title }), card);
}

/** Replace the rows of a card; null entries are skipped. */
const fill = (card, ...kids) => card.replaceChildren(...kids.filter(Boolean));

const mono = (text, title) => h('span', 'mono', { text: text || '—', title: title || text || null });
const path = (text) => h('span', 'path-text', { text: text || '—', title: text || null });

// ---- the sheet ------------------------------------------------------------------------------------

function build(content, update) {
  const controls = [];
  const inflight = new Map();

  const cardTheme = h('div', 'set-card');
  const cardBehaviour = h('div', 'set-card');
  const cardUpdates = h('div', 'set-card');
  const cardPanelVer = h('div', 'set-card set-card-follow');
  const cardPremiere = h('div', 'set-card');
  const cardPanel = h('div', 'set-card');
  const cardModules = h('div', 'set-card');
  const cardTools = h('div', 'set-card');
  const cardData = h('div', 'set-card');
  const cardAbout = h('div', 'set-card');

  const closeBtn = h('button', 'icon-btn icon-btn-plate sheet-x', { type: 'button', 'aria-label': 'Close settings', html: icon('x') });
  gel(closeBtn);
  closeBtn.addEventListener('click', () => overlay.close());

  scroller = h('div', 'sheet-scroll', null,
    group('Appearance', cardTheme),
    group('Behaviour', cardBehaviour),
    h('section', 'set-group', null, h('h3', '', { text: 'Updates' }), cardUpdates, cardPanelVer),
    group('Premiere', cardPremiere),
    group('Panel', cardPanel),
    group('Modules', cardModules),
    group('Tools', cardTools),
    group('Data', cardData),
    group('About', cardAbout));
  content.replaceChildren(h('header', 'sheet-head', null, h('h2', '', { id: 'settings-title', text: 'Settings' }), closeBtn), scroller);

  async function save(key, patch, revert) {
    inflight.set(key, (inflight.get(key) || 0) + 1);
    try {
      const s = await call('set_settings', patch);
      if (s) {
        store.settings = s;
        notify('settings', s);
      }
      return s;
    } catch (err) {
      failToast(err);
      revert && revert();
      return null;
    } finally {
      inflight.set(key, inflight.get(key) - 1);
    }
  }
  const busy = (k) => (inflight.get(k) || 0) > 0;

  async function refreshPremiere() {
    try {
      const st = await call('get_state');
      if (st && st.premiere) {
        store.premiere = st.premiere;
        notify('premiere', st.premiere);
      }
    } catch { /* keep the previous state */ }
  }

  // ---- Appearance ---------------------------------------------------------------------------------

  const theme = mountThemeControl(cardTheme, {
    value: (store.settings && store.settings.theme) || DEFAULT_THEME,
    onChange: (name) => {
      applyTheme(name);
      save('theme', { theme: name }, () => {
        const back = (store.settings && store.settings.theme) || DEFAULT_THEME;
        theme.set(back);
        applyTheme(back);
      });
    },
  });
  controls.push(theme);

  // ---- Behaviour ----------------------------------------------------------------------------------

  const keepSw = new Switch({
    label: 'Keep running with Premiere',
    checked: !!(store.settings && store.settings.keep_running_with_premiere),
    onChange: (v) => save('keep', { keep_running_with_premiere: v }, () => keepSw.set(!!(store.settings && store.settings.keep_running_with_premiere))),
  });
  controls.push(keepSw);
  cardBehaviour.append(row('Keep running with Premiere', 'Closing the window hides it to the tray', keepSw.el));

  // ---- Updates: the host (kit rows) and the panel -------------------------------------------------

  // The host's own version first (the kit's rows only say whether it is current), then the kit rows.
  cardUpdates.append(row(`Premiere Deck${store.version ? ` ${store.version}` : ''}`, update ? 'Installed version' : 'Updates are not available in this build', null));
  const updateRows = update ? update.mountSettings(cardUpdates) : null;
  if (updateRows) controls.push(updateRows);

  function paintPanelVersions() {
    const ps = store.panel;
    if (!ps) {
      fill(cardPanelVer, row('Panel', 'Reading the panel status', null));
      return;
    }
    const small = h('small', '', null,
      'Installed ', h('b', '', { text: ps.installed ? ps.installed_version || '?' : 'none' }),
      ' · bundled ', h('b', '', { text: ps.bundled_version || '?' }));
    const check = button('Check now', {
      glyph: 'panel-update',
      onClick: async () => {
        try {
          const r = await call('panel_check_update');
          if (r && r.updated) toast('success', `Panel updated · ${r.installed || 'none'} to ${r.bundled}`);
          else toast('info', `Panel is up to date · ${(r && (r.installed || r.bundled)) || ''}`.replace(/ · $/, ''));
          const fresh = await call('panel_status');
          if (fresh) {
            store.panel = fresh;
            notify('panel', fresh);
          }
        } catch (err) {
          failToast(err);
        }
      },
    });
    const reinstall = button('Reinstall', {
      glyph: 'reinstall',
      onClick: async () => {
        try {
          const r = await call('panel_reinstall');
          if (r) {
            store.panel = r;
            notify('panel', r);
          }
          const res = r && r.last_result;
          if (res && (res.action === 'failed' || res.action === 'no_source')) toast('error', res.message || 'The panel could not be installed.');
          else toast('success', 'The panel was reinstalled.');
        } catch (err) {
          failToast(err);
        }
      },
    });
    fill(cardPanelVer, h('div', 'set-row set-row-col', null,
      h('div', 'set-label', null, h('span', '', { text: 'Panel' }), small),
      h('div', 'set-btns set-btns-start', null, check, reinstall)));
  }

  // ---- Premiere -----------------------------------------------------------------------------------

  function paintPremiere() {
    const p = store.premiere || {};
    const s = store.settings || {};
    const auto = s.premiere_exe === 'auto' || !s.premiere_exe;
    const inst = (p.installs || []).find((i) => i.exe === p.exe);
    const version = (inst && inst.version) || p.premiere_version || null;
    const btns = h('div', 'set-btns', null,
      button('Choose file', {
        onClick: async () => {
          try {
            const r = await call('pick_premiere_exe');
            if (r) {
              store.settings = r;
              notify('settings', r);
              await refreshPremiere();
            }
          } catch (err) {
            failToast(err);
          }
        },
      }),
      button('Use detected', {
        disabled: auto,
        onClick: async () => {
          if (await save('premiere_exe', { premiere_exe: 'auto' })) await refreshPremiere();
        },
      }));
    fill(cardPremiere,
      colRow('Executable', path(p.exe || 'Not found'), { ctl: btns, hint: auto ? 'Detected automatically' : 'Chosen manually' }),
      row('Version', null, mono(version || '—')));
  }

  // ---- Panel --------------------------------------------------------------------------------------

  function paintPanel() {
    const ps = store.panel;
    paintPanelVersions();
    if (!ps) {
      fill(cardPanel, row('Status', 'Reading the panel status', null));
      return;
    }
    const dm = ps.debug_mode || { written_by_host: [], enabled: [] };
    const written = dm.written_by_host || [];
    const enabled = dm.enabled || [];
    const lr = ps.last_result;
    const lastMsg = lr && (lr.action === 'failed' || lr.action === 'no_source' || lr.action === 'skipped_junction') ? lr.message : null;

    const debugState = enabled.length ? `On · ${enabled.join(', ')}` : 'Off';
    const debugNote = enabled.length && !written.length ? 'Set outside Premiere Deck' : written.length ? `Written by Premiere Deck: ${written.join(', ')}` : null;

    const debugCtl = written.length ? button('Turn off', {
      onClick: async () => {
        try {
          const r = await call('panel_debug_mode', false);
          store.panel = { ...store.panel, debug_mode: r };
          notify('panel', store.panel);
          toast('success', 'Debug mode is off.');
        } catch (err) {
          failToast(err);
        }
      },
    }) : null;

    fill(cardPanel,
      colRow('Target folder', path(ps.target), { hint: ps.junction ? 'Linked to a source folder (development). Premiere Deck does not overwrite it.' : null }),
      // The value is long (the CSXS versions): it sits in a full-width well under the label, so the label column
      // never shrinks below its text.
      colRow('Extension debug mode', path(debugState), { ctl: debugCtl, hint: debugNote }),
      h('div', 'set-row set-actions', null,
        button('Remove the panel from Premiere', {
          cls: 'btn-danger',
          disabled: !ps.installed,
          onClick: async (b) => {
            const yes = await askConfirm({
              title: 'Remove the panel',
              question: 'The panel folder and the debug-mode values written by Premiere Deck are deleted. Premiere Pro needs a restart afterwards.',
              yes: 'Remove',
              danger: true,
              origin: b,
            });
            if (!yes) return;
            try {
              await call('panel_remove');
              const fresh = await call('panel_status');
              if (fresh) {
                store.panel = fresh;
                notify('panel', fresh);
              }
              toast('success', 'The panel was removed.');
            } catch (err) {
              failToast(err);
            }
          },
        })),
      lastMsg ? h('div', 'set-row', null, h('div', 'set-hint', { text: lastMsg })) : null);
  }

  // ---- Modules ------------------------------------------------------------------------------------

  const moduleSwitches = new Map(); // id -> Switch
  let modulesSig = '';
  function paintModules() {
    const mods = sortedModules();
    const sig = JSON.stringify(mods.map((m) => [m.id, m.title, m.description, m.enabled, m.state, m.error]));
    if (sig === modulesSig) return;
    modulesSig = sig;
    for (const sw of moduleSwitches.values()) sw.destroy();
    moduleSwitches.clear();
    if (!mods.length) {
      fill(cardModules, row('Modules', 'No modules in this build', null));
      return;
    }
    fill(cardModules, ...mods.map((m) => {
      const sw = new Switch({
        label: `${m.title} enabled`,
        checked: !!m.enabled,
        onChange: async (v) => {
          try {
            const r = await call('set_module_enabled', m.id, v);
            if (r) {
              store.modules = store.modules.map((x) => (x.id === r.id ? r : x));
              notify('modules', store.modules);
            }
          } catch (err) {
            failToast(err);
            sw.set(!v);
          }
        },
      });
      sw.setDisabled(m.state === 'missing' || m.state === 'import_failed');
      moduleSwitches.set(m.id, sw);
      const state = m.error || MODULE_STATE[m.state] || m.state;
      const small = h('small', m.state === 'failed' || m.state === 'import_failed' ? 't-error' : '', { text: [m.description, state].filter(Boolean).join(' · ') });
      return row(m.title, small, sw.el);
    }));
  }

  // ---- Tools (ffmpeg) -----------------------------------------------------------------------------

  function paintTools() {
    const f = store.ffmpeg;
    if (!f) {
      fill(cardTools, row('ffmpeg', 'Reading the ffmpeg status', null));
      return;
    }
    const ready = f.state === 'ready';
    const src = f.source === 'bin' ? 'Bundled with the app' : f.source === 'path' ? 'Found on the system path' : '—';
    fill(cardTools,
      row('ffmpeg', ready ? null : 'Needed to read media details such as duration and streams', h('div', 'set-btns', null,
        h('span', `badge ${ready ? 't-ok' : 't-warn'}`, { text: ready ? 'Ready' : 'Missing' }),
        ready ? null : button('Download', {
          onClick: async () => {
            try {
              const r = await call('ffmpeg_download');
              if (r) {
                store.ffmpeg = r;
                notify('ffmpeg', r);
              }
            } catch (err) {
              failToast(err);
            }
          },
        }))),
      ready ? row('Source', src, mono(f.version ? `v${f.version}` : '—')) : null,
      ready ? colRow('Path', path(f.path)) : null);
  }

  // ---- Data, About --------------------------------------------------------------------------------

  function paintData() {
    fill(cardData,
      colRow('Data folder', path(store.dataDir), {
        ctl: button('Open', {
          onClick: async () => {
            try {
              await call('open_data_folder');
            } catch (err) {
              failToast(err);
            }
          },
        }),
      }));
  }

  const link = button('github.com/softoblivion/premiere-deck', {
    cls: 'btn-ghost',
    onClick: async () => {
      try {
        await call('open_url', REPO_URL);
      } catch (err) {
        failToast(err);
      }
    },
  });
  cardAbout.append(
    row('Premiere Deck', null, mono(store.version ? `v${store.version}` : '—')),
    row('Licence', null, mono('GPL-3.0')),
    row('Source code', null, link));

  // ---- wiring -------------------------------------------------------------------------------------

  const syncSettings = (s) => {
    if (!s) return;
    if (!busy('keep')) keepSw.set(!!s.keep_running_with_premiere);
    if (!busy('theme')) theme.set(s.theme);
    paintPremiere();
  };
  watch('settings', syncSettings);
  watch('premiere', paintPremiere);
  watch('panel', paintPanel);
  watch('ffmpeg', paintTools);
  watch('modules', paintModules);

  paintPremiere();
  paintPanel();
  paintModules();
  paintTools();
  paintData();
}
