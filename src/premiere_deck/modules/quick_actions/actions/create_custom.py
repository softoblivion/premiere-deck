from __future__ import annotations

import math
import re
from typing import Any, Optional

from premiere_deck.modules.quick_actions.action import Outcome
from premiere_deck.modules.quick_actions.actions.create_16_9 import Reframe

FIELD = "size"
DEFAULT_SIZE = (1080, 1350)
SIZE_MIN, SIZE_MAX = 16, 16384  # Premiere's sequence frame size limits
_SIZE = re.compile(r"^\s*(\d{1,5})\s*[xX×*:,]\s*(\d{1,5})\s*$")


def parse_size(raw: Any) -> Optional[tuple[int, int]]:
    """`1080x1350` (also `1080 x 1350`, `1080×1350`, `1080*1350`) as (width, height); None when not a size
    within Premiere's limits."""
    match = _SIZE.match(raw) if isinstance(raw, str) else None
    if not match:
        return None
    width, height = int(match.group(1)), int(match.group(2))
    if not (SIZE_MIN <= width <= SIZE_MAX and SIZE_MIN <= height <= SIZE_MAX):
        return None
    return width, height


def ratio(width: int, height: int) -> tuple[int, int]:
    """The reduced ratio Auto Reframe gets (1080x1350 -> 4:5)."""
    divisor = math.gcd(width, height) or 1
    return width // divisor, height // divisor


class CreateCustom(Reframe):
    id = "create_custom"
    label = "Reframe custom size"
    title = "Reframe custom"
    short = "CUST"
    icon = "ratio-custom"
    hint = "Creates an auto-reframed copy of the active sequence at the size entered below the tiles."

    def size(self, module) -> tuple[int, int]:
        return parse_size(module.stored(f"{self.id}.{FIELD}")) or DEFAULT_SIZE

    def field(self, module) -> Optional[dict]:
        width, height = self.size(module)
        return {"name": FIELD, "label": "Custom size", "value": f"{width}x{height}",
                "placeholder": "Width x height", "submit_label": "Create"}

    def submit(self, module, name, value) -> Optional[Outcome]:
        size = parse_size(value) if name == FIELD else None
        if size is None:
            return Outcome("warn", "Enter a size like 1080x1350",
                           f"Width and height between {SIZE_MIN} and {SIZE_MAX} pixels.")
        module.store(f"{self.id}.{FIELD}", f"{size[0]}x{size[1]}")
        return None

    def target(self, module, width, height):
        target_w, target_h = self.size(module)
        return ratio(target_w, target_h) + (target_w, target_h, f"{target_w}x{target_h}")
