// Shared helpers of the facade scripts (docs/PREMIERE_API.md 5). Prepended by PremiereApi to every script that
// declares the lib marker in its first three lines. ES3 only; verified on Premiere 26.5.2.

var TICKS_PER_SECOND = 254016000000;
var ITEM_CLIP = 1;
var ITEM_BIN = 2;
var ITEM_ROOT = 3;
var ITEM_FILE = 4;
// Premiere returns this tick value for an in or out point that is not set.
var UNSET_TICKS = -400000;

function ticksToSeconds(t) {
    return Number(t) / TICKS_PER_SECOND;
}

function secondsToTicks(s) {
    return String(Math.round(Number(s) * TICKS_PER_SECOND));
}

function timeFromSeconds(s) {
    var t = new Time();
    t.seconds = Number(s);
    return t;
}

// Calls fn(item, treePath) for every item below `root`, depth-first; treePath is the bin path of the item's parent.
function walk(root, fn, prefix) {
    var base = prefix || "";
    var children = root.children;
    if (!children) {
        return;
    }
    for (var i = 0; i < children.numItems; i++) {
        var child = children[i];
        if (!child) {
            continue;
        }
        fn(child, base);
        if (child.type === ITEM_BIN) {
            walk(child, fn, base ? base + "/" + child.name : child.name);
        }
    }
}

function findByNodeId(root, id) {
    var children = root.children;
    if (!children) {
        return null;
    }
    for (var i = 0; i < children.numItems; i++) {
        var child = children[i];
        if (!child) {
            continue;
        }
        if (String(child.nodeId) === String(id)) {
            return child;
        }
        if (child.type === ITEM_BIN) {
            var found = findByNodeId(child, id);
            if (found) {
                return found;
            }
        }
    }
    return null;
}

function requireItem(id) {
    var item = findByNodeId(app.project.rootItem, id);
    if (!item) {
        throw new Error("project item not found: " + id);
    }
    return item;
}

function splitTreePath(path) {
    var parts = String(path || "").replace(/\\/g, "/").split("/");
    var out = [];
    for (var i = 0; i < parts.length; i++) {
        if (parts[i].length) {
            out.push(parts[i]);
        }
    }
    return out;
}

function childBin(parent, name) {
    for (var i = 0; i < parent.children.numItems; i++) {
        var child = parent.children[i];
        if (child && child.type === ITEM_BIN && child.name === name) {
            return child;
        }
    }
    return null;
}

// The bin at a "/"-separated path from the root ("" = the root); missing bins are created when `create` is true,
// existing ones are reused by exact name. Returns null when missing and not created.
function binByPath(path, create) {
    var parts = splitTreePath(path);
    var current = app.project.rootItem;
    for (var i = 0; i < parts.length; i++) {
        var next = childBin(current, parts[i]);
        if (!next) {
            if (!create) {
                return null;
            }
            var made = current.createBin(parts[i]);
            next = made && made.type === ITEM_BIN ? made : childBin(current, parts[i]);
            if (!next) {
                throw new Error("could not create bin " + parts[i]);
            }
        }
        current = next;
    }
    return current;
}

// Bin path of an item's parent from its treePath ("\Project.prproj\Footage\Day1\clip.mp4" -> "Footage/Day1").
function pathOf(item) {
    var parts = String(item.treePath || "").split("\\");
    var out = [];
    for (var i = 2; i < parts.length - 1; i++) {
        out.push(parts[i]);
    }
    return out.join("/");
}

function safeMediaPath(item) {
    try {
        var path = item.getMediaPath();
        return path ? String(path) : null;
    } catch (e) {
        return null;
    }
}

// Project panel columns of an item as {ColumnID: value}; {} when Premiere offers none.
function columnsOf(item) {
    var map = {};
    try {
        var list = JSON.parse(item.getProjectColumnsMetadata());
        for (var i = 0; i < list.length; i++) {
            map[list[i].ColumnID] = list[i].ColumnValue;
        }
    } catch (e) {
        return map;
    }
    return map;
}

function itemKind(item) {
    if (item.type === ITEM_BIN || item.type === ITEM_ROOT) {
        return "bin";
    }
    if (item.type === ITEM_FILE) {
        return "file";
    }
    try {
        if (item.isSequence()) {
            return "sequence";
        }
    } catch (e) {
        return "clip";
    }
    return "clip";
}

