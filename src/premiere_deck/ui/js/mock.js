// Mock backend: every method and event of docs/BRIDGE_API.md in plain JS, so every page and state of
// the host window can be checked in a normal browser (index.html?mock=1).
//
// URL flags: premiere=not_running|starting|connected|no_panel (default connected), missing=1 (the Files
// module is not in this build), noexe=1 (Premiere Pro not found), ffmpeg=missing, update=none (no update
// is announced), theme=<name>. Settings changes live in memory; the theme is read from localStorage
// ('pd.theme') like the real shell's cache.
//
// Simulates: a running job with moving progress, the activity feed, an update that becomes available a
// few seconds after start (so the update sheet opens), module actions with toasts, a confirm event on
// "Collect", a Premiere launch (starting -> connected), debug-mode and panel repair calls.

const VERSION = '1.0.0';
const DATA_DIR = 'D:\\Apps\\PremiereDeck\\premiere-deck_data';
const EXE = 'C:\\Program Files\\Adobe\\Adobe Premiere Pro 2026\\Adobe Premiere Pro.exe';
const THEMES = ['dusk', 'nova', 'frost', 'bloom'];

const delay = (ms) => new Promise((r) => setTimeout(r, ms));
const clone = (v) => (v == null ? v : JSON.parse(JSON.stringify(v)));
const nowS = () => Date.now() / 1000;
const btn = (id, label, extra = {}) => ({ kind: 'button', id, label, ...extra });

function fail(message) {
  return Promise.reject(new Error(message));
}

