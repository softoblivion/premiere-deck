from __future__ import annotations

from premiere_deck.modules.quick_actions import easing
from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, plural, require_selection, require_sequence,
)
from premiere_deck.modules.quick_actions.results import count, text, value

NO_PAIR = "Place the playhead between two keyframes"


def _ticks(raw) -> int:
    try:
        return int(round(float(raw)))
    except (TypeError, ValueError):
        return 0


class Ease(Action):
    """Eases (or linearises) the keyframe pair around the playhead on every animated property of the first selected
    clip by baking keys (docs/QUICK_ACTIONS.md 1.1): `ease_read.jsx` reports the keys, `easing.plan` computes the
    keys to remove, add and set to linear, `ease_apply.jsx` applies them."""

    preset = "med"
    group = "Keys"
    long = True
    timeout_ms = 60000
    feasibility = (
        "ComponentParam.isTimeVarying, getKeys, getValueAtKey, keyExistsAtTime, addKey, removeKey, setValueAtKey "
        "and setInterpolationTypeAtKey (documented, ppro-scripting); key times are in the clip's media time "
        "(sequence time - start + inPoint, verified 26.5.2). No bezier handle or keyframe selection accessor in "
        "the DOM or QE (QE component: getParamKeyframes returns timecodes only), so the curve is baked: one linear "
        "key per sequence frame."
    )

    def run(self, ctx, module):
        require_sequence(ctx)
        require_selection(ctx)
        data = self.eval(ctx, None, script="ease_read")
        props = value(data, "props", [])
        if not isinstance(props, list) or not props:
            raise ActionAbort("warn", "The selected clip has no keyframes")
        frame = _ticks(value(data, "frame"))
        at = _ticks(value(data, "at"))
        plans, names = [], []
        for prop in props:
            if not isinstance(prop, dict):
                continue
            times = [_ticks(t) for t in prop.get("keys") or []]
            values = list(prop.get("values") or [])
            if len(times) != len(values) or len(times) < 2:
                continue
            step = easing.plan(times, values, at, frame, self.preset)
            if step is None:
                continue
            plans.append({"c": prop.get("c"), "p": prop.get("p"), "remove": [str(t) for t in step.remove],
                          "add": [{"t": str(t), "v": v} for t, v in step.add],
                          "linear": [str(t) for t in step.linear]})
            names.append(str(prop.get("name") or "Property"))
        if not plans or value(data, "inside") is False:
            raise ActionAbort("warn", NO_PAIR)
        result = self.eval(ctx, {"kind": text(data, "kind"), "track": count(data, "track"),
                                 "start": text(data, "start"), "props": plans}, script="ease_apply")
        return self.report(len(plans), count(result, "added"), count(result, "removed"), ", ".join(names))

    def report(self, props: int, added: int, removed: int, names: str):
        return self.done(f"Eased {plural(props, 'property', 'properties')} · {plural(added, 'key')}", detail=names)


class EaseSlow(Ease):
    id = "ease_slow"
    label = "Ease slow"
    short = "SLOW"
    icon = "ease-slow"
    hint = "Eases the keyframe pair around the playhead gently (bezier 0.3, 0, 0.7, 1)."
    preset = "slow"
