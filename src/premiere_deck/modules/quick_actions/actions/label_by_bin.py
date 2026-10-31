from __future__ import annotations

from premiere_deck.modules.quick_actions.action import Action, plural, require_project
from premiere_deck.premiere.model import COLOR_LABELS, KIND_CLIP, KIND_FILE

# Fixed colours of the Files layout's top-level bins (case-insensitive names); other top-level bins take the
# palette in alphabetical order, cycling.
FIXED = {
    "footage": COLOR_LABELS.index("Cerulean"),
    "audio": COLOR_LABELS.index("Forest"),
    "graphics": COLOR_LABELS.index("Lavender"),
    "overlays": COLOR_LABELS.index("Mango"),
    "motion": COLOR_LABELS.index("Magenta"),
}
PALETTE = tuple(COLOR_LABELS.index(name) for name in (
    "Teal", "Rose", "Yellow", "Purple", "Caribbean", "Tan", "Green", "Brown", "Blue", "Violet", "Iris",
))
CHUNK = 50


def plan_labels(items) -> dict[int, list[str]]:
    """Label index -> node ids of the clips and files whose label differs from their top-level bin's colour."""
    binned = [item for item in items if item.kind in (KIND_CLIP, KIND_FILE) and item.tree_path]
    others = sorted({_top(item) for item in binned if _top(item).lower() not in FIXED}, key=str.lower)
    colours = {name: PALETTE[index % len(PALETTE)] for index, name in enumerate(others)}
    plan: dict[int, list[str]] = {}
    for item in binned:
        top = _top(item)
        label = FIXED.get(top.lower(), colours.get(top))
        if label is not None and item.color_label != label:
            plan.setdefault(label, []).append(item.node_id)
    return plan


def _top(item) -> str:
    return item.tree_path.split("/")[0]


class LabelByBin(Action):
    id = "label_by_bin"
    label = "Colour labels by bin"
    title = "Label by bin"
    short = "BIN"
    icon = "label-by-bin"
    hint = "Gives every project item the colour of its top-level bin."
    group = "Labels"
    long = True
    feasibility = (
        "Facade snapshot() and set_color_label() (ProjectItem.setColorLabel, documented, ppro-scripting, Premiere "
        "14+). TrackItem has no colour-label setter in ExtendScript, so clips already on a timeline keep their "
        "label unless Premiere carries the item label over (checked in phase 2); clips placed afterwards take it."
    )

    def run(self, ctx, module):
        require_project(ctx)
        snapshot = ctx.premiere.snapshot()
        binned = [item for item in snapshot.items if item.kind in (KIND_CLIP, KIND_FILE) and item.tree_path]
        if not binned:
            return self.done("No items in bins", "neutral")
        plan = plan_labels(snapshot.items)
        total = sum(len(ids) for ids in plan.values())
        if total == 0:
            return self.done("Labels already match the bins", "neutral")
        done = 0
        for label, node_ids in sorted(plan.items()):
            for offset in range(0, len(node_ids), CHUNK):
                chunk = node_ids[offset:offset + CHUNK]
                ctx.premiere.set_color_label(chunk, label)
                done += len(chunk)
                module.progress(done / total, f"{done} of {total}")
        changed = {node_id for ids in plan.values() for node_id in ids}
        bins = len({_top(item) for item in binned if item.node_id in changed})
        return self.done(f"Labelled {plural(total, 'item')} in {plural(bins, 'bin')}")
