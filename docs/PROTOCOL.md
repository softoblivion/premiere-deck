# Host <-> panel protocol

Owner: integrator. The only definition of what the host and the CEP extension say to each other. The host side
is `src/premiere_deck/host/protocol.py` (types, validation) and `host/server.py` (transport); the panel side is
`src/premiere_deck/panel/js/*.js`. Both code against this document; a change here is a change for both workers.

## 1. Transport

- WebSocket, `ws://127.0.0.1:<port>/ws?role=<agent|display>&token=<token>`. Text frames, one JSON object per
  frame, UTF-8. No binary frames. Maximum frame size 8 MB (a snapshot of a very large project stays below that;
  larger snapshots are paged, section 6.2).
- The host accepts connections from loopback only. A connection without the current token, or with an unknown
  role, is closed with code `4001` before any message. A second `agent` connection replaces the first (Premiere
  was restarted; the old socket is dead but not yet closed). Several `display` connections are allowed (Premiere
  can show one panel per workspace; all get the same UI tree).
- Keep-alive: WebSocket ping every 15 s from the host; a client that misses two pongs is dropped. The agent also
  sends `heartbeat` every 10 s with the current project path so the host notices a project switch even when no
  event fired.
- Every message is `{"type": "<name>", ...fields}`. Unknown types are ignored and logged at debug level, never an
  error, so an older panel keeps working against a newer host (and the host then reinstalls it).

## 2. Discovery file `deck.json`

Written by the host into the installed panel folder (`%APPDATA%\Adobe\CEP\extensions\com.softoblivion.premieredeck\deck.json`)
at every start and whenever port or token change; removed at a clean host exit (a stale file is harmless: the
panel's connect fails and it retries).

```json
{
  "port": 47800,
  "token": "b7c1…(32 hex)",
  "host_version": "1.0.0",
  "protocol": 1,
  "host_exe": "D:\\Apps\\PremiereDeck\\PremiereDeck.exe",
  "autostart_host": true,
  "written_at": 1761000000
}
```

The panel reads it with `window.cep.fs.readFile` from `csInterface.getSystemPath(SystemPath.EXTENSION)`. The
agent connects at start and then retries every 3 s while Premiere runs (backing off to 15 s after a minute). When
no connection succeeds within 5 s of Premiere's start, `autostart_host` is true and `host_exe` exists, the agent
starts `host_exe --from-premiere` once per Premiere session (`cep.process.createProcess`).

The token is random per host run. It protects against another local program talking to the panel, not against
the user.

## 3. Handshake

Protocol version: `1`. The client speaks first:

```json
{"type": "hello", "role": "agent", "panel_version": "1.0.0", "protocol": 1,
 "premiere_version": "26.3.0", "cep_version": "12.0", "extension_id": "com.softoblivion.premieredeck.agent",
 "project_path": "D:\\Edits\\Spring\\Spring.prproj"}
```

The host answers:

```json
{"type": "welcome", "host_version": "1.0.0", "protocol": 1, "session_id": "s-3f9a", "update": null}
```

or, when the panel version differs from the host's: `"update": {"action": "reload"}` (files were replaced; the
panel calls `location.reload()` right away; the host sends it only when the versions differ and the files were
actually replaced, and the panel reloads at most once per 30 s anyway; with the dev junction, where the panel's
`VERSION` comes from the source folder, the host sends `"update": null`) or `"update": {"action": "restart_premiere"}` (the manifest changed;
the panel shows the one-line notice "Restart Premiere Pro to finish the Premiere Deck update" and keeps working
on the old version). A protocol mismatch the host cannot serve: `{"type": "bye", "reason": "protocol"}` and close
`4002`; the panel shows "Update Premiere Deck" with the host version.

After `welcome` the agent is ready for `script` messages and the display for `ui` messages.

## 4. Messages host -> agent

| Type | Fields | Meaning |
|---|---|---|
| `script` | `id` (string), `name` (string, for logs), `code` (string, ExtendScript), `args` (JSON value), `timeout_ms` (int, default 30000) | run `code` in Premiere (section 7); exactly one `script.result` with the same `id` follows |
| `subscribe` | `events` (string[]) | the agent registers `app.bind` handlers for these Premiere events (section 8) and forwards them; called once after `welcome` and again when the module set changes |
| `open_display` | | `csInterface.requestOpenExtension("com.softoblivion.premieredeck.panel", "")` |
| `reload` | | `location.reload()` (after a file-level panel update) |

