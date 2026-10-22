// @uses lib
// track_items: every clip on every track of a sequence. __args = {sequence_id}.
var sequence = requireSequence(__args.sequence_id);
var items = [];
eachTrackItem(sequence, function (kind, trackIndex, index, clip) {
    items.push(trackItemInfo(sequence, kind, trackIndex, index, clip));
});
return items;
