"""Tests for the top-level entry point app.py (step 13).

Verifies the thin launcher logic without starting Qt:
* ``_parse_theme`` understands ``--theme x`` and ``--theme=x``.
* ``main`` short-circuits (returns 0, never touches Qt) when ``ensure_admin``
  reports a relaunch was triggered — we monkeypatch the admin layer.
* When no relaunch happens, ``main`` forwards the parsed theme to the Qt main.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import app
import core.admin as admin


class TestParseTheme(unittest.TestCase):
    def test_space_form(self):
        self.assertEqual(app._parse_theme(["x", "--theme", "dark"]), "dark")

    def test_equals_form(self):
        self.assertEqual(app._parse_theme(["x", "--theme=light"]), "light")

    def test_absent(self):
        self.assertIsNone(app._parse_theme(["x"]))


class TestMain(unittest.TestCase):
    def setUp(self):
        self._orig_ensure = admin.ensure_admin

    def tearDown(self):
        admin.ensure_admin = self._orig_ensure
        sys.modules.pop("ui.app_qt", None)

    def test_short_circuits_on_relaunch(self):
        admin.ensure_admin = lambda argv: True  # pretend we relaunched elevated
        rc = app.main(["app.exe"])
        self.assertEqual(rc, 0)

    def test_forwards_theme_to_qt(self):
        admin.ensure_admin = lambda argv: False
        captured = {}

        # Inject a fake ui.app_qt so we never spin up a real QApplication.
        import types
        fake = types.ModuleType("ui.app_qt")

        def fake_main(theme=None):
            captured["theme"] = theme
            return 42

        fake.main = fake_main
        sys.modules["ui.app_qt"] = fake

        rc = app.main(["app.exe", "--theme", "dark"])
        self.assertEqual(rc, 42)
        self.assertEqual(captured["theme"], "dark")


if __name__ == "__main__":
    unittest.main(verbosity=2)
