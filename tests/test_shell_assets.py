"""Host window assets (docs/HOST_UI.md 7): the icon sprite, the mock's 2.1 action set and the notice wiring."""

from __future__ import annotations

import re
import unittest
import xml.dom.minidom
from pathlib import Path

UI = Path(__file__).resolve().parent.parent / "src" / "premiere_deck" / "ui"
SPRITE = UI / "assets" / "icons.svg"

# Icons the 2.1 quick actions use (docs/QUICK_ACTIONS.md); the host's Actions tab draws one per row.
NEW_ICONS = ("track-video-add", "track-video-remove", "track-audio-add", "track-audio-remove", "colour",
             "ratio-16-9", "ratio-9-16", "ratio-custom", "export", "ease-slow", "ease-med", "ease-fast",
             "ease-linear")
KEPT_ICONS = ("export-frame", "export-sequence", "social-copy", "solo-track", "lock-others", "adjustment-layer")


def symbols() -> dict[str, str]:
    text = SPRITE.read_text("utf-8")
    return {m.group(1): m.group(0) for m in re.finditer(r'<symbol id="i-([a-z0-9-]+)".*?</symbol>', text, re.S)}


class IconSpriteTests(unittest.TestCase):
    def test_sprite_is_well_formed_xml(self):
        xml.dom.minidom.parse(str(SPRITE))

    def test_new_action_icons_exist_and_nothing_was_removed(self):
        found = symbols()
        for name in NEW_ICONS + KEPT_ICONS:
            self.assertIn(name, found, name)

    def test_new_icons_follow_the_grid_and_stroke(self):
        found = symbols()
        for name in NEW_ICONS:
            head = found[name].split(">", 1)[0]
            self.assertIn('viewBox="0 0 24 24"', head, name)
            self.assertIn('stroke-width="1.75"', head, name)
            self.assertIn('stroke-linecap="round"', head, name)
            self.assertIn('stroke-linejoin="round"', head, name)
            self.assertIn('fill="none"', head, name)

    def test_mock_actions_use_symbols_of_the_sprite(self):
        mock = (UI / "js" / "mock.js").read_text("utf-8")
        body = mock[mock.index("const ACTION_LIST = ["):mock.index("const hidden = new Set")]
        icons = re.findall(r"'([a-z0-9-]+)'\],\n", body)
        self.assertGreaterEqual(len(icons), 25)
        for name in icons:
            self.assertIn(name, symbols(), name)
        ids = re.findall(r"\['([a-z0-9_]+)', '", body)
        self.assertNotIn("social_copy", ids)
        self.assertNotIn("export_sequence", ids)
        for want in ("add_video_track", "colour", "create_custom", "export_open", "ease_linear"):
            self.assertIn(want, ids)
        self.assertNotIn("presets", mock[mock.index("function actionsPage"):mock.index("// ---- module settings")])


class NoticeWiringTests(unittest.TestCase):
    """The JS has no test runner here; these pin the contract points that the manual `--fake` check exercises."""

    def test_toast_closes_on_click_and_supports_an_extra_click_action(self):
        js = (UI / "vendor" / "liquid_ui" / "js" / "toast.js").read_text("utf-8")
        self.assertIn("el.addEventListener('click'", js)
        self.assertIn("opts.onClick", js)
        update = (UI / "vendor" / "liquid_ui" / "js" / "update.js").read_text("utf-8")
        self.assertIn("onClick: openSheet", update)

    def test_activity_strip_follows_the_setting(self):
        wide = (UI / "js" / "pages" / "wide.js").read_text("utf-8")
        self.assertIn("show_activity", wide)
        settings = (UI / "js" / "settings.js").read_text("utf-8")
        self.assertIn("label: 'Show activity'", settings)
        self.assertIn("{ show_activity: v }", settings)
        mock = (UI / "js" / "mock.js").read_text("utf-8")
        self.assertIn("'show_activity'", mock)

    def test_actions_module_comes_first_in_the_mock(self):
        mock = (UI / "js" / "mock.js").read_text("utf-8")
        orders = {m.group(1): int(m.group(2)) for m in re.finditer(r"\{ id: '(files|actions)', package: '[a-z_]+', title: '[^']+', description: '[^']+', order: (\d+)", mock)}
        self.assertLess(orders["actions"], orders["files"])


def css_rule(css: str, selector: str) -> str:
    """The body of the first rule whose selector list is exactly `selector`."""
    match = re.search(r"(?:^|\}|\*/)\s*" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else ""


class SheetBoundTests(unittest.TestCase):
    """A fit-content sheet must bound its `.ov-content` itself, or long content clips the buttons (HOST_UI.md 7);
    the layout cannot run here, so the rules that carry the bound are pinned."""

    def test_update_sheet_keeps_its_buttons_with_long_notes(self):
        css = (UI / "vendor" / "liquid_ui" / "css" / "components.css").read_text("utf-8")
        self.assertIn("max-height: var(--sheet-max-h)", css_rule(css, ".upd-sheet .ov-content"))
        self.assertIn("height: auto", css_rule(css, ".upd-sheet .ov-content"))
        self.assertIn("flex: none", css_rule(css, ".upd-sheet-head,\n.upd-sheet-foot"))
        self.assertIn("overflow-y: auto", css_rule(css, ".upd-sheet-foot .upd-msg"))
        self.assertIn("min-height: 96px", css_rule(css, ".upd-sheet-scroll"))
        self.assertIn("--sheet-max-h: calc(100vh - 2 * var(--titlebar-h))", css_rule(css, ".upd-sheet"))
        self.assertNotIn(".confirm-sheet", css, "the app sheet is bounded in app.css, not in the kit copy")

    def test_confirm_sheet_has_the_same_bound(self):
        css = (UI / "css" / "app.css").read_text("utf-8")
        self.assertIn("--sheet-max-h: calc(100vh - 2 * var(--titlebar-h))", css_rule(css, ".confirm-sheet"))
        self.assertIn("max-height: var(--sheet-max-h)", css_rule(css, ".confirm-sheet"))
        self.assertIn("max-height: var(--sheet-max-h)", css_rule(css, ".confirm-sheet .ov-content"))
        self.assertIn("overflow-y: auto", css_rule(css, ".cf-question"))
        self.assertIn("flex: none", css_rule(css, ".cf-btns"))

    def test_mock_offers_long_release_notes(self):
        mock = (UI / "js" / "mock.js").read_text("utf-8")
        self.assertIn("params.get('notes') === 'long'", mock)


if __name__ == "__main__":
    unittest.main()
