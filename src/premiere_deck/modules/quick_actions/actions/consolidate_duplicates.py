from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, require_project


class ConsolidateDuplicates(Action):
    id = "consolidate_duplicates"
    label = "Consolidate duplicates"
    title = "Consolidate"
    short = "CONS"
    icon = "consolidate"
    hint = "Merges project items that point to the same media file."
    group = "Project"
    feasibility = "Project.consolidateDuplicates() (documented, ppro-scripting, Premiere 14+); runs without a dialog."

    def run(self, ctx, module):
        require_project(ctx)
        self.eval(ctx)
        return self.done("Duplicates consolidated")
