"""The host window's type scale: every text is a named role of the kit copy's tokens.css (docs/HOST_UI.md 7).

`font:` shorthands and `font-size:` with a length live in tokens.css only; every other rule of the window's
stylesheets and scripts names a role (`font: var(--t-<role>)`), and every role it names is defined.
"""

import re
import unittest
from pathlib import Path

UI = Path(__file__).resolve().parents[1] / "src" / "premiere_deck" / "ui"
TOKENS = UI / "vendor" / "liquid_ui" / "css" / "tokens.css"
ROLES = ("display", "title", "section", "body", "label", "label-s", "caption", "mono", "mono-s")
AD_HOC_FONT = re.compile(r"font\s*:\s*(?!var\(--t-)[^;]*\d+(\.\d+)?px", re.IGNORECASE)
AD_HOC_SIZE = re.compile(r"font-size\s*:\s*\d+(\.\d+)?px", re.IGNORECASE)
ROLE_USE = re.compile(r"var\(--t-([a-z-]+)\)")
ROLE_DEF = re.compile(r"--t-([a-z-]+)\s*:")
# Motion tokens share the prefix (--t-fast, --t-med) and are not type roles.
NOT_ROLES = {"fast", "med"}


def _sources():
    for pattern in ("**/*.css", "**/*.js", "*.html"):
        for path in sorted(UI.glob(pattern)):
            if path != TOKENS:
                yield path


class TypeScaleTest(unittest.TestCase):
    def test_tokens_define_the_roles(self):
        defined = set(ROLE_DEF.findall(TOKENS.read_text(encoding="utf-8")))
        self.assertEqual([r for r in ROLES if r not in defined], [])

    def test_no_ad_hoc_font_sizes(self):
        hits = []
        for path in _sources():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if AD_HOC_FONT.search(line) or AD_HOC_SIZE.search(line):
                    hits.append(f"{path.relative_to(UI)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])

    def test_only_known_roles(self):
        hits = []
        for path in _sources():
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for role in ROLE_USE.findall(line):
                    if role not in ROLES and role not in NOT_ROLES:
                        hits.append(f"{path.relative_to(UI)}:{n}: --t-{role}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
