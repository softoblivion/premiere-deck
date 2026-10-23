from __future__ import annotations

from premiere_deck.modules.quick_actions.actions.remove_video_track import RemoveTrack


class RemoveAudioTrack(RemoveTrack):
    id = "remove_audio_track"
    label = "Remove audio track"
    title = "Remove A track"
    short = "−A"
    icon = "track-audio-remove"
    hint = "Removes the lowest audio track when it is empty and unlocked."
    kind = "audio"
    feasibility = (
        "QE sequence removeAudioTrack(index) (undocumented, present in 25.x and 26.x); Track.clips.numItems "
        "(documented) and Track.isLocked() (undocumented) decide whether it may go; one audio track always stays."
    )
