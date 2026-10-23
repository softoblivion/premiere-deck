from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, require_sequence
from premiere_deck.modules.quick_actions.actions.add_video_track import track_name
from premiere_deck.modules.quick_actions.results import count, text, value


class RemoveTrack(Action):
    """Removes the outermost track of `kind` (the highest video, the lowest audio) when it is empty and unlocked
    (script `remove_track`)."""

    kind = "video"
    group = "Tracks"

    def run(self, ctx, module):
        require_sequence(ctx)
        result = self.eval(ctx, {"kind": self.kind}, script="remove_track")
        name = track_name(self.kind, count(result, "index"))
        if value(result, "removed") is True:
            return self.done(f"Removed {name}")
        reason = text(result, "reason")
        if reason == "not_empty":
            return self.done(f"{name} is not empty", "warn")
        if reason == "locked":
            return self.done(f"{name} is locked", "warn")
        if reason == "last":
            return self.done(f"{name} is the only {self.kind} track", "neutral")
        return self.fail("Premiere did not remove the track.")


class RemoveVideoTrack(RemoveTrack):
    id = "remove_video_track"
    label = "Remove video track"
    title = "Remove V track"
    short = "−V"
    icon = "track-video-remove"
    hint = "Removes the highest video track when it is empty and unlocked."
    kind = "video"
    feasibility = (
        "QE sequence removeVideoTrack(index) (undocumented, present in 25.x and 26.x; delete_empty_tracks uses the "
        "same call); Track.clips.numItems (documented) and Track.isLocked() (undocumented) decide whether it may go; "
        "one video track always stays."
    )
