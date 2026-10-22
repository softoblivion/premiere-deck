from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, plural, require_sequence
from premiere_deck.modules.quick_actions.results import count


class SelectOffline(Action):
    id = "select_offline"
    label = "Select offline clips"
    hint = "Selects every clip of the active sequence whose media is offline."
    group = "Selection"
    feasibility = (
        "TrackItem.projectItem, ProjectItem.isOffline() and TrackItem.setSelected(state, updateUI) (documented, "
        "ppro-scripting, Premiere 14+)."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        offline = count(self.eval(ctx), "count")
        if offline == 0:
            return self.done("No offline clips", "neutral")
        return self.done(f"Selected {plural(offline, 'offline clip')}", "warn")
