// Selects every clip of the active sequence whose project item is `node_id`; every other clip is deselected.
// args: {node_id}
// returns: {count, name}
var seq = deckActiveSequence();
var tracks = deckTracks(seq);
var wanted = String(__args.node_id);
var count = 0;
var name = "";
for (var t = 0; t < tracks.length; t++) {
    var clips = tracks[t].track.clips;
    for (var c = 0; c < clips.numItems; c++) {
        var clip = clips[c];
        var source = deckSource(clip);
        var match = !!source && String(source.nodeId) === wanted;
        if (match) {
            count++;
            name = source.name;
        }
        clip.setSelected(match, true);
    }
}
return {count: count, name: name};
