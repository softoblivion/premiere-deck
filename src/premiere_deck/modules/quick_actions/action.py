"""The base class of a quick action and the helpers every action uses (docs/QUICK_ACTIONS.md).

An action is one class under `actions/` with `id`, `label`, `hint`, `group`, `feasibility` and `run(ctx, module)`.
Preconditions that fail raise `ActionAbort` with the toast to show; script errors raise `PremiereError` and are
turned into "<Label> failed: <sentence>" by the module. Scripts live in `jsx/<name>.jsx`; `jsx/_common.jsx` is
prepended to each of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from premiere_deck.modules.base import ActionResult, ModuleContext
from premiere_deck.premiere.model import ProjectInfo, Selection, SequenceInfo

if TYPE_CHECKING:  # pragma: no cover
    from premiere_deck.modules.quick_actions.module import QuickActionsModule

JSX_DIR = Path(__file__).parent / "jsx"
COMMON_JSX = "_common"

# Scripts throw `deckFail(sentence)`, which prefixes the sentence with this marker; only such sentences reach the
# user, every other script error becomes a generic sentence (docs/PROTOCOL.md 9).
USER_ERROR_PREFIX = "deck: "

GROUPS = ("Timeline", "Selection", "Clips", "Tracks", "Project", "Sequence", "Export")

DEFAULT_TIMEOUT_MS = 30000

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ActionAbort(Exception):
    """A precondition failed (nothing selected, no active sequence); carries the toast."""

    def __init__(self, tone: str, text: str) -> None:
        super().__init__(text)
        self.tone = tone
        self.text = text


@dataclass(frozen=True)
class Control:
    """A `select` or `input` node rendered right above the action's button; its value is passed to `run`."""

    name: str
    kind: str  # select | input
    label: str
    default: str = ""
    options: tuple[tuple[str, str], ...] = ()  # (value, label) for select
    placeholder: str = ""


class Action:
    id: str = ""
    label: str = ""
    hint: str = ""
    group: str = ""
    # The exact API calls the action relies on and where they are documented (docs/QUICK_ACTIONS.md 1).
    feasibility: str = ""
    # Long actions run as a job with `busy` on their button.
    long: bool = False
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    def controls(self, module: "QuickActionsModule") -> list[Control]:
        return []

    def button(self, module: "QuickActionsModule") -> dict:
        """Overrides of the button node (`label`, `tone`, `disabled`) for the action's current state."""
        return {}

    def run(self, ctx: ModuleContext, module: "QuickActionsModule") -> ActionResult:
        raise NotImplementedError

    # --- helpers --------------------------------------------------------------------------------------------

    def eval(self, ctx: ModuleContext, args: Any = None, *, script: Optional[str] = None,
             timeout_ms: Optional[int] = None) -> Any:
        """Runs `jsx/<script or id>.jsx` with `_common.jsx` prepended."""
        name = script or self.id
        return ctx.premiere.eval(f"actions.{name}", script_text(ctx, name), args,
                                 timeout_ms=timeout_ms or self.timeout_ms)

    def done(self, text: str, tone: str = "ok") -> ActionResult:
        return ActionResult(toast=(tone, text))


def script_text(ctx: ModuleContext, name: str) -> str:
    """The program sent for an action script: `_common.jsx` followed by the script. A `// @uses lib` line of the
    script is moved to the top, where the facade looks for it (docs/PREMIERE_API.md 5)."""
    body = ctx.premiere.script(str(JSX_DIR / f"{name}.jsx"))
    common = ctx.premiere.script(str(JSX_DIR / f"{COMMON_JSX}.jsx"))
    head = "// @uses lib\n" if "// @uses lib" in "\n".join(body.splitlines()[:3]) else ""
    return head + common + "\n" + body


def require_project(ctx: ModuleContext) -> ProjectInfo:
    project = ctx.premiere.project() or ctx.project
    if project is None:
        raise ActionAbort("warn", "No project open")
    return project


def require_sequence(ctx: ModuleContext) -> SequenceInfo:
    sequence = ctx.premiere.active_sequence()
    if sequence is None:
        raise ActionAbort("warn", "No active sequence")
    return sequence


def require_selection(ctx: ModuleContext) -> Selection:
    selection = ctx.premiere.selection()
    if not selection.items:
        raise ActionAbort("warn", "Nothing selected")
    return selection


def timecode(seconds: float) -> str:
    """`HH:MM:SS` of a time in seconds (the toast format of docs/QUICK_ACTIONS.md 4)."""
    total = max(0, int(seconds + 1e-6))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def safe_file_stem(name: str, fallback: str = "Sequence") -> str:
    """A Windows file name stem from a sequence name: invalid characters become `_`, no trailing dots or spaces."""
    stem = _INVALID_NAME.sub("_", name).strip().rstrip(". ")
    return stem or fallback


def error_sentence(message: str) -> str:
    """The user-facing sentence of a script error message."""
    text = str(message or "")
    if USER_ERROR_PREFIX in text:
        sentence = text.split(USER_ERROR_PREFIX, 1)[1].strip()
        return sentence if sentence.endswith(".") else sentence + "."
    if text.strip().lower() == "timeout":
        return "Premiere is waiting for a dialog."
    return "Premiere reported a script error."


def plural(count: int, word: str, many: Optional[str] = None) -> str:
    return f"{count} {word if count == 1 else (many or word + 's')}"
