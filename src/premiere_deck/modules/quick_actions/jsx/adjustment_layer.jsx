// Places an adjustment layer over [start, end) on the first free unlocked video track at or above min_track.
// The layer item is an adjustment layer already in the project (the top-level bin `bin` first); only where QE
// offers newAdjustmentLayer is one created there under `name`.
// args: {start, end (seconds), min_track (int), bin (string), name (string)}
// returns: {track_index, start, end (seconds), created (bool), name}
var seq = deckActiveSequence();
var frame = deckFrameTicks(seq);
var startT = Math.round(__args.start * DECK_TPS / frame) * frame;
var endT = Math.round(__args.end * DECK_TPS / frame) * frame;
if (endT - startT < frame) {
    endT = startT + frame;
}

var childIds = function (bin) {
    var ids = {};
    if (!bin) {
        return ids;
    }
    for (var i = 0; i < bin.children.numItems; i++) {
        if (bin.children[i]) {
            ids[String(bin.children[i].nodeId)] = true;
        }
    }
    return ids;
};

var newChild = function (bin, before) {
    if (!bin) {
        return null;
    }
    for (var i = bin.children.numItems - 1; i >= 0; i--) {
        var child = bin.children[i];
        if (child && !before[String(child.nodeId)]) {
            return child;
        }
    }
    return null;
};

var isLayer = function (item) {
    try {
        return !!item && item.type === ProjectItemType.CLIP && !!item.isAdjustmentLayer && item.isAdjustmentLayer();
    } catch (e) {
        return false;
    }
};

// An adjustment layer already in the project: the Deck bin first, then anywhere (depth-first).
var findLayer = function (bin) {
    var nested = [];
    for (var i = 0; i < bin.children.numItems; i++) {
        var child = bin.children[i];
        if (isLayer(child)) {
            return child;
        }
        if (child && child.type === ProjectItemType.BIN) {
            nested.push(child);
        }
    }
    for (var n = 0; n < nested.length; n++) {
        var hit = findLayer(nested[n]);
        if (hit) {
            return hit;
        }
    }
    return null;
};

var layer = null;
var created = false;
var root = app.project.rootItem;
for (var c = 0; c < root.children.numItems; c++) {
    var top = root.children[c];
    if (top && top.type === ProjectItemType.BIN && top.name === __args.bin) {
        layer = findLayer(top);
    }
}
if (!layer) {
    layer = findLayer(root);
}

// Premiere 26.5 has no scripting call that creates an adjustment layer (qe.project.newAdjustmentLayer existed in
// some earlier builds only); the user creates one once and Deck reuses it.
if (!layer) {
    app.enableQE();
    if (!qe.project.newAdjustmentLayer) {
        deckFail("No adjustment layer in the project. Create one once with File > New > Adjustment Layer; Deck reuses it.");
    }
    var deckBin = deckTopBin(__args.bin);
    var insertion = null;
    try {
        insertion = app.project.getInsertionBin();
    } catch (eBin) {}
    var beforeRoot = childIds(root);
    var beforeInsertion = childIds(insertion);
    qe.project.newAdjustmentLayer();
    layer = newChild(root, beforeRoot) || newChild(insertion, beforeInsertion);
    if (!layer) {
        deckFail("Premiere did not create the adjustment layer.");
    }
    layer.name = __args.name;
    layer.moveBin(deckBin);
    created = true;
}

// The layer is placed at its own duration first and trimmed after, so the whole placed span must be free.
var layerTicks = 0;
try {
    layerTicks = deckTicks(layer.getOutPoint()) - deckTicks(layer.getInPoint());
} catch (eDur) {}
if (!(layerTicks > 0)) {
    layerTicks = 10 * DECK_TPS;
}
var spanEnd = Math.max(endT, startT + layerTicks);

var rangeFree = function (track, from, to) {
    for (var i = 0; i < track.clips.numItems; i++) {
        var clip = track.clips[i];
        if (deckTicks(clip.start) < to && deckTicks(clip.end) > from) {
            return false;
        }
    }
    return true;
};

var trackIndex = -1;
for (var t = Math.max(0, __args.min_track); t < seq.videoTracks.numTracks; t++) {
    if (!deckIsLocked(seq.videoTracks[t]) && rangeFree(seq.videoTracks[t], startT, spanEnd)) {
        trackIndex = t;
        break;
    }
}
if (trackIndex < 0) {
    // QE addTracks(videoCount, videoIndex, audioCount, audioType, audioIndex, submixCount, submixType)
    var qeSeq = deckQeSequence();
    var countBefore = seq.videoTracks.numTracks;
    try {
        qeSeq.addTracks(1, countBefore, 0, 1, seq.audioTracks.numTracks, 0, 0);
    } catch (eAdd) {}
    if (seq.videoTracks.numTracks !== countBefore + 1) {
        deckFail("No free video track above the selection.");
    }
    trackIndex = countBefore;
}

var target = seq.videoTracks[trackIndex];
target.overwriteClip(layer, deckTime(startT));

var placed = null;
for (var p = 0; p < target.clips.numItems; p++) {
    var candidate = target.clips[p];
    var source = deckSource(candidate);
    if (source && String(source.nodeId) === String(layer.nodeId) && Math.abs(deckTicks(candidate.start) - startT) < frame / 2) {
        placed = candidate;
        break;
    }
}
if (!placed) {
    deckFail("Premiere did not place the adjustment layer.");
}
try {
    placed.end = deckTime(endT);
} catch (eEnd) {
    placed.end = String(endT);
}

return {
    track_index: trackIndex,
    start: deckSecs(deckTicks(placed.start)),
    end: deckSecs(deckTicks(placed.end)),
    created: created,
    name: layer.name
};
