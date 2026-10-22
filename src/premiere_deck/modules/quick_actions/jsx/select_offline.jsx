// Selects every clip of the active sequence whose media is offline; the rest is deselected. When no clip is
// offline the selection is left as it is.
// returns: {count}
var seq = deckActiveSequence();
var tracks = deckTracks(seq);
var all = [];
var count = 0;
for (var t = 0; t < tracks.length; t++) {
    var clips = tracks[t].track.clips;
    for (var c = 0; c < clips.numItems; c++) {
        var clip = clips[c];
        var source = deckSource(clip);
        var offline = false;
        try {
            offline = !!source && source.isOffline();
        } catch (e) {}
        if (offline) {
            count++;
        }
        all.push({clip: clip, offline: offline});
    }
}
if (count > 0) {
    for (var i = 0; i < all.length; i++) {
        all[i].clip.setSelected(all[i].offline, true);
    }
}
return {count: count};
