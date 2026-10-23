// Reads the animated properties of the first selected clip (a video clip first) for the easing presets: every
// ComponentParam with isTimeVarying(), its key times (ticks, media time of the clip) and numeric values.
// ExtendScript has no keyframe selection and no bezier handle accessor (docs/PREMIERE_API.md 7.17).
// returns: {clip, kind, track, start (ticks), frame (ticks), at (playhead in the clip's media time, ticks),
//           inside (bool), props: [{c, p, name, keys: [ticks], values: [number | [numbers]]}], skipped: [names]}
var seq = deckActiveSequence();
var selection = seq.getSelection();
var clip = null;
var i;
for (i = 0; i < selection.length && !clip; i++) {
    if (selection[i].mediaType === "Video") {
        clip = selection[i];
    }
}
if (!clip && selection.length) {
    clip = selection[0];
}
if (!clip) {
    deckFail("Nothing selected.");
}
var where = deckLocate(seq, clip);
var play = deckTicks(seq.getPlayerPosition());
var start = deckTicks(clip.start);
var at = play - start + deckTicks(clip.inPoint);

var numeric = function (v) {
    if (typeof v === "number") {
        return isFinite(v);
    }
    if (v instanceof Array && v.length) {
        for (var n = 0; n < v.length; n++) {
            if (typeof v[n] !== "number" || !isFinite(v[n])) {
                return false;
            }
        }
        return true;
    }
    return false;
};

var props = [];
var skipped = [];
for (var c = 0; c < clip.components.numItems; c++) {
    var comp = clip.components[c];
    for (var p = 0; p < comp.properties.numItems; p++) {
        var prop = comp.properties[p];
        var animated = false;
        try {
            animated = prop.isTimeVarying();
        } catch (eTv) {}
        if (!animated) {
            continue;
        }
        var name = comp.displayName + " · " + prop.displayName;
        var keys = prop.getKeys();
        var times = [];
        var values = [];
        var ok = !!keys;
        for (var k = 0; ok && k < keys.length; k++) {
            var value = prop.getValueAtKey(keys[k]);
            if (!numeric(value)) {
                ok = false;
                break;
            }
            times.push(String(keys[k].ticks));
            values.push(value);
        }
        if (ok && times.length >= 2) {
            props.push({c: c, p: p, name: name, keys: times, values: values});
        } else if (!ok) {
            skipped.push(name);
        }
    }
}
return {
    clip: clip.name,
    kind: where ? where.kind : "",
    track: where ? where.index : -1,
    start: String(start),
    frame: String(deckFrameTicks(seq)),
    at: String(at),
    inside: play >= start && play < deckTicks(clip.end),
    props: props,
    skipped: skipped
};
