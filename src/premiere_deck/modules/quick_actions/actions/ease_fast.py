from __future__ import annotations

from premiere_deck.modules.quick_actions.actions.ease_slow import Ease


class EaseFast(Ease):
    id = "ease_fast"
    label = "Ease fast"
    short = "FAST"
    icon = "ease-fast"
    hint = "Eases the keyframe pair around the playhead strongly (bezier 0.7, 0, 0.3, 1)."
    preset = "fast"
