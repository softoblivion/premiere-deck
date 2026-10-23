// Creates an auto-reframed copy of a sequence (Sequence.autoReframeSequence, documented) and sets its frame size;
// Premiere keeps the source height (1920x1080 at 9:16 gives 607x1080), so the size is set after. Shared by
// create_16_9, create_9_16 and create_custom.
// args: {sequence_id, numerator, denominator, motion ("slower" | "default" | "faster"), name, width, height}
// returns: {name, sequence_id, width, height}
var seq = deckSequenceById(__args.sequence_id) || deckActiveSequence();
if (!seq.autoReframeSequence) {
    deckFail("This Premiere version has no Auto Reframe scripting.");
}
var copy = seq.autoReframeSequence(__args.numerator, __args.denominator, __args.motion, __args.name, false);
if (!copy) {
    deckFail("Premiere did not create the reframed sequence.");
}
var width = Number(copy.frameSizeHorizontal);
var height = Number(copy.frameSizeVertical);
if (width !== __args.width || height !== __args.height) {
    try {
        var settings = copy.getSettings();
        settings.videoFrameWidth = __args.width;
        settings.videoFrameHeight = __args.height;
        copy.setSettings(settings);
        var applied = copy.getSettings();
        width = Number(applied.videoFrameWidth);
        height = Number(applied.videoFrameHeight);
    } catch (eSize) {}
}
return {name: copy.name, sequence_id: String(copy.sequenceID), width: width, height: height};
