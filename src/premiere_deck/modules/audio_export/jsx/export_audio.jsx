// One audio file of the quick export (AUDIO_EXPORT.md 3): saves the mute state of every audio track of the
// sequence, mutes every track that is not in `audible` and unmutes those that are, renders the sequence with an
// audio-only preset to `temp_path` (Sequence.exportAsMediaDirect, synchronous), and restores every mute state in
// `finally`, also when the export throws. The host renames the file to its final name.
// args: {sequence_id, audible: [track indexes], preset, temp_path, work_area (0 entire, 1 in to out)}
// returns: {written, bytes, restored, returned}
var sequence = null;
var all = app.project.sequences;
for (var s = 0; s < all.numSequences; s++) {
    if (String(all[s].sequenceID) === String(__args.sequence_id)) {
        sequence = all[s];
        break;
    }
}
if (!sequence) {
    throw new Error("deck: The sequence is no longer in the project.");
}

var tracks = sequence.audioTracks;
var audible = {};
for (var a = 0; a < __args.audible.length; a++) {
    audible[String(__args.audible[a])] = true;
}
var saved = [];
for (var t = 0; t < tracks.numTracks; t++) {
    var muted = false;
    try {
        muted = !!tracks[t].isMuted();
    } catch (eRead) {
        muted = false;
    }
    saved.push(muted);
}

var temp = new File(__args.temp_path);
if (temp.exists) {
    temp.remove();
}
var areaType = __args.work_area;
try {
    var encoder = app.encoder;
    if (encoder) {
        if (areaType === 1 && encoder.ENCODE_IN_TO_OUT !== undefined) {
            areaType = encoder.ENCODE_IN_TO_OUT;
        } else if (areaType === 0 && encoder.ENCODE_ENTIRE !== undefined) {
            areaType = encoder.ENCODE_ENTIRE;
        }
    }
} catch (eEncoder) {}

var returned = null;
try {
    for (var m = 0; m < tracks.numTracks; m++) {
        tracks[m].setMute(audible[String(m)] ? 0 : 1);
    }
    returned = sequence.exportAsMediaDirect(temp.fsName, __args.preset, areaType);
} finally {
    for (var r = 0; r < saved.length; r++) {
        try {
            tracks[r].setMute(saved[r] ? 1 : 0);
        } catch (eRestore) {}
    }
}

var restored = true;
for (var c = 0; c < saved.length; c++) {
    try {
        if (!!tracks[c].isMuted() !== saved[c]) {
            restored = false;
        }
    } catch (eCheck) {
        restored = false;
    }
}
temp = new File(__args.temp_path);
return {written: temp.exists && temp.length > 0, bytes: temp.exists ? temp.length : 0, restored: restored,
        returned: returned === undefined || returned === null ? null : String(returned)};
