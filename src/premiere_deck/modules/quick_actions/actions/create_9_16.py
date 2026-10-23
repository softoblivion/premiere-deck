from __future__ import annotations

from premiere_deck.modules.quick_actions.actions.create_16_9 import Reframe, target_size


class Create916(Reframe):
    id = "create_9_16"
    label = "Reframe 9:16"
    short = "9:16"
    icon = "ratio-9-16"
    hint = "Creates an auto-reframed 9:16 copy of the active sequence."

    def target(self, module, width, height):
        return (9, 16) + target_size(width, height, 9, 16) + ("9x16",)