## 5. Messages agent -> host

| Type | Fields | Meaning |
|---|---|---|
| `script.result` | `id`, `ok` (bool), `result` (JSON value when `ok`), `error` (`{"message", "line", "source"}` when not), `ms` (int) | result of a `script`; `source` is the first 200 characters of the failing script |
| `event` | `name` (string), `data` (JSON value), `at` (epoch ms) | a Premiere event (section 8) |
| `heartbeat` | `project_path` (string or null), `active_sequence_id` (string or null), `at` | every 10 s |
| `log` | `level` (`debug`/`info`/`warning`/`error`), `message` | written into `logs/host.log` under logger `panel.agent` |

## 6. Messages host -> display and display -> host

### 6.1 `ui` (host -> display): the whole panel content

The host sends the complete tree on every change; the display re-renders (DOM diffing by `id` keeps focus and
scroll). Trees are small (a few hundred nodes at most).

```json
{"type": "ui", "revision": 42, "title": "Premiere Deck", "status": {"text": "Connected · Spring.prproj", "tone": "ok"},
 "sections": [ {"id": "files", "title": "Files", "collapsed": false, "status": {"text": "Watching · 3 filed today", "tone": "ok"},
                "items": [ ...nodes ] } ],
 "footer": {"text": "Premiere Deck 1.0.0", "link": {"label": "Open host", "action": "host.open"}}}
```

Node kinds (every node has `kind`; interactive nodes have a unique `id`; `disabled` and `hint` are optional
everywhere; `tone` is one of `neutral`, `ok`, `warn`, `error`, `accent`):

| kind | Fields | Rendered as |
|---|---|---|
| `button` | `id`, `label`, `hint`, `tone`, `busy` (bool), `disabled`, `confirm` (string or null: a one-line question the display asks before sending the action) | a button; `busy` shows a spinner and blocks clicks |
| `toggle` | `id`, `label`, `value` (bool), `hint`, `disabled` | switch; sends `action` with `value` |
| `select` | `id`, `label`, `value`, `options` (`[{"value","label"}]`), `disabled` | drop-down; sends `action` with `value` |
| `input` | `id`, `label`, `value`, `placeholder`, `submit_label` (optional button text), `disabled` | text field; sends `action` with `value` on Enter or the submit button |
| `text` | `text`, `tone`, `small` (bool) | a line of text |
| `progress` | `value` (0..1 or null for indeterminate), `label` | progress bar |
| `list` | `id`, `rows` (`[{"id","title","subtitle","tone","thumb" (data URL or null),"bar" (0..1 or null),"actions":[button nodes]}]`), `empty` (text when no rows) | a list with per-row buttons; a row's button sends its own `id` with `row` = the row id; a numeric `bar` is drawn as a thin bar under the subtitle (the fraction of its track that is filled; `null` or absent draws none) |
| `row` | `items` (nodes) | nodes side by side |
| `group` | `title` (string or null), `items` (nodes), `columns` (1 or 2, default 1) | a titled block inside a section: a small caption and its nodes on a shared surface; with `columns` 2 the display lays consecutive buttons out in two columns (a node that is not a button takes the full width). Groups do not nest |
| `divider` | | a thin rule |

Action ids are `<module>.<action>` (`files.collect`, `actions.adjustment_layer`); `host.*` ids belong to the
host (`host.open`, `host.settings`). The display never interprets ids.

### 6.2 `display` -> host

| Type | Fields | Meaning |
|---|---|---|
| `action` | `id`, `value` (JSON value or absent), `row` (string, for list row actions), `revision` (the tree revision the click came from) | the user used a control; the host routes it by id prefix to the module. A stale `revision` is still delivered (the module decides) |
| `display.ready` | `size` (`{"w","h"}`) | sent after `welcome` and whenever the panel is resized (debounced 300 ms); the host adapts nothing yet, kept for layout decisions |
| `log` | as in section 5 | logger `panel.display` |

### 6.3 `toast` (host -> display)

`{"type": "toast", "tone": "ok", "text": "Filed pленка.mp4 into Overlays", "ms": 3500}`. Transient line at the
bottom of the panel. At most one visible; a new one replaces the old.

### 6.4 Paging