/** A placeholder frame: a gradient sky, a sun and two hills, as an SVG data URL. */
function thumb(i) {
  const a = (i * 47 + 200) % 360;
  const b = (a + 50) % 360;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="320" height="180" viewBox="0 0 320 180">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="hsl(${a} 62% 38%)"/><stop offset="1" stop-color="hsl(${b} 70% 16%)"/></linearGradient></defs>
    <rect width="320" height="180" fill="url(#g)"/>
    <circle cx="${70 + (i * 37) % 180}" cy="${52 + (i * 13) % 30}" r="${16 + (i % 3) * 5}" fill="hsl(${(a + 150) % 360} 90% 78%)" opacity=".85"/>
    <path d="M0 150 Q70 ${100 + (i % 4) * 8} 150 142 T320 ${128 + (i % 3) * 8} V180 H0Z" fill="hsl(${b} 55% 12%)" opacity=".9"/>
    <path d="M0 168 Q90 ${140 + (i % 5) * 4} 190 162 T320 158 V180 H0Z" fill="hsl(${b} 50% 8%)"/></svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

export function createMock({ params, dispatch }) {
  const emit = (name, payload) => setTimeout(() => dispatch(name, clone(payload)), 0);
  const flag = params.get('premiere') || 'connected';
  const exeMissing = params.get('noexe') === '1';

  // ---- Premiere ---------------------------------------------------------------------------------
  const SPRING = { path: 'D:\\Video\\Edits\\Spring_Promo.prproj', name: 'Spring_Promo.prproj', root: 'D:\\Video\\Edits' };
  const premiere = {
    state: flag,
    running: flag !== 'not_running',
    premiere_version: flag === 'not_running' || flag === 'starting' ? null : '26.0.0',
    panel_version: flag === 'connected' ? '1.0.0' : null,
    project: flag === 'connected' ? { ...SPRING } : null,
    project_path: flag === 'connected' ? SPRING.path : null,
    exe: exeMissing ? null : EXE,
    installs: exeMissing ? [] : [{ exe: EXE, version: '26.0.0', source: 'registry' }],
    fallback_offer: flag === 'no_panel',
    displays: 1,
    fake: false,
  };
  const premiereOut = () => clone(premiere);

  // ---- settings ---------------------------------------------------------------------------------
  let cachedTheme = null;
  try {
    cachedTheme = localStorage.getItem('pd.theme');
  } catch { /* storage unavailable */ }
  const settings = {
    premiere_exe: 'auto',
    keep_running_with_premiere: true,
    theme: THEMES.includes(params.get('theme')) ? params.get('theme') : THEMES.includes(cachedTheme) ? cachedTheme : 'nova',
    auto_download_updates: true,
    recent_projects: [],
    debug_mode_written: [],
  };

  // ---- modules ----------------------------------------------------------------------------------
  const modules = [
    { id: 'files', package: 'files', title: 'Files', description: 'Media sorted on disk and in bins, collect and pack.', order: 10, state: params.get('missing') === '1' ? 'missing' : 'running', enabled: params.get('missing') !== '1', error: null, status: null, requires: ['ffmpeg'], has_page: true, has_settings: true },
    { id: 'actions', package: 'quick_actions', title: 'Quick actions', description: 'Routine timeline and project operations.', order: 20, state: 'running', enabled: true, error: null, status: null, requires: [], has_page: true, has_settings: true },
    { id: 'search', package: 'search', title: 'AI search', description: 'Local CLIP index of the footage in the project.', order: 30, state: 'running', enabled: true, error: null, status: null, requires: ['ffmpeg'], has_page: true, has_settings: true },
  ];
  if (modules[0].state === 'missing') {
    modules[0].has_page = false;
    modules[0].has_settings = false;
  }
  const byId = (id) => modules.find((m) => m.id === id);

  // per-module runtime state
  const F = {
    filter: 'all', search: '', collecting: false, progress: 0, packName: 'Delivery_v2', packInclude: false, packFormat: 'zip',
    options: { copy_media: true, folder_template: 'type', name_suffix: '' },
    journal: [],
  };
  const names = ['Interview_A.mp4', 'Interview_B.mp4', 'Broll_beach_014.mov', 'Broll_street_002.mov', 'Drone_cliff.mp4', 'Voiceover_take3.wav', 'Music_bed_main.mp3', 'Title_card.png', 'Logo_sting.mov', 'Broll_market_007.mov', 'Ambience_forest.wav', 'Lower_third.png', 'Interview_C.mp4', 'Timelapse_sky.mp4'];
  const ops = [['Copied', 'ok'], ['Filed', 'accent'], ['Linked', 'neutral']];
  names.forEach((n, i) => {
    const op = ops[i % 3];
    const size = 3 + ((i * 37) % 90) + (i % 2 ? 0.4 : 0.7);
    F.journal.push({ id: `j${i}`, time: `${String(9 + Math.floor(i / 4)).padStart(2, '0')}:${String((i * 13) % 60).padStart(2, '0')}`, name: n, op: op[0], tone: op[1], dest: `${/\.wav|\.mp3/.test(n) ? 'Audio' : /\.png/.test(n) ? 'Graphics' : 'Video'}\\${n}`, size: `${size.toFixed(1)} MB` });
  });
  const A = {
    visible: { adjustment_layer: true, markers_from_cuts: true, ripple_gaps: true, color_labels: false, nest_selection: true, export_frame: true, disable_unused: false, rename_clips: true },
    options: { presets_folder: 'D:\\Presets\\Export', confirm_destructive: true },
  };
  const ACTIONS = [
    ['adjustment_layer', 'Add adjustment layer', 'Above the selected clips', 'Ctrl+1'],
    ['markers_from_cuts', 'Markers from cuts', 'One marker per edit on the active sequence', 'Ctrl+2'],
    ['ripple_gaps', 'Close gaps', 'Ripple delete empty space on all tracks', 'Ctrl+3'],
    ['color_labels', 'Label by track', 'Color labels follow the track', ''],
    ['nest_selection', 'Nest selection', 'Combine selected clips into a nested sequence', 'Ctrl+4'],
    ['export_frame', 'Export frame', 'PNG of the frame under the playhead', 'Ctrl+5'],
    ['disable_unused', 'Disable unused', 'Turn off clips outside the work area', ''],
    ['rename_clips', 'Rename clips', 'Name clips after their source files', ''],
  ];
  const S = { query: '', limit: 8, searching: false, indexing: false, indexProgress: 0, hits: [], device: 'gpu' };
  S.hits = makeHits('beach', 8);

  function makeHits(q, n) {
    const words = ['Beach at sunset', 'Wide shot, shoreline', 'Drone over cliffs', 'Street market', 'Interview, window light', 'City at night', 'Forest path', 'Close-up, hands', 'Harbour, morning', 'Crowd, festival', 'Sky timelapse', 'Waves, slow motion'];
    return Array.from({ length: n }, (_, i) => ({
      id: `h${i}`, title: words[(i + q.length) % words.length], subtitle: `${names[(i * 3) % names.length]} · ${((i * 17) % 4) + 1}:${String((i * 23) % 60).padStart(2, '0')}`,
      thumb: thumb(i + q.length * 3), score: 0.34 - i * 0.017,
      times: [0, 1, 2].slice(0, 1 + ((i + 1) % 3)).map((t) => ({ seconds: 12.5 + t * 31 + i, label: `0:${String(12 + t * 31 + i).padStart(2, '0')}`, bar: Math.max(0.15, 0.95 - t * 0.3 - i * 0.05) })),
    }));
  }

  const projectName = () => (premiere.project ? premiere.project.name : null);
  const todayCount = () => F.journal.length;

  function moduleInfo(m) {
    const o = clone(m);
    if (m.state === 'running') {
      if (m.id === 'files') o.status = { text: F.collecting ? `Collecting · ${Math.round(F.progress * 100)}%` : `Watching · ${todayCount()} filed today`, tone: F.collecting ? 'accent' : 'ok' };
      if (m.id === 'actions') o.status = { text: `${Object.values(A.visible).filter(Boolean).length} actions shown`, tone: 'ok' };
      if (m.id === 'search') o.status = { text: S.indexing ? `Indexing · ${Math.round(S.indexProgress * 100)}%` : '1,248 clips indexed', tone: S.indexing ? 'accent' : 'ok' };
    } else if (m.state === 'disabled') o.status = null;
    return o;
  }
  const modulesOut = () => modules.map(moduleInfo);
  const emitModules = () => emit('modules', modulesOut());

  // ---- activity and jobs ------------------------------------------------------------------------
  const activity = [
    { at: nowS() - 140, module: 'files', text: 'Collect project finished', tone: 'ok' },
    { at: nowS() - 620, module: 'search', text: 'Indexed 42 new clips', tone: 'ok' },
    { at: nowS() - 3100, module: 'actions', text: 'Add adjustment layer ran on 3 clips', tone: 'neutral' },
    { at: nowS() - 7200, module: null, text: 'Panel is up to date', tone: 'neutral' },
    { at: nowS() - 30000, module: 'files', text: 'Could not copy Drone_cliff.mp4: disk is full', tone: 'error' },
  ];
  const addActivity = (a) => {
    const line = { at: nowS(), module: null, tone: 'neutral', ...a };
    activity.unshift(line);
    activity.length = Math.min(activity.length, 20);
    emit('activity', line);
  };

  const jobs = new Map();
  let jobSeq = 0;
  function startJob({ title, module, label = '', seconds = 6, onDone, loop = false }) {
    const id = `job${++jobSeq}`;
    const job = { id, title, module, priority: 0, state: 'running', progress: 0, label, error: null, created_at: nowS(), started_at: nowS(), finished_at: null };
    jobs.set(id, job);
    emit('job', job);
    const t0 = performance.now();
    const timer = setInterval(() => {
      job.progress = Math.min(1, (performance.now() - t0) / (seconds * 1000));
      if (onDone && onDone.tick) onDone.tick(job.progress);
      if (job.progress >= 1) {
        clearInterval(timer);
        job.state = 'done';
        job.finished_at = nowS();
        jobs.delete(id);
        emit('job', job);
        onDone && onDone.done && onDone.done();
        addActivity({ module, text: `${title} finished`, tone: 'ok' });
      } else emit('job', job);
    }, 350);
    return job;
  }

  // ---- panel, ffmpeg, update --------------------------------------------------------------------
  const panel = {
    target: 'C:\\Users\\editor\\AppData\\Roaming\\Adobe\\CEP\\extensions\\com.softoblivion.premiere-deck',
    extensions_dir: 'C:\\Users\\editor\\AppData\\Roaming\\Adobe\\CEP\\extensions',
    installed: true, installed_version: '1.0.0', bundled_version: '1.0.0', host_version: VERSION,
    junction: false, up_to_date: true, manifest_changed: false,
    debug_mode: { written_by_host: [], enabled: ['CSXS.11', 'CSXS.12'] },
    last_result: { action: 'none', version: '1.0.0', previous_version: '1.0.0', manifest_changed: false, message: 'The panel is up to date.' },
  };
  const ffmpeg = params.get('ffmpeg') === 'missing'
    ? { state: 'missing', source: null, path: null, version: null }
    : { state: 'ready', source: 'path', path: 'C:\\Tools\\ffmpeg\\bin\\ffmpeg.exe', version: '9.0' };

  let update = { stage: 'up_to_date', current: VERSION, latest: VERSION, notes: null, size: null, published_at: null, progress: null, message: '', checked_at: nowS(), can_apply: true };
  const NEW_UPDATE = {
    stage: 'available', current: VERSION, latest: '0.2.0', size: 48234496, published_at: nowS() - 86400, checked_at: nowS(), progress: null, message: '', can_apply: true,
    notes: '## Premiere Deck 0.2.0\n\n- Faster journal search in Files\n- Quick actions can be hidden one by one\n- AI search keeps its index between sessions\n\n---\n\nSee [the releases page](https://github.com/softoblivion/premiere-deck/releases) for details.',
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
  const recent = [
    { path: SPRING.path, name: SPRING.name, folder: SPRING.root, opened_at: new Date(Date.now() - 3600e3).toISOString(), source: 'deck' },
    { path: 'D:\\Video\\Edits\\Summer_Reel.prproj', name: 'Summer_Reel.prproj', folder: 'D:\\Video\\Edits', opened_at: new Date(Date.now() - 26 * 3600e3).toISOString(), source: 'deck' },
    { path: 'E:\\Clients\\Northwind\\Northwind_Ad_v3.prproj', name: 'Northwind_Ad_v3.prproj', folder: 'E:\\Clients\\Northwind', opened_at: new Date(Date.now() - 6 * 86400e3).toISOString(), source: 'premiere' },
    { path: 'E:\\Clients\\Harbor\\Harbor_Documentary_Final_Cut.prproj', name: 'Harbor_Documentary_Final_Cut.prproj', folder: 'E:\\Clients\\Harbor', opened_at: new Date(Date.now() - 31 * 86400e3).toISOString(), source: 'premiere' },
  ];

  // ---- module pages -----------------------------------------------------------------------------
  function filesPage() {
    const rows = F.journal.filter((r) => (F.filter === 'all' || r.op.toLowerCase() === F.filter) && (!F.search || r.name.toLowerCase().includes(F.search.toLowerCase())));
    return {
      title: 'Files',
      status: modulesOut()[0].status,
      blocks: [
        { type: 'cards', id: 'overview', items: [
          { id: 'today', title: 'Filed today', value: String(todayCount()), detail: `Last: ${F.journal[F.journal.length - 1].name}`, tone: 'ok' },
          { id: 'plan', title: 'Collect plan', value: '128 files', detail: '4.2 GB to copy in 3 folders', progress: F.collecting ? F.progress : null, actions: [btn('files.collect', 'Run', { busy: F.collecting, confirm: null })] },
          { id: 'journal', title: 'Journal', value: String(F.journal.length), detail: 'entries in this project', actions: [btn('files.undo_last', 'Undo last', { confirm: 'Undo the last journal entry?' })] },
          { id: 'space', title: 'Free space', value: '412 GB', detail: 'on drive D:', tone: 'neutral' },
        ] },
        ...(F.collecting ? [{ type: 'progress', id: 'collect-progress', value: F.progress, label: 'Collecting project files' }] : []),
        { type: 'table', id: 'journal-table', title: 'Journal', columns: [
          { key: 'time', label: 'Time', mono: true }, { key: 'name', label: 'File' }, { key: 'op', label: 'Operation' }, { key: 'dest', label: 'Destination' }, { key: 'size', label: 'Size', align: 'right', mono: true },
        ], rows: rows.map((r) => ({ id: r.id, tone: r.tone, time: r.time, name: r.name, op: { text: r.op, tone: r.tone }, dest: r.dest, size: r.size, actions: [btn('files.undo', 'Undo', { confirm: `Remove the copy of ${r.name}?` })] })),
          empty: 'No journal entries match.',
          filters: [{ kind: 'select', id: 'files.filter', label: 'Show', value: F.filter, options: [{ value: 'all', label: 'All operations' }, { value: 'copied', label: 'Copied' }, { value: 'filed', label: 'Filed' }, { value: 'linked', label: 'Linked' }] }],
          search: { kind: 'input', id: 'files.search', label: 'Search', value: F.search, placeholder: 'Search by file name' } },
        { type: 'actions', id: 'files-actions', items: [btn('files.collect', 'Collect project', { tone: 'accent' }), btn('files.refresh', 'Refresh journal'), btn('host:open_journal', 'Open journal folder')] },
        { type: 'form', id: 'layout', title: 'Layout options', hint: 'Applied by Collect project and by automatic filing.', action: 'files.set_option', fields: [
          { key: 'copy_media', label: 'Copy media files', kind: 'bool', value: F.options.copy_media, hint: 'Off keeps the originals and links them' },
          { key: 'folder_template', label: 'Folder layout', kind: 'select', value: F.options.folder_template, options: [{ value: 'type', label: 'By media type' }, { value: 'date', label: 'By shoot date' }, { value: 'flat', label: 'One folder' }] },
          { key: 'name_suffix', label: 'Name suffix', kind: 'text', value: F.options.name_suffix, hint: 'Added to copied file names' },
        ] },
        { type: 'form', id: 'pack', title: 'Pack project', hint: 'Copies the project and its media into one archive.', fields: [
          { kind: 'input', id: 'files.pack_name', label: 'Archive name', value: F.packName, placeholder: 'Archive name' },
          { kind: 'toggle', id: 'files.pack_include', label: 'Include unused media', value: F.packInclude },
          { kind: 'select', id: 'files.pack_format', label: 'Format', value: F.packFormat, options: [{ value: 'zip', label: 'ZIP archive' }, { value: 'folder', label: 'Folder' }] },
        ], submit: btn('files.pack', 'Pack project') },
        { type: 'text', id: 'foot', text: 'The originals are never moved or deleted. Every step is written to the journal.', small: true },
      ],
    };
  }

  function actionsPage() {
    return {
      title: 'Quick actions',
      status: modulesOut()[1].status,
      blocks: [
        { type: 'text', id: 'intro', text: 'Actions run on the active sequence in Premiere Pro. Hidden actions stay out of the panel.', small: true },
        { type: 'list', id: 'action-list', title: 'Actions', empty: 'No actions.', rows: ACTIONS.map(([key, title, sub, keys]) => ({
          id: key, title, subtitle: sub, meta: keys, tone: A.visible[key] ? 'ok' : 'neutral',
          actions: [btn(`actions.${key}`, 'Run', { disabled: premiere.state !== 'connected', hint: premiere.state !== 'connected' ? 'Premiere Pro is not connected' : null }), btn('actions.toggle_visible', A.visible[key] ? 'Hide' : 'Show')],
        })) },
        { type: 'actions', id: 'act-actions', items: [btn('host:open_presets', 'Open presets folder'), btn('actions.show_all', 'Show all'), btn('actions.reset', 'Reset to defaults', { tone: 'error', confirm: 'Reset the visibility of all actions?' })] },
        { type: 'form', id: 'presets', title: 'Export presets', action: 'actions.set_option', fields: [
          { key: 'presets_folder', label: 'Presets folder', kind: 'path', value: A.options.presets_folder, hint: 'Folder with .epr files' },
          { key: 'confirm_destructive', label: 'Ask before destructive steps', kind: 'bool', value: A.options.confirm_destructive },
        ] },
      ],
    };
  }

  function searchPage() {
    return {
      title: 'AI search',
      status: modulesOut()[2].status,
      blocks: [
        { type: 'cards', id: 'index', items: [
          { id: 'idx', title: 'Index', value: '1,248 clips', detail: S.indexing ? `Indexing ${Math.round(S.indexProgress * 100)}%` : 'Updated 12 min ago', tone: 'ok', progress: S.indexing ? S.indexProgress : null, actions: [btn('search.reindex', 'Update index', { busy: S.indexing })] },
          { id: 'model', title: 'Model', value: 'Ready', detail: 'CLIP ViT-B/32', tone: 'ok' },
          { id: 'dev', title: 'Device', value: S.device === 'gpu' ? 'GPU' : 'CPU', detail: S.device === 'gpu' ? 'DirectML' : 'Slower, no GPU used' },
        ] },
        { type: 'form', id: 'query', title: 'Search', fields: [
          { kind: 'input', id: 'search.query', label: 'Describe a shot', value: S.query, placeholder: 'For example: wide beach at sunset', submit_label: 'Search' },
          { kind: 'select', id: 'search.limit', label: 'Results', value: String(S.limit), options: [{ value: '4', label: '4 results' }, { value: '8', label: '8 results' }, { value: '12', label: '12 results' }] },
        ] },
        { type: 'grid', id: 'results', title: 'Results', columns: 4, busy: S.searching, empty: 'No results yet. Describe a shot and press Search.', items: S.hits.slice(0, S.limit).map((hit) => ({
          id: hit.id, title: hit.title, subtitle: hit.subtitle, thumb: hit.thumb, badge: hit.score.toFixed(2), bar: Math.min(1, hit.score * 2.4),
          times: hit.times, actions: [btn('search.add', 'Add to timeline'), btn('search.reveal', 'Reveal')],
        })) },
      ],
    };
  }

  // ---- module settings --------------------------------------------------------------------------
  const MS = {
    files: [
      { key: 'auto_file', label: 'File new media automatically', kind: 'bool', default: true, scope: 'host', hint: 'Sorts files as they are imported', value: true },
      { key: 'max_depth', label: 'Folder depth', kind: 'int', default: 3, scope: 'host', hint: 'From 1 to 8', value: 3 },
      { key: 'bin_layout', label: 'Bin layout', kind: 'select', default: 'mirror', options: [{ value: 'mirror', label: 'Mirror folders' }, { value: 'type', label: 'By media type' }], scope: 'project', hint: 'Stored with the project', value: 'mirror' },
      { key: 'library_folder', label: 'Library folder', kind: 'path', default: '', scope: 'project', hint: 'Where collected media goes', value: 'D:\\Video\\Edits\\Library' },
    ],
    actions: [
      { key: 'default_duration', label: 'Default duration (frames)', kind: 'int', default: 120, scope: 'host', hint: 'For adjustment layers', value: 120 },
      { key: 'show_toasts', label: 'Show a message after each action', kind: 'bool', default: true, scope: 'host', value: true },
    ],
    search: [
      { key: 'device', label: 'Device', kind: 'select', default: 'gpu', options: [{ value: 'gpu', label: 'GPU' }, { value: 'cpu', label: 'CPU' }], scope: 'host', value: 'gpu' },
      { key: 'results', label: 'Default results', kind: 'int', default: 8, scope: 'host', value: 8 },
      { key: 'index_folder', label: 'Index folder', kind: 'path', default: '', scope: 'project', hint: 'Where the project index is kept', value: '' },
    ],
  };

  // ---- the backend object -----------------------------------------------------------------------
  const api = {
    async ready() {
      setTimeout(() => {
        if (params.get('update') === 'none') return;
        setUpdate(NEW_UPDATE);
      }, 4000);
      setTimeout(() => startDemoJob(), 1500);
      return api.get_state();
    },
    async get_state() {
      return clone({
        version: VERSION, fake: false, data_dir: DATA_DIR, premiere, panel, install: null, modules: modulesOut(), recent,
        activity, jobs: Array.from(jobs.values()), ffmpeg, settings, server: { port: 47800, agent: null, displays: 0 }, update, window: { focused: true },
      });
    },
    async get_settings() { return clone(settings); },
    async set_settings(patch) {
      await delay(80);
      const allowed = ['premiere_exe', 'keep_running_with_premiere', 'theme', 'auto_download_updates'];
      for (const k of Object.keys(patch || {})) if (!allowed.includes(k)) return fail(`The setting "${k}" cannot be changed here.`);
      if (patch.theme !== undefined && !THEMES.includes(patch.theme)) return fail('That theme does not exist.');
      Object.assign(settings, patch);
      emit('settings', settings);
      return clone(settings);
    },
    async launch_premiere(project) {
      await delay(150);
      if (premiere.running && !project) return fail('Premiere Pro is already running.');
      if (!premiere.exe && !premiere.fake) return fail('Premiere Pro was not found. Choose its location in Settings.');
      if (project) {
        const name = project.split('\\').pop();
        const folder = project.slice(0, project.lastIndexOf('\\'));
        const at = recent.findIndex((r) => r.path === project);
        if (at >= 0) recent.splice(at, 1);
        recent.unshift({ path: project, name, folder, opened_at: new Date().toISOString(), source: 'deck' });
        premiere.project = { path: project, name, root: folder };
        premiere.project_path = project;
      }
      if (premiere.running) {
        emit('premiere', premiere);
        return premiereOut();
      }
      premiere.state = 'starting';
      premiere.running = true;
      emit('premiere', premiere);
      setTimeout(() => {
        premiere.state = 'connected';
        premiere.premiere_version = '26.0.0';
        premiere.panel_version = '1.0.0';
        if (!premiere.project) {
          premiere.project = { ...SPRING };
          premiere.project_path = SPRING.path;
        }
        emit('premiere', premiere);
        emitModules();
        addActivity({ text: `Premiere Pro connected · ${premiere.project.name}`, tone: 'ok' });
      }, 2800);
      const out = premiereOut();
      return out;
    },
    async pick_project() { await delay(200); return 'D:\\Video\\Edits\\Summer_Reel.prproj'; },
    async pick_premiere_exe() {
      await delay(200);
      settings.premiere_exe = 'D:\\Adobe\\Adobe Premiere Pro 2026\\Adobe Premiere Pro.exe';
      premiere.exe = settings.premiere_exe;
      premiere.installs = [{ exe: premiere.exe, version: '26.0.0', source: 'manual' }];
      emit('settings', settings);
      return clone(settings);
    },
    async recent_projects() { return clone(recent); },
    async modules() { return modulesOut(); },
    async set_module_enabled(id, enabled) {
      const m = byId(id);
      if (!m) return fail('That module does not exist.');
      if (m.state === 'missing') return fail('This module is not installed in this build.');
      await delay(150);
      m.enabled = !!enabled;
      m.state = enabled ? 'running' : 'disabled';
      emitModules();
      return moduleInfo(m);
    },
    async module_page(id) {
      const m = byId(id);
      if (!m) return fail('That module does not exist.');
      await delay(40);
      if (m.state !== 'running') return { module: moduleInfo(m), page: null };
      const page = { files: filesPage, actions: actionsPage, search: searchPage }[id]();
      return clone({ module: moduleInfo(m), page });
    },
    async module_action(id, action, value, row) {
      await delay(160);
      const m = byId(id);
      if (!m || m.state !== 'running') return fail('That module is not running.');
      let toast = null;
      const say = (tone, text) => {
        toast = [tone, text];
        emit('toast', { kind: tone === 'ok' ? 'success' : tone, message: text });
      };
      if (id === 'files') {
        switch (action) {
          case 'files.filter': F.filter = value; break;
          case 'files.search': F.search = value || ''; break;
          case 'files.undo': {
            const r = F.journal.find((x) => x.id === row);
            F.journal = F.journal.filter((x) => x.id !== row);
            say('ok', `Undone: ${r ? r.name : 'entry'}`);
            break;
          }
          case 'files.undo_last': {
            const r = F.journal.pop();
            say('ok', r ? `Undone: ${r.name}` : 'The journal is empty.');
            break;
          }
          case 'files.refresh': say('info', 'Journal refreshed.'); break;
          case 'files.set_option': F.options[value.key] = value.value; break;
          case 'files.pack_name': F.packName = value; break;
          case 'files.pack_include': F.packInclude = !!value; break;
          case 'files.pack_format': F.packFormat = value; break;
          case 'files.pack':
            F.packName = (value && value['files.pack_name']) || F.packName;
            say('ok', `Packing "${(value && (value['files.pack_name'] ?? '')) || F.packName}" started.`);
            startJob({ title: 'Pack project', module: 'files', label: 'Copying media', seconds: 5 });
            break;
          case 'files.collect':
            if (F.collecting) break;
            askHost('files', 'Collect 128 files into the project folder? The source files stay where they are.').then((yes) => {
              if (!yes) {
                say('info', 'Collect cancelled.');
                emitModules();
                return;
              }
              F.collecting = true;
              F.progress = 0;
              emitModules();
              startJob({ title: 'Collect project', module: 'files', label: 'Copying 128 files', seconds: 7, onDone: {
                tick: (p) => { F.progress = p; emitModules(); },
                done: () => { F.collecting = false; F.progress = 1; say('ok', 'Collected 128 files.'); emitModules(); },
              } });
            });
            break;
          case 'host:open_journal': say('info', 'Opened the journal folder.'); break;
          default: say('info', `${action} done.`);
        }
      } else if (id === 'actions') {
        const key = action.replace(/^actions\./, '');
        if (key === 'toggle_visible') A.visible[row] = !A.visible[row];
        else if (key === 'show_all') Object.keys(A.visible).forEach((k) => (A.visible[k] = true));
        else if (key === 'reset') {
          Object.keys(A.visible).forEach((k) => (A.visible[k] = !['color_labels', 'disable_unused'].includes(k)));
          say('ok', 'Visibility reset.');
        } else if (key === 'set_option') A.options[value.key] = value.value;
        else if (action === 'host:open_presets') say('info', 'Opened the presets folder.');
        else if (A.visible[key] !== undefined) {
          if (premiere.state !== 'connected') return fail('Premiere Pro is not connected.');
          say('ok', `${ACTIONS.find((a) => a[0] === key)[1]} done.`);
          addActivity({ module: 'actions', text: `${ACTIONS.find((a) => a[0] === key)[1]} ran on the active sequence` });
        }
      } else if (id === 'search') {
        switch (action) {
          case 'search.query':
            S.query = value || '';
            S.searching = true;
            emitModules();
            setTimeout(() => {
              S.hits = makeHits(S.query, 12);
              S.searching = false;
              emitModules();
            }, 900);
            break;
          case 'search.limit': S.limit = Number(value) || 8; break;
          case 'search.add': say('ok', `Added at ${Math.floor((Number(value) || 0) / 60)}:${String(Math.floor((Number(value) || 0) % 60)).padStart(2, '0')}.`); break;
          case 'search.reveal': say('info', 'Shown in the project panel.'); break;
          case 'search.reindex':
            if (S.indexing) break;
            S.indexing = true;
            S.indexProgress = 0;
            startJob({ title: 'Update index', module: 'search', label: 'Reading clips', seconds: 6, onDone: {
              tick: (p) => { S.indexProgress = p; emitModules(); },
              done: () => { S.indexing = false; say('ok', 'Index updated.'); emitModules(); },
            } });
            break;
          default: say('info', `${action} done.`);
        }
      }
      setTimeout(emitModules, 0);
      return { ok: true, toast };
    },
    async module_settings(id) {
      const fields = (MS[id] || []).map((f) => ({ ...f, available: f.scope !== 'project' || !!premiere.project }));
      return clone({ module: moduleInfo(byId(id)), fields, project: projectName() });
    },
    async set_module_setting(id, key, value, scope) {
      await delay(100);
      const f = (MS[id] || []).find((x) => x.key === key);
      if (!f) return fail('That setting does not exist.');
      if (scope === 'project' && !premiere.project) return fail('Open a project in Premiere Pro first.');
      let v = value;
      if (f.kind === 'int') {
        v = Number(value);
        if (!Number.isInteger(v)) return fail(`${f.label} must be a whole number.`);
        if (key === 'max_depth' && (v < 1 || v > 8)) return fail('Folder depth must be between 1 and 8.');
      }
      f.value = v;
      if (id === 'search' && key === 'device') S.device = v;
      emitModules();
      return v;
    },
    async panel_status() { return clone(panel); },
    async panel_reinstall() {
      await delay(500);
      panel.installed = true;
      panel.installed_version = panel.bundled_version;
      panel.up_to_date = true;
      panel.last_result = { action: 'installed', version: panel.bundled_version, previous_version: null, manifest_changed: false, message: 'The panel was installed.' };
      emit('panel', panel);
      return clone(panel);
    },
    async panel_remove() {
      await delay(400);
      const removed = panel.debug_mode.written_by_host.slice();
      panel.installed = false;
      panel.installed_version = null;
      panel.up_to_date = false;
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
      return new Promise((resolve) => {
        startJob({ title: 'Download ffmpeg', module: null, label: '48 MB', seconds: 4, onDone: { done: () => {
          Object.assign(ffmpeg, { state: 'ready', source: 'bin', path: `${DATA_DIR}\\bin\\ffmpeg.exe`, version: '9.0' });
          resolve(clone(ffmpeg));
        } } });
      });
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
      if (!/^https:\/\//.test(url)) return fail('Only https links can be opened.');
      console.info('[mock] open_url', url);
      return true;
    },
    async window_minimize() { console.info('[mock] window_minimize'); return null; },
    async window_close() { console.info('[mock] window_close'); return null; },
    async quit() { return null; },
    async update_status() { return clone(update); },
    async check_updates() {
      setUpdate({ stage: 'checking' });
      await delay(900);
      return params.get('update') === 'none' ? setUpdate({ ...update, stage: 'up_to_date', checked_at: nowS() }) : setUpdate({ ...NEW_UPDATE, checked_at: nowS() });
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
    async log(level, message) { console.debug(`[mock] log ${level}: ${message}`); return null; },
  };

  // The demo job: index footage with moving progress, repeated.
  function startDemoJob() {
    startJob({ title: 'Index footage', module: 'search', label: 'Reading clips', seconds: 45, onDone: { done: () => setTimeout(startDemoJob, 8000) } });
  }

  return api;
}
