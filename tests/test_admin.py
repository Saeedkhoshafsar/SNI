"""Tests for the admin-elevation helper (step 13).

The Win32 ``ShellExecuteW`` / ``IsUserAnAdmin`` calls are platform-specific, so
:mod:`core.admin` factors the *decision* logic out into pure functions that take
injectable hooks. Here we exercise that logic on any OS:

* ``is_admin`` honours an injected checker and reports True off-Windows.
* ``relaunch_params`` strips ``argv[0]`` for a frozen build but keeps it in dev.
* ``ensure_admin`` only relaunches on Windows + non-admin, and routes the call
  through the injected *runner* so no real elevation occurs during tests.

Runs both standalone (``python tests/test_admin.py``) and under pytest.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import core.admin as admin


class TestPredicates(unittest.TestCase):
    def test_is_windows_matches_platform(self):
        self.assertEqual(admin.is_windows(), sys.platform.startswith("win"))

    def test_is_admin_uses_injected_checker(self):
        self.assertTrue(admin.is_admin(checker=lambda: True))
        self.assertFalse(admin.is_admin(checker=lambda: False))

    def test_is_admin_truthy_coercion(self):
        # checker returning 1/0 should coerce to bool
        self.assertTrue(admin.is_admin(checker=lambda: 1))
        self.assertFalse(admin.is_admin(checker=lambda: 0))

    @unittest.skipIf(sys.platform.startswith("win"), "non-Windows behaviour")
    def test_is_admin_true_off_windows_without_checker(self):
        self.assertTrue(admin.is_admin())


class TestRelaunchParams(unittest.TestCase):
    def test_frozen_strips_argv0(self):
        argv = ["MyApp.exe", "--foo", "bar baz"]
        exe, params = admin.relaunch_params(argv, frozen=True)
        self.assertEqual(exe, sys.executable)
        # argv[0] (the exe itself) is dropped; remaining args are quoted
        self.assertNotIn("MyApp.exe", params)
        self.assertIn("--foo", params)
        # space-containing arg must be quoted
        self.assertIn('"bar baz"', params)

    def test_dev_keeps_full_argv(self):
        argv = ["script.py", "--theme", "dark"]
        exe, params = admin.relaunch_params(argv, frozen=False)
        self.assertEqual(exe, sys.executable)
        self.assertIn("script.py", params)
        self.assertIn("--theme", params)
        self.assertIn("dark", params)

    def test_empty_args_frozen(self):
        exe, params = admin.relaunch_params(["app.exe"], frozen=True)
        self.assertEqual(params, "")

    def test_list2cmdline_quotes_spaces(self):
        out = admin.subprocess_list2cmdline(["a", "b c"])
        self.assertIn('"b c"', out)


class TestEnsureAdmin(unittest.TestCase):
    def test_noop_off_windows(self):
        calls = []
        triggered = admin.ensure_admin(
            ["app.exe"],
            is_admin_checker=lambda: False,
            runner=lambda e, p: calls.append((e, p)),
        )
        if admin.is_windows():
            # On Windows + not admin → relaunch must trigger
            self.assertTrue(triggered)
            self.assertEqual(len(calls), 1)
        else:
            # Off Windows → never relaunch regardless of checker
            self.assertFalse(triggered)
            self.assertEqual(calls, [])

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-only path")
    def test_relaunch_when_not_admin(self):
        calls = []
        triggered = admin.ensure_admin(
            ["app.exe", "--x"],
            is_admin_checker=lambda: False,
            runner=lambda e, p: calls.append((e, p)),
        )
        self.assertTrue(triggered)
        self.assertEqual(len(calls), 1)

    @unittest.skipUnless(sys.platform.startswith("win"), "Windows-only path")
    def test_no_relaunch_when_already_admin(self):
        calls = []
        triggered = admin.ensure_admin(
            ["app.exe"],
            is_admin_checker=lambda: True,
            runner=lambda e, p: calls.append((e, p)),
        )
        self.assertFalse(triggered)
        self.assertEqual(calls, [])

    def test_runner_receives_relaunch_params(self):
        # Force the Windows code path via monkeypatching is_windows so the
        # decision logic is exercised on any OS.
        orig = admin.is_windows
        admin.is_windows = lambda: True
        try:
            captured = {}

            def runner(exe, params):
                captured["exe"] = exe
                captured["params"] = params

            triggered = admin.ensure_admin(
                ["app.exe", "--theme", "light"],
                is_admin_checker=lambda: False,
                runner=runner,
            )
            self.assertTrue(triggered)
            self.assertEqual(captured["exe"], sys.executable)
            self.assertIn("--theme", captured["params"])
            self.assertIn("light", captured["params"])
        finally:
            admin.is_windows = orig

    def test_already_admin_no_runner_call(self):
        orig = admin.is_windows
        admin.is_windows = lambda: True
        try:
            calls = []
            triggered = admin.ensure_admin(
                ["app.exe"],
                is_admin_checker=lambda: True,
                runner=lambda e, p: calls.append(1),
            )
            self.assertFalse(triggered)
            self.assertEqual(calls, [])
        finally:
            admin.is_windows = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
