from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, require_sequence
from premiere_deck.modules.quick_actions.results import count

# QE audio track type of a new audio track (QE addTracks argument 4); 1 = stereo, verified on 26.5.2
# (docs/QUICK_ACTIONS.md 1).
AUDIO_TYPE = 1


def track_name(kind: str, index: int) -> str:
    """Premiere's track header name: V1, V2 ... / A1, A2 ... from a 0-based index."""
    return f"{'V' if kind == 'video' else 'A'}{index + 1}"


class AddTrack(Action):
    """Adds one track of `kind`: video above the highest, audio below the lowest (script `add_track`)."""

    kind = "video"
    group = "Tracks"

    def run(self, ctx, module):
        require_sequence(ctx)
        result = self.eval(ctx, {"kind": self.kind, "audio_type": AUDIO_TYPE}, script="add_track")
        return self.done(f"Added {track_name(self.kind, count(result, 'index'))}")


class AddVideoTrack(AddTrack):
    id = "add_video_track"
    label = "Add video track"
    title = "Add V track"
    short = "+V"
    icon = "track-video-add"
    hint = "Adds one video track above the highest."
    kind = "video"
    feasibility = (
        "QE sequence addTracks(videoCount, videoIndex, audioCount, audioType, audioIndex, submixCount, submixType) "
        "(undocumented, present in 25.x and 26.x; the adjustment layer uses the same call); the new count is read "
        "back from Sequence.videoTracks.numTracks (documented)."
    )
