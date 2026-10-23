// Label colour, script part. 26.5.2 has no colour setter on a DOM or QE track item and placed clips do not follow
// their project item's label (docs/PREMIERE_API.md 7.16), so selected timeline clips are coloured by the host
// through Edit > Label (modules/quick_actions/premiere_window.py); this script colours the markers at the playhead
// (Marker.setColorByIndex offers 8 colours) and, without a timeline selection, the items selected in the Project
// panel, bins included.
// args: {label (0..15), marker (0..7), mode ("select" | "clip_items": the fallback that labels the selected clips'
//        project items)}
// returns: {clips, items, bins, markers, sequence (name of the active sequence or "")}
var seq = app.project.activeSequence;
var result = {clips: 0, items: 0, bins: 0, markers: 0, sequence: seq ? seq.name : ""};
var coloured = {};

var colourItem = function (item) {
    if (!item || coloured[String(item.nodeId)]) {
        return;
    }
    item.setColorLabel(__args.label);
    coloured[String(item.nodeId)] = true;
};

// Colours every marker of a collection whose start is within half a frame of `ticks`.
var colourMarkersAt = function (markers, ticks, frame) {
    var hits = 0;
    if (!markers) {
        return hits;
    }
    var marker = markers.getFirstMarker();
    while (marker) {
        if (Math.abs(deckTicks(marker.start) - ticks) <= frame / 2) {
            marker.setColorByIndex(__args.marker);
            hits++;
        }
        marker = markers.getNextMarker(marker);
    }
    return hits;
};

var selection = seq ? seq.getSelection() : [];
result.clips = selection ? selection.length : 0;
if (__args.mode === "clip_items") {
    for (var s = 0; s < result.clips; s++) {
        colourItem(deckSource(selection[s]));
    }
    return result;
}
if (seq) {
    var frame = deckFrameTicks(seq);
    var play = deckTicks(seq.getPlayerPosition());
    var markedSources = {};
    for (var i = 0; i < result.clips; i++) {
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
}
if (!result.clips) {
    var picked = [];
    try {
        picked = app.getCurrentProjectViewSelection() || [];
    } catch (eView) {}
    for (var p = 0; p < picked.length; p++) {
        if (!picked[p]) {
            continue;
        }
        colourItem(picked[p]);
        if (picked[p].type === ProjectItemType.BIN) {
            result.bins++;
        } else {
            result.items++;
        }
    }
}
return result;
