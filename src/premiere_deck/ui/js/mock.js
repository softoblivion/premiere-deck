// Mock backend: every method and event of docs/BRIDGE_API.md in plain JS, so every screen of the host window can
// be checked in a normal browser (index.html?mock=1). The viewport stands in for the window: open it at 420 x 560
// for the compact screens and 720 x 560 for main (the mock sends the `window` events, it cannot resize a browser).
//
// URL flags: premiere=not_running|starting|waiting|connected (default connected), offer=1 (waiting with the
// debug-mode offer), tab=files|actions, missing=1 (the Files module is not in this build), noexe=1 (Premiere Pro
// not found), noproject=1, ffmpeg=missing, update=none (no update is announced), job=1 (a Sort job runs),
// theme=black|sky. Settings changes live in memory; the theme is read from localStorage ('pd.theme') like the real
// shell's cache. Alt+1 / Alt+2 / Alt+3 switch Premiere to not running / waiting / connected (`fake_premiere`).
//
// Simulates: a Premiere launch (starting -> waiting -> connected when the "panel" opens), the widen and narrow
// `window` events, module actions with `result` events, a Sort job with progress and Stop, a confirm event, an
// update that becomes available a few seconds after start, the panel checks and repairs.

const VERSION = '2.0.0';
const DATA_DIR = 'D:\\Apps\\PremiereDeck\\premiere-deck_data';
const EXE = 'C:\\Program Files\\Adobe\\Adobe Premiere Pro 2026\\Adobe Premiere Pro.exe';
const THEMES = ['black', 'sky'];
const WIDEN_DELAY_MS = 450;
const RESIZE_MS = 300;

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (v) => (v == null ? v : JSON.parse(JSON.stringify(v)));
const nowS = () => Date.now() / 1000;
const btn = (id, label, extra = {}) => ({ kind: 'button', id, label, tone: 'neutral', busy: false, disabled: false, ...extra });
const hhmm = (epoch) => {
  const t = new Date(epoch * 1000);
  return `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
};

function fail(message) {
  return Promise.reject(new Error(message));
}

export function createMock({ params, dispatch }) {
  const emit = (name, payload) => setTimeout(() => dispatch(name, clone(payload)), 0);
  const flag = ['not_running', 'starting', 'waiting', 'connected'].includes(params.get('premiere')) ? params.get('premiere') : 'connected';
  const exeMissing = params.get('noexe') === '1';

  // ---- Premiere ---------------------------------------------------------------------------------
  const SPRING = { path: 'D:\\Footage\\spring-promo\\spring-promo_v2.prproj', name: 'spring-promo_v2', root: 'D:\\Footage\\spring-promo' };
  const premiere = {
    state: 'not_running', running: false, agent_connected: false, premiere_version: null, panel_version: null,
    project: null, project_path: null, exe: exeMissing ? null : EXE,
    installs: exeMissing ? [] : [{ exe: EXE, version: '26.5.2', source: 'registry' }],
    fallback_offer: false, displays: 0, fake: false,
  };
  const premiereOut = () => clone(premiere);
  let windowMode = 'compact';
  let widenTimer = 0;

  /** Put Premiere into a state; emits `premiere` and, when the main state starts or ends, `window`. */
  function setPremiere(state, { offer = false } = {}) {
    premiere.state = state;
    premiere.running = state !== 'not_running';
    premiere.agent_connected = state === 'connected' || (state === 'waiting' && !offer);
    premiere.premiere_version = state === 'waiting' || state === 'connected' ? '26.5.2' : null;
    premiere.panel_version = premiere.agent_connected ? VERSION : null;
    premiere.displays = state === 'connected' ? 1 : 0;
    premiere.fallback_offer = state === 'waiting' && offer;
    const project = params.get('noproject') === '1' ? null : { ...SPRING };
    premiere.project = state === 'connected' ? project : null;
    premiere.project_path = premiere.project ? premiere.project.path : null;
    emit('premiere', premiere);
    emitModules();
    const want = state === 'connected' ? 'wide' : 'compact';
    clearTimeout(widenTimer);
    if (want === windowMode) return;
    const go = () => {
      windowMode = want;
      emit('window', { mode: want, focused: true, ms: RESIZE_MS });
    };
    if (want === 'wide') widenTimer = setTimeout(go, WIDEN_DELAY_MS);
    else go();
  }

  // ---- settings ---------------------------------------------------------------------------------
  let cachedTheme = null;
  try {
    cachedTheme = localStorage.getItem('pd.theme');
  } catch { /* storage unavailable */ }
  const settings = {
    premiere_exe: 'auto',
    keep_running_with_premiere: true,
    theme: THEMES.includes(params.get('theme')) ? params.get('theme') : THEMES.includes(cachedTheme) ? cachedTheme : 'black',
    auto_download_updates: true,
    recent_projects: [],
    debug_mode_written: [],
  };

  // ---- modules ----------------------------------------------------------------------------------
  const missing = params.get('missing') === '1';
  const modules = [
    { id: 'files', package: 'files', title: 'Files', description: 'Keeps project media sorted on disk and in bins.', order: 10, state: missing ? 'missing' : 'running', enabled: !missing, error: null, status: null, requires: ['ffprobe'], has_page: !missing, has_settings: !missing },
    { id: 'actions', package: 'quick_actions', title: 'Quick actions', description: 'Buttons for routine timeline and project operations.', order: 20, state: 'running', enabled: true, error: null, status: null, requires: [], has_page: true, has_settings: false },
  ];
  const byId = (id) => modules.find((m) => m.id === id);

  // Files state
  const F = {
    inside: 142, outside: 4, offline: 1, toFile: 18, inSequences: 2,
    running: null, // { id, kind, done, total, file }
    journalAll: false,
    last: { text: 'Synced 4 files to Media', detail: 'Copied into D:\\Footage\\spring-promo\\Media', outcome: 'ok', at: nowS() - 600 },
    journal: [
      { id: 'j3', at: nowS() - 600, op: 'Synced', name: 'drone_0412.mp4', from: 'E:\\Downloads', to: 'Media', status: 'done', icon: 'sync-all' },
      { id: 'j2', at: nowS() - 610, op: 'Synced', name: 'voice-over_take3.wav', from: 'E:\\Recordings', to: 'Media', status: 'done', icon: 'sync-all' },
      { id: 'j1', at: nowS() - 3000, op: 'Sorted', name: 'C0042.MP4', from: 'Project', to: 'Footage', status: 'done', icon: 'sort' },
      { id: 'j0', at: nowS() - 8000, op: 'Imported', name: 'logo_sting.mov', from: '', to: 'Motion', status: 'done', icon: 'folder' },
    ],
  };
  const ACTION_LIST = [
    ['adjustment_layer', 'Adjustment layer', 'Timeline', 'Adds an adjustment layer over the selected clips.', 'adjustment-layer'],
    ['razor_all', 'Razor all tracks', 'Timeline', 'Cuts every unlocked track at the playhead.', 'razor-all'],
    ['delete_empty_tracks', 'Delete empty tracks', 'Timeline', 'Removes tracks that hold no clips.', 'delete-empty-tracks'],
    ['select_same_source', 'Select same source', 'Selection', 'Selects every clip of the same media.', 'select-same-source'],
    ['select_offline', 'Select offline clips', 'Selection', 'Selects clips whose media is offline.', 'select-offline'],
    ['scale_to_frame', 'Set to frame size', 'Clips', 'Turns on Set to Frame Size.', 'scale-to-frame'],
    ['label_by_bin', 'Colour labels by bin', 'Clips', 'Colours items by their top-level bin.', 'label-by-bin'],
    ['solo_track', 'Solo audio track', 'Tracks', 'Mutes every other audio track.', 'solo-track'],
    ['lock_others', 'Lock other tracks', 'Tracks', 'Locks every track without a selected clip.', 'lock-others'],
    ['save_version', 'Save version', 'Project', 'Saves the project as the next version.', 'save-version'],
    ['consolidate_duplicates', 'Consolidate duplicates', 'Project', 'Merges items of the same media.', 'consolidate'],
    ['open_project_folder', 'Open project folder', 'Project', 'Opens the project folder in Explorer.', 'open-folder'],
    ['reveal_clip', 'Reveal clip file', 'Project', 'Shows the selected clip in Explorer.', 'reveal-clip'],
    ['duplicate_sequence', 'Duplicate sequence', 'Sequence', 'Copies the active sequence as v2, v3, ...', 'duplicate-sequence'],
    ['social_copy', 'Social copy', 'Sequence', 'A copy of the sequence in a social format.', 'social-copy'],
    ['export_frame', 'Export frame', 'Export', 'PNG of the frame at the playhead.', 'export-frame'],
    ['export_sequence', 'Export sequence', 'Export', 'Queues the sequence with a preset.', 'export-sequence'],
  ];
  const hidden = new Set(['reveal_clip']);

  function moduleInfo(m) {
    const o = clone(m);
    if (m.state === 'running' && m.id === 'files') {
      o.status = premiere.project ? (F.running ? { text: `Sorting · ${F.running.done} / ${F.running.total}`, tone: 'accent' } : { text: `Watching · ${premiere.project.name}`, tone: 'ok' }) : { text: 'No project open', tone: 'neutral' };
    }
    return o;
  }
  const modulesOut = () => modules.map(moduleInfo);
  const emitModules = () => emit('modules', modulesOut());

  // ---- activity ---------------------------------------------------------------------------------
  const activity = [
    [3, 'neutral', null, 'Premiere Pro started'],
    [3.1, 'warn', 'files', '1 offline item in the project'],
    [3.3, 'ok', 'files', 'Sorted 18 files'],
    [3.6, 'neutral', 'files', 'Filed 3 new imports'],
    [3.8, 'ok', 'actions', 'Saved spring-promo_v002'],
    [4.0, 'error', 'actions', 'Select same source: no clip selected'],
    [4.1, 'ok', 'files', 'Synced 4 files to Media'],
    [4.2, 'ok', 'actions', 'Adjustment layer added'],
  ].map(([h, tone, module, text]) => ({ at: nowS() - (5 - h) * 3600, module, text, tone })).reverse();
  const addActivity = (a) => {
    const line = { at: nowS(), module: null, tone: 'neutral', ...a };
    activity.unshift(line);
    activity.length = Math.min(activity.length, 20);
    emit('activity', line);
  };
  const result = (module, id, outcome, text, detail = null) => {
    emit('result', { module, id, outcome, text, detail, at: Date.now() });
    addActivity({ module, text, tone: outcome });
  };

  // ---- jobs -------------------------------------------------------------------------------------
  const jobs = new Map();
  let jobSeq = 0;
  function startSort() {
    const id = `job${++jobSeq}`;
    const total = 142;
    F.running = { id, done: 0, total, file: 'C0001.MP4 > Footage' };
    const job = { id, title: 'Sort project', module: 'files', priority: 'normal', state: 'running', progress: 0, label: 'Sorting', error: null, created_at: nowS(), started_at: nowS(), finished_at: null };
    jobs.set(id, job);
    emit('job', job);
    emitModules();
    const timer = setInterval(() => {
      if (!F.running || F.running.id !== id) {
        clearInterval(timer);
        return;
      }
      F.running.done = Math.min(total, F.running.done + 3);
      F.running.file = `C00${String(F.running.done).padStart(2, '0')}.MP4 > Footage\\Camera A`;
      job.progress = F.running.done / total;
      emit('job', job);
      emitModules();
      if (F.running.done >= total) {
        clearInterval(timer);
        F.running = null;
        job.state = 'done';
        job.finished_at = nowS();
        jobs.delete(id);
        emit('job', job);
        F.toFile = 0;
        F.last = { text: 'Sorted 18 files', detail: '', outcome: 'ok', at: nowS() };
        result('files', 'files.sort', 'ok', 'Sorted 18 files');
        emitModules();
      }
    }, 300);
  }

  // ---- panel, ffmpeg, update --------------------------------------------------------------------
  const panel = {
    target: 'C:\\Users\\editor\\AppData\\Roaming\\Adobe\\CEP\\extensions\\com.softoblivion.premieredeck',
    extensions_dir: 'C:\\Users\\editor\\AppData\\Roaming\\Adobe\\CEP\\extensions',
    installed: true, installed_version: VERSION, bundled_version: VERSION, host_version: VERSION,
    junction: false, up_to_date: true, manifest_changed: false,
    debug_mode: { written_by_host: [], enabled: ['CSXS.11', 'CSXS.12'] },
    last_result: { action: 'none', version: VERSION, previous_version: VERSION, manifest_changed: false, message: 'The panel is up to date.' },
  };
  const ffmpeg = params.get('ffmpeg') === 'missing'
    ? { state: 'missing', source: null, path: null, version: null }
    : { state: 'ready', source: 'path', path: 'C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe', version: '9.0' };

  let update = { stage: 'up_to_date', current: VERSION, latest: VERSION, notes: null, size: null, published_at: null, progress: null, message: '', checked_at: nowS() - 720, can_apply: true };
  const NEW_UPDATE = {
    stage: 'available', current: VERSION, latest: '2.1.0', size: 48234496, published_at: nowS() - 86400, checked_at: nowS(), progress: null, message: '', can_apply: true,
    notes: '## Premiere Deck 2.1.0\n\n- Sort project shows the files it will move first\n- Faster Sync used media on long sequences\n\n---\n\nSee [the releases page](https://github.com/softoblivion/premiere-deck/releases) for details.',
  };
  const setUpdate = (patch) => {
    update = { ...update, ...patch };
    emit('update', update);
    return clone(update);
  };

  // ---- confirm ----------------------------------------------------------------------------------
  const pendingConfirm = new Map();
  let confirmSeq = 0;
  function askHost(module, question) {
    const id = `c${++confirmSeq}`;
    return new Promise((resolve) => {
      pendingConfirm.set(id, resolve);
      emit('confirm', { id, question, module });
    });
  }

  // ---- recent projects --------------------------------------------------------------------------
  const iso = (hoursAgo) => new Date(Date.now() - hoursAgo * 3600e3).toISOString();
  const recent = [
    { path: SPRING.path, name: 'spring-promo_v2', folder: 'D:\\Footage\\spring-promo\\Project', opened_at: iso(4), source: 'deck' },
    { path: 'D:\\Footage\\city-timelapse\\city-timelapse.prproj', name: 'city-timelapse', folder: 'D:\\Footage\\city-timelapse', opened_at: iso(30), source: 'deck' },
    { path: 'E:\\Work\\brand-reel\\Project\\brand-reel-2026.prproj', name: 'brand-reel-2026', folder: 'E:\\Work\\brand-reel\\Project', opened_at: iso(74), source: 'premiere' },
    { path: 'E:\\Clients\\Harbor\\harbor-doc.prproj', name: 'harbor-doc', folder: 'E:\\Clients\\Harbor', opened_at: iso(24 * 12), source: 'premiere' },
  ];

  // ---- module pages (the shapes of FILES.md 7 and QUICK_ACTIONS.md 3) ---------------------------
  const tile = (id, label, icon, meta, extra = {}) => btn(`files.${id}`, label, { icon, short: label, meta, ...extra });

  function filesPage() {
    const project = premiere.project;
    if (!project) {
      return { title: 'Files', status: { text: 'No project open', tone: 'neutral' }, blocks: [
        { type: 'text', id: 'no_project', text: 'Open a project in Premiere Pro to file its media. The layout below is what a new project gets.', tone: 'neutral', small: true },
        ...layoutBlocks(),
      ] };
    }
    const busy = !!F.running;
    const blocks = [
      { type: 'chips', id: 'status_chips', items: [
        { id: 'inside', text: `${F.inside} in the root`, tone: 'ok', icon: null },
        { id: 'outside', text: `${F.outside} outside`, tone: F.outside ? 'warn' : 'neutral', icon: null },
        { id: 'offline', text: `${F.offline} offline`, tone: F.offline ? 'error' : 'neutral', icon: null },
      ] },
      { type: 'actions', id: 'main', tiles: true, columns: 2, items: [
        tile('sort', 'Sort project', 'sort', F.toFile ? `${F.toFile} items to file` : 'Nothing to file', { busy: busy, disabled: busy, confirm: 'Sort the media of the project folder into the layout folders? Every move is journaled and can be undone.' }),
        tile('sync_all', 'Sync all media', 'sync-all', `${F.outside} outside the root`, { disabled: busy }),
        tile('sync_used', 'Sync used media', 'sync-used', `${F.inSequences} in sequences`, { disabled: busy }),
        tile('zip', 'Make ZIP', 'zip', 'Checks offline first', { disabled: busy }),
      ] },
    ];
    if (F.running) {
      blocks.push({ type: 'progress', id: `job_${F.running.id}`, value: F.running.done / F.running.total, label: `Sorting project ${F.running.done} / ${F.running.total}`, detail: F.running.file, cancel: 'files.stop' });
    }
    if (F.last) {
      blocks.push({ type: 'list', id: 'last_result', rows: [{ id: 'last', title: F.last.text, subtitle: F.last.detail || '', meta: hhmm(F.last.at), tone: F.last.outcome, thumb: null, actions: [btn('files.undo_last', 'Undo', { variant: 'ghost', hint: 'Undo the last change' })] }], empty: '' });
    }
    const shown = F.journalAll ? F.journal : F.journal.slice(0, 3);
    blocks.push({ type: 'table', id: 'journal', title: 'Journal', columns: [
      { key: 'at', label: 'When' }, { key: 'op', label: 'Change' }, { key: 'name', label: 'Name' },
      { key: 'from', label: 'From', mono: true }, { key: 'to', label: 'To', mono: true }, { key: 'status', label: 'Status' },
    ], rows: shown.map((e) => ({ id: e.id, tone: 'ok', at: hhmm(e.at), op: e.op, name: e.name, from: e.from, to: e.to, status: { text: e.status, tone: 'ok' }, actions: [btn('files.undo', 'Undo', { variant: 'ghost' })] })), empty: 'No changes yet.' });
    blocks.push({ type: 'actions', id: 'journal_actions', items: [btn('files.host:journal_all', F.journalAll ? 'Show fewer' : 'Show all', { hint: `${F.journal.length} entries in the journal.` })] });
    blocks.push(...layoutBlocks());
    return { title: 'Files', status: moduleInfo(byId('files')).status, subtitle: project.root, header_actions: [btn('files.open_folder', 'Open project folder', { variant: 'icon', icon: 'open-folder', hint: 'Opens the project folder in Explorer.' })], blocks };
  }

  function layoutBlocks() {
    const folders = ['Footage', 'Stock', 'Generated', 'Overlays', 'Audio/Music', 'Audio/SFX', 'Audio/Voice', 'Graphics', 'Motion', 'Media', 'Exports', 'Project'];
    const roles = { Media: 'Sync target', Exports: 'Renders', Project: 'Project file' };
    return [
      { type: 'form', id: 'layout', title: 'Layout', action: 'files.host:layout_select', hint: 'Rules and folders apply to this project; saving makes the layout custom.', fields: [
        { key: 'layout', label: 'Layout', kind: 'select', options: [{ value: 'default', label: 'Default' }, { value: 'custom', label: 'Custom (this project)' }], scope: 'project', value: 'default' },
      ] },
      { type: 'list', id: 'layout_folders', title: 'Folders', rows: folders.map((f) => ({ id: f, title: f, subtitle: roles[f] || '', meta: '', tone: 'neutral', thumb: null, actions: [] })), empty: '' },
      { type: 'table', id: 'layout_rules', title: 'Rules', hint: 'First match wins.', columns: [{ key: 'order', label: '#', align: 'right' }, { key: 'match', label: 'Match' }, { key: 'to', label: 'Folder' }], rows: [
        ['video · alpha', 'Overlays'], ['video · source generated', 'Generated'], ['video · source stock', 'Stock'], ['video', 'Footage'], ['audio · sounds like speech', 'Audio/Voice'], ['audio · sounds like music', 'Audio/Music'], ['image', 'Graphics'],
      ].map(([match, to], i) => ({ id: String(i + 1), order: String(i + 1), match, to, actions: [] })), empty: '' },
    ];
  }

  function actionsPage() {
    return {
      title: 'Quick actions',
      blocks: [
        { type: 'list', id: 'visibility', title: 'Shown in the panel', hint: 'Hidden actions are not shown in the panel.', rows: ACTION_LIST.map(([id, title, group, hint, icon]) => ({
          id, title, subtitle: `${group} · ${hint}`, icon, toggle: { id: `actions.host:visible.${id}`, value: !hidden.has(id) },
        })) },
        { type: 'list', id: 'presets', title: 'Export presets', hint: 'Add your own .epr files to D:\\Footage\\spring-promo\\.premiere-deck\\presets.', rows: [
          { id: 'youtube_1080p', title: 'YouTube 1080p', subtitle: 'Adobe system preset' },
          { id: 'instagram_9x16', title: 'Instagram 9:16', subtitle: 'Adobe system preset' },
          { id: 'match_h264', title: 'Match source H.264', subtitle: 'Adobe system preset' },
        ], empty: 'No export presets found.', actions: [btn('actions.host:open_presets', 'Open folder', { icon: 'open-folder' }), btn('actions.host:refresh_presets', 'Rescan', { icon: 'update' })] },
      ],
    };
  }

  // ---- module settings --------------------------------------------------------------------------
  const MS = {
    files: [
      { key: 'watch', label: 'File new media automatically', kind: 'bool', default: true, scope: 'project', hint: 'New files in the project folder are imported into the matching bin', value: true },
      { key: 'enforce_bins', label: 'Keep bins in step with folders', kind: 'bool', default: true, scope: 'project', hint: null, value: true },
      { key: 'import_exports', label: 'Import renders from Exports', kind: 'bool', default: false, scope: 'project', hint: null, value: false },
    ],
  };

  // ---- the backend object -----------------------------------------------------------------------
  const api = {
    async ready() {
      setTimeout(() => {
        if (params.get('update') === 'none') return;
        setUpdate(NEW_UPDATE);
      }, 6000);
      if (params.get('job') === '1') setTimeout(startSort, 800);
      return api.get_state();
    },
    async get_state() {
      return clone({
        version: VERSION, fake: false, data_dir: DATA_DIR, premiere, panel, install: null, modules: modulesOut(), recent,
        activity, jobs: Array.from(jobs.values()), ffmpeg, settings, server: { port: 47800, agent: null, displays: premiere.displays }, update,
        window: { mode: windowMode, focused: true },
      });
    },
    async get_settings() { return clone(settings); },
    async set_settings(patch) {
      await delay(60);
      const allowed = ['premiere_exe', 'keep_running_with_premiere', 'theme', 'auto_download_updates'];
      for (const k of Object.keys(patch || {})) if (!allowed.includes(k)) return fail(`The setting "${k}" cannot be changed here.`);
      if (patch.theme !== undefined && !THEMES.includes(patch.theme)) return fail('Unknown theme.');
      Object.assign(settings, patch);
      emit('settings', settings);
      return clone(settings);
    },
    async launch_premiere(project) {
      await delay(150);
      if (premiere.running && !project) return fail('Premiere Pro is already running.');
      if (premiere.running && premiere.state !== 'connected') return fail('Premiere Pro is running without the panel; open the project there.');
      if (!premiere.exe) return fail('Premiere Pro was not found. Choose its program file in Settings.');
      if (premiere.running) return premiereOut();
      setPremiere('starting');
      setTimeout(() => premiere.state === 'starting' && setPremiere('waiting'), 2200);
      setTimeout(() => premiere.state === 'waiting' && setPremiere('connected'), 5200);
      return premiereOut();
    },
    async pick_project() { await delay(200); return 'D:\\Footage\\city-timelapse\\city-timelapse.prproj'; },
    async pick_premiere_exe() {
      await delay(200);
      settings.premiere_exe = 'D:\\Adobe\\Adobe Premiere Pro 2026\\Adobe Premiere Pro.exe';
      premiere.exe = settings.premiere_exe;
      premiere.installs = [{ exe: premiere.exe, version: '26.5.2', source: 'manual' }];
      emit('settings', settings);
      emit('premiere', premiere);
      return clone(settings);
    },
    async recent_projects() { return clone(recent); },
    async modules() { return modulesOut(); },
    async set_module_enabled(id, enabled) {
      const m = byId(id);
      if (!m) return fail('Unknown module.');
      if (m.state === 'missing') return fail('This module is not installed in this build.');
      await delay(120);
      m.enabled = !!enabled;
      m.state = enabled ? 'running' : 'disabled';
      emitModules();
      return moduleInfo(m);
    },
    async module_page(id) {
      const m = byId(id);
      if (!m) return fail('Unknown module.');
      await delay(30);
      if (m.state !== 'running') return { module: moduleInfo(m), page: null };
      const page = { files: filesPage, actions: actionsPage }[id]();
      return clone({ module: moduleInfo(m), page });
    },
    async module_action(id, action, value, row) {
      await delay(140);
      const m = byId(id);
      if (!m || m.state !== 'running') return fail('That module is not running.');
      if (id === 'files') {
        switch (action) {
          case 'files.sort': startSort(); break;
          case 'files.stop':
            if (F.running) {
              const job = jobs.get(F.running.id);
              F.running = null;
              if (job) {
                job.state = 'cancelled';
                jobs.delete(job.id);
                emit('job', job);
              }
              result('files', 'files.sort', 'warn', 'Sort project stopped');
            }
            break;
          case 'files.sync_all':
            F.outside = 0;
            F.last = { text: 'Synced 4 files to Media', detail: '', outcome: 'ok', at: nowS() };
            result('files', action, 'ok', 'Synced 4 files to Media');
            break;
          case 'files.sync_used':
            result('files', action, 'ok', 'Synced 2 used files to Media');
            break;
          case 'files.zip':
            if (F.offline) result('files', action, 'warn', 'ZIP written · 1 offline item left out', 'spring-promo.zip beside the project folder');
            else result('files', action, 'ok', 'ZIP written');
            break;
          case 'files.undo_last':
          case 'files.undo': {
            const e = action === 'files.undo' ? F.journal.find((x) => x.id === row) : F.journal[0];
            if (e) F.journal = F.journal.filter((x) => x !== e);
            result('files', action, 'ok', e ? `Undone: ${e.name}` : 'Nothing to undo');
            break;
          }
          case 'files.open_folder': result('files', action, 'ok', 'Opened the project folder'); break;
          case 'files.host:journal_all': F.journalAll = !F.journalAll; break;
          case 'files.host:layout_select':
            askHost('files', 'Switch this project to another layout? Files already sorted stay where they are.').then((yes) => {
              emit('toast', { kind: yes ? 'success' : 'info', message: yes ? 'Layout changed.' : 'Layout kept.' });
            });
            break;
          default: emit('toast', { kind: 'info', message: `${action} done.` });
        }
      } else if (id === 'actions') {
        const m2 = /^actions\.host:visible\.(.+)$/.exec(action);
        if (m2) {
          if (value) hidden.delete(m2[1]);
          else hidden.add(m2[1]);
        } else if (action === 'actions.host:open_presets') emit('toast', { kind: 'info', message: 'Opened the presets folder.' });
        else if (action === 'actions.host:refresh_presets') emit('toast', { kind: 'info', message: 'Found 3 presets.' });
      }
      setTimeout(emitModules, 0);
      return { ok: true, toast: null };
    },
    async module_settings(id) {
      const fields = (MS[id] || []).map((f) => ({ ...f, available: f.scope !== 'project' || !!premiere.project }));
      return clone({ module: moduleInfo(byId(id)), fields, project: premiere.project ? premiere.project.name : null });
    },
    async set_module_setting(id, key, value, scope) {
      await delay(80);
      const f = (MS[id] || []).find((x) => x.key === key);
      if (!f) return fail('Unknown setting.');
      if (scope === 'project' && !premiere.project) return fail('Open a project in Premiere Pro first.');
      f.value = value;
      emitModules();
      return value;
    },
    async panel_status() { return clone(panel); },
    async panel_reinstall() {
      await delay(500);
      Object.assign(panel, { installed: true, installed_version: panel.bundled_version, up_to_date: true });
      panel.last_result = { action: 'installed', version: panel.bundled_version, previous_version: null, manifest_changed: false, message: 'The panel was installed.' };
      emit('panel', panel);
      return clone(panel);
    },
    async panel_check_update() {
      await delay(400);
      const installed = panel.installed_version;
      const updated = installed !== panel.bundled_version;
      if (updated) {
        Object.assign(panel, { installed: true, installed_version: panel.bundled_version, up_to_date: true });
        emit('panel', panel);
      }
      return { updated, installed, bundled: panel.bundled_version };
    },
    async panel_remove() {
      await delay(400);
      const removed = panel.debug_mode.written_by_host.slice();
      Object.assign(panel, { installed: false, installed_version: null, up_to_date: false });
      panel.debug_mode.written_by_host = [];
      settings.debug_mode_written = [];
      emit('panel', panel);
      return { removed: true, registry_removed: removed };
    },
    async panel_debug_mode(enable) {
      await delay(200);
      if (enable) {
        panel.debug_mode.written_by_host = ['CSXS.11', 'CSXS.12'];
        panel.debug_mode.enabled = ['CSXS.11', 'CSXS.12'];
        settings.debug_mode_written = ['CSXS.11', 'CSXS.12'];
      } else {
        panel.debug_mode.enabled = panel.debug_mode.enabled.filter((k) => !panel.debug_mode.written_by_host.includes(k));
        panel.debug_mode.written_by_host = [];
        settings.debug_mode_written = [];
      }
      emit('panel', panel);
      return clone(panel.debug_mode);
    },
    async ffmpeg_status() { return clone(ffmpeg); },
    async ffmpeg_download() {
      await delay(1500);
      Object.assign(ffmpeg, { state: 'ready', source: 'bin', path: `${DATA_DIR}\\bin\\ffmpeg.exe`, version: '9.0' });
      return clone(ffmpeg);
    },
    async answer_confirm(id, yes) {
      const r = pendingConfirm.get(id);
      pendingConfirm.delete(id);
      if (r) r(!!yes);
      return !!r;
    },
    async open_data_folder() { return true; },
    async reveal() { return true; },
    async open_path() { return true; },
    async open_url(url) {
      if (!/^https:\/\//.test(url)) return false;
      console.info('[mock] open_url', url);
      return true;
    },
    async window_minimize() { console.info('[mock] window_minimize'); return null; },
    async window_close() { console.info('[mock] window_close'); return null; },
    async quit() { return null; },
    async update_status() { return clone(update); },
    async check_updates() {
      setUpdate({ stage: 'checking' });
      setTimeout(() => (params.get('update') === 'none' ? setUpdate({ stage: 'up_to_date', checked_at: nowS() }) : setUpdate({ ...NEW_UPDATE, checked_at: nowS() })), 900);
      return clone(update);
    },
    async download_update() {
      setUpdate({ stage: 'downloading', progress: 0 });
      const t0 = performance.now();
      const timer = setInterval(() => {
        const p = Math.min(1, (performance.now() - t0) / 3000);
        if (p >= 1) {
          clearInterval(timer);
          setUpdate({ stage: 'ready', progress: 1 });
        } else setUpdate({ stage: 'downloading', progress: p });
      }, 300);
      return clone(update);
    },
    async apply_update() { console.info('[mock] apply_update'); return true; },
    async fake_premiere(state) {
      if (!['not_running', 'waiting', 'connected'].includes(state)) return fail('Unknown state.');
      setPremiere(state);
      return premiereOut();
    },
    async log(level, message) { console.debug(`[mock] log ${level}: ${message}`); return null; },
  };

  // The start state of the URL flags (the window starts in its final mode, as after a show from the tray).
  if (['files', 'actions'].includes(params.get('tab'))) {
    try {
      localStorage.setItem('pd.tab', params.get('tab'));
    } catch { /* storage unavailable */ }
  }
  premiere.state = flag;
  setPremiere(flag, { offer: params.get('offer') === '1' });
  clearTimeout(widenTimer);
  windowMode = flag === 'connected' ? 'wide' : 'compact';
  return api;
}
