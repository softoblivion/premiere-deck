from __future__ import annotations

from premiere_deck.modules.quick_actions.action import plural
from premiere_deck.modules.quick_actions.actions.ease_slow import Ease


class EaseLinear(Ease):
    id = "ease_linear"
    label = "Linear keys"
    short = "LIN"
    icon = "ease-linear"
    hint = "Removes the keys an ease added around the playhead and sets the pair to linear."
    preset = "linear"

    def report(self, props, added, removed, names):
        line = f"Linear · {plural(props, 'property', 'properties')}"
        return self.done(line + (f" · {plural(removed, 'key')} removed" if removed else ""), detail=names)
