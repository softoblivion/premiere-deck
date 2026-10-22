// Queues a sequence in Adobe Media Encoder (Encoder.launchEncoder / encodeSequence, documented) into
// "<out_dir>\<base_name>.<ext>" (next free name), with the extension the preset produces
// (Sequence.getExportFileExtension, documented). The queue is not started.
// args: {sequence_id, preset, out_dir, base_name, in_to_out (bool)}
// returns: {path, job_id, extension}
var seq = deckSequenceById(__args.sequence_id) || deckActiveSequence();
var preset = new File(__args.preset);
if (!preset.exists) {
    deckFail("The export preset file is missing.");
}
var encoder = app.encoder;
if (!encoder) {
    deckFail("Adobe Media Encoder is not available.");
}
var ext = "mp4";
try {
    var found = seq.getExportFileExtension(preset.fsName);
    if (found) {
        ext = String(found).replace(/^\./, "");
    }
} catch (eExt) {}

var folder = new Folder(__args.out_dir);
if (!folder.exists && !folder.create()) {
    deckFail("The folder " + __args.out_dir + " cannot be created.");
}
var path = deckFreePath(folder.fsName, __args.base_name, ext);
var mode;
if (__args.in_to_out) {
    mode = typeof encoder.ENCODE_IN_TO_OUT === "number" ? encoder.ENCODE_IN_TO_OUT : 1;
} else {
    mode = typeof encoder.ENCODE_ENTIRE === "number" ? encoder.ENCODE_ENTIRE : 0;
}
encoder.launchEncoder();
var job = encoder.encodeSequence(seq, path, preset.fsName, mode, true);
if (!job || String(job) === "0") {
    deckFail("Media Encoder did not accept the job.");
}
return {path: path, job_id: String(job), extension: ext};
