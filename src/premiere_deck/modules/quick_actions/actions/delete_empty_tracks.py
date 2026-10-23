from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, plural, require_sequence
from premiere_deck.modules.quick_actions.results import count


class DeleteEmptyTracks(Action):
    id = "delete_empty_tracks"
    label = "Delete empty tracks"
    title = "Empty tracks"
    short = "EMPT"
    icon = "delete-empty-tracks"
    hint = "Removes unlocked video and audio tracks that hold no clips."
    group = "Timeline"
    feasibility = (
        "QE sequence removeVideoTrack(index) / removeAudioTrack(index) (undocumented, present in 25.x and 26.x; "
        "removeEmptyVideoTracks / removeEmptyAudioTracks named in the first spec could not be confirmed and would "
        "also remove locked tracks); Track.clips.numItems (documented); Track.isLocked() (undocumented). One "
        "track of each kind always stays."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        result = self.eval(ctx)
        video = count(result, "video")
        audio = count(result, "audio")
        if video == 0 and audio == 0:
            return self.done("No empty tracks", "neutral")
        parts = []
        if video:
            parts.append(plural(video, "video track"))
        if audio:
            parts.append(plural(audio, "audio track"))
        return self.done("Deleted " + " and ".join(parts))
