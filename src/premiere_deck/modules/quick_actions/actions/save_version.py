from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath

from premiere_deck.modules.quick_actions.action import Action, require_project

_VERSIONED = re.compile(r"^(?P<base>.*)_v(?P<number>\d{3,})$", re.IGNORECASE)
DEFAULT_PROJECT_DIR = "Project"


def next_version_path(current: Path, folder: Path) -> Path:
    """`<stem>_v<NNN>.prproj` in `folder`: a stem ending in `_vNNN` is incremented, any other stem starts at
    `_v001`; the number grows until the name is free. At least three digits."""
    match = _VERSIONED.match(current.stem)
    if match:
        base, number = match.group("base"), int(match.group("number")) + 1
    else:
        base, number = current.stem, 1
    while True:
        candidate = folder / f"{base}_v{number:03d}{current.suffix or '.prproj'}"
        if not candidate.exists():
            return candidate
        number += 1


def version_folder(project_path: Path, root: Path) -> Path:
    """The layout's project folder when the Files module is set up for the project (`<root>/.premiere-deck/
    project.json` exists), else the folder of the open project file."""
    deck = root / ".premiere-deck"
    if not (deck / "project.json").is_file():
        return project_path.parent
    name = DEFAULT_PROJECT_DIR
    try:
        layout = json.loads((deck / "layout.json").read_text(encoding="utf-8"))
        candidate = layout.get("project_dir") if isinstance(layout, dict) else None
        if isinstance(candidate, str) and candidate.strip() and _is_relative(candidate):
            name = candidate.strip()
    except (OSError, ValueError):
        pass
    return root / name


def _is_relative(path: str) -> bool:
    pure = PureWindowsPath(path)
    return not pure.is_absolute() and not pure.drive and ".." not in pure.parts


class SaveVersion(Action):
    id = "save_version"
    label = "Save version"
    short = "SAVE"
    icon = "save-version"
    hint = "Saves the project as the next numbered version and keeps editing the new file."
    group = "Project"
    feasibility = (
        "Facade save_project_as(path) (Project.saveAs, documented, ppro-scripting, Premiere 14+: writes the open "
        "project to the new path and makes it the open project). The numbering is done in the host."
    )

    def run(self, ctx, module):
        project = require_project(ctx)
        current = Path(project.path)
        folder = version_folder(current, Path(project.root))
        folder.mkdir(parents=True, exist_ok=True)
        target = next_version_path(current, folder)
        ctx.premiere.save_project_as(str(target))
        return self.done(f"Saved as {target.name}")
