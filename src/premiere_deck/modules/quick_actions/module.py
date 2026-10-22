"""Quick actions module (docs/QUICK_ACTIONS.md): renders the actions as button groups, routes clicks, runs long
actions as jobs with a busy button, and keeps only the last result, the selects' choices, the visibility setting
and the state of the two toggle actions (solo, lock)."""

from __future__ import annotations

import dataclasses
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from premiere_deck.modules.base import ActionResult, Module, ModuleContext
from premiere_deck.modules.quick_actions import presets as presets_mod
from premiere_deck.modules.quick_actions.action import (
    GROUPS, Action, ActionAbort, Control, error_sentence, plural,
)
from premiere_deck.modules.quick_actions.actions import ACTION_CLASSES
from premiere_deck.premiere.model import PremiereError, PremiereUnavailable, ProjectInfo

HIDDEN_KEY = "hidden"
CHOICES_KEY = "choices"
HOST_PREFIX = "host:"
NOT_CONNECTED = "Premiere Pro is not connected"


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
    title = "Quick actions"
    description = "Buttons for routine timeline and project operations."
    order = 20
    events = ()
    requires = ()

    def __init__(self, ctx: ModuleContext) -> None:
        super().__init__(ctx)
        self.actions: dict[str, Action] = {}
        for cls in ACTION_CLASSES:
            action = cls()
            self.actions[action.id] = action
        self.toggles: dict[str, dict] = {}
        self.presets: list[presets_mod.Preset] = []
        self.preset_roots: Optional[list[Path]] = None  # None: the installed Premiere / Media Encoder
        self.last: Optional[tuple[str, str]] = None
        self._running: dict[str, Any] = {}
        self._local = threading.local()

    # --- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.refresh_presets()

    def stop(self) -> None:
        self._cancel_jobs()
        self.toggles.clear()

    def project_opened(self, project: ProjectInfo) -> None:
        self.refresh_presets()

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
        """The stored value of an action's control, or its default when missing or no longer an option."""
        control = self._control(action, name)
        if control is None:
            return ""
        stored = self._choices().get(f"{action.id}.{name}")
        if control.kind == "select":
            values = [value for value, _ in control.options]
            return stored if isinstance(stored, str) and stored in values else control.default
        return stored if isinstance(stored, str) else control.default

    def set_choice(self, action: Action, name: str, value: Any) -> bool:
        control = self._control(action, name)
        if control is None or not isinstance(value, str):
            return False
        if control.kind == "select" and value not in [v for v, _ in control.options]:
            return False
        choices = self._choices()
        choices[f"{action.id}.{name}"] = value
        self.ctx.settings.set(CHOICES_KEY, choices)
        return True

    def _control(self, action: Action, name: str) -> Optional[Control]:
        return next((control for control in action.controls(self) if control.name == name), None)

    # --- presets ---------------------------------------------------------------------------------------------

    def user_presets_dir(self) -> Path:
        return Path(self.ctx.paths.root) / "presets"

    def refresh_presets(self) -> None:
        try:
            self.presets = presets_mod.available_presets(self.user_presets_dir(), system_roots=self.preset_roots)
        except OSError as exc:
            self.log.warning("export presets could not be read: %s", exc)
            self.presets = []

    def preset(self, preset_id: str) -> Optional[presets_mod.Preset]:
        return next((preset for preset in self.presets if preset.id == preset_id), None)

    # --- actions ---------------------------------------------------------------------------------------------

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        local = self.strip_prefix(action_id)
        if local.startswith(HOST_PREFIX):
            return self._host_action(local[len(HOST_PREFIX):], value)
        if local in self.actions:
            return self.trigger(local)
        action_part, _, name = local.partition(".")
        if action_part in self.actions and name:
            self.set_choice(self.actions[action_part], name, value)
            return None
        return super().on_action(action_id, value, row)

    def trigger(self, action_id: str) -> Optional[ActionResult]:
        action = self.actions[action_id]
        if action_id in self._running:
            return ActionResult(toast=None, rerender=False)
        if not action.long:
            result = self._execute(action, self.ctx)
            self.last = result.toast
            return result
        job_ctx = dataclasses.replace(self.ctx, premiere=_ModuleThreadPremiere(self.ctx.premiere))
        pending = object()
        self._running[action_id] = pending

        def work(job: Any) -> ActionResult:
            self._local.job = job
            try:
                return self._execute(action, job_ctx)
            finally:
                self._local.job = None

        def finish(job: Any, result: Any) -> None:
            self._running.pop(action_id, None)
            if not isinstance(result, ActionResult):
                result = ActionResult()
            self.last = result.toast
            if result.toast:
                self.ctx.ui.toast(*result.toast)
            self.ctx.ui.invalidate()

        def fail(job: Any, exc: BaseException) -> None:
            self.log.error("%s failed in its job: %s", action_id, exc)
            finish(job, ActionResult(toast=("error", f"{action.label} failed: an internal error occurred. "
                                                     "Details are in the log.")))

        job = self.ctx.jobs.submit(work, title=action.label, on_done=finish, on_error=fail)
        if self._running.get(action_id) is pending:
            self._running[action_id] = job
        return ActionResult(toast=None)

    def progress(self, value: Optional[float], label: str = "") -> None:
        """Reports progress of the running long action (no-op outside a job)."""
        job = getattr(self._local, "job", None)
        if job is not None:
            job.progress(value, label)

    def _execute(self, action: Action, ctx: ModuleContext) -> ActionResult:
        try:
            result = action.run(ctx, self)
            return result if isinstance(result, ActionResult) else ActionResult()
        except ActionAbort as abort:
            return ActionResult(toast=(abort.tone, abort.text))
        except PremiereUnavailable:
            return ActionResult(toast=("warn", NOT_CONNECTED))
        except PremiereError as exc:
            self.log.warning("%s: script error %s (line %s) %s", action.id, exc.message, exc.line, exc.source)
            return ActionResult(toast=("error", f"{action.label} failed: {error_sentence(exc.message)}"))
        except Exception:  # noqa: BLE001 - a bug in one action must not break the module
            self.log.exception("%s raised", action.id)
            return ActionResult(toast=("error", f"{action.label} failed: an internal error occurred. "
                                                "Details are in the log."))

    def _host_action(self, name: str, value: Any) -> Optional[ActionResult]:
        if name.startswith("visible."):
            action_id = name[len("visible."):]
            if action_id in self.actions:
                self.set_visible(action_id, bool(value))
                return None
        elif name == "open_presets":
            folder = self.user_presets_dir()
            folder.mkdir(parents=True, exist_ok=True)
            self.ctx.host.open_path(str(folder))
            return None
        elif name == "refresh_presets":
            self.refresh_presets()
            return ActionResult(toast=("neutral", f"{plural(len(self.presets), 'export preset')} found"))
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
            if not members:
                continue
            items: list = []
            for action in members:
                # An action's own controls come right before its button; the display lays buttons out in two
                # columns and gives every other node the full width.
                items.extend(self._control_node(action, control) for control in action.controls(self))
                items.append(self._button(action))
            nodes.append(self.group(group, items, columns=2))
        if self.last:
            nodes.append(self.text(self.last[1], tone=self.last[0], small=True))
        return nodes

    def _button(self, action: Action) -> dict:
        state = action.button(self)
        return self.button(action.id, state.get("label", action.label), hint=action.hint,
                           tone=state.get("tone", "neutral"), busy=action.id in self._running,
                           disabled=bool(state.get("disabled", False)))

    def _control_node(self, action: Action, control: Control) -> dict:
        node_id = f"{action.id}.{control.name}"
        if control.kind == "select":
            options = [{"value": value, "label": label} for value, label in control.options]
            disabled = len(options) == 1 and options[0]["value"] == ""
            return self.select(node_id, control.label, self.choice(action, control.name), options, disabled=disabled)
        return self.input(node_id, control.label, self.choice(action, control.name), placeholder=control.placeholder)

    def section_status(self) -> Optional[dict]:
        running = [self.actions[aid].label for aid in self._running if aid in self.actions]
        if running:
            return self.status(f"Running · {', '.join(running)}", "accent")
        return None

    def render_host(self) -> Optional[dict]:
        hidden = self.hidden_ids()
        rows = [{"id": action.id, "title": action.label, "subtitle": f"{action.group} · {action.hint}",
                 "toggle": {"id": self._id(f"{HOST_PREFIX}visible.{action.id}"), "value": action.id not in hidden}}
                for action in self.actions.values()]
        preset_rows = [{"id": preset.id, "title": preset.label,
                        "subtitle": {"shipped": "Shipped with Premiere Deck", "system": "Adobe system preset",
                                     "user": "Your preset"}[preset.source]}
                       for preset in self.presets]
        return {
            "title": self.title,
            "blocks": [
                {"type": "list", "id": "visibility", "title": "Actions in the panel",
                 "hint": "Hidden actions are not shown in the panel.", "rows": rows},
                {"type": "list", "id": "presets", "title": "Export presets",
                 "hint": f"Add your own .epr files to {self.user_presets_dir()}.", "rows": preset_rows,
                 "empty": "No export presets found.",
                 "actions": [{"id": self._id(f"{HOST_PREFIX}open_presets"), "label": "Open folder"},
                             {"id": self._id(f"{HOST_PREFIX}refresh_presets"), "label": "Rescan"}]},
            ],
        }
