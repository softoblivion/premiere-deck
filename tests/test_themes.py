"""The two project themes: ui/themes.json, the generated CSS (build/gen_themes.py) and text contrast.

docs/HOST_UI.md 7, docs/PANEL.md 3, docs/ARCHITECTURE.md 3.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UI = ROOT / "src" / "premiere_deck" / "ui"
KIT = UI / "vendor" / "liquid_ui"
PANEL = ROOT / "src" / "premiere_deck" / "panel"


def _load_generator():
    spec = importlib.util.spec_from_file_location("gen_themes", ROOT / "build" / "gen_themes.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_generator()

# Tokens every theme must define (docs/HOST_UI.md 7, docs/PANEL.md 3).
REQUIRED = (
    "--bg-top", "--bg-mid", "--bg-bottom", "--scene-hue", "--scene-accent", "--scene-line", "--accent",
    "--accent-ink", "--accent-grad", "--accent-grad-a", "--ink-1", "--ink-2", "--ink-3", "--ink-4", "--tint-clear",
    "--tint-regular", "--tint-sheet", "--well", "--fill-1", "--fill-2", "--fill-3", "--fill-4", "--stroke-1",
    "--stroke-2", "--shadow-s", "--shadow-m", "--shadow-l", "--mint", "--amber", "--rose", "--rose-ink", "--ok-ink",
    "--warn-ink", "--error-ink", "--hero-ink", "--rim-tone", "--line-soft", "--shade-tone", "--card-fill",
    "--card-rim", "--scene-vignette", "--scene-fields-o",
)


class ThemeFileTests(unittest.TestCase):
    def setUp(self):
        self.data = gen.load()

    def test_two_themes_black_is_default(self):
        self.assertEqual([t["name"] for t in self.data["themes"]], ["black", "sky"])
        self.assertEqual(self.data["default"], "black")
        labels = {t["name"]: (t["label"], t["scheme"]) for t in self.data["themes"]}
        self.assertEqual(labels, {"black": ("Crystal Black", "dark"), "sky": ("Crystal Sky", "light")})

    def test_file_is_consistent(self):
        self.assertEqual(gen.problems(self.data), [])

    def test_every_token_in_both_themes(self):
        black, sky = (gen.theme_tokens(t) for t in self.data["themes"])
        self.assertEqual(set(black), set(sky))
        for key in REQUIRED:
            self.assertIn(key, black)

    def test_derived_matches_derive(self):
        for t in self.data["themes"]:
            self.assertEqual(t["derived"], gen.derive(t["hue"], t["accent"]), t["name"])

    def test_derive_port_matches_known_values(self):
        # Values the kit's JavaScript derive() produced for the 1.x Dusk theme (css/themes.css of kit 3.1.0).
        d = gen.derive("#2a5bd8", "#ff7a59")
        self.assertEqual(d["--accent-2"], "#a66d8e")
        self.assertEqual(d["--accent-grad-a"], "linear-gradient(135deg, rgba(255, 151, 126, 0.74) 0%, "
                                                "rgba(255, 122, 89, 0.74) 48%, rgba(166, 109, 142, 0.78) 100%)")
        self.assertEqual(d["--bg-bottom"], "#0b1838")
        self.assertEqual(d["--tint-regular"], "rgba(8, 16, 39, 0.3)")

    def test_problems_are_found(self):
        broken = gen.load()
        broken["themes"][1]["tokens"].pop("--line-soft")
        broken["themes"][0]["derived"]["--accent"] = "#000000"
        found = " ".join(gen.problems(broken))
        self.assertIn("token set differs", found)
        self.assertIn("derive(", found)

    def test_body_text_contrast_at_least_4_5(self):
        for t in self.data["themes"]:
            for row in gen.contrast_report(gen.theme_tokens(t)):
                self.assertGreaterEqual(row["min"], 4.5, f"{t['name']} {row['ink']} over {row['worst']}")

    def test_contrast_math(self):
        self.assertAlmostEqual(gen.ratio((255, 255, 255, 1), (0, 0, 0, 1)), 21.0, places=3)
        self.assertEqual(gen.parse_color("rgba(9, 22, 45, 0.94)"), (9.0, 22.0, 45.0, 0.94))
        self.assertEqual(gen.parse_color("#ffffff"), (255, 255, 255, 1.0))
        self.assertEqual(gen.over((0, 0, 0, 0.5), (200, 100, 50, 1)), (100.0, 50.0, 25.0, 1.0))


class GeneratedFilesTests(unittest.TestCase):
    def test_generated_files_are_current(self):
        data = gen.load()
        for path, text in gen.outputs(data).items():
            self.assertEqual(path.read_text("utf-8"), text, f"{path} is out of date: run python build/gen_themes.py")

    def test_check_mode_passes(self):
        self.assertEqual(gen.main(["--check"]), 0)

    def test_panel_block_has_both_themes_and_the_font(self):
        css = (PANEL / "css" / "panel.css").read_text("utf-8")
        start, end = css.index(gen.BEGIN_MARK), css.index(gen.END_MARK)
        block = css[start:end]
        self.assertIn(':root,\n:root[data-theme="black"] {', block)
        self.assertIn(':root[data-theme="sky"] {', block)
        self.assertIn('url("../fonts/Sora-latin.woff2")', block)
        self.assertTrue((PANEL / "fonts" / "Sora-latin.woff2").is_file())
        self.assertTrue((PANEL / "fonts" / "LICENSE-Sora.txt").is_file())
        self.assertFalse(list((PANEL / "fonts").glob("Unbounded*")))

    def test_panel_block_carries_the_type_roles_of_the_host(self):
        css = (PANEL / "css" / "panel.css").read_text("utf-8")
        block = css[css.index(gen.BEGIN_MARK):css.index(gen.END_MARK)]
        tokens = (KIT / "css" / "tokens.css").read_text("utf-8")
        for role in gen.TYPE_ROLES:
            self.assertIn(f"--t-{role}", gen.TYPE_TOKENS)
        for name in gen.TYPE_TOKENS:
            kit = re.search(r"^\s*" + re.escape(name) + r"\s*:\s*([^;]+);", tokens, re.M)
            panel = re.search(r"^\s*" + re.escape(name) + r"\s*:\s*([^;]+);", block, re.M)
            self.assertIsNotNone(kit, f"{name} missing in tokens.css")
            self.assertIsNotNone(panel, f"{name} missing in the panel block")
            self.assertEqual(panel.group(1).strip(), kit.group(1).strip(), name)
        # The pixel sizes of the roles, as the kit documents them (LIQUID_UI.md, Typography).
        sizes = {role: re.search(r"(\d+(?:\.\d+)?)px", gen.type_tokens()[f"--t-{role}"]).group(1) for role in gen.TYPE_ROLES}
        self.assertEqual(sizes, {"display": "20", "title": "16", "section": "11", "body": "13", "label": "12.5",
                                 "label-s": "11.5", "caption": "11.5", "mono": "12", "mono-s": "11"})

    def test_panel_css_has_no_ad_hoc_font_size(self):
        # Below the generated block every text names a role: no `font-size: <n>px`, no `font:` shorthand with a pixel
        # size, and every role it names exists.
        css = (PANEL / "css" / "panel.css").read_text("utf-8")
        rest = css[css.index(gen.END_MARK) + len(gen.END_MARK):]
        ad_hoc = re.compile(r"font-size\s*:\s*\d+(\.\d+)?px|font\s*:\s*(?!var\(--t-)[^;]*\d+(\.\d+)?px", re.I)
        hits = [line.strip() for line in rest.splitlines() if ad_hoc.search(line)]
        self.assertEqual(hits, [])
        used = set(re.findall(r"var\(--t-([a-z-]+)\)", rest))
        self.assertTrue(used, "panel.css names no type role")
        self.assertEqual(sorted(used - set(gen.TYPE_ROLES)), [])

    def test_splice_inserts_and_replaces(self):
        block = gen.BEGIN_MARK + "\nX\n" + gen.END_MARK
        first = gen.splice("/* head */\nbody {}\n", block)
        self.assertTrue(first.startswith("/* head */\n\n" + block))
        again = gen.splice(first, gen.BEGIN_MARK + "\nY\n" + gen.END_MARK)
        self.assertIn("\nY\n", again)
        self.assertNotIn("\nX\n", again)
        self.assertTrue(again.endswith("body {}\n"))


class KitCopyTests(unittest.TestCase):
    def test_theme_js_lists_the_json_themes(self):
        js = (KIT / "js" / "theme.js").read_text("utf-8")
        found = re.findall(r"\{ name: '([a-z]+)', label: '([^']+)', scheme: '([a-z]+)', hue: '(#[0-9a-f]{6})', "
                           r"accent: '(#[0-9a-f]{6})' \}", js)
        expected = [(t["name"], t["label"], t["scheme"], t["hue"], t["accent"]) for t in gen.load()["themes"]]
        self.assertEqual(found, expected)
        self.assertIn("export const DEFAULT_THEME = 'black';", js)

    def test_sora_is_the_only_text_face(self):
        tokens = (KIT / "css" / "tokens.css").read_text("utf-8")
        text = gen.load()["fonts"]["text"]
        self.assertIn(f'font-family: "{text["family"]}";', tokens)
        self.assertIn(f'url("../fonts/{text["file"]}")', tokens)
        self.assertNotIn("Unbounded", tokens)
        self.assertTrue((KIT / "fonts" / text["file"]).is_file())
        self.assertTrue((KIT / "fonts" / "LICENSE-Sora.txt").is_file())
        self.assertFalse(list((KIT / "fonts").glob("Unbounded*")))
        self.assertEqual((KIT / "fonts" / text["file"]).read_bytes(), (PANEL / "fonts" / text["file"]).read_bytes())

    def test_tokens_css_holds_no_theme_colour(self):
        tokens = (KIT / "css" / "tokens.css").read_text("utf-8")
        names = set(gen.theme_tokens(gen.load()["themes"][0]))
        declared = set(re.findall(r"^\s*(--[a-z0-9-]+):", tokens, re.M))
        self.assertEqual(declared & names, set())

    def test_every_theme_token_used_by_the_kit_css_is_defined(self):
        names = set(gen.theme_tokens(gen.load()["themes"][0]))
        tokens = (KIT / "css" / "tokens.css").read_text("utf-8")
        names |= set(re.findall(r"^\s*(--[a-z0-9-]+):", tokens, re.M))
        css = "".join((KIT / "css" / f).read_text("utf-8") for f in ("base.css", "components.css", "glass.css", "scene.css"))
        css += (UI / "css" / "app.css").read_text("utf-8")
        local = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        used = set(re.findall(r"var\((--[a-z0-9-]+)", css))
        # Set inline by the kit's and the app's JavaScript (scene fields, springs, levels, light angle, grid).
        runtime = {"--a", "--anim", "--b", "--c", "--cols", "--d", "--delay", "--k-accent", "--la", "--lg-k", "--lift",
                   "--lines-o", "--p", "--rim-w", "--s", "--x", "--y"}
        self.assertEqual(sorted(used - names - local - runtime), [])

    def test_no_colour_literal_in_app_css(self):
        css = (UI / "css" / "app.css").read_text("utf-8")
        self.assertEqual(re.findall(r"rgba?\(\s*\d|#[0-9a-fA-F]{3,8}\b", css), [])

    def test_first_paint_accepts_both_themes(self):
        html = (UI / "index.html").read_text("utf-8")
        self.assertIn('data-theme="black"', html)
        self.assertIn('["black","sky"]', html)
        self.assertTrue((KIT / "KIT.txt").is_file())
        self.assertFalse((KIT / "VENDORED.txt").exists())


if __name__ == "__main__":
    unittest.main()
