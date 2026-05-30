"""Tests for the structured log buffer + classification (step 23).

All pure logic — runs on any OS without Qt.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.logbuffer import (
    LEVELS, LogBuffer, LogEntry, classify, matches,
)


class ClassifyTest(unittest.TestCase):
    def test_error_keywords(self):
        self.assertEqual(classify("خطا در توقف xray"), "err")
        self.assertEqual(classify("تنظیم پروکسی سیستم ناموفق: boom"), "err")
        self.assertEqual(classify("connection failed"), "err")

    def test_warn_keywords(self):
        self.assertEqual(classify("throttle detected, retrying"), "warn")
        self.assertEqual(classify("در SNI Only نادیده گرفته شد"), "warn")

    def test_ok_keywords(self):
        self.assertEqual(classify("✓ اتصال برقرار شد"), "ok")
        self.assertEqual(classify("پروکسی سیستم روشن شد → 127.0.0.1:10809"), "ok")

    def test_plain_is_info(self):
        self.assertEqual(classify("منتظر شروع تونل…"), "info")
        self.assertEqual(classify(""), "info")

    def test_error_beats_warn_and_ok(self):
        # a line that mentions both success and failure → most severe wins
        self.assertEqual(classify("اتصال برقرار شد ولی خطا داشت"), "err")


class LogEntryTest(unittest.TestCase):
    def test_format_includes_level_and_stamp(self):
        e = LogEntry("hello", level="ok", ts=0)
        out = e.format()
        self.assertIn("OK", out)
        self.assertIn("hello", out)
        self.assertTrue(out.startswith("["))
        # stamp is HH:MM:SS
        self.assertRegex(e.stamp, r"^\d{2}:\d{2}:\d{2}$")


class MatchesTest(unittest.TestCase):
    def _e(self, msg, lvl):
        return LogEntry(msg, level=lvl, ts=0)

    def test_level_filter(self):
        e = self._e("boom", "err")
        self.assertTrue(matches(e, level="all"))
        self.assertTrue(matches(e, level="err"))
        self.assertFalse(matches(e, level="info"))

    def test_query_substring_case_insensitive(self):
        e = self._e("Xray started OK", "ok")
        self.assertTrue(matches(e, query="xray"))
        self.assertTrue(matches(e, query="STARTED"))
        self.assertFalse(matches(e, query="warp"))

    def test_combined_level_and_query(self):
        e = self._e("warp throttled", "warn")
        self.assertTrue(matches(e, level="warn", query="warp"))
        self.assertFalse(matches(e, level="err", query="warp"))


class LogBufferTest(unittest.TestCase):
    def test_add_classifies_and_counts(self):
        b = LogBuffer()
        b.add("✓ اتصال برقرار شد")          # ok
        b.add("خطا در توقف xray")            # err
        b.add("منتظر…")                      # info
        self.assertEqual(b.counts["ok"], 1)
        self.assertEqual(b.counts["err"], 1)
        self.assertEqual(b.counts["info"], 1)
        self.assertEqual(b.counts["warn"], 0)
        self.assertEqual(len(b), 3)

    def test_explicit_level_overrides_classify(self):
        b = LogBuffer()
        e = b.add("plain text", level="warn")
        self.assertEqual(e.level, "warn")
        self.assertEqual(b.counts["warn"], 1)

    def test_capacity_evicts_oldest_and_updates_counts(self):
        b = LogBuffer(capacity=3)
        b.add("a", level="info")
        b.add("b", level="err")
        b.add("c", level="info")
        b.add("d", level="ok")   # evicts "a" (info)
        self.assertEqual(len(b), 3)
        self.assertEqual(b.counts["info"], 1)  # was 2, one evicted
        self.assertEqual(b.counts["err"], 1)
        self.assertEqual(b.counts["ok"], 1)
        msgs = [e.message for e in b.entries]
        self.assertEqual(msgs, ["b", "c", "d"])

    def test_clear_resets_counts(self):
        b = LogBuffer()
        b.add("x", level="err")
        b.clear()
        self.assertEqual(len(b), 0)
        self.assertTrue(all(v == 0 for v in b.counts.values()))

    def test_filtered_applies_level_and_query(self):
        b = LogBuffer()
        b.add("warp connected", level="ok")
        b.add("warp throttled", level="warn")
        b.add("xray failed", level="err")
        only_warp = b.filtered(query="warp")
        self.assertEqual(len(only_warp), 2)
        only_err = b.filtered(level="err")
        self.assertEqual(len(only_err), 1)
        self.assertEqual(only_err[0].message, "xray failed")

    def test_counts_summary_covers_all_levels(self):
        b = LogBuffer()
        b.add("x", level="err")
        summary = b.counts_summary()
        for lv in LEVELS:
            self.assertIn(lv, summary)
        self.assertIn("err 1", summary)


if __name__ == "__main__":
    unittest.main()
