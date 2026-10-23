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
agent connects at start and then retries every 3 s while Premiere runs (backing off to 15 s after a minute). Both
clients read `deck.json` again before every attempt and every 0.5 s while they wait for the next one, and connect at
once when its port or token changed, so a host restarted while Premiere runs (the old token gets close `4001`) is
joined within about a second of writing its file. When
no connection succeeds within 5 s of Premiere's start, `autostart_host` is true and `host_exe` exists, the agent
starts `host_exe --from-premiere` once per Premiere session (`cep.process.createProcess`).

The token is random per host run. It protects against another local program talking to the panel, not against
the user.

## 3. Handshake

Protocol version: `1` (2.0 only adds optional fields and message types and removes a message the host no longer
sends, none of which breaks an older client, so the version stays). The client speaks first:

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
actually replaced, and the panel reloads at most once per 30 s anyway; a client that is already connected when the files change gets a `reload` message instead (section 4); with the dev junction, where the panel's
`VERSION` comes from the source folder, the host sends `"update": null`) or `"update": {"action": "restart_premiere"}` (the manifest changed;
the panel shows the one-line notice "Restart Premiere Pro to finish the Premiere Deck update" and keeps working
on the old version). A protocol mismatch the host cannot serve: `{"type": "bye", "reason": "protocol"}` and close
`4002`; the panel shows "Update Premiere Deck" with the host version.

After `welcome` the agent is ready for `script` messages and the display for `ui` messages. To a display the host
sends `theme` (6.5) right after `welcome`, before the first `ui`.

The host never asks the agent to open the visible panel: the user opens it once from Window > Extensions and
Premiere keeps it in the workspace (`PANEL.md` 4). The 1.x message `open_display` is gone; the host never sends it,
and a 2.x agent ignores it like any unknown type.

## 4. Messages host -> agent

| Type | Fields | Meaning |
|---|---|---|
| `script` | `id` (string), `name` (string, for logs), `code` (string, ExtendScript), `args` (JSON value), `timeout_ms` (int, default 30000) | run `code` in Premiere (section 7); exactly one `script.result` with the same `id` follows |
| `subscribe` | `events` (string[]) | the agent registers `app.bind` handlers for these Premiere events (section 8) and forwards them; called once after `welcome` and again when the module set changes |
| `reload` | | `location.reload()` (after a file-level panel update; the host sends it to the agent and to every display, because a visible panel keeps its old files until its page is reloaded) |

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
 "sections": [ {"id": "files", "title": "Files", "icon": "folder", "status": {"text": "Watching · 3 filed today", "tone": "ok"},
                "items": [ ...nodes ] } ],
 "footer": {"text": "Premiere Deck 2.0.0", "link": {"label": "Open host", "action": "host.open"}}}
