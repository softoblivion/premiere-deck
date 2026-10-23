"""Development host for live tests of the panel and the facade (docs/PANEL.md 4.5). Not shipped.

A small `websockets` server implementing docs/PROTOCOL.md 1-3 for one agent and any number of displays, with a
`PremiereApi` on top. It writes `deck.json` into the installed panel folder (the dev junction) and removes it on
exit. The display gets a 3.0 sample tree in the host's tab order: the Actions tab (one lane per group with its code,
two `switch` tiles, the colour menu and the custom size prompt; with `--real-actions` the real Quick actions module on a fake host
renders it instead), the Files tab (the row tiles of docs/FILES.md 6, SORT and SYNC with their AUTO capsule, a job with
Stop, the undo row, the Offline group) and a tab with every node kind; the tree carries the pinned row (`pins`), which
`panel.pin` / `panel.unpin` edit as the hub does. Its actions change the tree and send `action_result` / `toast`,
so the round trip is visible in the panel; the host's `theme` message is sent right after `welcome`. A switch flip
is stored and comes back in the next tree; `server.model["refuse"] = {"actions.lock_others"}` makes the host refuse
that switch (the old value plus a `warn` result), for the optimistic flip.

Control, for scripted checks: a loopback HTTP endpoint (default port 47890)
    POST /exec   body = Python source, run with `api`, `server`, `state` in scope; `result = ...` is returned as JSON.
                 Handy: `server.set_theme("sky")`, `server.model["job"] = {...}; server.push_ui()`,
                 `server.broadcast(protocol.action_result("actions.razor_all", "ok", "Cut 3 tracks", None))`
    GET  /state  connection, recorded events, actions and agent messages
Run:
    set PYTHONPATH=src
    .venv\\Scripts\\python tests\\panel\\live_server.py [--panel-dir DIR] [--port N] [--control-port N] [--repl]
"""

from __future__ import annotations

import argparse
import asyncio
import code as code_module
import contextlib
import io
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve

from premiere_deck import PROTOCOL_VERSION, __version__
from premiere_deck.host import protocol
from premiere_deck.modules.base import FakeHost, FakeUi
from premiere_deck.premiere.api import PremiereApi
from premiere_deck.premiere.model import PremiereError, Selection, SequenceInfo, TrackItem

DEFAULT_PANEL_DIR = Path(os.environ.get("APPDATA", "")) / "Adobe" / "CEP" / "extensions" / "com.softoblivion.premieredeck"


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.actions: list[dict] = []
        self.agent_messages: list[dict] = []
        self.display_messages: list[dict] = []
        self.hellos: list[dict] = []

    def record(self, bucket: str, item: dict) -> None:
        with self.lock:
            target = getattr(self, bucket)
            target.append(item)
            del target[:-500]

    def dump(self) -> dict:
        with self.lock:
            return {"events": list(self.events), "actions": list(self.actions),
                    "agent_messages": list(self.agent_messages), "display_messages": list(self.display_messages),
                    "hellos": list(self.hellos)}


PINS_MAX = 5


