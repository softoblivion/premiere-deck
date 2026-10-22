from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, require_sequence, timecode
from premiere_deck.modules.quick_actions.results import number, value

DECK_BIN = "Deck"
DEFAULT_SECONDS = 5.0


class AdjustmentLayer(Action):
    id = "adjustment_layer"
    label = "Adjustment layer"
    hint = "Adds an adjustment layer over the selected clips, or for 5 seconds at the playhead."
    group = "Timeline"
    feasibility = (
        "ProjectItem.isAdjustmentLayer() (documented) finds an adjustment layer already in the project, the Deck "
        "bin first; Premiere 26.5.2 has no scripting call that creates one (QE qe.project offers no "
        "newAdjustmentLayer, checked live), so the user creates it once; where QE offers newAdjustmentLayer it "
        "is used; ProjectItem.name, "
        "ProjectItem.moveBin, ProjectItem.getInPoint/getOutPoint, ProjectItem.createBin, Project.getInsertionBin, "
        "Track.overwriteClip(item, Time) and the TrackItem.end setter (documented, ppro-scripting, Premiere 14+) "
        "place and trim it; QE sequence.addTracks(1, index, 0, 1, audioIndex, 0, 0) (undocumented) adds a video "
        "track when no track above the selection is free."
    )

    def run(self, ctx, module):
        sequence = require_sequence(ctx)
        selection = ctx.premiere.selection()
        if selection.items:
            start = min(item.start for item in selection.items)
            end = max(item.end for item in selection.items)
            video = [item.track_index for item in selection.items if item.track_kind == "video"]
            min_track = max(video) + 1 if video else 0
        else:
            start = ctx.premiere.playhead()
            end = start + DEFAULT_SECONDS
            min_track = 0
        result = self.eval(ctx, {"start": start, "end": end, "min_track": min_track, "bin": DECK_BIN,
                                 "name": f"Adjustment {sequence.width}x{sequence.height}"})
        track = int(number(result, "track_index", min_track))
        placed_start = number(result, "start", start)
        placed_end = number(result, "end", end)
        created = " · new item in the Deck bin" if value(result, "created", False) else ""
        return self.done(f"Adjustment layer added · V{track + 1} · {timecode(placed_start)} to "
                         f"{timecode(placed_end)}{created}")
