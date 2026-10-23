// Applies easing plans to the clip ease_read.jsx reported: removes keys, adds keys with values and sets keys to
// linear interpolation (setInterpolationTypeAtKey type 0, measured on 26.5.2: docs/PREMIERE_API.md 7.17).
// args: {kind, track, start (ticks), props: [{c, p, remove: [ticks], add: [{t (ticks), v}], linear: [ticks]}]}
// returns: {removed, added, props}
var seq = deckActiveSequence();
var list = __args.kind === "audio" ? seq.audioTracks : seq.videoTracks;
if (__args.track < 0 || __args.track >= list.numTracks) {
    deckFail("The clip is no longer in the sequence.");
}
var clip = null;
var clips = list[__args.track].clips;
for (var i = 0; i < clips.numItems; i++) {
    if (Math.abs(deckTicks(clips[i].start) - parseFloat(__args.start)) < 1) {
        clip = clips[i];
        break;
    }
}
if (!clip) {
    deckFail("The clip is no longer in the sequence.");
}
var at = function (ticks) {
    var t = new Time();
    t.ticks = String(ticks);
    return t;
};
var removed = 0;
var added = 0;
var done = 0;
var plans = __args.props || [];
for (var n = 0; n < plans.length; n++) {
    var plan = plans[n];
    var prop = clip.components[plan.c].properties[plan.p];
    var r, a, l;
    for (r = 0; r < plan.remove.length; r++) {
        if (prop.keyExistsAtTime(at(plan.remove[r]))) {
            prop.removeKey(at(plan.remove[r]));
            removed++;
        }
    }
    for (a = 0; a < plan.add.length; a++) {
        var when = at(plan.add[a].t);
        prop.addKey(when);
        prop.setValueAtKey(when, plan.add[a].v, false);
        added++;
    }
    for (l = 0; l < plan.linear.length; l++) {
        prop.setInterpolationTypeAtKey(at(plan.linear[l]), 0, n === plans.length - 1 && l === plan.linear.length - 1);
    }
    done++;
}
return {removed: removed, added: added, props: done};
