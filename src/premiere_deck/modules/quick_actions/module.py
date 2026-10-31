"""Quick actions module (docs/QUICK_ACTIONS.md): renders the actions as collapsible tile groups, routes clicks, runs
long actions as jobs with a busy tile, reports every press through `ctx.ui.result`, and keeps only the choices of
the tile menus, palettes and prompts, the visibility setting, the state of the two switch actions (solo, lock), and
for the Labels lane the label palette and which selection came last (section 1.2)."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Optional

from premiere_deck.modules.base import ActionResult, Module, ModuleContext
from premiere_deck.modules.base import ModuleThreadPremiere as _ModuleThreadPremiere
from premiere_deck.modules.quick_actions.action import (
    GROUP_CODES, GROUPS, Action, ActionAbort, Menu, Outcome, error_sentence,
)
from premiere_deck.modules.quick_actions.actions import ACTION_CLASSES
from premiere_deck.modules.quick_actions.results import count
from premiere_deck.premiere.model import LabelPalette, PremiereError, PremiereUnavailable, default_label_palette

HIDDEN_KEY = "hidden"
CHOICES_KEY = "choices"
HOST_PREFIX = "host:"
NOT_CONNECTED = "Premiere Pro is not connected"
INTERNAL_ERROR = "An internal error occurred. Details are in the log."
CLIPS = "clips"
ITEMS = "items"
TARGET_HINT = "A colour press labels the newest selection: timeline clips or Project panel items."


class QuickActionsModule(Module):
    id = "actions"
    title = "Actions"
    icon = "actions"
    description = "Buttons for routine timeline and project operations."
    order = 5
    # Labels (section 1.2): the selection events name the newest selection; the others refresh the palette and the
    # target counts.
    events = ("project.selection_changed", "sequence.selection_changed", "sequence.activated", "panel.opened")
    requires = ()

    def __init__(self, ctx: ModuleContext) -> None:
        super().__init__(ctx)
        self.actions: dict[str, Action] = {}
        for cls in ACTION_CLASSES:
            action = cls()
            self.actions[action.id] = action
        # State of the switch actions while applied: action id -> what the restore needs.
        self.toggles: dict[str, dict] = {}
        self._running: dict[str, Any] = {}
        self._local = threading.local()
        # Labels lane (section 1.2): the palette shown and used, which selection came last, and the selection sizes
        # the latest events or scripts reported (None while unknown).
        self.palette: LabelPalette = default_label_palette()
        self._palette_source: Optional[str] = None
        self.last_target: Optional[str] = None
        self.counts: dict[str, Optional[int]] = {CLIPS: None, ITEMS: None}

    # --- lifecycle -------------------------------------------------------------------------------------------

    def stop(self) -> None:
        self._cancel_jobs()
        self.toggles.clear()

    def project_opened(self, project) -> None:
        self._forget_selection()
        self.ctx.post(self._refresh_labels)

    def project_closed(self) -> None:
        self._cancel_jobs()
        self.toggles.clear()
        self._forget_selection()

    def on_event(self, name: str, data: Any) -> None:
        size = data.get("count") if isinstance(data, dict) else None
        size = size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None
        if name == "sequence.selection_changed":
            self._note(CLIPS, size)
        elif name == "project.selection_changed":
            self._note(ITEMS, size)
        elif name == "sequence.activated":
            self.counts[CLIPS] = None
            self.ctx.post(self._refresh_labels)
        elif name == "panel.opened":
            self.ctx.post(self._refresh_labels)
        self.ctx.ui.invalidate()

    # --- labels (section 1.2) --------------------------------------------------------------------------------

    def _forget_selection(self) -> None:
        self.last_target = None
        self.counts = {CLIPS: None, ITEMS: None}

    def _note(self, target: str, size: Optional[int]) -> None:
        """A selection event: its size, and when something is selected, this selection is the newest."""
        self.counts[target] = size
        if size:
            self.last_target = target

    def preferred_target(self) -> str:
        """The selection a colour press labels first: the newest one, timeline clips before any event came."""
        return self.last_target or CLIPS

    def predicted_target(self) -> Optional[str]:
        """What a colour press would label now: "clips", "items", "" (nothing selected) or None (not known yet)."""
        first = self.preferred_target()
        other = ITEMS if first == CLIPS else CLIPS
        for target in (first, other):
            if self.counts[target]:
                return target
        if self.counts[first] is None or self.counts[other] is None:
            return None
        return ""

    def note_counts(self, result: Any) -> None:
        """The selection sizes a colour script reported (`clips`, `items` + `bins`)."""
        if isinstance(result, dict):
            self.counts[CLIPS] = count(result, "clips")
            self.counts[ITEMS] = count(result, "items") + count(result, "bins")

    def target_chip(self) -> dict:
        """The small target indicator of the Labels lane: what a colour press labels now."""
        target = self.predicted_target()
        if target == CLIPS:
            chip = self.text(f"Clips · {self.counts[CLIPS]}", tone="accent", chip=True)
        elif target == ITEMS:
            chip = self.text(f"Items · {self.counts[ITEMS]}", tone="accent", chip=True)
        elif target == "":
            chip = self.text("No selection", chip=True)
        else:
            chip = self.text("Newest selection", chip=True)
        chip["hint"] = TARGET_HINT
        return chip

    def refresh_palette(self, premiere: Any) -> None:
        """Reads Premiere's label names and colours; keeps the current palette when Premiere cannot answer. The
        source ("premiere", "partial", "defaults") is logged when it changes."""
        try:
            palette = premiere.label_palette()
        except PremiereError as exc:
            self.log.debug("label palette not read: %s", exc.message)
            return
        if not isinstance(palette, LabelPalette):
            return
        self.palette = palette
        if palette.source != self._palette_source:
            self._palette_source = palette.source
            live = sum(1 for label in palette.labels if label.live)
            self.log.info("label palette source: %s (%d of 16 labels read from Premiere)", palette.source, live)

    def _refresh_labels(self) -> None:
        """On the module thread: the palette and the current selection sizes (the target indicator)."""
        if "colour" in self.hidden_ids():
            return
        self.refresh_palette(self.ctx.premiere)
        try:
            action = self.actions["colour"]
            self.note_counts(action.eval(self.ctx, {"label": 0, "target": self.preferred_target(), "mode": "count"}))
        except PremiereError as exc:
            self.log.debug("label target not read: %s", exc.message)
        self.ctx.ui.invalidate()

    def _cancel_jobs(self) -> None:
        for job in list(self._running.values()):
            cancel = getattr(job, "cancel", None)
            if callable(cancel):
                cancel()

    # --- settings --------------------------------------------------------------------------------------------

    def hidden_ids(self) -> set[str]:
        value = self.ctx.settings.get(HIDDEN_KEY, [])
        if not isinstance(value, (list, tuple)):
            return set()
        return {item for item in value if isinstance(item, str) and item in self.actions}

    def set_visible(self, action_id: str, visible: bool) -> None:
        hidden = self.hidden_ids()
        if visible:
            hidden.discard(action_id)
        else:
            hidden.add(action_id)
        self.ctx.settings.set(HIDDEN_KEY, [aid for aid in self.actions if aid in hidden])

    def _choices(self) -> dict:
        value = self.ctx.settings.get(CHOICES_KEY, {})
        return dict(value) if isinstance(value, dict) else {}

    def choice(self, action: Action, name: str) -> str:
        """The stored value of an action's menu, or its default when missing or no longer an option."""
        menu = self._menu(action, name)
        if menu is None:
            return ""
        stored = self._choices().get(f"{action.id}.{name}")
        values = [value for value, _ in menu.options]
        return stored if isinstance(stored, str) and stored in values else menu.default

    def set_choice(self, action: Action, name: str, value: Any) -> bool:
        menu = self._menu(action, name)
        if menu is None or not isinstance(value, str) or value not in [v for v, _ in menu.options]:
            return False
        self.store(f"{action.id}.{name}", value)
        return True

    def _menu(self, action: Action, name: str) -> Optional[Menu]:
        menu = action.menu(self)
        return menu if menu is not None and menu.name == name else None

    def stored(self, key: str) -> Any:
        """A raw remembered value of `choices` (`create_custom.size`); the action validates it on read."""
        return self._choices().get(key)

    def store(self, key: str, value: Any) -> None:
        choices = self._choices()
        choices[key] = value
        self.ctx.settings.set(CHOICES_KEY, choices)

    # --- actions ---------------------------------------------------------------------------------------------

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        local = self.strip_prefix(action_id)
        if local.startswith(HOST_PREFIX):
            return self._host_action(local[len(HOST_PREFIX):], value)
        if local in self.actions:
            action = self.actions[local]
            if action.switch and isinstance(value, bool) and value == action.is_on(self):
                return None  # already in the requested state; the next tree shows it
            return self.trigger(local)
        action_part, _, name = local.partition(".")
        if action_part in self.actions and name:
            action = self.actions[action_part]
            field = action.field(self)
            if field is not None and field.get("name") == name:
                refused = action.submit(self, name, value)
                if refused is not None:
                    self._report(action, refused)
                    return None
                return self.trigger(action_part)
            menu = self._menu(action, name)
            if self.set_choice(action, name, value) and menu is not None and menu.runs:
                return self.trigger(action_part)
            return None
        return super().on_action(action_id, value, row)

    def trigger(self, action_id: str) -> Optional[ActionResult]:
        action = self.actions[action_id]
        if action_id in self._running:
            return ActionResult(toast=None, rerender=False)
        if not action.long:
            self._report(action, self._execute(action, self.ctx))
            return ActionResult(toast=None)
        job_ctx = dataclasses.replace(self.ctx, premiere=_ModuleThreadPremiere(self.ctx.premiere))
        pending = object()
        self._running[action_id] = pending

        def work(job: Any) -> Outcome:
            self._local.job = job
            try:
                return self._execute(action, job_ctx)
            finally:
                self._local.job = None

        def finish(job: Any, outcome: Any) -> None:
            self._running.pop(action_id, None)
            if not isinstance(outcome, Outcome):
                outcome = Outcome("neutral", f"{action.label} finished")
            self._report(action, outcome)
            self.ctx.ui.invalidate()

        def fail(job: Any, exc: BaseException) -> None:
            self.log.error("%s failed in its job: %s", action_id, exc)
            finish(job, Outcome("error", f"{action.label} failed", INTERNAL_ERROR))

        job = self.ctx.jobs.submit(work, title=action.label, on_done=finish, on_error=fail)
        if self._running.get(action_id) is pending:
            self._running[action_id] = job
        return ActionResult(toast=None)

    def _report(self, action: Action, outcome: Outcome) -> None:
        """One result per press (docs/MODULES.md 4): the panel morphs the tile and keeps the group's result line."""
        self.ctx.ui.result(action.id, outcome.outcome, outcome.text, outcome.detail)

    def progress(self, value: Optional[float], label: str = "") -> None:
        """Reports progress of the running long action (no-op outside a job)."""
        job = getattr(self._local, "job", None)
        if job is not None:
            job.progress(value, label)

    def _execute(self, action: Action, ctx: ModuleContext) -> Outcome:
        try:
            outcome = action.run(ctx, self)
            return outcome if isinstance(outcome, Outcome) else Outcome("neutral", f"{action.label} finished")
        except ActionAbort as abort:
            return Outcome(abort.tone, abort.text, abort.detail)
        except PremiereUnavailable:
            return Outcome("warn", NOT_CONNECTED)
        except PremiereError as exc:
            self.log.warning("%s: script error %s (line %s) %s", action.id, exc.message, exc.line, exc.source)
            return Outcome("error", f"{action.label} failed", error_sentence(exc.message))
        except Exception:  # noqa: BLE001 - a bug in one action must not break the module
            self.log.exception("%s raised", action.id)
            return Outcome("error", f"{action.label} failed", INTERNAL_ERROR)

    def _host_action(self, name: str, value: Any) -> Optional[ActionResult]:
        if name.startswith("visible."):
            action_id = name[len("visible."):]
            if action_id in self.actions:
                self.set_visible(action_id, bool(value))
                return None
        return ActionResult(toast=("warn", f"{self.title}: unknown action {name}"), rerender=False)

    # --- rendering -------------------------------------------------------------------------------------------

    def visible_actions(self) -> list[Action]:
        hidden = self.hidden_ids()
        return [action for action in self.actions.values() if action.id not in hidden]

    def render(self) -> list:
        visible = self.visible_actions()
        if not visible:
            return [self.text("All quick actions are hidden. Show them in the host window under Actions.",
                              small=True)]
        nodes: list = []
        for group in GROUPS:
            members = [action for action in visible if action.group == group]
            if members:
                items = [self._tile(action) for action in members]
                for action in members:
                    items.extend(self._palette(action))
                for action in members:
                    items.extend(action.extras(self))
                nodes.append(self.group(group, items, id=group.lower(), tiles=True, collapsible=True,
                                        short=GROUP_CODES[group]))
        return nodes

    def _tile(self, action: Action) -> dict:
        if action.switch:
            return self.switch(action.id, action.label, action.is_on(self), short=action.short, icon=action.icon,
                               hint=action.hint, busy=action.id in self._running)
        return self._button(action)

    def _palette(self, action: Action) -> list:
        """The `swatches` row of an action whose menu is a palette (3.2), after the tiles of its lane."""
        menu = action.menu(self)
        if menu is None or not menu.palette:
            return []
        options = [dict({"value": value, "label": label}, **menu.swatch(value)) for value, label in menu.options]
        return [self.swatches(f"{action.id}.{menu.name}", menu.label, self.choice(action, menu.name), options,
                              hint=menu.hint or None, columns=menu.columns)]

    def _button(self, action: Action) -> dict:
        state = action.button(self)
        menu = action.menu(self)
        if menu is not None and menu.palette:
            menu = None  # drawn as the lane's swatches row, not as a corner menu
        kw: dict = {}
        spec = action.field(self)
        if spec is not None:
            kw["prompt"] = self.prompt(f"{action.id}.{spec['name']}", spec.get("label", action.label),
                                       spec.get("value", ""), placeholder=spec.get("placeholder", ""))
        elif menu is not None:
            current = self.choice(action, menu.name)
            kw["menu"] = self.menu(f"{action.id}.{menu.name}", current,
                                   [dict({"value": value, "label": label}, **menu.swatch(value))
                                    for value, label in menu.options],
                                   menu.badge(current))
        return self.button(action.id, state.get("label", action.label), hint=action.hint,
                           tone=state.get("tone", "neutral"), busy=action.id in self._running,
                           disabled=bool(state.get("disabled", False)), icon=action.icon,
                           short=state.get("short", action.short), **kw)

    def section_status(self) -> Optional[dict]:
        running = [self.actions[aid].label for aid in self._running if aid in self.actions]
        if running:
            return self.status(f"Running · {', '.join(running)}", "accent")
        return None

    def render_host(self) -> Optional[dict]:
        hidden = self.hidden_ids()
        rows = [{"id": action.id, "title": action.title or action.label, "icon": action.icon,
                 "subtitle": f"{action.group} · {action.hint}",
                 "toggle": {"id": self._id(f"{HOST_PREFIX}visible.{action.id}"), "value": action.id not in hidden}}
                for action in self.actions.values()]
        return {
            "title": self.title,
            "blocks": [
                {"type": "list", "id": "visibility", "title": "Actions in the panel",
                 "hint": "Hidden actions are not shown in the panel.", "rows": rows},
            ],
        }
