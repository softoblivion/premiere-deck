from __future__ import annotations

import re

from premiere_deck.modules.quick_actions.action import Action, require_sequence
from premiere_deck.modules.quick_actions.results import text

_VERSIONED = re.compile(r"^(?P<base>.+) v(?P<number>\d+)$", re.IGNORECASE)


def next_sequence_name(name: str, existing) -> str:
    """`<name> v2`, `v3`, ...: a name already ending in ` v<n>` is the base without that suffix and counting starts at
    `n + 1`, otherwise at 2; the first number whose name no sequence has (compared without case) is used."""
    match = _VERSIONED.match(name)
    if match:
        base, number = match.group("base"), int(match.group("number")) + 1
    else:
        base, number = name, 2
    taken = {str(other).lower() for other in existing}
    while f"{base} v{number}".lower() in taken:
        number += 1
    return f"{base} v{number}"


class DuplicateSequence(Action):
    id = "duplicate_sequence"
    label = "Duplicate sequence"
    short = "Duplicate"
    icon = "duplicate-sequence"
    hint = "Copies the active sequence as the next numbered version."
    group = "Sequence"
    feasibility = (
        "Sequence.clone() (documented, ppro-scripting, Premiere 14+) copies the sequence into the original's bin; "
        "the copy is found by comparing Project.sequences before and after, renamed through Sequence.name and the "
        "original is made active again with Project.openSequence() when Premiere activated the copy. The naming "
        "is done in the host (next_sequence_name)."
    )

    def run(self, ctx, module):
        sequence = require_sequence(ctx)
        name = next_sequence_name(sequence.name, [other.name for other in ctx.premiere.sequences()])
        result = self.eval(ctx, {"sequence_id": sequence.sequence_id, "name": name})
        return self.done(f"Duplicated as {text(result, 'name') or name}")
