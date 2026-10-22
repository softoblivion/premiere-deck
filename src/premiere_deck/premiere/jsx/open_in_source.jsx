// @uses lib
// open_in_source: opens an item in the Source Monitor and, when __args.seconds is set, positions it there through
// the QE source player (docs/PREMIERE_API.md 7.4). Returns {positioned, position}; the facade adds a marker when
// positioning was not possible. __args = {node_id, seconds}.
var item = requireItem(__args.node_id);
app.sourceMonitor.openProjectItem(item);
if (__args.seconds === null || __args.seconds === undefined) {
    return {positioned: false, position: app.sourceMonitor.getPosition().seconds};
}
var target = Number(__args.seconds);
var fps = 0;
try {
    fps = Number(item.getFootageInterpretation().frameRate);
} catch (e) {
    fps = 0;
}
if (!(fps >= 1)) {
    // Audio-only or unknown rate: no frame timecode to scrub to.
    return {positioned: false, position: app.sourceMonitor.getPosition().seconds};
}
function pad(n) {
    return n < 10 ? "0" + n : String(n);
}
// Non-drop timecode of frame number `frame` at the nominal (rounded) rate; Premiere counts frames the same way.
function timecode(frame) {
    var nominal = Math.round(fps);
    var ff = frame % nominal;
    var total = Math.floor(frame / nominal);
    return pad(Math.floor(total / 3600)) + ":" + pad(Math.floor(total / 60) % 60) + ":" + pad(total % 60) + ":" + pad(ff);
}
var positioned = false;
var position = 0;
try {
    app.enableQE();
    var player = qe.source.player;
    player.startScrubbing();
    player.scrubTo(timecode(Math.round(target * fps)));
    player.endScrubbing();
    position = app.sourceMonitor.getPosition().seconds;
    positioned = Math.abs(position - target) <= 1.5 / fps;
} catch (e) {
    positioned = false;
}
return {positioned: positioned, position: position};
