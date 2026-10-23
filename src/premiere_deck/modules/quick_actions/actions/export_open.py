from __future__ import annotations

from premiere_deck.modules.quick_actions import premiere_window
from premiere_deck.modules.quick_actions.action import Action, require_sequence
from premiere_deck.modules.quick_actions.results import text


class ExportOpen(Action):
    id = "export_open"
    label = "Export"
    short = "EXP"
    icon = "export"
    hint = "Opens Premiere's Export mode for the active sequence."
    group = "Export"
    feasibility = (
        "Project.openSequence (documented) opens the active sequence in the Timeline; ExtendScript 26.5.2 has no "
        "app.executeCommand and no export UI call, so the host posts Window > Timelines > <sequence> (focus) and "
        "File > Export > Media... (menu command 11240, found by its label path when the id moves) to Premiere's "
        "main window as WM_COMMAND (Win32 GetMenu, PostMessage), which works while the Deck panel has the "
        "keyboard focus (verified 26.5.2)."
    )

    def run(self, ctx, module):
        sequence = require_sequence(ctx)
        result = self.eval(ctx, {"sequence_id": sequence.sequence_id})
        if not premiere_window.open_export_mode(text(result, "name") or sequence.name):
            return self.fail("Premiere's File > Export > Media command was not found or is unavailable.")
        return self.done(f"Export opened · {text(result, 'name') or sequence.name}")
