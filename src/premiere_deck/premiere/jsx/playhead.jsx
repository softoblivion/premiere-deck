// @uses lib
// playhead: reads, or sets when __args.set is a number, the active sequence's playhead. __args = {set}.
var sequence = requireSequence(null);
if (__args.set !== null && __args.set !== undefined) {
    sequence.setPlayerPosition(secondsToTicks(__args.set));
}
return {seconds: sequence.getPlayerPosition().seconds};
