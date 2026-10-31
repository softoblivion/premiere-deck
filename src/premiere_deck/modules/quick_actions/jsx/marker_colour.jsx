// Marker colour: gives the markers at the playhead one of the 8 marker colours (Marker.setColorByIndex, 0..7,
// docs/PREMIERE_API.md 7.16): the sequence markers and the item markers of the selected clips under the playhead.
// A marker counts when its start is within half a frame of the playhead, or, for a range marker, when the playhead
// is inside its range.
// args: {colour (0..7)}
// returns: {markers, sequence (name of the active sequence or "")}
var seq = app.project.activeSequence;
var result = {markers: 0, sequence: seq ? seq.name : ""};
if (!seq) {
    return result;
}

var atPlayhead = function (marker, ticks, frame) {
    var start = deckTicks(marker.start);
    if (Math.abs(start - ticks) <= frame / 2) {
        return true;
    }
    var end = start;
    try {
        end = deckTicks(marker.end);
    } catch (eEnd) {
        end = start;
    }
    return end > start && ticks >= start && ticks < end;
};

var colourMarkersAt = function (markers, ticks, frame) {
    var hits = 0;
    if (!markers) {
        return hits;
    }
    var marker = markers.getFirstMarker();
    while (marker) {
        if (atPlayhead(marker, ticks, frame)) {
            marker.setColorByIndex(__args.colour);
            hits++;
        }
        marker = markers.getNextMarker(marker);
    }
    return hits;
};

var frame = deckFrameTicks(seq);
var play = deckTicks(seq.getPlayerPosition());
var selection = seq.getSelection() || [];
var markedSources = {};
for (var i = 0; i < selection.length; i++) {
    var clip = selection[i];
    var source = deckSource(clip);
    var inside = play >= deckTicks(clip.start) && play < deckTicks(clip.end);
    if (source && inside && !markedSources[String(source.nodeId)]) {
        markedSources[String(source.nodeId)] = true;
        var local = play - deckTicks(clip.start) + deckTicks(clip.inPoint);
        try {
            result.markers += colourMarkersAt(source.getMarkers(), local, frame);
        } catch (eClipMarkers) {}
    }
}
result.markers += colourMarkersAt(seq.markers, play, frame);
return result;
