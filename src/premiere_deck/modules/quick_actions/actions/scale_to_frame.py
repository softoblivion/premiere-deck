from __future__ import annotations

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, plural, require_selection, require_sequence,
)
from premiere_deck.modules.quick_actions.results import count, text


class ScaleToFrame(Action):
    id = "scale_to_frame"
    label = "Set to frame size"
    hint = "Turns on Set to Frame Size for the selected video clips."
    group = "Clips"
    feasibility = (
        "Sequence.getSelection() (documented); per clip through the QE track item setScaleToFrameSize() when the "
        "QE item offers it (undocumented, existence checked at run time; 26.5.2 requires the flag argument, "
        "setScaleToFrameSize(true)), otherwise "
        "ProjectItem.setScaleToFrameSize() (documented, ppro-scripting, Premiere 14+) on the clip's source item. "
        "ExtendScript has no getter for the flag and the Motion Scale value stays 100; verified on 26.5.2 by "
        "comparing exported frames with the flag off and on."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        selection = require_selection(ctx)
        if not any(item.track_kind == "video" for item in selection.items):
            raise ActionAbort("warn", "Select a video clip")
        result = self.eval(ctx)
        clips = count(result, "clips")
        suffix = " · set on the source items" if text(result, "method") == "item" else ""
        return self.done(f"Set to frame size · {plural(clips, 'clip')}{suffix}")
