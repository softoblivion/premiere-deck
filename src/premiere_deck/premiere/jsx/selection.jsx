// @uses lib
// selection: the selected track items of the active sequence ({sequence_id: "", items: []} without one).
var sequence = app.project.activeSequence;
if (!sequence) {
    return {sequence_id: "", items: []};
}
var items = [];
eachTrackItem(sequence, function (kind, trackIndex, index, clip) {
    var selected = false;
    try {
        selected = clip.isSelected();
    } catch (e) {
        selected = false;
    }
    if (selected) {
        items.push(trackItemInfo(sequence, kind, trackIndex, index, clip));
    }
});
return {sequence_id: String(sequence.sequenceID), items: items};