```

Sections are the panel's tabs: one section per running module, in the module's `order`; the tab shows the
section's `icon` (a symbol name of the panel's icon sprite, `PANEL.md` 1) and `title`, and the selected tab fills
the panel (one screen per tab, no long scroll). The section's `status` is drawn at the top of its tab. The 1.x
section field `collapsed` is ignored (collapsing belongs to groups).

Node kinds (every node has `kind`; interactive nodes have a unique `id`; `disabled` and `hint` are optional
everywhere; `tone` is one of `neutral`, `ok`, `warn`, `error`, `accent`). New fields of 2.0 are optional; a node
without them renders as in 1.x:

| kind | Fields | Rendered as |
|---|---|---|
| `button` | `id`, `label`, `hint`, `tone`, `busy` (bool), `disabled`, `confirm` (string or null: a one-line question the display asks before sending the action); 2.0: `icon` (symbol name of the panel sprite or null), `short` (tile label, at most 16 characters; `label` stays the full name and heads the tooltip), `meta` (one line under the tile label, or null), `menu` (below), `variant` (`"icon"` or absent) | a button; `busy` shows a spinner and blocks clicks. Inside a `tiles` group: an icon tile (icon over `short`, `meta` under it). `variant` `icon`: a small round button with the icon only, anywhere (the full `label` is its tooltip) |
| `toggle` | `id`, `label`, `value` (bool), `hint`, `disabled` | switch; sends `action` with `value` |
| `select` | `id`, `label`, `value`, `options` (`[{"value","label"}]`), `disabled` | drop-down; sends `action` with `value` |
| `input` | `id`, `label`, `value`, `placeholder`, `submit_label` (optional button text), `disabled` | text field; sends `action` with `value` on Enter or the submit button |
| `text` | `text`, `tone`, `small` (bool); 2.0: `chip` (bool) | a line of text; with `chip` a small status chip (a dot in the tone's colour and the text); consecutive chips inside a `row` sit side by side |
| `progress` | `value` (0..1 or null for indeterminate), `label`; 2.0: `detail` (one line, the current item, or null), `cancel` (a button node id or null) | progress bar; with `detail` a line under the bar; with `cancel` a "Stop" pill that sends `action` with that id |
| `list` | `id`, `rows` (`[{"id","title","subtitle","tone","thumb" (data URL or null),"bar" (0..1 or null),"actions":[button nodes]}]`), `empty` (text when no rows) | a list with per-row buttons; a row's button sends its own `id` with `row` = the row id; a numeric `bar` is drawn as a thin bar under the subtitle (the fraction of its track that is filled; `null` or absent draws none) |
| `row` | `items` (nodes) | nodes side by side |
| `group` | `title` (string or null), `items` (nodes), `columns` (1 to 3; default 1, or 3 with `tiles`); 2.0: `id` (string, unique in the tree), `collapsible` (bool), `tiles` (bool) | a titled block inside a section: a small caption and its nodes on a shared surface; with `columns` 2 or 3 the display lays consecutive buttons out in that many columns (a node that is not a button takes the full width). With `tiles` the buttons are icon tiles. With `collapsible` the caption is the collapse control (chevron; the number of items while collapsed); the display remembers the collapsed state per group `id` in `localStorage`, expanded by default. Under the group the display keeps its result line (6.6). Groups do not nest |
| `divider` | | a thin rule |

`menu` of a button: `{"id": <node id>, "value": <current value>, "options": [{"value", "label"}], "badge": <short
text>}` or absent. The tile shows `badge` (the current choice, "9:16", "H.264") as a small badge; a click on the
badge opens a small menu of `options`; a pick sends `action` with the menu `id` and the picked `value`, exactly what a
`select` node with that id sends. A click on the tile outside the badge sends the button's own `id`.

Tooltips: every node's `hint` is drawn by the display as its own tooltip (`PANEL.md` 2): for a button the full
`label` in bold over the `hint` line. No node becomes a `title` attribute.

Validation (`host/protocol.py`, `validate_tree`): node ids, group ids and menu ids share one id space and are unique in
the tree; a `short` longer than 16 characters, a `variant` other than `icon`, a `menu` without options and a
`columns` outside 1 to 3 are refused; a `collapsible` group needs an `id`. Modules build the nodes through the
helpers of `MODULES.md` 4, which add the module prefix to every id.

Action ids are `<module>.<action>` (`files.sort`, `actions.adjustment_layer`); `host.*` ids belong to the
host (`host.open`, `host.settings`). The display never interprets ids.

### 6.2 `display` -> host

| Type | Fields | Meaning |
|---|---|---|
| `action` | `id`, `value` (JSON value or absent), `row` (string, for list row actions), `revision` (the tree revision the click came from) | the user used a control; the host routes it by id prefix to the module. A stale `revision` is still delivered (the module decides) |
| `display.ready` | `size` (`{"w","h"}`) | sent after `welcome` and whenever the panel is resized (debounced 300 ms); the host adapts nothing yet, kept for layout decisions |
| `log` | as in section 5 | logger `panel.display` |

### 6.3 `toast` (host -> display)

`{"type": "toast", "tone": "ok", "text": "Filed pленка.mp4 into Overlays", "ms": 3500}`. Transient line at the
bottom of the panel. At most one visible; a new one replaces the old. Since 2.0 the toast carries cross-cutting
notices only (automatic filing, host notices); the outcome of a pressed control is an `action_result` (6.6).

### 6.4 Paging

A `script.result` whose `result` would exceed 6 MB is returned by the agent in pages: `{"type": "script.result",
"id", "ok": true, "page": 1, "pages": 3, "chunk": "<JSON text fragment>"}`; the host concatenates the chunks in
order and parses once. Used by the snapshot of very large projects.

### 6.5 `theme` (host -> display)

`{"type": "theme", "name": "black"}`, `name` one of `black` (Crystal Black) and `sky` (Crystal Sky), the host
setting `theme` (`BRIDGE_API.md` 3.3). Sent right after `welcome` and on every change of the setting, to every
display. The display sets `data-theme` on its root element (`PANEL.md` 3); an unknown name is treated as `black`
(the host sends `black` for a setting value it does not know). The window calls `Hub.notify_theme()` after a theme
change; a display that connects later gets the current theme right after `welcome`.

### 6.6 `action_result` (host -> display)

```json
{"type": "action_result", "id": "actions.adjustment_layer", "outcome": "ok",
 "text": "Adjustment layer added · V3", "detail": null, "at": 1761000000000}
```

The outcome of one action: `id` = the button node id the result belongs to (for a menu pick, the button that
carries the menu), `outcome` one of `ok`, `warn`, `error`, `neutral`, `text` one line, `detail` the one-line
explanation (string or null), `at` epoch ms. The display morphs that tile for 2 s (check or cross, `PANEL.md` 2)
and keeps the line as the result line of the group that holds the button (or of the section when the button is in
no group) until the next result of that group; a line with `detail` opens it on click. Modules send it through
`ctx.ui.result(...)` (`MODULES.md` 3); the host also records it in the window's activity feed (`BRIDGE_API.md`
3.5). A result for an id that is not in the current tree changes no tile and no result line; it is still recorded in the
activity feed.

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
