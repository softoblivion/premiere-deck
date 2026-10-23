from __future__ import annotations

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, plural, require_selection, require_sequence,
)
from premiere_deck.modules.quick_actions.results import count, text


class SelectSameSource(Action):
    id = "select_same_source"
    label = "Select same source"
    short = "Same source"
    icon = "select-same-source"
    hint = "Selects every clip that uses the same media as the selected clip."
    group = "Selection"
    feasibility = (
        "TrackItem.projectItem, ProjectItem.nodeId and TrackItem.setSelected(state, updateUI) (documented, "
        "ppro-scripting, Premiere 14+); the source item comes from the facade's selection()."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        first = require_selection(ctx).items[0]
        if not first.node_id:
            raise ActionAbort("warn", "The selected clip has no source item")
        result = self.eval(ctx, {"node_id": first.node_id})
        selected = count(result, "count")
        name = text(result, "name") or first.name
        return self.done(f"Selected {plural(selected, 'clip')} of {name}")
