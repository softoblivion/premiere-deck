// @uses lib
// add_marker: a sequence marker when target_id is a sequence id, else a clip marker on the project item.
// __args = {target_id, seconds, name, comment}.
var sequence = findSequence(__args.target_id);
var markers;
if (sequence) {
    markers = sequence.markers;
} else {
    markers = requireItem(__args.target_id).getMarkers();
}
var marker = markers.createMarker(Number(__args.seconds));
marker.name = String(__args.name || "");
marker.comments = String(__args.comment || "");
return {guid: String(marker.guid), on: sequence ? "sequence" : "item"};
