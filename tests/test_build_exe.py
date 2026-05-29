"""Tests for the OS-agnostic parts of the build helper (step 13).

We don't run PyInstaller here (it's Windows-only and heavy); instead we verify
that ``scripts/build_exe.py`` imports cleanly on any OS and that its
``preflight`` diagnostics behave sensibly: off-Windows it must refuse, and it
must flag a missing PyInstaller. Also confirms the .spec file is syntactically
valid Python and references the expected entry point + icon, and that the
generated .ico asset exists and has the right magic bytes.
"""
import ast
import importlib.util
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_build_module():
    path = os.path.join(ROOT, "scripts", "build_exe.py")
    spec = importlib.util.spec_from_file_location("build_exe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBuildHelper(unittest.TestCase):
    def setUp(self):
        self.mod = _load_build_module()

    def test_imports_and_has_entrypoints(self):
        for fn in ("preflight", "ensure_icon", "clean", "build", "main"):
            self.assertTrue(callable(getattr(self.mod, fn)), fn)

    def test_preflight_off_windows_refuses(self):
        problems = self.mod.preflight()
        if not sys.platform.startswith("win"):
            self.assertTrue(any("Windows" in p for p in problems))

    def test_preflight_flags_missing_pyinstaller(self):
        if not self.mod._have_pyinstaller():
            problems = self.mod.preflight()
            self.assertTrue(any("PyInstaller" in p for p in problems))

    def test_paths_point_into_project(self):
        self.assertTrue(self.mod.SPEC.endswith("SNISpoofer.spec"))
        self.assertTrue(
            self.mod.ICON.endswith(os.path.join("assets", "app.ico")))


class TestSpecFile(unittest.TestCase):
    def test_spec_parses(self):
        path = os.path.join(ROOT, "SNISpoofer.spec")
        with open(path, "r", encoding="utf-8") as fp:
            src = fp.read()
        ast.parse(src)  # raises on syntax error
        self.assertIn('"app.py"', src)
        self.assertIn("console=False", src)
        self.assertIn('name="SNISpoofer"', src)
        self.assertIn("collect_dynamic_libs", src)  # pydivert/WinDivert


class TestIconAsset(unittest.TestCase):
    def test_icon_exists_and_is_ico(self):
        ico = os.path.join(ROOT, "assets", "app.ico")
        self.assertTrue(os.path.isfile(ico))
        with open(ico, "rb") as fp:
            header = fp.read(4)
        # ICO files start with 00 00 01 00
        self.assertEqual(header[:4], b"\x00\x00\x01\x00")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestReleaseWorkflow(unittest.TestCase):
    """Guard the CI workflow against regressing to the old tkinter entrypoint.

    The canonical workflow lives at ``ci/release.yml`` (a regular file the bot
    is allowed to push). The user copies it into ``.github/workflows/`` — see
    BUILD.md — because GitHub Apps can't push workflow files directly.
    """

    def _wf(self):
        path = os.path.join(ROOT, "ci", "release.yml")
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read()

    def test_workflow_uses_spec_not_old_gui(self):
        wf = self._wf()
        self.assertIn("SNISpoofer.spec", wf)
        # must NOT build the archived tkinter GUI
        self.assertNotIn("gui.py", wf)
        self.assertNotIn("gui_old2", wf)

    def test_workflow_uploads_artifact(self):
        wf = self._wf()
        self.assertIn("upload-artifact", wf)

    def test_old_gui_is_archived(self):
        # old GUIs live under legacy/, not at project root
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "gui.py")))
        self.assertFalse(os.path.isfile(os.path.join(ROOT, "gui_old2.py")))
        self.assertTrue(os.path.isfile(os.path.join(ROOT, "legacy", "gui.py")))
