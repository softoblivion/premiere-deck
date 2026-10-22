// Settings page (docs/HOST_UI.md 3.5): Premiere Pro executable, panel status and repair, ffmpeg,
// data folder, behaviour, theme, update rows, About.

import { Switch } from '../../vendor/liquid_ui/js/controls.js';
import { gel } from '../../vendor/liquid_ui/js/glass.js';
import { applyTheme, mountThemeControl } from '../../vendor/liquid_ui/js/theme.js';
import { toast } from '../../vendor/liquid_ui/js/toast.js';
import { call } from '../api.js';
import { store, notify, watch } from '../store.js';
import { askConfirm } from '../confirm.js';
import { h, failToast } from '../ui.js';

const REPO_URL = 'https://github.com/softoblivion/premiere-deck';

function button(label, { cls = '', onClick, disabled = false, title = null } = {}) {
  const b = h('button', `btn btn-s ${cls}`.trim(), { type: 'button', disabled, title }, h('span', '', { text: label }));
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

function colRow(label, body, hint) {
  return h('div', 'set-row set-row-col', null, h('div', 'set-label', null, h('span', '', { text: label })), body, hint ? h('div', 'set-hint', { text: hint }) : null);
}

function group(title, card) {
  return h('section', 'set-group', null, h('h3', '', { text: title }), card);
}

/** Replace the rows of a card; null entries are skipped. */
const fill = (card, ...kids) => card.replaceChildren(...kids.filter(Boolean));

const mono = (text, title) => h('span', 'path-text mono', { text: text || '—', title: title || text || null });

export function mountSettings(host, { update }) {
  const offs = [];
  const controls = [];
  const inflight = new Map();

  const cardPremiere = h('div', 'set-card');
  const cardPanel = h('div', 'set-card');
  const cardFfmpeg = h('div', 'set-card');
  const cardApp = h('div', 'set-card');
  const cardTheme = h('div', 'set-card');
  const cardUpdates = h('div', 'set-card');
  const cardAbout = h('div', 'set-card');

  const left = h('div', 'set-col', null, group('Premiere Pro', cardPremiere), group('Panel', cardPanel), group('ffmpeg', cardFfmpeg));
  const right = h('div', 'set-col', null, group('Application', cardApp), group('Appearance', cardTheme), group('Updates', cardUpdates), group('About', cardAbout));
  const root = h('div', 'settings', null, left, right);
  host.append(root);

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

  // ---- Premiere Pro ---------------------------------------------------------------------------

  function paintPremiere() {
    const p = store.premiere || {};
    const s = store.settings || {};
    const auto = s.premiere_exe === 'auto' || !s.premiere_exe;
    const inst = (p.installs || []).find((i) => i.exe === p.exe);
    const version = (inst && inst.version) || p.premiere_version || null;
    fill(cardPremiere, 
      colRow('Executable', mono(p.exe || 'Not found', p.exe || ''), auto ? 'Detected automatically' : 'Chosen manually'),
      row('Version', null, mono(version || '—')),
      row('Location', null, h('div', 'set-btns', null,
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
        }))));
  }

  // ---- Panel ----------------------------------------------------------------------------------

  function paintPanel() {
    const ps = store.panel;
    if (!ps) {
      fill(cardPanel, row('Status', 'Reading the panel status', null));
      return;
    }
    const dm = ps.debug_mode || { written_by_host: [], enabled: [] };
    const written = dm.written_by_host || [];
    const enabled = dm.enabled || [];
    const state = !ps.installed ? 'Not installed' : ps.up_to_date ? 'Up to date' : 'Update pending';
    const installedCtl = h('div', 'set-btns', null, mono(ps.installed ? `v${ps.installed_version}` : '—'), h('span', `badge ${ps.installed && ps.up_to_date ? 't-ok' : 't-warn'}`, { text: state }));
    const lr = ps.last_result;
    const lastMsg = lr && (lr.action === 'failed' || lr.action === 'no_source' || lr.action === 'skipped_junction') ? lr.message : null;

    const debugState = enabled.length ? `On · ${enabled.join(', ')}` : 'Off';
    const debugNote = enabled.length && !written.length ? 'Set outside Premiere Deck' : written.length ? `Written by Premiere Deck: ${written.join(', ')}` : null;

    fill(cardPanel, 
      row('Installed version', null, installedCtl),
      row('Bundled version', null, mono(ps.bundled_version ? `v${ps.bundled_version}` : '—')),
      colRow('Target folder', mono(ps.target, ps.target), ps.junction ? 'Linked to a source folder (development). Premiere Deck does not overwrite it.' : null),
      colRow('Extension debug mode', h('div', 'set-btns set-btns-start', null, h('span', 'mono', { text: debugState }),
        written.length ? button('Turn off', {
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
        }) : null), debugNote),
      h('div', 'set-row set-actions', null,
        button('Reinstall', {
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
        }),
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

  // ---- ffmpeg ---------------------------------------------------------------------------------

  function paintFfmpeg() {
    const f = store.ffmpeg;
    if (!f) {
      fill(cardFfmpeg, row('Status', 'Reading the ffmpeg status', null));
      return;
    }
    const ready = f.state === 'ready';
    const src = f.source === 'bin' ? 'Bundled with the app' : f.source === 'path' ? 'Found on the system path' : '—';
    fill(cardFfmpeg, 
      row('Status', ready ? null : 'Needed for thumbnails and the AI search index', h('div', 'set-btns', null,
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
      ready ? colRow('Path', mono(f.path, f.path)) : null);
  }

  // ---- Application, theme, updates, about -----------------------------------------------------

  const keepSw = new Switch({
    label: 'Keep running with Premiere',
    checked: !!(store.settings && store.settings.keep_running_with_premiere),
    onChange: (v) => save('keep', { keep_running_with_premiere: v }, () => keepSw.set(!!store.settings.keep_running_with_premiere)),
  });
  controls.push(keepSw);

  function paintApp() {
    fill(cardApp, 
      colRow('Data folder', h('div', 'path-line', null, mono(store.dataDir, store.dataDir), button('Open', {
        onClick: async () => {
          try {
            await call('open_data_folder');
          } catch (err) {
            failToast(err);
          }
        },
      }))),
      row('Keep running with Premiere', 'Closing the window hides it to the tray while Premiere Pro is running', keepSw.el));
  }

  const theme = mountThemeControl(cardTheme, {
    value: (store.settings && store.settings.theme) || 'nova',
    onChange: (name) => {
      applyTheme(name);
      save('theme', { theme: name }, () => {
        theme.set(store.settings.theme);
        applyTheme(store.settings.theme);
      });
    },
  });
  controls.push(theme);

  const updateRows = update ? update.mountSettings(cardUpdates) : null;
  if (updateRows) controls.push(updateRows);

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

  // ---- wiring ---------------------------------------------------------------------------------

  const syncSettings = (s) => {
    if (!s) return;
    if (!busy('keep')) keepSw.set(!!s.keep_running_with_premiere);
    if (!busy('theme')) theme.set(s.theme);
    paintPremiere();
  };
  offs.push(watch('settings', syncSettings));
  offs.push(watch('premiere', paintPremiere));
  offs.push(watch('panel', paintPanel));
  offs.push(watch('ffmpeg', paintFfmpeg));

  paintPremiere();
  paintPanel();
  paintFfmpeg();
  paintApp();

  // Fresh values from the shell.
  call('panel_status').then((r) => { if (r) { store.panel = r; notify('panel', r); } }).catch(() => {});
  call('ffmpeg_status').then((r) => { if (r) { store.ffmpeg = r; notify('ffmpeg', r); } }).catch(() => {});
  call('get_settings').then((r) => { if (r) { store.settings = r; notify('settings', r); } }).catch(() => {});

  return {
    destroy() {
      offs.forEach((o) => o && o());
      controls.forEach((c) => c.destroy && c.destroy());
      root.remove();
    },
  };
}
