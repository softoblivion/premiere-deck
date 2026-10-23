// Brings the active sequence forward in the Timeline (Project.openSequence, documented) before the host runs
// File > Export > Media... through the menu bar (modules/quick_actions/premiere_window.py), so Export mode opens
// for that sequence.
// returns: {name, sequence_id}
var seq = deckActiveSequence();
app.project.openSequence(seq.sequenceID);
return {name: seq.name, sequence_id: String(seq.sequenceID)};
