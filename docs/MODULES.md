# Module contract

Owner: integrator. Every function of Premiere Deck is a module: a Python package under `src/premiere_deck/modules/<name>/`
with a `module.py` that defines one subclass of `Module` (`src/premiere_deck/modules/base.py`). The host discovers
modules from the registry list (`host/registry.py`, `MODULES = ["files", "quick_actions", "ai_search"]`), creates
them at start and routes everything by the module `id`. A module never imports another module; shared needs go
through `HostServices`.

## 1. Manifest

```python
class FilesModule(Module):
    id = "files"                      # prefix of every action id and setting key; folder name
    title = "Files"                   # section title in the panel and the host window
    description = "Keeps project media sorted on disk and in bins."
    order = 10                        # section order in the panel (lower first)
    events = ["project.opened", "project.closed", "project.items_added", "project.changed"]
    requires = []                     # host capabilities: "ffprobe", "ffmpeg", "gpu" (informational, shown when missing)
    default_enabled = True            # a disabled module is created but never started
```

## 2. Lifecycle

```
create(ctx) -> start() -> [project_opened(project) ... project_closed()]* -> stop()
```

| Hook | Called | Rules |
|---|---|---|
| `__init__(self, ctx: ModuleContext)` | at host start, enabled or not | no I/O, no threads |
| `start()` | when enabled and the host is running (also when the user enables the module later) | may read settings, start nothing heavy; return quickly |
| `project_opened(project: ProjectInfo)` | when the agent reports a project (also right after `start()` if a project is already open) | start watchers, take the module's own snapshot if needed |
| `project_closed()` | project closed, Premiere closed or agent lost | stop watchers, cancel jobs |
| `on_event(name, data)` | for every event in `events` | must return fast; schedule work with `ctx.jobs` |
| `on_action(action_id, value, row) -> ActionResult | None` | a display control with this module's prefix | may run a short script directly; long work goes to a job |
| `render() -> list[UiNode]` | whenever the host rebuilds the UI tree (after any action, event, job progress or `ctx.ui.invalidate()`) | pure: builds nodes from state, no I/O |
| `render_host() -> dict | None` | the host window asks for the module's page model (`HOST_UI.md` 4) | pure |
| `settings_schema() -> list[SettingField]` | the host window builds the module's settings | static |
| `stop()` | host shutdown or module disabled | release everything; idempotent |

Exceptions in any hook are caught by the registry, logged under `module.<id>` and shown as a toast "Files: <one
sentence>"; the module stays alive. A module that raises in `start()` is marked `failed` and shown so in the host
window with the message.

Threading: hooks are called on the host's module thread (one thread, in order). Jobs run on the job pool; a job
reports back through `ctx.jobs` callbacks, which the host marshals onto the module thread. A module never touches
`PremiereApi` from a job thread directly: it posts a function onto the module thread with `ctx.post(fn)` (a
watcher or job thread does the same), or returns a result for the module thread to apply (the facade enforces
this with a thread check).

## 3. `ModuleContext` (what a module gets)

| Attribute | Type | Purpose |
|---|---|---|
| `ctx.premiere` | `PremiereApi` (`PREMIERE_API.md`) | typed operations in Premiere and `eval(name, code, args)` for module jsx |
| `ctx.jobs` | `JobRunner` | `submit(fn, *, title, on_progress=None, on_done=None, on_error=None) -> Job`; `Job.cancel()`, `Job.progress(value, label)` from inside; `running()` lists the module's own queued and running jobs; `high_priority_running() -> bool` is true while a job of priority `high` of any module (a Files copy) is queued or running, so low-priority work (AI search indexing) can pause cooperatively; it is false unless such a job exists (`FakeJobs.high_priority` sets it in tests) |
| `ctx.settings` | `ModuleSettings` | `get(key, default)`, `set(key, value)` for host-level settings (`settings.json` under `modules.<id>`); `project.get/set` for the current project's settings (`<root>\.premiere-deck\project.json` under `modules.<id>`) |
| `ctx.paths` | `AppPaths` | data folder helpers; `models_dir`, `logs_dir`, `cache_dir(module_id)` |
| `ctx.tools` | `Tools` | `ffprobe(path) -> MediaInfo`, `ffmpeg(args, ...)`, `has("ffmpeg")`, `ensure("ffmpeg", on_progress)`; `ffmpeg_pipe(args) -> subprocess.Popen` starts ffmpeg (the executable is prepended) for a caller that reads its output: stdout and stderr are pipes, no console window, UTF-8 environment, stdin closed, and the process is registered in a Job Object of the host, so it ends with the host. The caller reads both pipes, waits, and calls `kill()` on a cancel; it raises `FileNotFoundError` when ffmpeg is missing (`FakeTools` records the arguments and returns a Popen of `python -c "pass"`) |
| `ctx.ui` | `UiPort` | `invalidate()` (re-render soon, coalesced), `toast(tone, text)`, `confirm(question) -> bool` (host window dialog, blocking on a job thread only) |
| `ctx.project` | `ProjectInfo | None` | current project: `path`, `root` (project folder), `name`, `deck_dir` (`<root>\.premiere-deck`) |
| `ctx.log` | `logging.Logger` | `module.<id>` |
| `ctx.host` | `HostInfo` | `version`, `data_dir`, `open_path(path)`, `reveal(path)`, `open_url(url)` |
| `ctx.events` | `EventBus` | `emit(name, data)` for module-to-host notifications (`journal.appended`, used by the host window) |
| `ctx.post` | `Callable[[Callable[[], None]], None]` | runs a function on the module thread; the only way for a job, watcher or server thread to touch module state; a module may own a `threading.Timer` (Files coalesces Premiere events this way) as long as its callback goes through `ctx.post` |

