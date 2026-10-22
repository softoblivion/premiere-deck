// @uses lib
// sequences: every sequence of the project.
var out = [];
var sequences = app.project.sequences;
for (var i = 0; i < sequences.numSequences; i++) {
    out.push(sequenceInfo(sequences[i]));
}
return out;
