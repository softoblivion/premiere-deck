from __future__ import annotations

from premiere_deck.modules.quick_actions import premiere_window
from premiere_deck.modules.quick_actions.action import Action, Menu, plural
from premiere_deck.modules.quick_actions.results import count, text
from premiere_deck.premiere.model import COLOR_LABELS, DEFAULT_LABEL_RGB

DEFAULT = "Cerulean"
# The timeline draws label colours brighter than Premiere stores them: a swatch is the stored colour lifted so its
# brightest channel is 224.
SWATCH_PEAK = 224
VIA_ITEMS = ("Premiere's Label menu was not reachable, so the colour went on the clips' project items; clips "
             "already in a sequence may keep their own colour.")


def lift(rgb: str) -> str:
    """The swatch of a stored label colour ("#3e0aae" -> "#500de0")."""
    red, green, blue = (int(rgb[i:i + 2], 16) for i in (1, 3, 5))
    peak = max(red, green, blue)
    if peak == 0:
        return "#000000"
    scale = SWATCH_PEAK / peak
    return "#" + "".join(f"{min(255, round(channel * scale)):02x}" for channel in (red, green, blue))


# The swatches of Premiere's default label colours (the fallback when the preferences cannot be read).
SWATCHES = {name: lift(rgb) for name, rgb in zip(COLOR_LABELS, DEFAULT_LABEL_RGB)}


class Colour(Action):
    id = "colour"
    label = "Colour label"
    short = "CLR"
    icon = "colour"
    hint = "Gives the newest selection, timeline clips or Project panel items, the last label colour."
    group = "Labels"
    feasibility = (
        "No colour setter on a DOM TrackItem or a QE track item in 26.5.2 (reflected live), and placed clips do not "
        "follow ProjectItem.setColorLabel; selected timeline clips are coloured by Premiere's Edit > Label > "
        "<colour> (menu commands 13124..13139), posted as WM_COMMAND after Window > Timelines > <sequence> focused "
        "the Timeline (verified 26.5.2). Project panel items: ProjectItem.setColorLabel (documented) on "
        "app.getCurrentProjectViewSelection (documented), bins included. Names and colours: app.properties "
        "BE.Prefs.LabelNames / LabelColors (facade label_palette, unverified keys, defaults as fallback)."
    )

    def menu(self, module):
        palette = module.palette
        return Menu("label", "Label colour", DEFAULT, tuple((name, palette.name(i)) for i, name in enumerate(COLOR_LABELS)),
                    runs=True, palette=True, columns=16, hint="Labels the newest selection with this colour.",
                    swatches=tuple((name, lift(palette.labels[i].rgb)) for i, name in enumerate(COLOR_LABELS)))

    def extras(self, module):
        return [module.target_chip()]

    def run(self, ctx, module):
        module.refresh_palette(ctx.premiere)
        value = module.choice(self, "label")
        index = COLOR_LABELS.index(value)
        name = module.palette.name(index)
        args = {"label": index, "target": module.preferred_target()}
        result = self.eval(ctx, dict(args, mode="select"))
        module.note_counts(result)
        target = text(result, "target")
        clips, items, bins = count(result, "clips"), count(result, "items"), count(result, "bins")
        if target == "clips" and clips:
            if premiere_window.label_timeline_selection(text(result, "sequence"), index, name):
                return self.done(f"Coloured {plural(clips, 'clip')} {name}")
            self.eval(ctx, dict(args, mode="clip_items"))
            return self.done(f"Coloured {plural(clips, 'clip')} {name}", "warn", VIA_ITEMS)
        if target == "items" and (items or bins):
            parts = [plural(n, word) for n, word in ((items, "item"), (bins, "bin")) if n]
            return self.done(f"Coloured {' and '.join(parts)} {name}")
        return self.done("Nothing selected", "warn")
