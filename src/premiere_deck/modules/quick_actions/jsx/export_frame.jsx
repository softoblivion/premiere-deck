// Writes the frame at the playhead as "<out_dir>\<base_name>_<timecode>.png" (QE exportFramePNG).
// QE takes (timecodeString, pathWithoutExtension), appends ".png" itself and cuts the file name at its first dot,
// so it writes a dot-free temporary name that is renamed after.
// args: {out_dir, base_name}
// returns: {path, timecode}
var seq = deckActiveSequence();
var qeSeq = deckQeSequence();
if (!qeSeq.exportFramePNG) {
    deckFail("This Premiere version cannot export frames by script.");
}
var frame = deckFrameTicks(seq);
var ticks = Math.floor(deckTicks(seq.getPlayerPosition()) / frame + 0.000001) * frame;
var tc = deckTimecode(seq, ticks);

var folder = new Folder(__args.out_dir);
if (!folder.exists && !folder.create()) {
    deckFail("The folder " + __args.out_dir + " cannot be created.");
}
var target = deckFreePath(folder.fsName, __args.base_name + "_" + tc.replace(/[:;]/g, "-"), "png");
var tempBase = folder.fsName + "\\deckframe" + new Date().getTime();
var temp = new File(tempBase + ".png");
if (temp.exists) {
    temp.remove();
}
qeSeq.exportFramePNG(tc, tempBase);
temp = new File(tempBase + ".png");
if (!temp.exists || temp.length <= 0) {
    deckFail("Premiere wrote no frame file.");
}
var targetFile = new File(target);
if (!temp.rename(decodeURI(targetFile.name))) {
    temp.remove();
    deckFail("The frame file could not be renamed.");
}
return {path: targetFile.fsName, timecode: tc};
