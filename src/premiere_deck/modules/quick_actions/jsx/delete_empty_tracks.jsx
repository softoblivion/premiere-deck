// Removes the empty, unlocked video and audio tracks of the active sequence (QE removeVideoTrack /
// removeAudioTrack, highest index first). One track of each kind always stays.
// returns: {video, audio}
var seq = deckActiveSequence();
var qeSeq = deckQeSequence();

var sweep = function (kind) {
    var list = kind === "video" ? seq.videoTracks : seq.audioTracks;
    var removed = 0;
    for (var i = list.numTracks - 1; i >= 0; i--) {
        list = kind === "video" ? seq.videoTracks : seq.audioTracks;
        if (list.numTracks <= 1) {
            break;
        }
        var track = list[i];
        if (track.clips.numItems > 0 || deckIsLocked(track)) {
            continue;
        }
        var before = list.numTracks;
        if (kind === "video") {
            qeSeq.removeVideoTrack(i);
        } else {
            qeSeq.removeAudioTrack(i);
        }
        var after = (kind === "video" ? seq.videoTracks : seq.audioTracks).numTracks;
        if (after !== before - 1) {
            deckFail("Premiere did not remove the empty " + kind + " track " + (i + 1) + ".");
        }
        removed++;
    }
    return removed;
};

if (!qeSeq.removeVideoTrack || !qeSeq.removeAudioTrack) {
    deckFail("This Premiere version cannot remove tracks by script.");
}
var video = sweep("video");
var audio = sweep("audio");
return {video: video, audio: audio};
