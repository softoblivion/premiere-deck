// Duplicates a sequence (Sequence.clone, documented) and renames the copy; the original stays the active sequence.
// The copy is found by comparing the project's sequence ids before and after the call.
// args: {sequence_id, name}
// returns: {name, sequence_id}
var seq = deckSequenceById(__args.sequence_id) || deckActiveSequence();
if (!seq.clone) {
    deckFail("This Premiere version cannot duplicate a sequence from a script.");
}
var before = {};
var seqs = app.project.sequences;
var i;
for (i = 0; i < seqs.numSequences; i++) {
    before[String(seqs[i].sequenceID)] = true;
}
seq.clone();
var copy = null;
seqs = app.project.sequences;
for (i = 0; i < seqs.numSequences; i++) {
    if (!before[String(seqs[i].sequenceID)]) {
        copy = seqs[i];
        break;
    }
}
if (!copy) {
    deckFail("Premiere did not create the copy.");
}
copy.name = __args.name;
// Some Premiere versions open the copy; the original is made the active sequence again.
try {
    var active = app.project.activeSequence;
    if (!active || String(active.sequenceID) !== String(seq.sequenceID)) {
        app.project.openSequence(seq.sequenceID);
    }
} catch (eOpen) {}
return {name: String(copy.name), sequence_id: String(copy.sequenceID)};
