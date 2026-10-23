from __future__ import annotations

from typing import Optional

from premiere_deck.modules.quick_actions.action import Action, require_sequence
from premiere_deck.modules.quick_actions.results import count, text

MOTION = "default"


def target_size(width: int, height: int, numerator: int, denominator: int) -> tuple[int, int]:
    """Frame size of a reframed copy: the source's short side is kept, the long side follows the ratio; even
    numbers (1080x1920 at 16:9 -> 1920x1080, 1920x1080 at 9:16 -> 1080x1920, 1920x1080 at 16:9 stays)."""
    short = min(int(width), int(height)) or 1080
    if numerator >= denominator:
        return _even(short * numerator / denominator), _even(short)
    return _even(short), _even(short * denominator / numerator)


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def free_name(base: str, taken) -> str:
    """`base`, or `base 2`, `base 3`, ... when a sequence of the project already has that name (case-insensitive):
    reframing "Spring" at 9:16 while "Spring 9x16" exists gives "Spring 9x16 2"."""
    names = {str(name).lower() for name in taken}
    if base.lower() not in names:
        return base
    number = 2
    while f"{base} {number}".lower() in names:
        number += 1
    return f"{base} {number}"


class Reframe(Action):
    """A copy of the active sequence through Auto Reframe at a ratio and frame size (script `reframe`)."""

    group = "Sequence"
    long = True
    timeout_ms = 600000  # Auto Reframe analyses the whole sequence
    feasibility = (
        "Sequence.autoReframeSequence(numerator, denominator, motionPreset, newName, useNestedSequences) "
        "(documented, ppro-scripting, Premiere 14.x and later) creates the copy; Premiere keeps the source "
        "height, so Sequence.getSettings / setSettings (documented) set the frame size after. The analysis can "
        "take minutes; the script blocks Premiere meanwhile."
    )

    def target(self, module, width: int, height: int) -> Optional[tuple[int, int, int, int, str]]:
        """(numerator, denominator, width, height, name suffix) of the copy."""
        raise NotImplementedError

    def run(self, ctx, module):
        sequence = require_sequence(ctx)
        numerator, denominator, width, height, suffix = self.target(module, sequence.width, sequence.height)
        name = free_name(f"{sequence.name} {suffix}", [s.name for s in ctx.premiere.sequences()])
        result = self.eval(ctx, {"sequence_id": sequence.sequence_id, "numerator": numerator,
                                 "denominator": denominator, "motion": MOTION, "name": name,
                                 "width": width, "height": height}, script="reframe")
        made_w, made_h = count(result, "width") or width, count(result, "height") or height
        line = f"Created {text(result, 'name') or name} · {made_w}x{made_h}"
        if (made_w, made_h) != (width, height):
            return self.done(line + f" · frame size not changed to {width}x{height}", "warn")
        return self.done(line)


class Create169(Reframe):
    id = "create_16_9"
    label = "Reframe 16:9"
    short = "16:9"
    icon = "ratio-16-9"
    hint = "Creates an auto-reframed 16:9 copy of the active sequence."

    def target(self, module, width, height):
        return (16, 9) + target_size(width, height, 16, 9) + ("16x9",)
