"""Tests for the unified control-center dashboard (step 20).

Covers the pure formatting helpers + mode classification (no Qt needed) and,
where Qt is available, the live DashboardPage slots (traffic rates, total
usage, tunnel/proxy badge, resilience strip, sparkline buffering, idle reset)
and the Sparkline rolling-window widget.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ui.window import fmt_bytes, fmt_rate, mode_kind

try:
    from PySide6.QtWidgets import QApplication
    from ui.theme import get_palette
    from ui.window import DashboardPage
    from ui.widgets import Sparkline
    _HAVE_QT = True
except Exception:                                   # pragma: no cover
    _HAVE_QT = False

_app = None


def setUpModule():
    global _app
    if _HAVE_QT:
        _app = QApplication.instance() or QApplication([])


class FormatHelpersTest(unittest.TestCase):
    def test_fmt_bytes_scales(self):
        self.assertEqual(fmt_bytes(0), "0 B")
        self.assertEqual(fmt_bytes(512), "512 B")
        self.assertEqual(fmt_bytes(2048), "2.0 KB")
        self.assertEqual(fmt_bytes(1048576), "1.0 MB")
        self.assertEqual(fmt_bytes(1073741824), "1.0 GB")

    def test_fmt_rate_appends_per_second(self):
        self.assertTrue(fmt_rate(2048).endswith("/s"))
        self.assertEqual(fmt_rate(1048576), "1.0 MB/s")

    def test_mode_kind_classifies_tunnel_vs_proxy(self):
        # only Tunnel / SNI Only remain (#5)
        self.assertEqual(mode_kind("Tunnel"), "tunnel")
        self.assertEqual(mode_kind("SNI Only"), "proxy")
        self.assertEqual(mode_kind(""), "proxy")


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class SparklineTest(unittest.TestCase):
    def test_rolling_window_capacity(self):
        s = Sparkline(capacity=10)
        for i in range(25):
            s.push(float(i), float(i))
        self.assertEqual(len(s._down), 10)
        self.assertEqual(len(s._up), 10)
        # keeps the most-recent samples
        self.assertEqual(s._down[-1], 24.0)
        self.assertEqual(s._down[0], 15.0)

    def test_peak_and_clear(self):
        s = Sparkline(capacity=8)
        s.push(100.0, 50.0)
        s.push(40.0, 200.0)
        self.assertEqual(s._peak(), 200.0)
        s.clear()
        self.assertEqual(s._peak(), 0.0)
        self.assertEqual(len(s._down), 0)

    def test_min_capacity_floor(self):
        self.assertGreaterEqual(Sparkline(capacity=1)._cap, 8)


@unittest.skipUnless(_HAVE_QT, "PySide6 / Qt platform unavailable")
class DashboardPageTest(unittest.TestCase):
    def _page(self):
        return DashboardPage(get_palette("dark"))

    def test_traffic_updates_rates_total_and_spark(self):
        d = self._page()
        d.on_traffic(2048, 1048576, 1500.0, 320000.0)
        self.assertIn("KB/s", d.rate_down.text())
        self.assertTrue(d.rate_down.text().startswith("↓"))
        self.assertTrue(d.rate_up.text().startswith("↑"))
        # total shows down / up
        self.assertEqual(d.stat_total.value_label.text(), "1.0 MB / 2.0 KB")
        self.assertEqual(len(d.spark._down), 1)

    def test_mode_badge_flips(self):
        d = self._page()
        d.set_mode("Tunnel")
        self.assertEqual(d.mode_badge.property("kind"), "tunnel")
        self.assertEqual(d.mode_badge.text(), "تونل کامل")
        d.set_mode("SNI Only")
        self.assertEqual(d.mode_badge.property("kind"), "proxy")
        self.assertEqual(d.mode_badge.text(), "پروکسی محلی")

    def test_resilience_strip(self):
        d = self._page()
        d.set_resilience("RST 2/10 · زنجیره fake_ttl")
        self.assertIn("RST 2/10", d.lbl_resilience.text())
        self.assertTrue(d.lbl_resilience.text().startswith("تاب‌آوری:"))

    def test_idle_resets_live_picture(self):
        d = self._page()
        d.on_traffic(2048, 1048576, 1500.0, 320000.0)
        d.set_resilience("RST 2/10")
        d.set_status("idle")
        self.assertEqual(len(d.spark._down), 0)
        self.assertEqual(d.rate_down.text(), "↓ 0 B/s")
        self.assertEqual(d.rate_up.text(), "↑ 0 B/s")
        self.assertIn("—", d.lbl_resilience.text())


if __name__ == "__main__":
    unittest.main()
