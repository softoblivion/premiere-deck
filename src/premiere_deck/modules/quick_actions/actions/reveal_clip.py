from __future__ import annotations

import os
from pathlib import PureWindowsPath

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, require_selection, require_sequence,
)


class RevealClip(Action):
    id = "reveal_clip"
    label = "Reveal clip file"
    hint = "Shows the selected clip's media file in Explorer."
    group = "Project"
    feasibility = (
        "Facade selection() and item(node_id) (ProjectItem.getMediaPath, documented), HostInfo.reveal for "
        "Explorer. No script of its own."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        selection = require_selection(ctx)
        first = next((item for item in selection.items if item.node_id), None)
        item = ctx.premiere.item(first.node_id) if first else None
        if item is None or not item.media_path:
            raise ActionAbort("warn", "The selected clip has no media file")
        name = PureWindowsPath(item.media_path).name
        if not os.path.exists(item.media_path):
            raise ActionAbort("warn", f"File not found: {name}")
        ctx.host.reveal(item.media_path)
        return self.done(f"Revealed {name}", "neutral")