def files_section(model: dict) -> dict:
    """The Files tab as the Files module renders it in 3.0 (docs/FILES.md 6): header, path, the three count chips, the
    2 x 3 row tiles (SORT and SYNC with their AUTO capsule, SETUP after set-up), the running job with Stop, the undo
    row and the Offline group; no status line, no Journal tile."""
    job = model["job"]
    running = job["id"] if job else None

    def tile(local: str, label: str, code: str, icon: str, confirm: str = None, auto: tuple = None) -> dict:
        node = protocol.node_button(f"files.{local}", label, hint=FILES_HINTS[local], icon=icon, short=code,
                                    confirm=confirm, busy=running == local, disabled=bool(running) and running != local)
        if auto:
            auto_id, auto_label, auto_hint = auto
            node["auto"] = {"id": auto_id, "label": auto_label, "hint": auto_hint, "value": model["switches"][auto_id]}
        return node

    items = [
        protocol.node_row([protocol.node_text("spring-promo_v2"),
                           protocol.node_button("files.open_folder", "Open project folder", icon="open-folder",
                                                variant="icon", hint="Opens the project folder in Explorer.")]),
        protocol.node_text("D:\\Footage\\spring-promo", small=True),
        protocol.node_row([protocol.node_text("142 in the root", tone="neutral", small=True, chip=True),
                           protocol.node_text("4 outside", tone="warn", small=True, chip=True),
                           protocol.node_text("1 offline", tone="error", small=True, chip=True)]),
        protocol.node_group(None, [
            tile("sort", "Sort project", "SORT", "sort", "Sort 18 items into the layout folders?",
                 ("files.auto_sort", "Auto sort", "Files new media of the project folder into the layout folders and bins.")),
            tile("sync_all", "Sync all media", "SYNC", "sync-all", None,
                 ("files.auto_copy", "Auto copy",
                  "Copies files imported from outside the project folder into Media and relinks them.")),
            tile("sync_used", "Sync used media", "USED", "sync-used"),
            tile("zip", "Make ZIP", "ZIP", "zip"),
            tile("setup", "Check project folders", "SETUP", "folder"),
        ], id="files.actions", columns=2, tiles=True),
    ]
    if job:
        items.append(protocol.node_progress(job["value"], job["label"], detail=job["detail"], cancel="files.stop"))
    items.append(protocol.node_row([
        protocol.node_button("files.undo_last", "Undo last", icon="undo", variant="icon", hint="Undoes: Sorted 18 items."),
    ]))
    rows = [protocol.list_row("o1", "C0042.MP4", subtitle="Footage\\Camera A", tone="warn",
                              actions=[protocol.node_button("files.locate", "Locate"),
                                       protocol.node_button("files.remove", "Remove",
                                                            confirm="Remove C0042.MP4 from the project?")])]
    items.append(protocol.node_group("Offline", [protocol.node_list("files.offline_list", rows, empty="")],
                                     id="files.offline", collapsible=True))
    return protocol.section("files", "Files", items, icon="folder")


FILES_HINTS = {
    "sort": "Moves the media inside the project folder into the layout folders and bins.",
    "sync_all": "Copies every file from outside the project folder into Media.",
    "sync_used": "Copies the files used in sequences from outside the project folder into Media.",
    "zip": "Writes the project folder as a ZIP archive into its Exports folder.",
    "setup": "Creates the layout folders and bins again when any are missing.",
}

