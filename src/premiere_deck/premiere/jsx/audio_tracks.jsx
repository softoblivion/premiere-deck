// audio_tracks: the audio tracks of a sequence in track order. __args = {sequence_id}.
// returns: {found, tracks: [{index, name, muted, clip_count}]}
var sequence = null;
var all = app.project.sequences;
for (var s = 0; s < all.numSequences; s++) {
    if (String(all[s].sequenceID) === String(__args.sequence_id)) {
        sequence = all[s];
        break;
    }
}
if (!sequence) {
    return {found: false, tracks: []};
}
var tracks = [];
for (var i = 0; i < sequence.audioTracks.numTracks; i++) {
    var track = sequence.audioTracks[i];
    var muted = false;
    try {
        muted = !!track.isMuted();
    } catch (eMuted) {
        muted = false;
    }
    tracks.push({index: i, name: String(track.name || ""), muted: muted, clip_count: track.clips.numItems});
}
return {found: true, tracks: tracks};
