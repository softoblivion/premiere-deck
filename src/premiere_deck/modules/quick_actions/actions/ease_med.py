from __future__ import annotations

from premiere_deck.modules.quick_actions.actions.ease_slow import Ease


class EaseMed(Ease):
    id = "ease_med"
    label = "Ease medium"
    short = "MED"
    icon = "ease-med"
    hint = "Eases the keyframe pair around the playhead with medium strength (bezier 0.5, 0, 0.5, 1)."
    preset = "med"
