// Turns on "Set to Frame Size" for the selected video clips. Per clip through the QE track item when this
// Premiere offers it; otherwise on the clip's project item (ProjectItem.setScaleToFrameSize, documented), which
// Premiere applies to that item's clips.
// returns: {clips, method ("clip" | "item" | "mixed")}
var seq = deckActiveSequence();
var selection = seq.getSelection();
var qeSeq = null;
try {
    qeSeq = deckQeSequence();
} catch (eQe) {}

var clipsDone = 0;
var byClip = 0;
var byItem = 0;
var itemsDone = {};
for (var i = 0; i < selection.length; i++) {
    var item = selection[i];
    if (String(item.mediaType) !== "Video") {
        continue;
    }
    var done = false;
    if (qeSeq) {
        var where = deckLocate(seq, item);
        if (where) {
            var qeClip = deckFindQeClip(deckQeTrack(qeSeq, where.kind, where.index), item);
            if (qeClip && qeClip.setScaleToFrameSize) {
                // 26.5.2: the QE call needs the flag ("Not Enough Parameters" without it)
                qeClip.setScaleToFrameSize(true);
                done = true;
                byClip++;
            }
        }
    }
    if (!done) {
        var source = deckSource(item);
        if (!source) {
            continue;
        }
        if (!itemsDone[String(source.nodeId)]) {
            source.setScaleToFrameSize();
            itemsDone[String(source.nodeId)] = true;
        }
        byItem++;
    }
    clipsDone++;
}
if (clipsDone === 0) {
    deckFail("Select a video clip.");
}
return {clips: clipsDone, method: byItem === 0 ? "clip" : (byClip === 0 ? "item" : "mixed")};
