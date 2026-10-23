from __future__ import annotations

from premiere_deck.modules.quick_actions import premiere_window
from premiere_deck.modules.quick_actions.action import Action, Menu, plural
from premiere_deck.modules.quick_actions.results import count, text
from premiere_deck.premiere.model import COLOR_LABELS

DEFAULT = "Cerulean"
# Marker.setColorByIndex offers 8 colours (verified 26.5.2: indexes above 7 read back as 7; the Marker dialog shows
# them in this order); each label colour maps to the nearest one.
MARKER_COLOURS = ("Green", "Red", "Purple", "Orange", "Yellow", "White", "Blue", "Cyan")
MARKER_FOR = {
    "Violet": "Purple", "Iris": "Blue", "Caribbean": "Cyan", "Lavender": "Purple", "Cerulean": "Blue",
    "Forest": "Green", "Rose": "Red", "Mango": "Orange", "Purple": "Purple", "Blue": "Blue", "Teal": "Cyan",
    "Magenta": "Red", "Tan": "White", "Green": "Green", "Brown": "Orange", "Yellow": "Yellow",
}
# Swatch of each label colour for the menu: the hue of Premiere's default label colours (`BE.Prefs.LabelColors.<n>`
# of the 26.0 prefs file, Windows COLORREF 0x00BBGGRR), lifted so the brightest channel is 224, as the timeline
# draws them brighter than the stored values.
SWATCHES = {
    "Violet": "#500de0", "Iris": "#00a3e0", "Caribbean": "#6fe012", "Lavender": "#c21ce0", "Cerulean": "#0cd1e0",
    "Forest": "#b9e000", "Rose": "#e00355", "Mango": "#e05f07", "Purple": "#7700e0", "Blue": "#1a41e0",
    "Teal": "#03e0c6", "Magenta": "#e01695", "Tan": "#e0b68b", "Green": "#1fe05e", "Brown": "#e08e0e",
    "Yellow": "#e0ce32",
}
VIA_ITEMS = ("Premiere's Label menu was not reachable, so the colour went on the clips' project items; clips "
             "already in a sequence may keep their own colour.")


class Colour(Action):
    id = "colour"
    label = "Colour label"
    short = "CLR"
    icon = "colour"
    hint = "Gives the selected clips or project items, and a marker at the playhead, the chosen label colour."
    group = "Clips"
    feasibility = (
        "No colour setter on a DOM TrackItem or a QE track item in 26.5.2 (reflected live), and placed clips do not "
        "follow ProjectItem.setColorLabel; selected timeline clips are coloured by Premiere's Edit > Label > "
        "<colour> (menu commands 13124..13139), posted as WM_COMMAND after Window > Timelines > <sequence> focused "
        "the Timeline (verified 26.5.2). Without a timeline selection: ProjectItem.setColorLabel (documented) on "
        "app.getCurrentProjectViewSelection (documented), bins included. Markers at the playhead: "
        "Marker.setColorByIndex(0..7) (documented) on the sequence markers and the selected clips' item markers."
    )

    def menu(self, module):
        return Menu("label", "Colour", DEFAULT, tuple((name, name) for name in COLOR_LABELS), runs=True,
                    swatches=tuple(SWATCHES.items()))

    def run(self, ctx, module):
        name = module.choice(self, "label")
        args = {"label": COLOR_LABELS.index(name), "marker": MARKER_COLOURS.index(MARKER_FOR[name])}
        result = self.eval(ctx, dict(args, mode="select"))
        clips, items, bins = count(result, "clips"), count(result, "items"), count(result, "bins")
        markers = count(result, "markers")
        via_items = False
        if clips and not premiere_window.label_timeline_selection(text(result, "sequence"),
                                                                  COLOR_LABELS.index(name), name):
            self.eval(ctx, dict(args, mode="clip_items"))
            via_items = True
        parts = [plural(n, word) for n, word in ((clips, "clip"), (items, "item"), (bins, "bin")) if n]
        marked = f"{plural(markers, 'marker')} {MARKER_FOR[name]}"
        if not parts and not markers:
            return self.done("Nothing selected", "warn")
        if not parts:
            return self.done(f"Coloured {marked}")
        line = f"Coloured {' and '.join(parts)} {name}" + (f" · {marked}" if markers else "")
        if via_items:
            return self.done(line, "warn", VIA_ITEMS)
        return self.done(line)
