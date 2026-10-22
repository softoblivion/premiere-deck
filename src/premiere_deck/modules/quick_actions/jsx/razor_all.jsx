// Cuts every unlocked track of the active sequence at the playhead (QE razor with Premiere's own timecode string).
// returns: {cuts, locked, failed, seconds, timecode}
var seq = deckActiveSequence();
var qeSeq = deckQeSequence();
var frame = deckFrameTicks(seq);
var ticks = Math.round(deckTicks(seq.getPlayerPosition()) / frame) * frame;
var tc = deckTimecode(seq, ticks);

var spans = function (track, at) {
    for (var i = 0; i < track.clips.numItems; i++) {
        var clip = track.clips[i];
        if (at > deckTicks(clip.start) && at < deckTicks(clip.end)) {
            return true;
        }
    }
    return false;
};

var tracks = deckTracks(seq);
var cuts = 0;
var locked = 0;
var failed = 0;
for (var t = 0; t < tracks.length; t++) {
    var entry = tracks[t];
    if (!spans(entry.track, ticks)) {
        continue;
    }
    if (entry.locked) {
        locked++;
        continue;
    }
    var before = entry.track.clips.numItems;
    try {
        deckQeTrack(qeSeq, entry.kind, entry.index).razor(tc);
    } catch (e) {
        failed++;
        continue;
    }
    if (entry.track.clips.numItems > before) {
        cuts++;
    } else {
        failed++;
    }
}
return {cuts: cuts, locked: locked, failed: failed, seconds: deckSecs(ticks), timecode: tc};
