"""Tolerant readers of the plain objects the action scripts return (a missing or odd field never crashes)."""

from __future__ import annotations

from typing import Any


def value(result: Any, key: str, default: Any = None) -> Any:
    return result.get(key, default) if isinstance(result, dict) else default


def number(result: Any, key: str, default: float = 0.0) -> float:
    raw = value(result, key, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return float(default)


def count(result: Any, key: str) -> int:
    return max(0, int(number(result, key, 0)))


def text(result: Any, key: str, default: str = "") -> str:
    raw = value(result, key, default)
    return default if raw is None else str(raw)
