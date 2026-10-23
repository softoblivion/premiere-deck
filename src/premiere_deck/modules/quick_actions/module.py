"""Quick actions module (docs/QUICK_ACTIONS.md): renders the actions as collapsible tile groups, routes clicks, runs
long actions as jobs with a busy tile, reports every press through `ctx.ui.result`, and keeps only the choices of
the tile menus and inputs, the visibility setting and the state of the two switch actions (solo, lock)."""

from __future__ import annotations

import dataclasses
import threading
from typing import Any, Callable, Optional

from premiere_deck.modules.base import ActionResult, Module, ModuleContext
from premiere_deck.modules.quick_actions.action import (
    GROUPS, Action, ActionAbort, Menu, Outcome, error_sentence,
)
from premiere_deck.modules.quick_actions.actions import ACTION_CLASSES
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable

HIDDEN_KEY = "hidden"
CHOICES_KEY = "choices"
HOST_PREFIX = "host:"
NOT_CONNECTED = "Premiere Pro is not connected"
INTERNAL_ERROR = "An internal error occurred. Details are in the log."


class _ModuleThreadPremiere:
    """The facade as seen from a job thread: every call goes through `run_on_module_thread(fn, *args, **kw)` when
    the facade has it (docs/MODULES.md 2, threading) and waits for the Future it returns; the test double has none
    and is called directly."""

    def __init__(self, premiere: Any) -> None:
        self._premiere = premiere
        self._runner: Optional[Callable] = getattr(premiere, "run_on_module_thread", None)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._premiere, name)
        if self._runner is None or not callable(attr):
            return attr
        runner = self._runner

        def call(*args: Any, **kw: Any) -> Any:
            outcome = runner(attr, *args, **kw)
            wait = getattr(outcome, "result", None)
            return wait() if callable(wait) else outcome

        return call


class QuickActionsModule(Module):
    id = "actions"
    title = "Actions"
    icon = "actions"
    description = "Buttons for routine timeline and project operations."
    order = 5
    events = ()
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

    # --- lifecycle -------------------------------------------------------------------------------------------

    def stop(self) -> None:
        self._cancel_jobs()
        self.toggles.clear()

    def project_closed(self) -> None:
        self._cancel_jobs()
        self.toggles.clear()

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
                # Inputs follow the tiles, so they never split the tile grid.
                items = [self._tile(action) for action in members]
                items += [self._field(action) for action in members if action.field(self) is not None]
                nodes.append(self.group(group, items, id=group.lower(), tiles=True, collapsible=True))
        return nodes

    def _tile(self, action: Action) -> dict:
        if action.switch:
            return self.switch(action.id, action.label, action.is_on(self), short=action.short, icon=action.icon,
                               hint=action.hint, busy=action.id in self._running)
        return self._button(action)

    def _field(self, action: Action) -> dict:
        spec = action.field(self) or {}
        return self.input(f"{action.id}.{spec['name']}", spec.get("label", action.label), spec.get("value", ""),
                          placeholder=spec.get("placeholder", ""), submit_label=spec.get("submit_label"),
                          disabled=action.id in self._running)

    def _button(self, action: Action) -> dict:
        state = action.button(self)
        menu = action.menu(self)
        kw: dict = {}
        if menu is not None:
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