# The 3.0 Quick actions set in panel order (docs/QUICK_ACTIONS.md 1): group title, group code, then per action its
# id, code, full name, icon, kind and hint.
ACTIONS_30 = [
    ("Timeline", "TIME", [
        ("adjustment_layer", "ADJ", "Adjustment layer", "adjustment-layer", "button",
         "Adds an adjustment layer over the selected clips, or for 5 seconds at the playhead."),
        ("razor_all", "RZR", "Razor all tracks", "razor-all", "button", "Cuts every unlocked track at the playhead."),
        ("delete_empty_tracks", "EMPT", "Delete empty tracks", "delete-empty-tracks", "button",
         "Removes unlocked video and audio tracks that hold no clips.")]),
    ("Tracks", "TRKS", [
        ("add_video_track", "+V", "Add video track", "track-video-add", "button", "Adds one video track above the highest."),
        ("add_audio_track", "+A", "Add audio track", "track-audio-add", "button",
         "Adds one stereo audio track below the lowest."),
        ("remove_video_track", "\u2212V", "Remove video track", "track-video-remove", "button",
         "Removes the highest video track when it is empty and unlocked."),
        ("remove_audio_track", "\u2212A", "Remove audio track", "track-audio-remove", "button",
         "Removes the lowest audio track when it is empty and unlocked."),
        ("solo_track", "SOLO", "Solo audio track", "solo-track", "switch",
         "Mutes every audio track except the selected clip's; switch off to restore the mutes."),
        ("lock_others", "LOCK", "Lock other tracks", "lock-others", "switch",
         "Locks every track without a selected clip; switch off to restore the locks.")]),
    ("Selection", "SEL", [
        ("select_same_source", "SRC", "Select same source", "select-same-source", "button",
         "Selects every clip that uses the same media as the selected clip."),
        ("select_offline", "OFF", "Select offline clips", "select-offline", "button",
         "Selects every clip of the active sequence whose media is offline.")]),
    ("Clips", "CLIP", [
        ("scale_to_frame", "FIT", "Set to frame size", "scale-to-frame", "button",
         "Turns on Set to Frame Size for the selected video clips."),
        ("colour", "CLR", "Colour label", "colour", "menu",
         "Gives the selected clips or project items, and a marker at the playhead, the chosen label colour."),
        ("label_by_bin", "BIN", "Colour labels by bin", "label-by-bin", "button",
         "Gives every project item the colour of its top-level bin.")]),
    ("Project", "PROJ", [
        ("save_version", "SAVE", "Save version", "save-version", "button",
         "Saves the project as the next numbered version and keeps editing the new file."),
        ("consolidate_duplicates", "CONS", "Consolidate duplicates", "consolidate", "button",
         "Merges project items that point to the same media file."),
        ("open_project_folder", "DIR", "Open project folder", "open-folder", "button", "Opens the project folder in Explorer."),
        ("reveal_clip", "REV", "Reveal clip file", "reveal-clip", "button", "Shows the selected clip's media file in Explorer.")]),
    ("Sequence", "SEQ", [
        ("duplicate_sequence", "DUP", "Duplicate sequence", "duplicate-sequence", "button",
         "Copies the active sequence as the next numbered version."),
        ("create_16_9", "16:9", "Reframe 16:9", "ratio-16-9", "button", "Creates an auto-reframed 16:9 copy of the active sequence."),
        ("create_9_16", "9:16", "Reframe 9:16", "ratio-9-16", "button", "Creates an auto-reframed 9:16 copy of the active sequence."),
        ("create_custom", "CUST", "Reframe custom size", "ratio-custom", "prompt",
         "Creates an auto-reframed copy of the active sequence at the stored size.")]),
    ("Export", "OUT", [
        ("export_open", "EXP", "Export", "export", "button", "Opens Premiere's Export mode for the active sequence."),
        ("export_frame", "FRM", "Export frame", "export-frame", "button",
         "Saves the frame at the playhead as a PNG in Exports\\Frames and shows it in Explorer.")]),
    ("Keys", "KEYS", [
        ("ease_slow", "SLOW", "Ease slow", "ease-slow", "button", "Eases the keyframe pair around the playhead gently."),
        ("ease_med", "MED", "Ease medium", "ease-med", "button",
         "Eases the keyframe pair around the playhead with medium strength."),
        ("ease_fast", "FAST", "Ease fast", "ease-fast", "button", "Eases the keyframe pair around the playhead strongly."),
        ("ease_linear", "LIN", "Linear keys", "ease-linear", "button",
         "Removes the keys an ease added around the playhead and sets the pair to linear.")]),
]
COLOURS = [("Violet", "#a28ae5"), ("Iris", "#8f8ef0"), ("Caribbean", "#2fd0b0"), ("Lavender", "#e08ee0"),
           ("Cerulean", "#38a6e0"), ("Forest", "#6eb04a"), ("Rose", "#e06a9c"), ("Mango", "#e8a040"),
           ("Purple", "#b048e0"), ("Blue", "#4a6ef0"), ("Teal", "#2aa8b8"), ("Magenta", "#e040b8"),
           ("Tan", "#d0b090"), ("Green", "#48b048"), ("Brown", "#b0703a"), ("Yellow", "#e0d040")]


def actions_section(model: dict) -> dict:
    """The Actions tab of 3.0: one lane per group with its code, SOLO / LOCK as switches, CLR with the colour menu,
    CUST with its size prompt (no module behind it; the results are canned in `DevServer.on_action`)."""
    groups = []
    for title, group_code, entries in ACTIONS_30:
        items = []
        for local, code, label, icon, kind, hint in entries:
            node_id = f"actions.{local}"
            if kind == "switch":
                items.append(protocol.node_switch(node_id, label, model["switches"].get(node_id, False), short=code,
                                                  icon=icon, hint=hint))
                continue
            node = protocol.node_button(node_id, label, short=code, icon=icon, hint=hint)
            if kind == "menu":
                node["menu"] = {"id": node_id + ".pick", "value": model["colour"], "badge": model["colour"].title(),
                                "options": [{"value": name.lower(), "label": name, "swatch": swatch}
                                            for name, swatch in COLOURS]}
            elif kind == "prompt":
                node["prompt"] = {"id": node_id + ".size", "label": "Custom size", "value": model["custom"],
                                  "placeholder": "Width x height"}
            items.append(node)
        group = protocol.node_group(title, items, id=f"actions.{title.lower()}", tiles=True, collapsible=True)
        group["short"] = group_code
        groups.append(group)
    return protocol.section("actions", "Actions", groups, icon="actions")


# Canned outcomes of the static Actions tab (button id -> outcome, text, detail).
ACTION_RESULTS = {
    "actions.adjustment_layer": ("ok", "Adjustment layer placed on V3", None),
    "actions.razor_all": ("ok", "Cut 4 tracks at 00:00:12:08", None),
    "actions.delete_empty_tracks": ("neutral", "No empty tracks", None),
    "actions.remove_video_track": ("warn", "V3 is not empty",
                                   "The highest video track holds 2 clips; Deck removes only empty, unlocked tracks."),
    "actions.select_same_source": ("error", "No clip with a source is selected", "Select a clip in the timeline first."),
    "actions.create_16_9": ("ok", "Created Spring 16x9 \u00b7 1920x1080", None),
    "actions.export_frame": ("error", "Export failed", "No sequence is open in the Timeline."),
}


