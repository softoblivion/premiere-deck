"""Premiere Deck configuration of the vendored self-updater (`premiere_deck.vendor.selfupdate`).

The one place that knows the application values (repository, asset, display name). The updater itself is never
edited here; the panel updates with the exe (docs/PANEL.md 4.2). Payload `UpdateStatus` = docs/BRIDGE_API.md 3.6.
"""

from __future__ import annotations

from typing import Any, Callable

from premiere_deck.vendor.selfupdate import APPLY_FLAG, SelfUpdater, UpdateConfig, UpdateScheduler, run_apply

from .paths import running_exe

REPO = "softoblivion/premiere-deck"
ASSET_NAME = "PremiereDeck.exe"
APP_NAME = "Premiere Deck"
RELEASE_URL_ENV = "PREMIERE_DECK_RELEASE_URL"
LOGGER_NAME = "premiere_deck.selfupdate"

__all__ = ["APPLY_FLAG", "APP_NAME", "ASSET_NAME", "REPO", "build_config", "build_scheduler", "build_updater",
           "run_apply"]


def build_config(paths: Any, version: str, on_status: Callable[[dict], None],
                 on_toast: Callable[[str, str], None] | None = None) -> UpdateConfig:
    """`paths` needs `updates_dir`; `exe_path` when present, else the running exe (None from source)."""
    exe_path = getattr(paths, "exe_path", None) or running_exe()
    return UpdateConfig(
        repo=REPO,
        asset_name=ASSET_NAME,
        app_name=APP_NAME,
        version=version,
        updates_dir=str(paths.updates_dir),
        exe_path=str(exe_path) if exe_path else None,
        on_status=on_status,
        on_toast=on_toast,
        release_url_env=RELEASE_URL_ENV,
        logger_name=LOGGER_NAME,
    )


def build_updater(paths: Any, version: str, on_status: Callable[[dict], None],
                  on_toast: Callable[[str, str], None] | None = None) -> SelfUpdater:
    """No network is touched until a check or download is requested."""
    return SelfUpdater(build_config(paths, version, on_status, on_toast))


def build_scheduler(updater: SelfUpdater) -> UpdateScheduler:
    """Periodic checks at the library's fixed period (5 minutes, longer after failures, none offline)."""
    return UpdateScheduler(updater, logger_name=LOGGER_NAME + ".schedule")
