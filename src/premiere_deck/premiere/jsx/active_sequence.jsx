// @uses lib
// active_sequence: the active sequence or null.
var sequence = app.project.activeSequence;
return sequence ? sequenceInfo(sequence) : null;
