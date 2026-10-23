from __future__ import annotations

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, require_selection, require_sequence,
)
from premiere_deck.modules.quick_actions.results import value


class SoloTrack(Action):
    id = "solo_track"
    label = "Solo audio track"
    short = "SOLO"
    icon = "solo-track"
    hint = "Mutes every audio track except the selected clip's; switch off to restore the mutes."
    group = "Tracks"
    switch = True
    feasibility = (
        "Sequence.audioTracks, Track.isMuted() and Track.setMute(0 | 1) (documented, ppro-scripting, Premiere "
        "14+). The mute states before the solo are kept in the module (not in the project) and restored when the "
        "switch is turned off."
    )

    def run(self, ctx, module):
        state = module.toggles.pop(self.id, None)
        if state is not None:
            self.eval(ctx, {"mode": "restore", "sequence_id": state["sequence_id"], "states": state["states"]})
            return self.done("Audio tracks restored", "neutral")
        sequence = require_sequence(ctx)
        audio = [item for item in require_selection(ctx).items if item.track_kind == "audio"]
        if not audio:
            raise ActionAbort("warn", "Select a clip with audio")
        keep = audio[0].track_index
        result = self.eval(ctx, {"mode": "solo", "sequence_id": sequence.sequence_id, "keep_index": keep})
        states = value(result, "states", [])
        module.toggles[self.id] = {"sequence_id": sequence.sequence_id,
                                   "states": [1 if s else 0 for s in states] if isinstance(states, list) else []}
        return self.done(f"Soloed A{keep + 1}")
