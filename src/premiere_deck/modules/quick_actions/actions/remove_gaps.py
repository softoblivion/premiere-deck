from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, plural, require_sequence, timecode
from premiere_deck.modules.quick_actions.results import count, number


class RemoveGaps(Action):
    id = "remove_gaps"
    label = "Remove all gaps"
    hint = "Closes every gap that is empty on all tracks, keeping audio and video in sync."
    group = "Timeline"
    feasibility = (
        "Sequence.videoTracks / audioTracks, Track.clips, TrackItem.start / end and TrackItem.move(Time) "
        "(documented, ppro-scripting, Premiere 14+); Track.isLocked() (undocumented, present in 25.x and 26.x; a "
        "track without it counts as unlocked). Gap objects do not exist in the scripting API, so gaps are computed "
        "from clip extents. Only gaps empty on every track close (the gaps Premiere's own Ripple Delete accepts); "
        "gaps before the last clip of a locked track stay, as moving the rest would desync that track."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        result = self.eval(ctx)
        closed = count(result, "closed")
        blocked = count(result, "blocked")
        if closed == 0:
            if blocked:
                return self.done(f"{plural(blocked, 'gap')} kept: a locked track has clips after them", "warn")
            return self.done("No gaps found", "neutral")
        text = f"Removed {plural(closed, 'gap')} · {timecode(number(result, 'removed'))}"
        if blocked:
            text += f" · {blocked} kept by a locked track"
        return self.done(text)