def kinds_section(model: dict) -> dict:
    """Every node kind of PROTOCOL 6.1, including the 2.0 fields, for the look of the plain controls."""
    rows = [protocol.list_row(row_id, title, subtitle=sub, tone=tone,
                              actions=[protocol.node_button("demo.list.open", "Open"),
                                       protocol.node_button("demo.list.remove", "Remove", confirm="Remove this row?")])
            for row_id, title, sub, tone in model["rows"]]
    controls = [
        protocol.node_button("demo.button", "Plain button", hint="Sends an action, the host answers with a toast"),
        protocol.node_button("demo.busy", "Busy for 2 s", busy=model["busy"], tone="accent"),
        protocol.node_button("demo.confirm", "Confirmed action", confirm="Run the confirmed action?", tone="warn"),
        protocol.node_button("demo.disabled", "Disabled button", disabled=True),
        protocol.node_button("demo.icon", "With an icon", icon="open"),
        protocol.node_toggle("demo.toggle", "A toggle", model["toggle"], hint="Sends its value"),
        protocol.node_switch("demo.switch", "A switch", model["switches"]["demo.switch"], short="SW",
                             hint="A switch outside a tile group"),
        protocol.node_select("demo.select", "A select", model["select"],
                             [{"value": "a", "label": "Option A"}, {"value": "b", "label": "Option B"},
                              {"value": "c", "label": "Option C"}]),
        protocol.node_input("demo.input", "An input", model["input"], placeholder="Type and press Enter",
                            submit_label="Send"),
        protocol.node_text(f"Last input: {model['last_input'] or '-'}"),
        protocol.node_text("Small dim text", small=True),
        protocol.node_text("Warning text", tone="warn"),
        protocol.node_progress(model["progress"], "Progress", detail="a-file-name.mp4 > Footage", cancel="demo.stop"),
        protocol.node_progress(None, "Indeterminate"),
        protocol.node_divider(),
        protocol.node_row([protocol.node_button("demo.row.a", "Row A"), protocol.node_button("demo.row.b", "Row B")]),
        protocol.node_group("Group, two columns", [
            protocol.node_button("demo.g2.a", "Accent", tone="accent"),
            protocol.node_button("demo.g2.b", "Warn", tone="warn"),
            protocol.node_button("demo.g2.c", "Error", tone="error", confirm="Run the error action?"),
            protocol.node_button("demo.g2.d", "Ok", tone="ok"),
            protocol.node_text("A text node breaks the run", small=True),
            protocol.node_button("demo.g2.e", "Busy", busy=model["busy"]),
            protocol.node_button("demo.g2.f", "Disabled", disabled=True),
            protocol.node_button("demo.g2.g", "A label that is much too long to fit one half-width pill"),
        ], columns=2, id="demo.two"),
        protocol.node_list("demo.list", rows, empty="No rows"),
    ]
    return protocol.section("demo", "Nodes", controls, status=protocol.status("revision {rev}", "ok"), icon="info")


def sample_ui(model: dict, revision: int, actions: Optional[dict]) -> dict:
    sections = [actions or actions_section(model), files_section(model)]
    if model.get("kinds", True):  # `server.model["kinds"] = False` leaves the two tabs of the product
        sections.append(kinds_section(model))
        sections[2]["status"] = protocol.status(f"revision {revision}", "ok")
    tree = protocol.ui(revision, "Premiere Deck", protocol.status("Connected · dev server", "ok"), sections,
                       {"text": f"Premiere Deck {__version__} (dev)", "link": {"label": "Open host", "action": "host.open"}})
    tree["pins"] = {"items": list(model["pins"]), "max": PINS_MAX}
    return tree


class LiveUi(FakeUi):
    """`ctx.ui` of the Quick actions module on the dev host: results and toasts go to the displays."""

    def __init__(self, server: "DevServer") -> None:
        super().__init__()
        self.server = server

    def result(self, button_id: str, outcome: str, text: str, detail: Optional[str] = None) -> None:
        super().result(button_id, outcome, text, detail)
        full = button_id if button_id.startswith("actions.") else "actions." + button_id
        self.server.broadcast(protocol.action_result(full, outcome, text, detail))

    def toast(self, tone: str, text: str) -> None:
        super().toast(tone, text)
        self.server.toast(text, tone)

    def invalidate(self) -> None:
        super().invalidate()
        self.server.push_ui()


