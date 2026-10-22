// Lock: locks every track not listed in `keep` and returns the lock states before. Restore: applies states.
// Track.isLocked / Track.setLocked are not in the documented DOM; a Premiere without them fails clearly.
// args: {mode ("lock" | "restore"), sequence_id, keep ([{kind, index}]), states ({video: [0|1], audio: [0|1]})}
// returns: {states, locked} for lock, {restored} for restore
var seq = deckSequenceById(__args.sequence_id);
if (!seq) {
    if (__args.mode === "restore") {
        deckFail("The sequence is no longer in the project.");
    }
    seq = deckActiveSequence();
}
if (!seq.videoTracks[0] || !seq.videoTracks[0].setLocked) {
    deckFail("This Premiere version cannot lock tracks by script.");
}
var lists = {video: seq.videoTracks, audio: seq.audioTracks};
var kinds = ["video", "audio"];
var k, i, list;

if (__args.mode === "restore") {
    var restored = 0;
    var states = __args.states || {};
    for (k = 0; k < kinds.length; k++) {
        list = lists[kinds[k]];
        var wanted = states[kinds[k]] || [];
        for (i = 0; i < list.numTracks && i < wanted.length; i++) {
            list[i].setLocked(wanted[i] ? 1 : 0);
            restored++;
        }
    }
    return {restored: restored};
}

var keep = {};
var entries = __args.keep || [];
for (i = 0; i < entries.length; i++) {
    keep[entries[i].kind + ":" + entries[i].index] = true;
}
var before = {video: [], audio: []};
var locked = 0;
for (k = 0; k < kinds.length; k++) {
    list = lists[kinds[k]];
    for (i = 0; i < list.numTracks; i++) {
        var wasLocked = deckIsLocked(list[i]);
        before[kinds[k]].push(wasLocked ? 1 : 0);
        if (!keep[kinds[k] + ":" + i] && !wasLocked) {
            list[i].setLocked(1);
            locked++;
        }
    }
}
return {states: before, locked: locked};
