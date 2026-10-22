// @uses lib
// insert_clip: inserts the range start..end (seconds of the source) of a project item into the active sequence at
// `at` (default: the playhead) on the given track indexes. The item's own in and out points are restored
// afterwards. __args = {node_id, start, end, at, video_track, audio_track}.
var item = requireItem(__args.node_id);
var sequence = requireSequence(null);
var ALL = 4;
var previousIn = item.getInPoint(ALL).seconds;
var previousOut = item.getOutPoint(ALL).seconds;
var columns = columnsOf(item);
var mediaDuration = columns["Column.Intrinsic.MediaDuration"] ? ticksToSeconds(columns["Column.Intrinsic.MediaDuration"]) : null;
var at = __args.at === null || __args.at === undefined ? sequence.getPlayerPosition().seconds : Number(__args.at);
item.setInPoint(Number(__args.start), ALL);
item.setOutPoint(Number(__args.end), ALL);
var error = null;
try {
    sequence.insertClip(item, secondsToTicks(at), Number(__args.video_track) || 0, Number(__args.audio_track) || 0);
} catch (e) {
    error = e;
}
// Restore the item's marks: "cleared" when they spanned the whole media before.
if (previousIn <= 0) {
    item.clearInPoint();
} else {
    item.setInPoint(previousIn, ALL);
}
if (mediaDuration !== null && Math.abs(previousOut - mediaDuration) < 0.001) {
    item.clearOutPoint();
} else {
    item.setOutPoint(previousOut, ALL);
}
if (error) {
    throw error;
}
return {at: at};