// The plain object of model.ProjectItem.
function itemInfo(item, treePath) {
    var kind = itemKind(item);
    var info = {
        node_id: String(item.nodeId),
        name: String(item.name),
        kind: kind,
        tree_path: treePath === undefined ? pathOf(item) : treePath,
        media_path: null,
        is_offline: false,
        is_sequence: kind === "sequence",
        duration: null,
        has_video: null,
        has_audio: null,
        color_label: null
    };
    try {
        info.color_label = Number(item.getColorLabel());
    } catch (e) {
        info.color_label = null;
    }
    if (kind === "bin") {
        return info;
    }
    if (kind !== "sequence") {
        info.media_path = safeMediaPath(item);
    }
    try {
        info.is_offline = !!item.isOffline();
    } catch (e) {
        info.is_offline = false;
    }
    var columns = columnsOf(item);
    var duration = columns["Column.Intrinsic.MediaDuration"];
    if (duration !== undefined && duration !== "" && !isNaN(Number(duration))) {
        info.duration = ticksToSeconds(duration);
    }
    if (columns.hasOwnProperty("Column.Intrinsic.VideoInfo") || columns.hasOwnProperty("Column.Intrinsic.AudioInfo")) {
        info.has_video = !!columns["Column.Intrinsic.VideoInfo"];
        info.has_audio = !!columns["Column.Intrinsic.AudioInfo"];
    }
    return info;
}

function findSequence(id) {
    var sequences = app.project.sequences;
    for (var i = 0; i < sequences.numSequences; i++) {
        if (String(sequences[i].sequenceID) === String(id)) {
            return sequences[i];
        }
    }
    return null;
}

function requireSequence(id) {
    var sequence = id ? findSequence(id) : app.project.activeSequence;
    if (!sequence) {
        throw new Error(id ? "sequence not found: " + id : "no active sequence");
    }
    return sequence;
}

function pointOrNull(ticks) {
    var value = Number(ticks);
    return isNaN(value) || value <= UNSET_TICKS || value < 0 ? null : ticksToSeconds(value);
}

// The plain object of model.SequenceInfo.
function sequenceInfo(sequence) {
    var fps = 0;
    var timebase = Number(sequence.timebase);
    if (timebase > 0) {
        fps = TICKS_PER_SECOND / timebase;
    }
    return {
        sequence_id: String(sequence.sequenceID),
        name: String(sequence.name),
        width: Number(sequence.frameSizeHorizontal),
        height: Number(sequence.frameSizeVertical),
        fps: fps,
        duration: ticksToSeconds(sequence.end),
        in_point: pointOrNull(sequence.getInPointAsTime().ticks),
        out_point: pointOrNull(sequence.getOutPointAsTime().ticks)
    };
}

// The plain object of model.TrackItem.
function trackItemInfo(sequence, trackKind, trackIndex, index, clip) {
    var nodeId = null;
    try {
        nodeId = clip.projectItem ? String(clip.projectItem.nodeId) : null;
    } catch (e) {
        nodeId = null;
    }
    var selected = false;
    try {
        selected = !!clip.isSelected();
    } catch (e) {
        selected = false;
    }
    return {
        sequence_id: String(sequence.sequenceID),
        track_kind: trackKind,
        track_index: trackIndex,
        index: index,
        name: String(clip.name),
        node_id: nodeId,
        start: clip.start.seconds,
        end: clip.end.seconds,
        in_point: clip.inPoint.seconds,
        out_point: clip.outPoint.seconds,
        selected: selected
    };
}

// Calls fn(trackKind, trackIndex, index, clip) for every clip of every track.
function eachTrackItem(sequence, fn) {
    var kinds = [["video", sequence.videoTracks], ["audio", sequence.audioTracks]];
    for (var k = 0; k < kinds.length; k++) {
        var tracks = kinds[k][1];
        for (var t = 0; t < tracks.numTracks; t++) {
            var clips = tracks[t].clips;
            for (var c = 0; c < clips.numItems; c++) {
                fn(kinds[k][0], t, c, clips[c]);
            }
        }
    }
}
