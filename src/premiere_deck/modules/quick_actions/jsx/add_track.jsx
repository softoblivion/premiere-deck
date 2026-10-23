// Adds one track to the active sequence: a video track above the highest, or an audio track below the lowest
// (QE addTracks(videoCount, videoIndex, audioCount, audioType, audioIndex, submixCount, submixType)).
// args: {kind ("video" | "audio"), audio_type (QE audio track type of a new audio track)}
// returns: {kind, index (0-based index of the new track), count (tracks of that kind afterwards)}
var seq = deckActiveSequence();
var qeSeq = deckQeSequence();
if (!qeSeq.addTracks) {
    deckFail("This Premiere version cannot add tracks by script.");
}
var video = __args.kind === "video";
var countOf = function () {
    return video ? seq.videoTracks.numTracks : seq.audioTracks.numTracks;
};
var before = countOf();
if (video) {
    qeSeq.addTracks(1, seq.videoTracks.numTracks, 0, 1, seq.audioTracks.numTracks, 0, 0);
} else {
    qeSeq.addTracks(0, seq.videoTracks.numTracks, 1, __args.audio_type, seq.audioTracks.numTracks, 0, 0);
}
var after = countOf();
if (after !== before + 1) {
    deckFail("Premiere did not add the " + __args.kind + " track.");
}
return {kind: __args.kind, index: after - 1, count: after};
