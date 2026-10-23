// Removes the highest video track or the lowest audio track of the active sequence when it is empty and unlocked
// (QE removeVideoTrack / removeAudioTrack). One track of each kind always stays.
// args: {kind ("video" | "audio")}
// returns: {kind, index (0-based index of the last track), removed (bool), reason ("" | "not_empty" | "locked" | "last")}
var seq = deckActiveSequence();
var qeSeq = deckQeSequence();
var video = __args.kind === "video";
var listOf = function () {
    return video ? seq.videoTracks : seq.audioTracks;
};
var list = listOf();
var index = list.numTracks - 1;
var answer = function (removed, reason) {
    return {kind: __args.kind, index: index, removed: removed, reason: reason};
};
if (list.numTracks <= 1) {
    return answer(false, "last");
}
var track = list[index];
if (track.clips.numItems > 0) {
    return answer(false, "not_empty");
}
if (deckIsLocked(track)) {
    return answer(false, "locked");
}
if (video) {
    if (!qeSeq.removeVideoTrack) {
        deckFail("This Premiere version cannot remove tracks by script.");
    }
    qeSeq.removeVideoTrack(index);
} else {
    if (!qeSeq.removeAudioTrack) {
        deckFail("This Premiere version cannot remove tracks by script.");
    }
    qeSeq.removeAudioTrack(index);
}
if (listOf().numTracks !== index) {
    deckFail("Premiere did not remove the " + __args.kind + " track.");
}
return answer(true, "");
