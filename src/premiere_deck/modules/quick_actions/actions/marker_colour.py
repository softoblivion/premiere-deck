from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, Menu, plural
from premiere_deck.modules.quick_actions.results import count, text

DEFAULT = "Green"
# Marker.setColorByIndex offers 8 colours (verified 26.5.2: indexes above 7 read back as 7; the Marker dialog shows
# them in this order).
MARKER_COLOURS = ("Green", "Red", "Purple", "Orange", "Yellow", "White", "Blue", "Cyan")
# Swatches close to the marker colours of Premiere's timeline (read from a screen, not from the preferences).
SWATCHES = {
    "Green": "#4fc35a", "Red": "#e0464e", "Purple": "#a35ce0", "Orange": "#e0892e", "Yellow": "#e0cf3a",
    "White": "#e6e6e6", "Blue": "#3f7fe0", "Cyan": "#38c9e0",
}


class MarkerColour(Action):
    id = "marker_colour"
    label = "Marker colour"
    short = "MRK"
    icon = "marker-colour"
    hint = "Gives the markers at the playhead the last marker colour."
    group = "Labels"
    feasibility = (
        "Marker.setColorByIndex(0..7) (documented; 8..15 read back as 7 on 26.5.2) on the active sequence's markers "
        "(Sequence.markers) and the selected clips' item markers (ProjectItem.getMarkers), for the markers whose "
        "start is within half a frame of the playhead (Sequence.getPlayerPosition) or whose range holds it "
        "(Marker.end). Marker selection is not scriptable, so the playhead stands for it."
    )

    def menu(self, module):
        return Menu("colour", "Marker colour", DEFAULT, tuple((name, name) for name in MARKER_COLOURS), runs=True,
                    palette=True, columns=8, hint="Gives the markers at the playhead this colour.",
                    swatches=tuple(SWATCHES.items()))

    def run(self, ctx, module):
        name = module.choice(self, "colour")
        result = self.eval(ctx, {"colour": MARKER_COLOURS.index(name)})
        if not text(result, "sequence"):
            return self.done("No active sequence", "warn")
        markers = count(result, "markers")
        if not markers:
            return self.done("No marker at the playhead", "warn")
        return self.done(f"Coloured {plural(markers, 'marker')} {name}")
