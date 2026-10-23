from __future__ import annotations

from premiere_deck.modules.quick_actions.action import (
    Action, plural, require_selection, require_sequence,
)
from premiere_deck.modules.quick_actions.results import count, value


class LockOthers(Action):
    id = "lock_others"
    label = "Lock other tracks"
    title = "Lock others"
    short = "LOCK"
    icon = "lock-others"
    hint = "Locks every track without a selected clip; switch off to restore the locks."
    group = "Tracks"
    switch = True
    feasibility = (
        "Track.isLocked() and Track.setLocked(0 | 1) (undocumented, present in 25.x and 26.x; the script fails "
        "with a clear sentence where they are missing). The lock states before are kept in the module and "
        "restored when the switch is turned off."
    )

    def run(self, ctx, module):
        state = module.toggles.pop(self.id, None)
        if state is not None:
            self.eval(ctx, {"mode": "restore", "sequence_id": state["sequence_id"], "states": state["states"]})
            return self.done("Track locks restored", "neutral")
        sequence = require_sequence(ctx)
        keep = sorted({(item.track_kind, item.track_index) for item in require_selection(ctx).items})
        result = self.eval(ctx, {"mode": "lock", "sequence_id": sequence.sequence_id,
                                 "keep": [{"kind": kind, "index": index} for kind, index in keep]})
        locked = count(result, "locked")
        if locked == 0:
            return self.done("No other tracks to lock", "neutral")
        states = value(result, "states", {})
        if not isinstance(states, dict):
            states = {}
        module.toggles[self.id] = {"sequence_id": sequence.sequence_id,
                                   "states": {kind: list(states.get(kind) or []) for kind in ("video", "audio")}}
        return self.done(f"Locked {plural(locked, 'track')}")
