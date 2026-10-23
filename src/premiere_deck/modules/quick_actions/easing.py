"""Keyframe easing by baking (docs/QUICK_ACTIONS.md 1.1): ExtendScript exposes no bezier handles, so an ease is a
run of linear keys, one per sequence frame, sampled from a cubic bezier between the two keys around the playhead.

All times are integer ticks (`TICKS_PER_SECOND` per second) in the clip's media time, as `ComponentParam.getKeys()`
returns them; values are numbers or lists of numbers (Position, Anchor Point).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence, Union

TICKS_PER_SECOND = 254016000000

# Cubic bezier (x1, y1, x2, y2) of time x value per preset; the strength grows with the pull of the handles.
CURVES = {
    "slow": (0.3, 0.0, 0.7, 1.0),
    "med": (0.5, 0.0, 0.5, 1.0),
    "fast": (0.7, 0.0, 0.3, 1.0),
}

Value = Union[float, list]


def bezier_y(x: float, x1: float, y1: float, x2: float, y2: float) -> float:
    """y of the cubic bezier from (0, 0) to (1, 1) with control points (x1, y1), (x2, y2) at the given x (CSS
    `cubic-bezier` semantics): s is solved from x(s) = x by Newton steps with a bisection fallback."""
    x = min(1.0, max(0.0, float(x)))

    def coord(s: float, a: float, b: float) -> float:
        return 3 * a * s * (1 - s) ** 2 + 3 * b * s * s * (1 - s) + s ** 3

    def slope(s: float, a: float, b: float) -> float:
        return 3 * a * (1 - s) ** 2 + 6 * (b - a) * s * (1 - s) + 3 * (1 - b) * s * s

    s = x
    for _ in range(8):
        error = coord(s, x1, x2) - x
        if abs(error) < 1e-9:
            return coord(s, y1, y2)
        d = slope(s, x1, x2)
        if abs(d) < 1e-9:
            break
        s -= error / d
        if not 0.0 <= s <= 1.0:
            break
    low, high = 0.0, 1.0
    s = x
    for _ in range(60):
        value = coord(s, x1, x2)
        if abs(value - x) < 1e-10:
            break
        if value < x:
            low = s
        else:
            high = s
        s = (low + high) / 2
    return coord(s, y1, y2)


def mix(v0: Value, v1: Value, f: float) -> Optional[Value]:
    """v0 + (v1 - v0) * f for numbers or equally long lists of numbers; None when the values do not mix."""
    if _number(v0) and _number(v1):
        return v0 + (v1 - v0) * f
    if isinstance(v0, (list, tuple)) and isinstance(v1, (list, tuple)) and len(v0) == len(v1) and v0 and \
            all(_number(a) and _number(b) for a, b in zip(v0, v1)):
        return [a + (b - a) * f for a, b in zip(v0, v1)]
    return None


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def find_pair(times: Sequence[int], at: int, frame: int) -> Optional[tuple[int, int]]:
    """Indexes (first, last) of the keys around `at` (first <= at < last), widened over an earlier bake: keys
    exactly one frame apart around `at` are taken as baked, and the pair becomes the outer keys of that run (the
    last step of a bake may be shorter than a frame). None when `at` is not between two keys."""
    count = len(times)
    hit = next((i for i in range(count - 1) if times[i] <= at < times[i + 1]), None)
    if hit is None:
        return None
    tol = max(1, frame // 100)

    def step(a: int) -> bool:  # the gap after key a is one frame
        return abs((times[a + 1] - times[a]) - frame) <= tol

    def short(a: int) -> bool:  # the gap after key a is the shorter last step of a bake
        return 0 < times[a + 1] - times[a] < frame - tol

    first, last = hit, hit + 1
    if short(hit):  # the end of a bake: widen to the left only
        while first > 0 and step(first - 1):
            first -= 1
        return (first, last) if first < hit else (hit, hit + 1)
    if not step(hit):
        return hit, hit + 1
    while first > 0 and step(first - 1):
        first -= 1
    while last < count - 1 and step(last):
        last += 1
    if last < count - 1 and short(last):
        last += 1
    return first, last


@dataclass
class Plan:
    """What `ease_apply.jsx` does on one property: remove keys, add keys with values, set keys to linear."""

    remove: list[int] = field(default_factory=list)
    add: list[tuple[int, Value]] = field(default_factory=list)
    linear: list[int] = field(default_factory=list)


def bake(t0: int, v0: Value, t1: int, v1: Value, frame: int, curve: tuple[float, float, float, float]) -> list:
    """[(ticks, value)] of the keys strictly between t0 and t1, one per frame from t0, sampled from the curve."""
    keys: list = []
    if frame <= 0 or t1 <= t0:
        return keys
    tol = max(1, frame // 100)
    span = t1 - t0
    t = t0 + frame
    while t < t1 - tol:
        value = mix(v0, v1, bezier_y((t - t0) / span, *curve))
        if value is None:
            return []
        keys.append((t, value))
        t += frame
    return keys


def plan(times: Sequence[int], values: Sequence[Value], at: int, frame: int, preset: str) -> Optional[Plan]:
    """The plan for one property (`preset` is a key of CURVES or "linear"); None without a pair around `at` or
    when the pair's values do not mix."""
    pair = find_pair(times, at, frame)
    if pair is None:
        return None
    first, last = pair
    inner = list(times[first + 1:last])
    if preset == "linear":
        return Plan(remove=inner, linear=[times[first], times[last]])
    if mix(values[first], values[last], 0.5) is None:
        return None
    added = bake(times[first], values[first], times[last], values[last], frame, CURVES[preset])
    return Plan(remove=inner, add=added,
                linear=[times[first]] + [t for t, _ in added] + [times[last]])
