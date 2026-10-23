from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, Menu, require_sequence
from premiere_deck.modules.quick_actions.results import count, text

# value -> (numerator, denominator, label)
FORMATS = {
    "9x16": (9, 16, "9:16"),
    "1x1": (1, 1, "1:1"),
    "4x5": (4, 5, "4:5"),
}
MOTION = "default"


def target_size(width: int, height: int, numerator: int, denominator: int) -> tuple[int, int]:
    """Frame size of the copy: the source's short side becomes the width, the height follows the ratio; even
    numbers (1920x1080 at 9:16 -> 1080x1920, at 4:5 -> 1080x1350)."""
    base = min(int(width), int(height)) or 1080
    return _even(base), _even(base * denominator / numerator)


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


class SocialCopy(Action):
    id = "social_copy"
    label = "Social copy"
    short = "Social copy"
    icon = "social-copy"
    hint = "Creates an auto-reframed copy of the active sequence in the chosen format."
    group = "Sequence"
    long = True
    timeout_ms = 600000  # Auto Reframe analyses the whole sequence
    feasibility = (
        "Sequence.autoReframeSequence(numerator, denominator, motionPreset, newName, useNestedSequences) "
        "(documented, ppro-scripting, Premiere 14.x and later) creates the copy; Premiere keeps the source "
        "height, so Sequence.getSettings / setSettings (documented) set the frame size after. The analysis can "
        "take minutes; the script blocks Premiere meanwhile."
    )

    def menu(self, module):
        return Menu("format", "Format", "9x16", tuple((value, spec[2]) for value, spec in FORMATS.items()))

    def run(self, ctx, module):
        choice = module.choice(self, "format")
        numerator, denominator, _ = FORMATS[choice]
        sequence = require_sequence(ctx)
        width, height = target_size(sequence.width, sequence.height, numerator, denominator)
        name = f"{sequence.name} {choice}"
        result = self.eval(ctx, {"sequence_id": sequence.sequence_id, "numerator": numerator,
                                 "denominator": denominator, "motion": MOTION, "name": name,
                                 "width": width, "height": height})
        made_w, made_h = count(result, "width") or width, count(result, "height") or height
        text_ = f"Created {text(result, 'name') or name} · {made_w}x{made_h}"
        if (made_w, made_h) != (width, height):
            return self.done(text_ + f" · frame size not changed to {width}x{height}", "warn")
        return self.done(text_)