A `script.result` whose `result` would exceed 6 MB is returned by the agent in pages: `{"type": "script.result",
"id", "ok": true, "page": 1, "pages": 3, "chunk": "<JSON text fragment>"}`; the host concatenates the chunks in
order and parses once. Used by the snapshot of very large projects.

## 7. Scripts

- The host wraps nothing: `code` is the full ExtendScript program text. The agent evaluates
  `(function(){ var __args = <JSON of args>; function __deckBody() { <code> } var __r; try { __r = __deckBody(); }
  catch (e) { __r = __deckError(e, <source>, 3); } return __deckEncode(__r); })()`
  through `evalScript`, where `code` ends with a `return` of a JSON-serialisable value, and `__deckError`,
  `__deckEncode` and `JSON` come from `host.jsx` (`json2.js` is loaded there, because ExtendScript has no JSON
  object). `code` is the body of a nested function and not of the `try` block because ExtendScript rejects function
  declarations inside `try { }` (`PREMIERE_API.md` 7.7). The `3` is the number of wrapper lines in front of `code`:
  `line` in an error counts from the script's first line. The agent JSON-encodes the return value
  (`JSON.stringify` on the ExtendScript side) and parses it back in the panel so `result` is a JSON value, never a
  string of a string.
- One script at a time per agent; the host queues them (`PremiereApi` guarantees this). A script that exceeds
  `timeout_ms` yields `script.result` with `ok: false`, `error.message: "timeout"`; the agent cannot abort a
  running ExtendScript, so the next script waits for it. Scripts are therefore written to finish fast; long work
  is split (snapshot in pages, batch operations in chunks of 50 items).
- Script files: `src/premiere_deck/premiere/jsx/<operation>.jsx` for facade operations, `modules/<m>/jsx/*.jsx`
  for module-specific ones. The Python side reads the file once and sends its text; `lib.jsx` functions are
  prepended by `PremiereApi` when a script declares `// @uses lib` in its first lines. ExtendScript reads a comment
  starting with `// @` as a preprocessor directive, so `PremiereApi` replaces the marker line with `// (uses lib)`
  before sending (`PREMIERE_API.md` 5, 7.6).
- Conventions inside a script (`PREMIERE_API.md` 5): reach items by `nodeId` (stable within a project), never by
  name; return plain objects and arrays; times in seconds as floats (`ticks / 254016000000`); paths as Windows
  paths with backslashes; booleans as booleans; never `alert()` or `$.writeln`.

## 8. Premiere events

The agent registers handlers for the names in `subscribe` and forwards them as `event`. Names and the `data` the
agent attaches (verified against Premiere 2026 by the panel worker; the exact `app.bind` names per event are the
table in `PREMIERE_API.md` 6, which is the only place that lists them):

| Name | When | `data` |
|---|---|---|
| `project.opened` | the agent sees a new `app.project.path` (2 s poll and `heartbeat`) | `{"path"}` |
| `project.closed` | the path becomes null or another project becomes the current one | `{"path"}` of the closed project |
| `project.items_added` | the `app.bind` names of `PREMIERE_API.md` 6, or the 2 s poll sees the root item count grow | `{}` (Premiere passes no arguments; the host takes a snapshot diff) |
| `project.changed` | any project-structure event the API offers (bins, renames) | `{}` |
| `sequence.activated` | the `app.bind` names of `PREMIERE_API.md` 6 | `{"sequence_id", "name"}` |
| `sequence.structure_changed` | the `app.bind` names of `PREMIERE_API.md` 6 | `{"sequence_id"}` |
| `sequence.selection_changed` | the `app.bind` names of `PREMIERE_API.md` 6 | `{"sequence_id", "count"}` |
| `panel.opened` / `panel.closed` | the display connected / disconnected (sent by the host to itself, listed here because modules receive it) | `{}` |

Events are advisory: a module reacts by asking `PremiereApi` for the current state, never by trusting `data`
alone (Premiere's event APIs lose events under load; the 2 s poll is the safety net).

## 9. Errors and logging

- Transport errors close the socket; both sides reconnect (the agent as in section 2; the host just waits).
- A `script.result` with `ok: false` is an exception on the Python side (`PremiereError(message, line, source)`),
  handled by the caller; the host never shows raw ExtendScript errors in the panel, only the module's sentence.
- Every message is logged at debug level without the `code` field (scripts are logged by `name` and `ms`).
