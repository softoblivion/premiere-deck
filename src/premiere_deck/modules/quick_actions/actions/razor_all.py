from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, plural, require_sequence, timecode
from premiere_deck.modules.quick_actions.results import count, number


class RazorAll(Action):
    id = "razor_all"
    label = "Razor all tracks"
    short = "Razor all"
    icon = "razor-all"
    hint = "Cuts every unlocked track at the playhead."
    group = "Timeline"
    feasibility = (
        "QE qe.project.getActiveSequence(), getVideoTrackAt / getAudioTrackAt and QE track razor(timecodeString) "
        "(undocumented; used by Adobe's PProPanel sample, works in 25.x and 26.x when given Premiere's own "
        "timecode string from Time.getFormatted(frameRate, displayFormat)); Sequence.getPlayerPosition, "
        "Sequence.timebase, Sequence.getSettings (documented); Track.isLocked() (undocumented)."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        result = self.eval(ctx)
        cuts = count(result, "cuts")
        locked = count(result, "locked")
        failed = count(result, "failed")
        at = timecode(number(result, "seconds"))
        if cuts == 0 and failed == 0:
            if locked:
                return self.done("Nothing cut", "warn", "The clips under the playhead are on locked tracks.")
            return self.done("No clip under the playhead", "neutral")
        if cuts == 0:
            return self.fail("Premiere did not cut at the playhead.")
        text = f"Cut {plural(cuts, 'track')} at {at}"
        if locked:
            text += f" · {plural(locked, 'locked track')} skipped"
        if failed:
            return self.done(text + f" · {failed} not cut", "warn")
        return self.done(text)