## 4. UI

`render()` returns the nodes of the module's section (`PROTOCOL.md` 6.1). Helpers in `base.py` build nodes:
`button(id, label, **kw)`, `toggle(id, label, value, **kw)`, `select(...)`, `input(...)`, `text(...)`,
`progress(...)`, `list_(...)`, `row(...)`, `divider()`. Ids are given without the module prefix; `base.py` adds
`<id>.` and strips it again before `on_action`. A node id must be stable across renders (the display diffs by it).
Window-only controls (`HOST_UI.md` 4) use ids `host:<name>` after the prefix (`files.host:undo`); the registry
strips the module prefix, so `on_action` sees `host:undo`. Only the first dot separates the module id, so a name may
contain dots (`actions.host:visible.<id>` arrives as `host:visible.<id>`); the window may also send the bare
`host:<name>` together with the module id, which is passed on unchanged.

`ActionResult` (optional return of `on_action`): `toast: tuple[tone, text] | None`, `rerender: bool = True`.
Returning `None` means "re-render, no toast".

Copy rules: neutral, factual sentences; no exclamation marks, no emoji; labels of two or three words; hints one
sentence (`_workspace\CONVENTIONS.md`, UI copy).

## 5. Settings

`settings_schema()` returns `SettingField(key, label, kind, default, options=None, hint=None, scope="host"|"project")`
with kinds `bool`, `int`, `text`, `select`, `path`. The host window renders them; values are read with
`ctx.settings.get` (`scope="host"`) or `ctx.settings.project.get` (`scope="project"`). A module validates on read
and falls back to the default on a bad value; nothing crashes on a hand-edited file.

## 6. Scripts of a module

Module jsx files live in `modules/<id>/jsx/`. `ctx.premiere.eval(name, code_or_path, args)` runs them with the
conventions of `PREMIERE_API.md` 5; `ctx.premiere.script(path)` loads a file once (cached text). Prefer the typed
facade operations; write module jsx only for operations the facade does not offer, and ask the integrator to
promote a script into the facade when a second module needs it.

## 7. Tests

`modules/base.py` ships `FakeHost`: builds a `ModuleContext` with a `FakePremiere` (records `eval` calls, returns
canned results per script name, fires events on demand), synchronous `FakeJobs` (runs a job inline, or on
`step()` when `manual=True`), in-memory settings, a scratch `AppPaths` under `PREMIERE_DECK_HOME`, a `FakeTools`
with canned `MediaInfo` per path, and a `FakeUi` that records toasts and renders. Module tests use `unittest`
and never touch the user's Premiere, the registry, or real media; media needed by a test is generated with ffmpeg
or Pillow in the scratch folder.

Each module's tests live in `tests/test_modules_<id>*.py` and cover: render in every state, every action, every
event, the job paths (success, failure, cancel), and settings fallback.

## 8. Adding a module (checklist)

1. Owning document `docs/<MODULE>.md` first (purpose, settings, actions, scripts, invariants).
2. Package under `modules/<id>/` with `module.py`, tests, optional `jsx/`.
3. Add the id to `MODULES` in `host/registry.py` and a row to the module table in `ARCHITECTURE.md`.
4. Nothing in `panel/` changes.
