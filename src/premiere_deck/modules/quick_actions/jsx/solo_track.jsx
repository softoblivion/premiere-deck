// Solo: mutes every audio track except keep_index and returns the mute states before. Restore: applies states.
// args: {mode ("solo" | "restore"), sequence_id, keep_index, states ([0|1, ...])}
// returns: {states, count} for solo, {restored} for restore
var seq = deckSequenceById(__args.sequence_id);
if (!seq) {
    if (__args.mode === "restore") {
        deckFail("The sequence is no longer in the project.");
    }
    seq = deckActiveSequence();
}
var tracks = seq.audioTracks;
var i;
if (__args.mode === "restore") {
    var states = __args.states || [];
    var restored = 0;
    for (i = 0; i < tracks.numTracks && i < states.length; i++) {
        tracks[i].setMute(states[i] ? 1 : 0);
        restored++;
    }
    return {restored: restored};
}
if (__args.keep_index < 0 || __args.keep_index >= tracks.numTracks) {
    deckFail("The audio track of the clip no longer exists.");
}
var before = [];
for (i = 0; i < tracks.numTracks; i++) {
    before.push(tracks[i].isMuted() ? 1 : 0);
}
for (i = 0; i < tracks.numTracks; i++) {
    tracks[i].setMute(i === __args.keep_index ? 0 : 1);
}
return {states: before, count: tracks.numTracks};
