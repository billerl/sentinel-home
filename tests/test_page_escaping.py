"""The web pages' esc() helpers must escape quotes: their output goes inside HTML attribute values, and names
come from people and from outside (a Meshtastic radio's name reached the live page; bug hunt 2026-11-13)."""

import _test_env  # noqa: F401  (isolates the event log)

import glob
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sentinel", "web")
PAGES = sorted(glob.glob(os.path.join(ROOT, "templates", "*.html")) + glob.glob(os.path.join(ROOT, "static", "*.js")))


class TestPageEscaping(unittest.TestCase):
    def test_every_esc_escapes_quotes(self):
        found = 0
        for path in PAGES:
            src = open(path, encoding="utf-8").read()
            for m in re.finditer(r"function esc\(\w\) \{", src):
                body = src[m.end():m.end() + 300].split("}[c]", 1)[0]  # (up to the end of its lookup table)
                found += 1
                self.assertNotIn("textContent", body, f"{path}: esc() built on textContent doesn't escape quotes")
                self.assertRegex(body, r"replace\(/\[&<>\"'\]/g", f"{path}: esc() should escape & < > \" and '")
        self.assertGreater(found, 10)

    def test_map_labels_are_not_raw_names(self):
        for path in PAGES:
            src = open(path, encoding="utf-8").read()
            self.assertIsNone(re.search(r"bindTooltip\(\s*p\.name\b", src), f"{path}: a name as raw HTML tooltip")


if __name__ == "__main__":
    unittest.main()
