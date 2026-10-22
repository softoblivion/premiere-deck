// Helpers shared by the Quick actions scripts. The module prepends this file to every action script
// (modules/quick_actions/action.py, script_text). ES3 only: var, function expressions, no Array.indexOf.

var DECK_TPS = 254016000000;

// Throws an error whose sentence the host shows to the user ("<Label> failed: <sentence>").
var deckFail = function (sentence) {
    throw new Error("deck: " + sentence);
};

var deckTicks = function (time) {
    return parseFloat(time.ticks);
};

var deckSecs = function (ticks) {
    return ticks / DECK_TPS;
};

var deckTime = function (ticks) {
    var t = new Time();
    t.ticks = String(Math.round(ticks));
    return t;
};

var deckActiveSequence = function () {
    var seq = app.project.activeSequence;
    if (!seq) {
        deckFail("No active sequence.");
    }
    return seq;
};

var deckSequenceById = function (id) {
    if (id === undefined || id === null || id === "") {
        return null;
    }
    var seqs = app.project.sequences;
    for (var i = 0; i < seqs.numSequences; i++) {
        if (String(seqs[i].sequenceID) === String(id)) {
            return seqs[i];
        }
    }
    return null;
};

// Ticks per frame of a sequence (sequence.timebase is a string of ticks).
var deckFrameTicks = function (seq) {
    var tb = parseFloat(seq.timebase);
    return (isFinite(tb) && tb > 0) ? tb : DECK_TPS / 30;
};

// Track.isLocked() is not in the documented DOM; a track without it counts as unlocked.
var deckIsLocked = function (track) {
    try {
        if (track.isLocked) {
            return !!track.isLocked();
        }
    } catch (e) {}
    return false;
};

// Every track of a sequence: [{kind, index, track, locked}], video first.
var deckTracks = function (seq) {
    var out = [];
    var i;
    for (i = 0; i < seq.videoTracks.numTracks; i++) {
        out.push({kind: "video", index: i, track: seq.videoTracks[i], locked: deckIsLocked(seq.videoTracks[i])});
    }
    for (i = 0; i < seq.audioTracks.numTracks; i++) {
        out.push({kind: "audio", index: i, track: seq.audioTracks[i], locked: deckIsLocked(seq.audioTracks[i])});
    }
    return out;
};

var deckSource = function (clip) {
    try {
        return clip.projectItem || null;
    } catch (e) {
        return null;
    }
};

var deckQeSequence = function () {
    app.enableQE();
    var qeSeq = qe.project.getActiveSequence();
    if (!qeSeq) {
        deckFail("No active sequence.");
    }
    return qeSeq;
};

var deckQeTrack = function (qeSeq, kind, index) {
    return kind === "video" ? qeSeq.getVideoTrackAt(index) : qeSeq.getAudioTrackAt(index);
};

// QE track items include gaps ("Empty"), so a DOM clip is matched by its start.
var deckFindQeClip = function (qeTrack, domClip) {
    var wanted = deckTicks(domClip.start);
    for (var i = 0; i < qeTrack.numItems; i++) {
        var candidate = null;
        try {
            candidate = qeTrack.getItemAt(i);
        } catch (e) {}
        if (!candidate || String(candidate.type) !== "Clip") {
            continue;
        }
        try {
            if (Math.abs(parseFloat(candidate.start.ticks) - wanted) < 1) {
                return candidate;
            }
        } catch (e2) {}
    }
    return null;
};

var deckPad = function (n) {
    return n < 10 ? "0" + n : String(n);
};

// Premiere's own timecode string of a tick position in the sequence's display format. QE razor and the QE frame
// exporter parse a timecode string; a tick count or a non-drop string on a drop-frame sequence lands elsewhere.
var deckTimecode = function (seq, ticks) {
    var tb = deckFrameTicks(seq);
    var frame = Math.floor(ticks / tb + 0.000001);
    if (!(frame >= 0)) {
        frame = 0;
    }
    var displayFormat = 100;
    try {
        displayFormat = seq.getSettings().videoDisplayFormat;
    } catch (e) {}
    try {
        var rate = new Time();
        rate.ticks = String(tb);
        var at = new Time();
        at.ticks = String(frame * tb);
        if (at.getFormatted) {
            return at.getFormatted(rate, displayFormat);
        }
    } catch (e2) {}
    var nominal = Math.round(DECK_TPS / tb);
    return deckPad(Math.floor(frame / (nominal * 3600))) + ":" + deckPad(Math.floor(frame / (nominal * 60)) % 60) + ":" +
        deckPad(Math.floor(frame / nominal) % 60) + ":" + deckPad(frame % nominal);
};

// Track kind and index of a DOM track item, found by its nodeId.
var deckLocate = function (seq, item) {
    var tracks = deckTracks(seq);
    for (var t = 0; t < tracks.length; t++) {
        var clips = tracks[t].track.clips;
        for (var c = 0; c < clips.numItems; c++) {
            if (String(clips[c].nodeId) === String(item.nodeId)) {
                return tracks[t];
            }
        }
    }
    return null;
};

// A top-level bin by name, created when missing.
var deckTopBin = function (name) {
    var root = app.project.rootItem;
    for (var i = 0; i < root.children.numItems; i++) {
        var child = root.children[i];
        if (child && child.type === ProjectItemType.BIN && child.name === name) {
            return child;
        }
    }
    var bin = root.createBin(name);
    if (!bin) {
        deckFail("Premiere did not create the " + name + " bin.");
    }
    return bin;
};

// The next free path "<dir>\<base>.<ext>", "<dir>\<base>_2.<ext>", ...
var deckFreePath = function (dir, base, ext) {
    var path = dir + "\\" + base + "." + ext;
    var n = 2;
    while (new File(path).exists) {
        path = dir + "\\" + base + "_" + n + "." + ext;
        n++;
    }
    return path;
};
