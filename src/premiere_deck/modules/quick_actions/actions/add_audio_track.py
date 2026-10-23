from __future__ import annotations

from premiere_deck.modules.quick_actions.actions.add_video_track import AddTrack


class AddAudioTrack(AddTrack):
    id = "add_audio_track"
    label = "Add audio track"
    title = "Add A track"
    short = "+A"
    icon = "track-audio-add"
    hint = "Adds one stereo audio track below the lowest."
    kind = "audio"
    feasibility = (
        "QE sequence addTracks(0, videoIndex, 1, audioType, audioIndex, 0, 0) (undocumented, present in 25.x and "
        "26.x) with audio type 1 (stereo); the new count is read back from Sequence.audioTracks.numTracks "
        "(documented)."
    )