SEQUENCE = SequenceInfo("seq1", "Spring", 1920, 1080, 25.0, 60.0)
CLIPS = (TrackItem("seq1", "video", 0, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True),
         TrackItem("seq1", "audio", 1, 0, "a.mp4", "n1", 12.0, 18.0, 0.0, 6.0, True))
CANNED = {
    "actions.adjustment_layer": {"track_index": 2, "start": 12.0, "end": 18.0, "created": False},
    "actions.razor_all": {"cuts": 3, "locked": 1, "failed": 0, "seconds": 62.4},
    "actions.delete_empty_tracks": {"video": 2, "audio": 1},
    "actions.select_offline": {"count": 2},
    "actions.scale_to_frame": {"clips": 3, "method": "clip"},
    "actions.solo_track": {"states": [0, 1, 0], "count": 3},
    "actions.lock_others": {"states": {"video": [0, 0], "audio": [0, 0]}, "locked": 2},
    "actions.consolidate_duplicates": {},
    "actions.duplicate_sequence": lambda args: {"name": args["name"], "sequence_id": "seq10"},
    "actions.export_frame": {"path": "D:\\Footage\\spring-promo\\Exports\\Frames\\Spring_00-00-12-05.png"},
}


class DevServer:
    """Implements `AgentTransport` for `PremiereApi`."""

    def __init__(self, panel_dir: Path, port: Optional[int], state: State) -> None:
        self.panel_dir = panel_dir
        self.port = port
        self.state = state
        self.token = protocol.new_token()
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.agent: Optional[ServerConnection] = None
        self.displays: set[ServerConnection] = set()
        self.connected = False
        self.handler: Callable[[dict], None] = lambda message: None
        self.presence: Callable[[bool, Optional[protocol.Hello]], None] = lambda connected, hello: None
        self.revision = 0
        self.model = {"busy": False, "toggle": True, "select": "b", "input": "", "last_input": "", "progress": 0.3,
                      "job": None, "theme": "black", "colour": "rose", "custom": "1080x1350", "refuse": set(),
                      "pins": ["actions.adjustment_layer", "actions.colour", "actions.duplicate_sequence", "actions.export_open"],
                      "switches": {"files.auto_copy": True, "files.auto_sort": False, "actions.solo_track": False,
                                   "actions.lock_others": True, "demo.switch": False},
                      "rows": [("r1", "First row", "subtitle one", "ok"), ("r2", "Second row", "subtitle two", "warn"),
                               ("r3", "Third row", "", "neutral")]}
        self.fake = FakeHost(module_id="actions")
        self.fake.make_project(name="Spring")
        self.fake.premiere.active_sequence_value = SEQUENCE
        self.fake.premiere.sequences_value = [SEQUENCE]
        self.fake.premiere.selection_value = Selection("seq1", CLIPS)
        self.fake.premiere.eval_results.update(CANNED)
        self.fake.premiere.eval_errors["actions.select_same_source"] = PremiereError("deck: No clip with a source is selected.")
        self.fake.ui = LiveUi(self)
        self.actions = None  # the real Quick actions module with --real-actions (use_real_actions)
        self.job_timer: Optional[threading.Timer] = None
        self.custom_ui: Optional[dict] = None
        self.autostart = False
        self.host_exe: Optional[str] = None
        # Test switches: welcome.update for the next handshakes, or a protocol refusal (PROTOCOL 3).
        self.welcome_update: Optional[dict] = None
        self.refuse_protocol = False

    # AgentTransport
    def send(self, message: dict) -> None:
        agent = self.agent
        if agent is None or self.loop is None:
            raise ConnectionError("no agent")
        text = protocol.dumps(message)
        asyncio.run_coroutine_threadsafe(agent.send(text), self.loop)

    def set_handler(self, handler: Callable[[dict], None]) -> None:
        self.handler = handler

    def set_presence_handler(self, handler: Callable[[bool, Optional[protocol.Hello]], None]) -> None:
        self.presence = handler

    # display side
    def broadcast(self, message: dict) -> None:
        if self.loop is None:
            return
        text = protocol.dumps(message)
        for display in list(self.displays):
            asyncio.run_coroutine_threadsafe(display.send(text), self.loop)

    def use_real_actions(self) -> None:
        from premiere_deck.modules.quick_actions.module import QuickActionsModule
        self.actions = QuickActionsModule(self.fake.context())
        self.actions.start()

    def push_ui(self) -> None:
        self.revision += 1
        actions = None
        if self.actions is not None:
            actions = protocol.section("actions", "Actions", self.actions.render(),
                                       status=self.actions.section_status(), icon="actions")
        tree = self.custom_ui or sample_ui(self.model, self.revision, actions)
        tree = dict(tree, revision=self.revision)
        self.broadcast(tree)

    def set_theme(self, name: str) -> None:
        self.model["theme"] = name
        self.broadcast(protocol.theme(name))

    def toast(self, text: str, tone: str = "ok", ms: int = 3500) -> None:
        self.broadcast(protocol.toast(tone, text, ms))

    def send_agent(self, message: dict) -> None:
        self.send(message)

    def write_deck(self) -> None:
        deck = {"port": self.port, "token": self.token, "host_version": __version__, "protocol": PROTOCOL_VERSION,
                "host_exe": self.host_exe, "autostart_host": self.autostart, "written_at": int(time.time())}
        (self.panel_dir / "deck.json").write_text(json.dumps(deck, indent=2), encoding="utf-8")

    def remove_deck(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            (self.panel_dir / "deck.json").unlink()

    def start_job(self, job_id: str, label: str, steps: int, finish: tuple) -> None:
        """A fake Files job: progress every 0.5 s, then a result (docs/FILES.md 6)."""
        model = self.model
        if model["job"]:
            return
        model["job"] = {"id": job_id, "label": label, "value": 0.0, "detail": "C0001.MP4 > Footage\\Camera A",
                        "tile_meta": "0 of %d" % steps, "step": 0, "steps": steps}
        self.push_ui()

        def tick() -> None:
            job = model["job"]
            if not job:
                return
            job["step"] += 1
            if job["step"] >= job["steps"]:
                model["job"] = None
                self.broadcast(protocol.action_result(f"files.{job_id}", *finish))
                self.push_ui()
                return
            job["value"] = job["step"] / job["steps"]
            job["label"] = f"{label} {job['step']} / {job['steps']}"
            job["tile_meta"] = f"{job['step']} of {job['steps']}"
            job["detail"] = f"C{job['step']:04d}.MP4 > Footage\\Camera A"
            self.push_ui()
            self.job_timer = threading.Timer(0.5, tick)
            self.job_timer.start()

        self.job_timer = threading.Timer(0.5, tick)
        self.job_timer.start()

    def on_action(self, message: dict) -> None:
        action = message.get("id")
        model = self.model
        if isinstance(action, str) and action.startswith("actions.") and self.actions is not None:
            self.actions.on_action(action, message.get("value"), message.get("row"))
            self.push_ui()
            return
        if action in model["switches"]:
            if action in model["refuse"]:
                self.broadcast(protocol.action_result(action, "warn", "Lock others needs a selected clip"
                                                      if action == "actions.lock_others" else "Change refused", None))
            else:
                model["switches"][action] = bool(message.get("value"))
            self.push_ui()
            return
        if action == "actions.colour.pick":
            model["colour"] = str(message.get("value"))
            self.broadcast(protocol.action_result("actions.colour", "ok", f"Coloured 4 clips {model['colour'].title()}"))
            self.push_ui()
            return
        if action == "actions.create_custom.size":
            model["custom"] = str(message.get("value") or model["custom"])
            self.broadcast(protocol.action_result("actions.create_custom", "ok", f"Size stored \u00b7 {model['custom']}"))
            self.push_ui()
            return
        if action == "actions.create_custom":
            self.broadcast(protocol.action_result(action, "ok", f"Created Spring {model['custom']}"))
            self.push_ui()
            return
        if action in ("panel.pin", "panel.unpin"):
            self.on_pin(action, str(message.get("value")))
            return
        if isinstance(action, str) and action.startswith("actions."):
            outcome = ACTION_RESULTS.get(action, ("ok", f"{action.split('.', 1)[1].replace('_', ' ').capitalize()} done", None))
            self.broadcast(protocol.action_result(action, *outcome))
            self.push_ui()
            return
        results = {
            "files.open_folder": ("ok", "Opened spring-promo", None),
            "files.undo_last": ("neutral", "Nothing to undo", None),
        }
        if action in results:
            self.broadcast(protocol.action_result(action, *results[action]))
        elif action == "files.sort":
            self.start_job("sort", "Sorting project", 12, ("ok", "Sorted 18 files into 4 folders", None))
        elif action in ("files.sync_all", "files.sync_used"):
            self.start_job(action.split(".")[1], "Syncing media", 6, ("ok", "Synced 4 files to Media", None))
        elif action == "files.zip":
            self.start_job("zip", "Making ZIP", 6, ("warn", "ZIP made · 1 offline file skipped",
                                                    "spring-promo.zip was made without 1 file that is offline."))
        elif action == "files.stop":
            job = model["job"]
            if job and self.job_timer:
                self.job_timer.cancel()
                done = job["step"]
                model["job"] = None
                self.broadcast(protocol.action_result(f"files.{job['id']}", "neutral",
                                                      f"Stopped · {done} of {job['steps']}", None))
        elif action == "demo.busy":
            model["busy"] = True

            def clear() -> None:
                model["busy"] = False
                self.push_ui()

            threading.Timer(2.0, clear).start()
        elif action == "demo.toggle":
            model["toggle"] = bool(message.get("value"))
        elif action == "demo.select":
            model["select"] = str(message.get("value"))
        elif action == "demo.input":
            model["last_input"] = str(message.get("value"))
            model["input"] = ""
        elif action == "demo.list.remove":
            model["rows"] = [row for row in model["rows"] if row[0] != message.get("row")]
        if isinstance(action, str) and action.startswith("demo."):
            model["progress"] = round((model["progress"] + 0.1) % 1.0, 2)
            self.toast(f"{action} value={message.get('value')!r} row={message.get('row')!r}", "ok")
        elif action == "host.open":
            self.toast("host.open: the host window would come to the front", "neutral")
        self.push_ui()

    def on_pin(self, action: str, target: str) -> None:
        """`panel.pin` / `panel.unpin` as the hub handles them (docs/PROTOCOL.md 6.1 `pins`): the list changes and the
        tree is sent again; a full row or an id that is no tile answers with a `panel.pins` result."""
        pins = self.model["pins"]
        if action == "panel.unpin":
            if target in pins:
                pins.remove(target)
                self.push_ui()
            return
        if target in pins:
            return
        tree = sample_ui(self.model, self.revision, None)
        known = {node.get("id") for section in tree["sections"] for group in section["items"] if group.get("kind") == "group"
                 for node in group["items"] if node.get("kind") in ("button", "switch")}
        if len(pins) >= PINS_MAX:
            self.broadcast(protocol.action_result("panel.pins", "warn", "Five pins at most", None))
        elif target not in known:
            self.broadcast(protocol.action_result("panel.pins", "warn", "This control cannot be pinned", None))
        else:
            pins.append(target)
            self.push_ui()

    async def connection(self, ws: ServerConnection) -> None:
        query = parse_qs(urlparse(ws.request.path).query)
        role = (query.get("role") or [""])[0]
        token = (query.get("token") or [""])[0]
        if token != self.token or role not in protocol.ROLES:
            await ws.close(protocol.CLOSE_UNAUTHORIZED, "unauthorized")
            return
        try:
            hello_text = await asyncio.wait_for(ws.recv(), 10)
            hello_message = protocol.loads(hello_text)
            hello = protocol.Hello.parse(hello_message)
        except Exception:
            await ws.close(protocol.CLOSE_PROTOCOL, "bad hello")
            return
        self.state.record("hellos", dict(hello_message, at=time.time()))
        if self.refuse_protocol:
            await ws.send(protocol.dumps(protocol.bye("protocol")))
            await ws.close(protocol.CLOSE_PROTOCOL, "protocol")
            return
        await ws.send(protocol.dumps(protocol.welcome(__version__, f"s-{int(time.time()) % 100000:05d}",
                                                      self.welcome_update)))
        if role == protocol.ROLE_AGENT:
            await self.serve_agent(ws, hello)
        else:
            await ws.send(protocol.dumps(protocol.theme(self.model["theme"])))
            await self.serve_display(ws)

    async def serve_agent(self, ws: ServerConnection, hello: protocol.Hello) -> None:
        self.agent = ws
        self.connected = True
        await asyncio.to_thread(self.presence, True, hello)
        try:
            async for text in ws:
                message = protocol.loads(text)
                if message.get("type") != protocol.T_SCRIPT_RESULT:
                    self.state.record("agent_messages", dict(message, received=time.time()))
                if message.get("type") == protocol.T_LOG:
                    print(f"[agent {message.get('level')}] {message.get('message')}", flush=True)
                self.handler(message)
        finally:
            if self.agent is ws:
                self.agent = None
                self.connected = False
                await asyncio.to_thread(self.presence, False, None)

    async def serve_display(self, ws: ServerConnection) -> None:
        self.displays.add(ws)
        self.state.record("events", {"name": "panel.opened", "data": {}, "at": time.time()})
        try:
            self.push_ui()
            async for text in ws:
                message = protocol.loads(text)
                self.state.record("display_messages", dict(message, received=time.time()))
                if message.get("type") == protocol.T_ACTION:
                    self.state.record("actions", dict(message, received=time.time()))
                    await asyncio.to_thread(self.on_action, message)
                elif message.get("type") == protocol.T_LOG:
                    print(f"[display {message.get('level')}] {message.get('message')}", flush=True)
        finally:
            self.displays.discard(ws)
            self.state.record("events", {"name": "panel.closed", "data": {}, "at": time.time()})

    async def run(self, ready: threading.Event) -> None:
        self.loop = asyncio.get_running_loop()
        ports = [self.port] if self.port else list(protocol.PORT_RANGE)
        last_error: Optional[Exception] = None
        for port in ports:
            try:
                server = await serve(self.connection, "127.0.0.1", port, max_size=protocol.MAX_FRAME_BYTES,
                                     ping_interval=15, ping_timeout=30)
            except OSError as exc:
                last_error = exc
                continue
            self.port = port
            self.write_deck()
            print(f"dev server on ws://127.0.0.1:{port}/ws, deck.json in {self.panel_dir}", flush=True)
            ready.set()
            async with server:
                await server.serve_forever()
            return
        raise RuntimeError(f"no free port: {last_error}")


def control_server(port: int, scope: dict) -> ThreadingHTTPServer:
    state: State = scope["state"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def reply(self, code: int, payload: Any) -> None:
            body = json.dumps(payload, default=repr, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/state"):
                server: DevServer = scope["server"]
                self.reply(200, dict(state.dump(), connected=server.connected, displays=len(server.displays),
                                     project_path=scope["api"].project_path, port=server.port))
            else:
                self.reply(404, {"error": "unknown"})

        def do_POST(self) -> None:
            if not self.path.startswith("/exec"):
                self.reply(404, {"error": "unknown"})
                return
            source = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
            local = dict(scope)
            out = io.StringIO()
            started = time.monotonic()
            try:
                with contextlib.redirect_stdout(out):
                    exec(compile(source, "<exec>", "exec"), local)
                self.reply(200, {"ok": True, "result": local.get("result"), "stdout": out.getvalue(),
                                 "ms": int((time.monotonic() - started) * 1000)})
            except BaseException as exc:
                self.reply(200, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc(),
                                 "stdout": out.getvalue(), "ms": int((time.monotonic() - started) * 1000)})

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--panel-dir", type=Path, default=DEFAULT_PANEL_DIR)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--control-port", type=int, default=47890)
    parser.add_argument("--host-exe", default=None, help="host_exe written into deck.json")
    parser.add_argument("--autostart", action="store_true", help="autostart_host in deck.json")
    parser.add_argument("--repl", action="store_true")
    parser.add_argument("--real-actions", action="store_true", help="render the Actions tab with the real module")
    args = parser.parse_args()

    if not args.panel_dir.is_dir():
        print(f"panel folder not found: {args.panel_dir}", file=sys.stderr)
        return 2
    state = State()
    server = DevServer(args.panel_dir, args.port, state)
    server.host_exe = args.host_exe
    server.autostart = args.autostart
    if args.real_actions:
        server.use_real_actions()
    api = PremiereApi(server)
    api.on_event(lambda name, data: state.record("events", {"name": name, "data": data, "at": time.time()}))
    scope = {"api": api, "server": server, "state": state, "protocol": protocol, "time": time, "json": json}

    ready = threading.Event()
    failure: list[BaseException] = []

    def run_loop() -> None:
        try:
            asyncio.run(server.run(ready))
        except BaseException as exc:
            failure.append(exc)
            ready.set()

    threading.Thread(target=run_loop, daemon=True).start()
    ready.wait()
    if failure:
        print(f"server failed: {failure[0]}", file=sys.stderr)
        return 1
    httpd = control_server(args.control_port, scope)
    print(f"control on http://127.0.0.1:{args.control_port}/ (POST /exec, GET /state)", flush=True)
    try:
        if args.repl:
            code_module.interact(local=scope, banner="api, server, state are in scope")
        else:
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
        server.remove_deck()
    return 0


if __name__ == "__main__":
    sys.exit(main())
