from __future__ import annotations

from pathlib import Path

from premiere_deck.modules.quick_actions.action import Action, require_project


class OpenProjectFolder(Action):
    id = "open_project_folder"
    label = "Open project folder"
    title = "Project folder"
    short = "DIR"
    icon = "open-folder"
    hint = "Opens the project folder in Explorer."
    group = "Project"
    feasibility = "Host only: facade project() for the root, HostInfo.open_path for Explorer. No script."

    def run(self, ctx, module):
        project = require_project(ctx)
        ctx.host.open_path(project.root)
        return self.done(f"Opened {Path(project.root).name}", "neutral")
