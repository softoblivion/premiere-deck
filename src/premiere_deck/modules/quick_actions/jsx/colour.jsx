// Label colour, script part. 26.5.2 has no colour setter on a DOM or QE track item and placed clips do not follow
// their project item's label (docs/PREMIERE_API.md 7.16), so selected timeline clips are coloured by the host
// through Edit > Label (modules/quick_actions/premiere_window.py); this script picks the target and colours the
// items selected in the Project panel, bins included. The target is the selection the host names as the newest
// ("clips" or "items"); when that one is empty, the other one; nothing selected colours nothing.
// args: {label (0..15), target ("clips" | "items"), mode ("select" | "clip_items": the fallback that labels the
//        selected clips' project items | "count": nothing changes)}
// returns: {target ("clips" | "items" | ""), clips, items, bins, sequence (name of the active sequence or "")}
var seq = app.project.activeSequence;
var selection = seq ? seq.getSelection() : [];
var clipCount = selection ? selection.length : 0;
var picked = [];
try {
    picked = app.getCurrentProjectViewSelection() || [];
} catch (eView) {
    picked = [];
}
var result = {target: "", clips: clipCount, items: 0, bins: 0, sequence: seq ? seq.name : ""};
for (var c = 0; c < picked.length; c++) {
    if (!picked[c]) {
        continue;
    }
    if (picked[c].type === ProjectItemType.BIN) {
        result.bins++;
    } else {
        result.items++;
    }
}
var hasItems = result.items + result.bins > 0;
if (__args.target === "items") {
    result.target = hasItems ? "items" : (clipCount ? "clips" : "");
} else {
    result.target = clipCount ? "clips" : (hasItems ? "items" : "");
}
if (__args.mode === "count") {
    return result;
}

var coloured = {};
var colourItem = function (item) {
    if (!item || coloured[String(item.nodeId)]) {
        return;
    }
    item.setColorLabel(__args.label);
    coloured[String(item.nodeId)] = true;
};

if (__args.mode === "clip_items") {
    for (var s = 0; s < clipCount; s++) {
        colourItem(deckSource(selection[s]));
    }
    return result;
}
if (result.target === "items") {
    for (var p = 0; p < picked.length; p++) {
        colourItem(picked[p]);
    }
}
return result;
