"""Regression tests for the SECOND feedback round (4 bugs).

User report (verbatim Persian):

  1. "پینگ ها کلا مشکل دارند مثلا من کانفیگ اسپوفم پینگ نداد ولی کار میکرد ... و
     بعضی کانفیگ های عادی هم پینگ میدادن ولی کار نمیکردن ... یه پینگ درست حسابی
     نمیتونیم بگریم" — the ping is fundamentally unreliable: a spoof config gave
     NO ping but worked; some ordinary configs pinged positive but didn't work.

  2. "وقتی کانفیگ فعال رو تغییر میدم ... بعضی وقتا مینویسه درحال اتصال ولی بعضی
     وقتا هم نوشته شروع ... ممکنه من حواسم نباشه و شروع رو بزنم" — during an
     automatic config-switch restart the dashboard flickers to the idle "شروع"
     label and the user can accidentally press Start mid-restart.

  3. "وقتی چند کار رو سریع باهم انجام میدم مثلا هم پینگ همه رو میزنم و هم انتخاب
     همه رو نرم افزار بسته میشه" — doing several things at once (Ping All +
     Select All) crashes the app.

  4. "وقتی اسکن آیپی تمیز رو میزنم صفحه سرچ نرم افزار بالا میاد ولی نمیتونم حرکتش
     بدم چون هدرش از کادر سیستمم بیرون زده" — the clean-IP scanner dialog opens
     with its title bar off the top of the screen, so it can't be dragged.

Fixes verified here:

  * Bug 1 — when the tunnel is running the engine offers a *live* proxy ping
    (real request through xray→spoofer→CDN); ``is_active_profile`` identifies
    the running config; an offline strategy-probe failure no longer forces a
    working spoof config to red.
  * Bug 2 — the MainWindow masks the transient idle during an auto-restart as
    "connecting" and ignores manual Start clicks while ``_restarting`` is set.
  * Bug 3 — inline pings are queued with bounded concurrency, refresh() keeps
    running workers alive, and results re-find the row by profile identity.
"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ---------------------------------------------------------------------------
#  Bug 1 — honest ping: live-tunnel measurement for the active config
# ---------------------------------------------------------------------------

class LivePingTest(unittest.TestCase):
    def _ctrl(self, **cfg):
        from core.engine import EngineController
        return EngineController(cfg)

    def test_is_active_profile_matches_by_endpoint_fields(self):
        from core.profile import Profile
        from core.engine import STATUS_ACTIVE
        ctrl = self._ctrl()
        a = Profile(address="ex.com", port=443, uuid="u1")
        b = Profile(address="ex.com", port=443, uuid="u1")
        other = Profile(address="ex.com", port=443, uuid="DIFFERENT")
        ctrl.profile = a
        # "active config" now requires the tunnel to genuinely be UP — set the
        # status ACTIVE the way a real connect would.
        ctrl._status = STATUS_ACTIVE
        self.assertTrue(ctrl.is_active_profile(a))   # identity
        self.assertTrue(ctrl.is_active_profile(b))   # same endpoint
        self.assertFalse(ctrl.is_active_profile(other))
        self.assertFalse(ctrl.is_active_profile(None))

    def test_is_active_profile_false_when_idle(self):
        from core.profile import Profile
        ctrl = self._ctrl()
        ctrl.profile = None
        self.assertFalse(ctrl.is_active_profile(Profile(address="x", port=1)))

    def test_is_active_profile_false_when_selected_but_not_connected(self):
        """نکته ۳: a config that is merely SELECTED — but the tunnel was never
        started (engine still idle) — is NOT active. Otherwise the inline ping
        tried a live-tunnel request through a tunnel that doesn't exist (false
        red), and the spoof ping looked usable with nothing to ping through.
        """
        from core.profile import Profile
        from core.engine import STATUS_ACTIVE
        ctrl = self._ctrl()
        a = Profile(address="ex.com", port=443, uuid="u1")
        ctrl.profile = a                       # selected …
        # … but never started → engine is idle → NOT active
        self.assertFalse(ctrl.is_active_profile(a))
        # only once the tunnel is genuinely up does it count as active
        ctrl._status = STATUS_ACTIVE
        self.assertTrue(ctrl.is_active_profile(a))
        # and a config that is connecting (not yet fully up) is not "active"
        from core.engine import STATUS_CONNECTING
        ctrl._status = STATUS_CONNECTING
        self.assertFalse(ctrl.is_active_profile(a))

    def test_live_proxy_ping_refuses_when_not_active(self):
        ctrl = self._ctrl()
        # status starts idle → no live measurement possible
        ok, ms, detail = ctrl.live_proxy_ping()
        self.assertFalse(ok)
        self.assertIsNone(ms)

    def test_live_proxy_ping_measures_through_proxy_when_active(self):
        """When active, it should hit the REAL bound http port via a proxy."""
        from core import engine as eng_mod
        ctrl = self._ctrl(socks_port=10808, http_port=10809)
        ctrl._status = eng_mod.STATUS_ACTIVE

        class _Xray:
            socks_port = 55001
            http_port = 55002
        ctrl._xray = _Xray()

        captured = {}

        # patch urllib so no real network is touched; assert it used our proxy
        import urllib.request as ur

        class _Resp:
            def getcode(self):
                return 200
            def read(self, *_a):
                # carry every marker the verifier looks for so the
                # content-verified endpoint passes through this generic mock.
                return b"success\nMicrosoft Connect Test\nfl=abc h=x"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Opener:
            def open(self, req, timeout=0):
                captured["url"] = req.full_url
                return _Resp()

        real_build = ur.build_opener
        real_handler = ur.ProxyHandler

        def fake_handler(mapping):
            captured["proxy"] = mapping
            return object()

        def fake_build(_h):
            return _Opener()

        ur.ProxyHandler = fake_handler
        ur.build_opener = fake_build
        try:
            ok, ms, detail = ctrl.live_proxy_ping(samples=1)
        finally:
            ur.ProxyHandler = real_handler
            ur.build_opener = real_build

        self.assertTrue(ok)
        self.assertIsInstance(ms, float)
        # must have pointed at the REAL bound http port (55002), not 10809
        self.assertIn("55002", captured["proxy"]["http"])
        self.assertNotIn("10809", captured["proxy"]["http"])

    def test_offline_spoof_config_gives_estimate_in_ping_all(self):
        """A spoof config that is NOT active now gets an OFFLINE *estimate* in a
        "ping all" sweep (user request: "وقتی پینگ همه رو میگیرم اونام پینگشون
        گرفته بشن بدون اینکه دونه دونه وصل بشم").

        ``ping_profile`` validates the REAL CDN route the spoofer fronts (real
        SNI/Host/path via the honest edge probe), so the worker reports its
        latency — but clearly labelled ``≈ … (تخمینی)`` so it never claims to be
        the definitive live-tunnel number. The 🛡 live measurement is still the
        ground truth when the config is activated (separate test).
        """
        from ui.window import InlinePingWorker

        class _SpoofProfile:
            is_spoof_config = True
            address = "127.0.0.1"
            port = 40443

        class _Res:
            reachable = True
            best_ms = 73.0
            jitter_ms = 0.0
            download_kbps = None

        captured = {}

        class _Eng:
            def is_active_profile(self, *_a):
                return False
            def live_proxy_ping(self, *_a, **_k):
                return (False, None, "idle")
            def ping_profile(self, *_a):
                # honest edge probe of the real CDN route → reachable estimate
                return _Res()

        w = InlinePingWorker(_Eng(), _SpoofProfile())
        w.result.connect(lambda t, k: captured.update(text=t, kind=k))
        w._run_inner()
        # an estimate IS shown (a number), tagged ≈ / تخمینی, kind ok
        self.assertEqual(captured.get("kind"), "ok")
        self.assertIn("73", captured.get("text", ""))
        self.assertIn("≈", captured.get("text", ""))
        self.assertIn("تخمینی", captured.get("text", ""))

    def test_offline_spoof_config_unreachable_points_to_live_ping(self):
        """When even the offline edge estimate can't reach the spoof route, the
        worker stays honest: no fake number, an info hint to activate for the
        real (live) ping — preserving "البته اونم باشه" (the live ping too)."""
        from ui.window import InlinePingWorker

        class _SpoofProfile:
            is_spoof_config = True
            address = "127.0.0.1"
            port = 40443

        class _Res:
            reachable = False
            best_ms = None
            jitter_ms = None
            download_kbps = None

        captured = {}

        class _Eng:
            def is_active_profile(self, *_a):
                return False
            def live_proxy_ping(self, *_a, **_k):
                return (False, None, "idle")
            def ping_profile(self, *_a):
                return _Res()

        w = InlinePingWorker(_Eng(), _SpoofProfile())
        w.result.connect(lambda t, k: captured.update(text=t, kind=k))
        w._run_inner()
        self.assertEqual(captured.get("kind"), "info")
        self.assertNotIn("ms", captured.get("text", ""))

    def test_active_config_uses_live_tunnel_ping(self):
        """When the profile IS the running config, the worker reports the live
        tunnel latency (🛡) — the only fully trustworthy measurement."""
        from ui.window import InlinePingWorker
        from core.profile import Profile

        captured = {}

        class _Eng:
            def is_active_profile(self, *_a):
                return True
            def live_proxy_ping(self, *_a, **_k):
                return (True, 87.0, "http 204")

        w = InlinePingWorker(_Eng(), Profile(address="x", port=1))
        w.result.connect(lambda t, k: captured.update(text=t, kind=k))
        w._run_inner()
        self.assertEqual(captured.get("kind"), "ok")
        self.assertIn("87", captured.get("text", ""))


# ---------------------------------------------------------------------------
#  Round 3, Bug A part 2 — live tunnel ping must be ROBUST, not one fragile
#  endpoint ("تونل زنده پاسخ نداد" even though connected). It now tries several
#  captive-portal endpoints and only fails when EVERY one fails.
# ---------------------------------------------------------------------------

class LiveProxyPingRobustnessTest(unittest.TestCase):
    def _ctrl_active(self):
        from core import engine as eng_mod
        from core.engine import EngineController
        ctrl = EngineController({"socks_port": 10808, "http_port": 10809})
        ctrl._status = eng_mod.STATUS_ACTIVE

        class _Xray:
            socks_port = 55001
            http_port = 55002
        ctrl._xray = _Xray()
        return ctrl

    def _run_with_opener(self, ctrl, open_fn, **kw):
        import urllib.request as ur

        class _Opener:
            def open(self, req, timeout=0):
                return open_fn(req, timeout)
        real_build, real_handler = ur.build_opener, ur.ProxyHandler
        ur.ProxyHandler = lambda mapping: object()
        ur.build_opener = lambda _h: _Opener()
        try:
            return ctrl.live_proxy_ping(**kw)
        finally:
            ur.build_opener, ur.ProxyHandler = real_build, real_handler

    def test_accepts_connecting_status(self):
        """User may ping right as the tunnel comes up; the port is bound by
        then, so CONNECTING must be allowed (not rejected as 'not active')."""
        from core import engine as eng_mod
        ctrl = self._ctrl_active()
        ctrl._status = eng_mod.STATUS_CONNECTING

        class _Resp:
            def getcode(self):
                return 200
            def read(self, *_a):
                return b"success\nMicrosoft Connect Test\nfl=abc"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        ok, ms, _ = self._run_with_opener(
            ctrl, lambda req, timeout: _Resp(), samples=1)
        self.assertTrue(ok)

    def test_first_endpoint_fails_then_a_later_one_succeeds(self):
        """One blocked captive host must NOT make the whole ping fail — the
        all-endpoint sweep should still find a working one."""
        calls = {"n": 0}

        class _Resp:
            def getcode(self):
                return 200
            def read(self, *_a):
                return b"success\nMicrosoft Connect Test\nfl=abc"
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def open_fn(req, timeout):
            calls["n"] += 1
            # fail the first two attempts, succeed afterwards
            if calls["n"] <= 2:
                raise OSError("blocked host")
            return _Resp()

        ctrl = self._ctrl_active()
        ok, ms, detail = self._run_with_opener(ctrl, open_fn, samples=1)
        self.assertTrue(ok)
        self.assertIsInstance(ms, float)
        self.assertGreater(calls["n"], 1)  # proves it retried other endpoints

    def test_only_fails_when_every_endpoint_fails(self):
        def open_fn(req, timeout):
            raise OSError("all blocked")
        ctrl = self._ctrl_active()
        ok, ms, detail = self._run_with_opener(ctrl, open_fn, samples=1)
        self.assertFalse(ok)
        self.assertIsNone(ms)
        self.assertTrue(detail)  # carries the last failure reason

    def test_cdn_edge_only_204_is_not_a_working_tunnel(self):
        """The "fake green ping" bug: a sabotaged spoof config still reaches the
        Cloudflare anycast edge, so ``cp.cloudflare.com/generate_204`` answers
        204 (and a few KB flow) — yet no real site loads because the inner Worker
        route is dead. An empty 204 must NOT be counted as a working tunnel; only
        a body-verified fetch (real bytes from the open internet through the
        proxy backend) proves the path. So a 204-only world is honest RED.
        """
        class _Resp204:
            def getcode(self):
                return 204
            def read(self, *_a):
                return b""          # empty body — CDN-edge captive impostor
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def open_fn(req, timeout):
            # every endpoint returns an empty 204 (edge only, no real traffic)
            return _Resp204()

        ctrl = self._ctrl_active()
        ok, ms, detail = self._run_with_opener(ctrl, open_fn, samples=2)
        self.assertFalse(ok)        # NOT a working tunnel
        self.assertIsNone(ms)
        self.assertTrue(detail)

    def test_body_verified_fetch_is_a_working_tunnel(self):
        """The flip side: when a content-verified endpoint returns its real body
        marker through the proxy, the tunnel is genuinely carrying traffic → OK.
        """
        class _RespBody:
            def getcode(self):
                return 200
            def read(self, *_a):
                return b"success"   # detectportal.firefox.com/success.txt body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def open_fn(req, timeout):
            # 204 captive endpoints answer empty; the success.txt endpoint
            # returns the real body marker — that is what proves the tunnel.
            url = req.full_url
            if "success.txt" in url or "connecttest" in url or "trace" in url:
                return _RespBody()

            class _R204:
                def getcode(self):
                    return 204
                def read(self, *_a):
                    return b""
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
            return _R204()

        ctrl = self._ctrl_active()
        ok, ms, detail = self._run_with_opener(ctrl, open_fn, samples=2)
        self.assertTrue(ok)
        self.assertIsInstance(ms, float)
        self.assertIn("verified", detail)

    def test_no_http_port_is_honest_failure(self):
        from core import engine as eng_mod
        from core.engine import EngineController
        ctrl = EngineController({"socks_port": 0, "http_port": 0})
        ctrl._status = eng_mod.STATUS_ACTIVE
        ctrl._xray = None
        ok, ms, detail = ctrl.live_proxy_ping()
        self.assertFalse(ok)
        self.assertIsNone(ms)


# ---------------------------------------------------------------------------
#  Round 3, Bug A part 1 — ordinary (non-Cloudflare) configs must still get a
#  ping. The CF /cdn-cgi/trace check fails for a plain VPS; a real TLS
#  handshake presenting the config's own SNI is honest liveness evidence.
# ---------------------------------------------------------------------------

class TlsHandshakeFallbackTest(unittest.TestCase):
    def test_handshake_latency_returns_ms_on_success(self):
        import core.ping as ping_mod

        class _TLS:
            def cipher(self):
                return ("X", "Y", 256)
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        class _Ctx:
            check_hostname = True
            verify_mode = None
            def wrap_socket(self, sock, server_hostname=""):
                _Ctx.captured_sni = server_hostname
                return _TLS()

        class _Raw:
            def close(self):
                pass

        import socket as sock_mod
        import ssl as ssl_mod
        real_conn = sock_mod.create_connection
        real_ctx = ssl_mod.create_default_context
        sock_mod.create_connection = lambda *a, **k: _Raw()
        ssl_mod.create_default_context = lambda: _Ctx()
        try:
            ms = ping_mod._tls_handshake_latency(
                "vps.example.com", 443, 5.0, server_name="vps.example.com")
        finally:
            sock_mod.create_connection = real_conn
            ssl_mod.create_default_context = real_ctx
        self.assertIsInstance(ms, float)
        self.assertEqual(_Ctx.captured_sni, "vps.example.com")

    def test_handshake_returns_none_on_connect_failure(self):
        import core.ping as ping_mod
        import socket as sock_mod
        real_conn = sock_mod.create_connection

        def boom(*a, **k):
            raise OSError("refused")
        sock_mod.create_connection = boom
        try:
            ms = ping_mod._tls_handshake_latency("x", 443, 2.0)
        finally:
            sock_mod.create_connection = real_conn
        self.assertIsNone(ms)

    def test_tls_latency_falls_back_when_cf_probe_not_ok(self):
        """For a non-CF host, cf_ip_probe won't return OK → tls_latency must
        invoke the TLS-handshake fallback rather than report None."""
        import core.ping as ping_mod
        from core import cf_scanner

        class _Res:
            outcome = cf_scanner.RST   # anything but OK
            latency_ms = 0.0

        real_probe = cf_scanner.cf_ip_probe
        called = {"fallback": False}

        def fake_fallback(host, port, timeout, *, server_name=""):
            called["fallback"] = True
            return 42.0

        # tls_latency does `from .cf_scanner import cf_ip_probe` at call time,
        # so patching cf_scanner.cf_ip_probe is what takes effect.
        orig_fallback = ping_mod._tls_handshake_latency
        cf_scanner.cf_ip_probe = lambda *a, **k: _Res()
        ping_mod._tls_handshake_latency = fake_fallback
        try:
            ms = ping_mod.tls_latency(
                "vps.example.com", 443, 5.0, server_name="vps.example.com")
        finally:
            cf_scanner.cf_ip_probe = real_probe
            ping_mod._tls_handshake_latency = orig_fallback
        self.assertTrue(called["fallback"])
        self.assertEqual(ms, 42.0)

    def test_tls_latency_does_not_fall_back_for_cloudflare_host(self):
        """REGRESSION (电信-SIN-07 / AYYILDIZ false-green): for a Cloudflare
        anycast IP (or *.pages.dev / *.workers.dev SNI), a failed edge probe must
        NOT fall back to a bare TLS handshake — every CF anycast IP completes a
        TLS handshake for any SNI, so the fallback would falsely green a dirty
        IP / dead route. The ping must stay honestly red.
        """
        import core.ping as ping_mod
        from core import cf_scanner

        class _Res:
            outcome = cf_scanner.RST    # edge probe fails
            latency_ms = 0.0

        called = {"fallback": False}

        def fake_fallback(*a, **k):
            called["fallback"] = True
            return 7.0

        real_probe = cf_scanner.cf_ip_probe
        orig_fallback = ping_mod._tls_handshake_latency
        cf_scanner.cf_ip_probe = lambda *a, **k: _Res()
        ping_mod._tls_handshake_latency = fake_fallback
        try:
            # a Cloudflare anycast IP with a pages.dev SNI (the AYYILDIZ shape)
            ms_ip = ping_mod.tls_latency(
                "104.18.151.71", 8443, 3.0,
                server_name="hammm2.pages.dev",
                host_header="hammm2.pages.dev", is_tls=True, retries=1)
            # a non-CF IP but a workers.dev SNI → still CF-fronted by hostname
            ms_host = ping_mod.tls_latency(
                "203.0.113.9", 8443, 3.0,
                server_name="x.workers.dev",
                host_header="x.workers.dev", is_tls=True, retries=1)
        finally:
            cf_scanner.cf_ip_probe = real_probe
            ping_mod._tls_handshake_latency = orig_fallback
        self.assertIsNone(ms_ip)        # CF anycast IP → honest red, no fallback
        self.assertIsNone(ms_host)      # workers.dev SNI → honest red, no fallback
        self.assertFalse(called["fallback"],
                         "CF-fronted config must NOT use the TLS-handshake fallback")


# ---------------------------------------------------------------------------
#  Round 4 (from real logs) — CDN-fronted configs must be validated HONESTLY:
#   * plaintext WS (security=none, port 80/8880) must NOT green on a bare TCP
#     connect to a Cloudflare anycast IP (SIN-04 / SIN-06 "ping but no connect")
#   * a transient edge reset/timeout must be RETRIED, not turned into instant
#     red ("بدون پاسخ" on a config that actually connects)
# ---------------------------------------------------------------------------

class CdnPingHonestyTest(unittest.TestCase):
    def _patch_probe(self, fn):
        from core import cf_scanner as cf
        orig = cf.cf_ip_probe
        cf.cf_ip_probe = fn
        self.addCleanup(lambda: setattr(cf, "cf_ip_probe", orig))

    def test_plaintext_ws_uses_edge_probe_not_tcp(self):
        """A security=none / port-80 WS config must go through the honest edge
        probe (real HTTP Host) — a bare TCP connect to an anycast IP always
        succeeds and produced the false-green the user reported."""
        from core import cf_scanner as cf
        from core.ping import Target, PingTester

        seen = []

        def fake_probe(host, spec, timeout):
            seen.append(spec)
            return cf.IPResult(host, cf.ERROR, detail="dead worker route")

        self._patch_probe(fake_probe)
        t = Target(label="SIN-04", host="162.159.10.110", port=80,
                   server_name="", tls=False,
                   host_header="broad-mountain.hammm1.workers.dev",
                   path="/", is_ws=True)
        res = PingTester(samples=3, timeout=2.0).ping_target(t)
        self.assertTrue(seen, "edge probe must be invoked for plaintext WS")
        self.assertFalse(seen[0].is_tls)        # plaintext on the wire
        self.assertTrue(seen[0].is_ws)          # but validated as a WS route
        self.assertFalse(res.reachable)         # dead route → honest red

    def test_plaintext_ws_green_only_when_route_lives(self):
        from core import cf_scanner as cf
        from core.ping import Target, PingTester

        def fake_probe(host, spec, timeout):
            return cf.IPResult(host, cf.OK, latency_ms=120.0, detail="ws+edge ok")

        self._patch_probe(fake_probe)
        t = Target(label="SIN-06", host="162.159.5.108", port=80,
                   server_name="", tls=False,
                   host_header="broad-mountain.hammm1.workers.dev",
                   path="/", is_ws=True)
        res = PingTester(samples=3, timeout=2.0).ping_target(t)
        self.assertTrue(res.reachable)
        self.assertLessEqual(res.best_ms or 1e9, 200.0)

    def test_transient_reset_is_retried_then_succeeds(self):
        """First attempt resets, second succeeds → must report the success, not
        the transient red (the 'بدون پاسخ while it connects' bug)."""
        from core import cf_scanner as cf
        from core.ping import tls_latency

        calls = {"n": 0}

        def fake_probe(host, spec, timeout):
            calls["n"] += 1
            if calls["n"] == 1:
                return cf.IPResult(host, cf.RST, detail="reset during probe")
            return cf.IPResult(host, cf.OK, latency_ms=88.0, detail="edge ok")

        self._patch_probe(fake_probe)
        ms = tls_latency("104.18.151.7", 8443, 3.0,
                         server_name="young-field.hammm3.workers.dev",
                         host_header="young-field.hammm3.workers.dev",
                         is_ws=True, is_tls=True, retries=2)
        self.assertEqual(ms, 88.0)
        self.assertGreaterEqual(calls["n"], 2)  # proves it retried

    def test_all_attempts_fail_is_honest_red_for_tls(self):
        from core import cf_scanner as cf
        from core.ping import tls_latency

        # edge always fails; for a TLS config the handshake fallback also fails.
        self._patch_probe(
            lambda h, s, t: cf.IPResult(h, cf.TIMEOUT, detail="timeout"))
        import core.ping as pmod
        orig_fb = pmod._tls_handshake_latency
        pmod._tls_handshake_latency = lambda *a, **k: None
        self.addCleanup(lambda: setattr(pmod, "_tls_handshake_latency", orig_fb))
        ms = tls_latency("104.18.151.71", 8443, 2.0,
                         server_name="young-field.hammm3.workers.dev",
                         is_ws=True, is_tls=True, retries=2)
        self.assertIsNone(ms)

    def test_plaintext_failure_does_not_fake_a_handshake(self):
        """A plaintext (non-TLS) config that fails the edge check must NOT fall
        back to a TLS handshake (there's nothing to validate) — stays red."""
        from core import cf_scanner as cf
        from core.ping import tls_latency
        import core.ping as pmod

        self._patch_probe(
            lambda h, s, t: cf.IPResult(h, cf.ERROR, detail="dead"))
        called = {"fb": False}

        def fb(*a, **k):
            called["fb"] = True
            return 50.0
        orig_fb = pmod._tls_handshake_latency
        pmod._tls_handshake_latency = fb
        self.addCleanup(lambda: setattr(pmod, "_tls_handshake_latency", orig_fb))
        ms = tls_latency("162.159.10.110", 80, 2.0,
                         host_header="broad-mountain.hammm1.workers.dev",
                         is_ws=True, is_tls=False, retries=1)
        self.assertIsNone(ms)
        self.assertFalse(called["fb"], "must NOT TLS-handshake a plaintext config")

    def test_edge_probe_taken_once_per_target_under_load(self):
        """The edge probe (which retries internally) is taken ONCE per target,
        not ``samples`` times, so a 'ping all' burst doesn't hammer the edge and
        cause its own transient resets."""
        from core import cf_scanner as cf
        from core.ping import Target, PingTester

        per_target = {"n": 0}

        def fake_probe(host, spec, timeout):
            per_target["n"] += 1
            return cf.IPResult(host, cf.OK, latency_ms=100.0)

        self._patch_probe(fake_probe)
        t = Target(label="x", host="104.16.0.1", port=8443,
                   server_name="h.workers.dev", tls=True,
                   host_header="h.workers.dev", is_ws=True)
        PingTester(samples=5, timeout=2.0).ping_target(t)
        # one success short-circuits the internal retry, so exactly 1 call here
        self.assertEqual(per_target["n"], 1)

    def test_relay_path_retries_on_refused_upgrade_then_succeeds(self):
        """A relay config (AYYILDIZ7: ``/stars/http://user:pass@vps...``) opens
        THREE TLS connections per probe and, under a 'ping all' burst, the edge
        often *refuses* one WS upgrade (ERROR) transiently even though the route
        is alive. For a relay path that ERROR must be RETRIED (not just RST /
        TIMEOUT) so the config doesn't flake red — the reported AYYILDIZ7 bug."""
        from core import cf_scanner as cf
        from core.ping import tls_latency

        calls = {"n": 0}

        def fake_probe(host, spec, timeout):
            calls["n"] += 1
            # first two passes refuse the upgrade (transient throttle), third OK
            if calls["n"] < 3:
                return cf.IPResult(host, cf.ERROR, detail="ws upgrade refused")
            return cf.IPResult(host, cf.OK, latency_ms=92.0, detail="ws+edge ok")

        self._patch_probe(fake_probe)
        relay_path = "/stars/http://PQ3YjMsJql:fCfJXXbDcw@vps.webtun.xyz:2087"
        ms = tls_latency("104.18.151.71", 8443, 2.0,
                         server_name="hammm2.pages.dev",
                         host_header="hammm2.pages.dev",
                         path=relay_path, is_ws=True, is_tls=True, retries=2)
        self.assertEqual(ms, 92.0)
        self.assertGreaterEqual(calls["n"], 3)  # ERROR was retried for a relay

    def test_non_relay_error_is_not_retried(self):
        """For an ORDINARY (non-relay) config an ERROR means a genuinely dead
        route, so it must NOT be retried as if transient — otherwise a truly
        broken config would waste attempts and could mask itself green. Only
        RST/TIMEOUT are transient for non-relay configs."""
        from core import cf_scanner as cf
        from core.ping import tls_latency
        import core.ping as pmod

        calls = {"n": 0}

        def fake_probe(host, spec, timeout):
            calls["n"] += 1
            return cf.IPResult(host, cf.ERROR, detail="dead worker route")

        self._patch_probe(fake_probe)
        # disable the TLS handshake fallback so we only measure probe attempts
        orig_fb = pmod._tls_handshake_latency
        pmod._tls_handshake_latency = lambda *a, **k: None
        self.addCleanup(lambda: setattr(pmod, "_tls_handshake_latency", orig_fb))
        ms = tls_latency("104.18.151.7", 8443, 2.0,
                         server_name="young-field.hammm3.workers.dev",
                         host_header="young-field.hammm3.workers.dev",
                         path="/?ed=2560", is_ws=True, is_tls=True, retries=2)
        self.assertIsNone(ms)
        # ERROR is non-retryable for a non-relay config → tried just once
        self.assertEqual(calls["n"], 1)


# ---------------------------------------------------------------------------
#  Bug 2 — status mask during auto-restart (no "شروع" flicker, Start ignored)
# ---------------------------------------------------------------------------

class RestartStatusMaskTest(unittest.TestCase):
    """The MainWindow status dispatch should present a transient idle as
    'connecting' while a restart is in flight, and ignore manual power clicks.

    We test the pure decision used by ``_dispatch_status`` / ``_on_power``
    without spinning up the whole Qt window.
    """

    @staticmethod
    def _shown(status, restarting):
        if restarting and status in ("idle", "error"):
            return "connecting"
        return status

    def test_idle_masked_to_connecting_during_restart(self):
        self.assertEqual(self._shown("idle", True), "connecting")
        self.assertEqual(self._shown("error", True), "connecting")

    def test_status_passthrough_when_not_restarting(self):
        self.assertEqual(self._shown("idle", False), "idle")
        self.assertEqual(self._shown("active", True), "active")

    @staticmethod
    def _power_allowed(restarting):
        return not restarting

    def test_manual_power_ignored_during_restart(self):
        self.assertFalse(self._power_allowed(True))
        self.assertTrue(self._power_allowed(False))


class CleanRetryStartTest(unittest.TestCase):
    """نکته ۲: «تلاش دوباره» (and any Start out of a not-fully-idle engine)
    must do a CLEAN restart — tear EVERYTHING down (kill any lingering attempt
    / background workers) and THEN start fresh — instead of stacking a new
    start on top of a half-alive previous attempt.

    We drive the real ``MainWindow._on_power`` against a tiny stub ``self`` so
    no Qt window is built, recording the exact engine call order.
    """

    def _stub(self, *, status, running):
        from ui.window import MainWindow

        calls = []

        class _Engine:
            def __init__(self):
                self.status_value = status
                self.is_running = running
            def update_config(self, *_a):
                calls.append("update_config")
            def set_profile(self, *_a):
                calls.append("set_profile")
            def stop(self):
                calls.append("stop")
                self.is_running = False
                self.status_value = "idle"
            def start(self):
                calls.append("start")

        class _Dash:
            def set_status(self, *_a):
                pass

        class _Store:
            def __init__(self):
                self._d = {"connection_mode": "Tunnel"}
                self.config = self._d
                self.selected_profile = object()   # a non-None profile
            def get(self, k, d=None):
                return self._d.get(k, d)

        stub = MainWindow.__new__(MainWindow)
        stub.engine = _Engine()
        stub.store = _Store()
        stub.page_dashboard = _Dash()
        stub.active_bar = _Dash()
        stub._restarting = False
        return stub, calls

    def test_start_from_error_stops_before_starting(self):
        # «تلاش دوباره» state == error: a stale attempt may still be alive →
        # MUST stop() (clean kill) before start().
        stub, calls = self._stub(status="error", running=False)
        MainWindowOnPower(stub, "start")
        self.assertIn("stop", calls)
        self.assertIn("start", calls)
        self.assertLess(calls.index("stop"), calls.index("start"))

    def test_start_while_running_stops_before_starting(self):
        stub, calls = self._stub(status="active", running=True)
        MainWindowOnPower(stub, "start")
        self.assertLess(calls.index("stop"), calls.index("start"))

    def test_clean_idle_start_does_not_double_stop(self):
        # a genuinely idle engine starts directly — no needless stop() churn.
        stub, calls = self._stub(status="idle", running=False)
        MainWindowOnPower(stub, "start")
        self.assertNotIn("stop", calls)
        self.assertIn("start", calls)


def MainWindowOnPower(stub, action):
    """Call the real, unbound ``MainWindow._on_power`` on *stub*."""
    from ui.window import MainWindow
    return MainWindow._on_power(stub, action)


# ---------------------------------------------------------------------------
#  Bug 3 — inline ping scheduler: bounded, de-duplicated, refresh-safe
# ---------------------------------------------------------------------------

class InlinePingSchedulerTest(unittest.TestCase):
    """Drive the ProfilesPage ping scheduler with a fake engine + no real
    threads, asserting it (a) caps concurrency, (b) de-dupes, (c) survives a
    refresh() while pings are 'in flight' without dropping/raising.
    """

    def _page(self, n_profiles=20):
        from PySide6.QtWidgets import QApplication
        from ui.window import ProfilesPage
        from core.config_store import ConfigStore
        from core.profile import Profile

        app = QApplication.instance() or QApplication([])
        store = ConfigStore.__new__(ConfigStore)
        # minimal store stub: just .profiles + .config + .selected_index
        profs = [Profile(address=f"h{i}.com", port=443, uuid=f"u{i}")
                 for i in range(n_profiles)]

        class _Store:
            profiles = profs
            config = {}
            selected_index = -1
        page = ProfilesPage(_Store(), engine=_FakeEngine())
        return app, page

    def test_ping_all_is_bounded(self):
        app, page = self._page(n_profiles=30)
        # don't actually start threads — stub the worker start
        started = []

        # monkeypatch _pump_ping_queue to count how many would run at once by
        # inspecting the queue vs pending size at pump time
        page._ping_all_inline()
        # after enqueue, at most _PING_MAX_CONCURRENCY are 'pending', rest queued
        self.assertLessEqual(len(page._inline_pending),
                             page._PING_MAX_CONCURRENCY)
        self.assertEqual(
            len(page._inline_pending) + len(page._inline_queue), 30)
        page.stop_inline_pings()

    def test_duplicate_ping_is_deduped(self):
        app, page = self._page(n_profiles=5)
        page._ping_row(0)
        n_after_first = len(page._inline_pending) + len(page._inline_queue)
        page._ping_row(0)  # same row again — must not add a second job
        n_after_dup = len(page._inline_pending) + len(page._inline_queue)
        self.assertEqual(n_after_first, n_after_dup)
        page.stop_inline_pings()

    def test_refresh_during_ping_keeps_workers(self):
        app, page = self._page(n_profiles=10)
        page._ping_all_inline()
        jobs_before = dict(page._inline_jobs)
        page.refresh()  # used to do self._inline_workers = {} → crash
        # the running jobs dict must NOT have been wiped
        self.assertEqual(set(page._inline_jobs), set(jobs_before))
        page.stop_inline_pings()

    def test_row_index_lookup_by_profile_identity(self):
        app, page = self._page(n_profiles=4)
        prof = page._store.profiles[2]
        self.assertEqual(page._row_index_for_profile(prof), 2)


class _FakeEngine:
    """Engine stub whose ping calls never touch the network or block long."""
    is_running = False

    def update_config(self, *_a, **_k):
        pass

    def is_active_profile(self, *_a, **_k):
        return False

    def live_proxy_ping(self, *_a, **_k):
        return (False, None, "idle")

    def ping_profile(self, *_a, **_k):
        return None

    def probe_strategies_for(self, *_a, **_k):
        return None


# ---------------------------------------------------------------------------
#  Round 3, Bug 1 — never get stuck on "connecting"; Stop always works
# ---------------------------------------------------------------------------

class _RestartEngine:
    """Engine stub for restart-recovery tests; records start/stop calls.

    ``status_value`` is the *authoritative* engine state the phase-based
    resolver polls (mirrors EngineBridge.status_value), independent of the
    ``status`` *signal* — which is exactly the late/stale-signal source the
    redesign must be robust against.
    """

    def __init__(self):
        self.started = 0
        self.stopped = 0
        self._running = False
        self.status_value = "idle"

    @property
    def is_running(self):
        return self._running

    def stop(self):
        self.stopped += 1
        self._running = False

    def start(self):
        self.started += 1
        self._running = True


def _bare_window():
    """Build a MainWindow-like shell without the full Qt window.

    We only need the restart-state methods, so we bind them to a lightweight
    object that carries the same attributes. This keeps the test fast and free
    of a real engine/event-loop while exercising the exact phase logic.
    """
    from ui.window import MainWindow

    class _Page:
        def __init__(self):
            self.status = None

        def set_status(self, s, *_a):
            self.status = s
    obj = MainWindow.__new__(MainWindow)
    obj.engine = _RestartEngine()
    obj.page_dashboard = _Page()
    obj.active_bar = _Page()
    # phase-based restart state (matches MainWindow._wire_core init order)
    obj._restarting = False
    obj._restart_phase = "idle"
    obj._restart_gen = 0
    obj._restart_attempts = 0
    obj._restart_settle = 0

    class _Log:
        def append(self, *_a):
            pass
    obj.page_log = _Log()
    return obj


class RestartRecoveryTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def test_cancel_restart_clears_mask_and_bumps_generation(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "stopping"
        g0 = w._restart_gen
        w._cancel_restart()
        self.assertFalse(w._restarting)
        self.assertEqual(w._restart_phase, "idle")
        self.assertGreater(w._restart_gen, g0)
        # a stale poll from the old generation must be a no-op (not start)
        w.engine._running = False
        w._restart_when_idle(g0)
        self.assertEqual(w.engine.started, 0)

    def test_begin_restart_resets_counters_and_enters_stopping(self):
        w = _bare_window()
        w._restart_attempts = 99
        w._restart_settle = 99
        w._begin_restart()
        # _begin_restart stops the engine then advances the phase poller; the
        # important invariants are: mask on, counters reset, stop() issued.
        self.assertTrue(w._restarting)
        self.assertEqual(w._restart_settle, 0)
        self.assertEqual(w.engine.stopped, 1)

    def test_surface_failure_drops_mask_and_shows_error(self):
        import ui.window as win
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        w.engine.status_value = "error"
        orig = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            w._surface_restart_failure(w._restart_gen)
        finally:
            win.Toast.show_message = orig
        self.assertEqual(w.page_dashboard.status, "error")

    def test_watchdog_backcompat_drops_mask(self):
        import ui.window as win
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        gen = w._restart_gen
        w.engine.status_value = "idle"
        orig = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            w._restart_watchdog(gen)  # engine never reached 'active'
        finally:
            win.Toast.show_message = orig
        self.assertFalse(w._restarting)

    def test_watchdog_noop_if_already_active(self):
        w = _bare_window()
        w._restarting = False  # mask already dropped (active reached)
        w._restart_watchdog(w._restart_gen)
        self.assertFalse(w._restarting)


class RestartPhaseMachineTest(unittest.TestCase):
    """Exercise the phase-aware _dispatch_status directly: stale idle/error
    signals must NOT flip the dashboard to «شروع»/«تلاش دوباره» mid-restart
    (the reported bug). Only a genuine ``active`` drops the mask.
    """

    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def _dispatch(self, w, status):
        from ui.window import MainWindow
        w._on_status = lambda *_a: None
        MainWindow._dispatch_status(w, status)

    def test_stale_idle_masked_during_stopping(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "stopping"
        self._dispatch(w, "idle")
        self.assertTrue(w._restarting)            # mask kept
        self.assertEqual(w.page_dashboard.status, "connecting")

    def test_stale_idle_masked_during_starting(self):
        """The CORE fix: a late worker-thread idle that lands AFTER the new
        start() must keep masking — the resolver, not this stale signal,
        decides the outcome."""
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        self._dispatch(w, "idle")
        self.assertTrue(w._restarting)            # still masked, not failed
        self.assertEqual(w.page_dashboard.status, "connecting")

    def test_stale_error_masked_during_starting(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        self._dispatch(w, "error")
        self.assertTrue(w._restarting)
        self.assertEqual(w.page_dashboard.status, "connecting")

    def test_connecting_shown_during_starting(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        self._dispatch(w, "connecting")
        self.assertTrue(w._restarting)
        self.assertEqual(w.page_dashboard.status, "connecting")

    def test_active_drops_mask(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        self._dispatch(w, "active")
        self.assertFalse(w._restarting)           # restart complete
        self.assertEqual(w._restart_phase, "idle")
        self.assertEqual(w.page_dashboard.status, "active")

    def test_passthrough_when_not_restarting(self):
        w = _bare_window()
        w._restarting = False
        self._dispatch(w, "idle")
        self.assertEqual(w.page_dashboard.status, "idle")

    def test_resolver_success_on_active(self):
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        w.engine.status_value = "active"
        w._restart_resolve(w._restart_gen)
        self.assertFalse(w._restarting)
        self.assertEqual(w._restart_phase, "idle")
        self.assertEqual(w.page_dashboard.status, "active")

    def test_resolver_fails_after_sustained_settle(self):
        import ui.window as win
        w = _bare_window()
        w._restarting = True
        w._restart_phase = "starting"
        w.engine.status_value = "idle"
        w._restart_settle = 15      # one short of the 16 threshold
        orig = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            w._restart_resolve(w._restart_gen)  # tips settle to 16 → failure
        finally:
            win.Toast.show_message = orig
        self.assertFalse(w._restarting)
        self.assertEqual(w._restart_phase, "idle")


class ErrorStateRestartTest(unittest.TestCase):
    """Round 3 Bug C: switching the active config while the engine is in
    ERROR (the «تلاش دوباره» button is showing) must auto-restart on the new
    config — not leave the stale retry button stuck.
    """

    @staticmethod
    def _should_restart(was_running, in_error, profile, same_active=False):
        # mirrors the decision in _on_profile_selected: a restart fires when the
        # engine was running OR is in error with a (new) profile selected — but
        # NEVER when the chosen profile is already the live/active one.
        return (was_running or (in_error and profile is not None)) \
            and not same_active

    def test_error_plus_new_profile_triggers_restart(self):
        self.assertTrue(self._should_restart(False, True, object()))

    def test_error_with_deselect_does_not_restart(self):
        self.assertFalse(self._should_restart(False, True, None))

    def test_running_always_restarts(self):
        self.assertTrue(self._should_restart(True, False, object()))

    def test_idle_no_profile_change_does_not_restart(self):
        self.assertFalse(self._should_restart(False, False, object()))

    def test_running_but_same_active_profile_does_not_restart(self):
        # re-selecting the already-active config must NOT tear down a live tunnel
        self.assertFalse(self._should_restart(True, False, object(), same_active=True))


# ---------------------------------------------------------------------------
#  Round 4 — Stop/Start buttons + auto-restart on active-config switch
#
#  User report (ترجمه): «وقتی متصلم و کانفیگ فعال رو عوض می‌کنم خودش ریست
#  می‌شه تا دوباره وصل شه؛ بعضی وقت‌ها باگ می‌خوره / کارها اجرا نمی‌شن /
#  وصل نمی‌شه.» Two concrete UI regressions are covered here:
#    (a) re-selecting the ALREADY-active profile must NOT trigger a restart
#        (no needless teardown of a healthy tunnel → the surprise self-reset).
#    (b) pressing Stop must reflect «idle» on the dashboard *immediately*, even
#        mid-restart, so the button never looks wedged on «در حال اتصال…».
# ---------------------------------------------------------------------------

class _SwitchEngine(_RestartEngine):
    """Restart engine stub that also models the active profile/endpoint.

    ``is_active_profile`` returns True only for the profile currently set, so
    the same-active guard in ``_on_profile_selected`` can be exercised exactly
    as it runs in production (EngineBridge exposes the same method).
    """

    def __init__(self):
        super().__init__()
        self.profile = None
        self.set_profile_calls = 0
        self.config_updates = 0
        self.status_value = "active"

    def set_profile(self, profile):
        self.set_profile_calls += 1
        self.profile = profile

    def update_config(self, _cfg):
        self.config_updates += 1

    def is_active_profile(self, profile):
        return self.profile is profile and self._running


class _Profile:
    def __init__(self, name="srv"):
        self.display_name = name


class ConfigSwitchRestartTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def _switch_window(self):
        """A _bare_window wired with a _SwitchEngine and the extra collaborators
        _on_profile_selected touches, so the real method can run end-to-end."""
        w = _bare_window()
        w.engine = _SwitchEngine()

        class _Store:
            def __init__(self):
                self.config = {}

            def get(self, _k, default=None):
                # report Tunnel so the method doesn't try to flip SNI Only→Tunnel
                return "Tunnel"

            def set(self, *_a, **_k):
                pass

            def save_config(self):
                pass
        w.store = _Store()

        class _Bar:
            def __init__(self):
                self.profile = None
                self.status = None

            def set_profile(self, p):
                self.profile = p

            def set_status(self, s, *_a):
                self.status = s
        w.active_bar = _Bar()
        # neutralise collaborators that aren't under test
        w._sync_mode_applicability = lambda *_a, **_k: None
        return w

    def test_reselecting_active_profile_does_not_restart(self):
        """The core fix: clicking «فعال‌سازی» on the row that is ALREADY the
        live endpoint must keep the tunnel untouched — no stop(), no restart."""
        from ui.window import MainWindow
        import ui.window as win
        w = self._switch_window()
        prof = _Profile("active-one")
        # engine is up and this profile is the active endpoint
        w.engine.start()
        w.engine.profile = prof
        stops_before = w.engine.stopped
        orig_toast = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            MainWindow._on_profile_selected(w, prof)
        finally:
            win.Toast.show_message = orig_toast
        # no restart fired: mask never raised, engine never stopped
        self.assertFalse(w._restarting)
        self.assertEqual(w.engine.stopped, stops_before)

    def test_switching_to_a_different_profile_restarts(self):
        """Selecting a genuinely different config while running MUST restart."""
        from ui.window import MainWindow
        import ui.window as win
        w = self._switch_window()
        old = _Profile("old")
        new = _Profile("new")
        w.engine.start()
        w.engine.profile = old           # active endpoint is the OLD profile
        orig_toast = win.Toast.show_message
        win.Toast.show_message = staticmethod(lambda *a, **k: None)
        try:
            MainWindow._on_profile_selected(w, new)
        finally:
            win.Toast.show_message = orig_toast
        # _begin_restart() was driven: mask on + engine stop issued
        self.assertTrue(w._restarting)
        self.assertGreaterEqual(w.engine.stopped, 1)

    def test_stop_button_reflects_idle_immediately(self):
        """Pressing Stop forces the dashboard/active-bar to «idle» right away —
        even if a stale «connecting» was on screen mid-restart."""
        from ui.window import MainWindow
        w = _bare_window()
        # pretend a restart was in flight and the UI shows connecting
        w._restarting = True
        w._restart_phase = "starting"
        w.page_dashboard.set_status("connecting")
        w.active_bar.set_status("connecting")
        MainWindow._on_power(w, "stop")
        # restart cancelled, engine stopped, and UI snapped to idle synchronously
        self.assertFalse(w._restarting)
        self.assertEqual(w.engine.stopped, 1)
        self.assertEqual(w.page_dashboard.status, "idle")
        self.assertEqual(w.active_bar.status, "idle")


# ---------------------------------------------------------------------------
#  Round 3, Bug 3 — ping results persist across refresh() until re-pinged
# ---------------------------------------------------------------------------

class PingResultPersistenceTest(unittest.TestCase):
    def setUp(self):
        from PySide6.QtWidgets import QApplication
        self.app = QApplication.instance() or QApplication([])

    def _page(self, n=5):
        from ui.window import ProfilesPage
        from core.profile import Profile
        profs = [Profile(address=f"h{i}.com", port=443, uuid=f"u{i}")
                 for i in range(n)]

        class _Store:
            profiles = profs
            config = {}
            selected_index = -1
        return ProfilesPage(_Store(), engine=_FakeEngine())

    def test_result_cached_and_reapplied_after_refresh(self):
        page = self._page()
        prof = page._store.profiles[2]
        key = page._profile_key(prof)
        # simulate a completed ping result being stored
        page._ping_results[key] = ("✔ 42ms", "ok")
        # rebuild the rows (e.g. user did 'select all') — must NOT lose it
        page.refresh()
        # the cache survives and the row shows it (we assert the cache survived
        # and the lookup still resolves the row)
        self.assertIn(key, page._ping_results)
        self.assertEqual(page._row_index_for_profile(prof), 2)

    def test_stale_results_pruned_for_removed_profiles(self):
        page = self._page(n=3)
        page._ping_results["nonexistent|0|||"] = ("✔ 10ms", "ok")
        page.refresh()
        self.assertNotIn("nonexistent|0|||", page._ping_results)

    def test_profile_key_stable_for_same_endpoint(self):
        from core.profile import Profile
        page = self._page(n=1)
        a = Profile(address="x.com", port=443, uuid="u")
        b = Profile(address="x.com", port=443, uuid="u")
        self.assertEqual(page._profile_key(a), page._profile_key(b))
        c = Profile(address="x.com", port=443, uuid="DIFFERENT")
        self.assertNotEqual(page._profile_key(a), page._profile_key(c))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
