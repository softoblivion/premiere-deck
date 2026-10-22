// Closes every gap that is empty on all tracks of the active sequence (the gaps Premiere's own Ripple Delete
// accepts), by moving the clips after each gap left. Gaps before the first clip are kept. A gap followed by a clip
// on a locked track cannot close without desyncing that track and is kept.
// returns: {closed, blocked, removed (seconds)}
var seq = deckActiveSequence();
var frame = deckFrameTicks(seq);
var tracks = deckTracks(seq);

var spans = [];
var lockedStarts = [];
var t, c, clip;
for (t = 0; t < tracks.length; t++) {
    var clips = tracks[t].track.clips;
    for (c = 0; c < clips.numItems; c++) {
        clip = clips[c];
        var s = deckTicks(clip.start);
        spans.push({start: s, end: deckTicks(clip.end)});
        if (tracks[t].locked) {
            lockedStarts.push(s);
        }
    }
}
if (spans.length === 0) {
    return {closed: 0, blocked: 0, removed: 0};
}
spans.sort(function (a, b) { return a.start - b.start; });

// Gaps between the merged occupied spans; shorter than half a frame is not a gap.
var gaps = [];
var reach = spans[0].end;
for (var i = 1; i < spans.length; i++) {
    if (spans[i].start - reach >= frame / 2) {
        gaps.push({start: reach, end: spans[i].start});
    }
    if (spans[i].end > reach) {
        reach = spans[i].end;
    }
}

var lastLocked = -1;
for (i = 0; i < lockedStarts.length; i++) {
    if (lockedStarts[i] > lastLocked) {
        lastLocked = lockedStarts[i];
    }
}
var closing = [];
var blocked = 0;
for (i = 0; i < gaps.length; i++) {
    if (lastLocked >= gaps[i].end - frame / 2) {
        blocked++;
    } else {
        closing.push(gaps[i]);
    }
}
if (closing.length === 0) {
    return {closed: 0, blocked: blocked, removed: 0};
}

var shiftAt = function (ticks) {
    var shift = 0;
    for (var g = 0; g < closing.length; g++) {
        if (closing[g].end <= ticks + frame / 2) {
            shift += closing[g].end - closing[g].start;
        }
    }
    return shift;
};

// Every clip of an unlocked track that moves, in ascending start order so no clip lands on a neighbour that has
// not moved yet. The current start is read again before each move: a clip already moved with a linked partner
// gets no second move.
var movers = [];
for (t = 0; t < tracks.length; t++) {
    if (tracks[t].locked) {
        continue;
    }
    for (c = 0; c < tracks[t].track.clips.numItems; c++) {
        clip = tracks[t].track.clips[c];
        var start = deckTicks(clip.start);
        var shift = shiftAt(start);
        if (shift > 0) {
            movers.push({clip: clip, start: start, target: start - shift});
        }
    }
}
movers.sort(function (a, b) { return a.start - b.start; });

var wrong = 0;
for (i = 0; i < movers.length; i++) {
    var delta = movers[i].target - deckTicks(movers[i].clip.start);
    if (Math.abs(delta) >= 1) {
        movers[i].clip.move(deckTime(delta));
    }
}
for (i = 0; i < movers.length; i++) {
    if (Math.abs(deckTicks(movers[i].clip.start) - movers[i].target) >= frame / 2) {
        wrong++;
    }
}
if (wrong > 0) {
    deckFail("Some clips did not land where expected. Use Undo to revert.");
}

var removed = 0;
for (i = 0; i < closing.length; i++) {
    removed += closing[i].end - closing[i].start;
}
return {closed: closing.length, blocked: blocked, removed: deckSecs(removed)};
