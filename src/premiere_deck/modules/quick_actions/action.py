"""The base class of a quick action and the helpers every action uses (docs/QUICK_ACTIONS.md).

An action is one class under `actions/` with `id`, `label`, `short`, `icon`, `hint`, `group`, `feasibility` and
`run(ctx, module) -> Outcome`. Preconditions that fail raise `ActionAbort` with the result to show; script errors
raise `PremiereError` and become the result "<Label> failed" with the sentence as `detail` (the module reports every
outcome through `ctx.ui.result`, docs/MODULES.md 4). Scripts live in `jsx/<name>.jsx`; `jsx/_common.jsx` is
prepended to each of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from premiere_deck.modules.base import ModuleContext
from premiere_deck.premiere.model import ProjectInfo, Selection, SequenceInfo

if TYPE_CHECKING:  # pragma: no cover
    from premiere_deck.modules.quick_actions.module import QuickActionsModule

JSX_DIR = Path(__file__).parent / "jsx"
COMMON_JSX = "_common"

# Scripts throw `deckFail(sentence)`, which prefixes the sentence with this marker; only such sentences reach the
# user, every other script error becomes a generic sentence (docs/PROTOCOL.md 9).
USER_ERROR_PREFIX = "deck: "

GROUPS = ("Timeline", "Tracks", "Selection", "Clips", "Labels", "Project", "Sequence", "Export", "Keys")
# The group code drawn at the edge of the group's row in the panel (docs/QUICK_ACTIONS.md 1, group `short`).
GROUP_CODES = {"Timeline": "TIME", "Tracks": "TRKS", "Selection": "SEL", "Clips": "CLIP", "Labels": "LBL",
               "Project": "PROJ",
               "Sequence": "SEQ", "Export": "OUT", "Keys": "KEYS"}

DEFAULT_TIMEOUT_MS = 30000

_INVALID_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass
class Outcome:
    """What one press reports through `ctx.ui.result`: `outcome` is ok | warn | error | neutral."""

    outcome: str
    text: str
    detail: Optional[str] = None


class ActionAbort(Exception):
    """A precondition failed (nothing selected, no active sequence); carries the result to show."""

    def __init__(self, tone: str, text: str, detail: Optional[str] = None) -> None:
        super().__init__(text)
        self.tone = tone  # the outcome: warn | error | neutral
        self.text = text
        self.detail = detail


@dataclass(frozen=True)
class Menu:
    """The choice an action carries as the `menu` of its tile (docs/QUICK_ACTIONS.md 1). `options` are
    (value, label); the badge of a value is its entry in `badges` or else its label; `swatches` (value, CSS hex)
    give an option its colour dot. With `runs` a pick also runs the action (the colour menu); otherwise a pick only
    stores the choice. With `palette` (3.2) the choice is drawn as a `swatches` row of the action's lane in
    `columns` cells per row instead of the tile's corner menu (every option then needs a swatch)."""

    name: str
    label: str
    default: str
    options: tuple[tuple[str, str], ...]
    badges: tuple[tuple[str, str], ...] = ()
    runs: bool = False
    swatches: tuple[tuple[str, str], ...] = ()
    palette: bool = False
    columns: int = 8
    hint: str = ""

    def badge(self, value: str) -> str:
        badges = dict(self.badges)
        if value in badges:
            return badges[value]
        return dict(self.options).get(value, "")

    def swatch(self, value: str) -> dict:
        """`{"swatch": "#rrggbb"}` for an option with a colour, else `{}`."""
        colour = dict(self.swatches).get(value)
        return {"swatch": colour} if colour else {}


class Action:
    id: str = ""
    label: str = ""
    # Row title on the host window's Actions page when `label` is longer than its column (about 16 characters).
    title: str = ""
    short: str = ""  # tile code (docs/QUICK_ACTIONS.md 1), at most 16 characters (protocol.SHORT_MAX)
    icon: str = ""  # symbol name of the panel sprite
    hint: str = ""
    group: str = ""
    # The exact API calls the action relies on and where they are documented (docs/QUICK_ACTIONS.md 1).
    feasibility: str = ""
    # Long actions run as a job with `busy` on their button.
    long: bool = False
    # Switch actions render as a `switch` node whose value is `is_on(module)`; a click runs the action only when
    # the requested state differs from it.
    switch: bool = False
    timeout_ms: int = DEFAULT_TIMEOUT_MS

    def menu(self, module: "QuickActionsModule") -> Optional[Menu]:
        return None

    def is_on(self, module: "QuickActionsModule") -> bool:
        """The state of a switch action: True while its effect is applied (the module remembers what to restore)."""
        return self.id in module.toggles

    def field(self, module: "QuickActionsModule") -> Optional[dict]:
        """The stored text the action's tile carries as its `prompt` (docs/PROTOCOL.md 6.1): {name, label, value,
        placeholder}; the prompt id is `<action id>.<name>` and a submit goes to `submit(module, name, value)`."""
        return None

    def submit(self, module: "QuickActionsModule", name: str, value: Any) -> Optional["Outcome"]:
        """Stores a prompt value; returns None when stored (the module then runs the action) or the result to
        show when the value is refused."""
        return Outcome("warn", f"{self.label}: unknown field {name}")

    def extras(self, module: "QuickActionsModule") -> list:
        """Nodes the action adds to its lane after the tiles (3.2: the Labels target chip)."""
        return []

    def button(self, module: "QuickActionsModule") -> dict:
        """Overrides of the button node (`label`, `short`, `tone`, `disabled`) for the action's current state."""
        return {}

    def run(self, ctx: ModuleContext, module: "QuickActionsModule") -> Outcome:
        raise NotImplementedError

    # --- helpers --------------------------------------------------------------------------------------------

    def eval(self, ctx: ModuleContext, args: Any = None, *, script: Optional[str] = None,
             timeout_ms: Optional[int] = None) -> Any:
        """Runs `jsx/<script or id>.jsx` with `_common.jsx` prepended."""
        name = script or self.id
        return ctx.premiere.eval(f"actions.{name}", script_text(ctx, name), args,
                                 timeout_ms=timeout_ms or self.timeout_ms)

    def done(self, text: str, outcome: str = "ok", detail: Optional[str] = None) -> Outcome:
        return Outcome(outcome, text, detail)

    def fail(self, detail: str) -> Outcome:
        """The result "<Label> failed" with the one-sentence reason as `detail`."""
        return Outcome("error", f"{self.label} failed", detail)


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
    """`HH:MM:SS` of a time in seconds (the result format of docs/QUICK_ACTIONS.md 4)."""
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
