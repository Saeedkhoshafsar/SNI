"""Startup-handshake tests for :class:`main.ProxyServer`.

These guard the fix for the #1 "connects in V2RayTun but not standalone" bug:
the engine used to launch xray *before* the spoofer had bound 127.0.0.1:40443,
so xray's very first dial hit a closed port and the tunnel silently failed.

``ProxyServer.start()`` must now **block until the listen socket is actually
bound** and return ``True`` (success) / ``False`` (startup failed, reason in
``_start_error``). We can't run the real WinDivert injector in the sandbox, so
we stub ``FakeTcpInjector`` out and exercise the pure startup/socket logic.
"""
import socket
import sys
import types
import unittest


# ---------------------------------------------------------------------------
#  Stub the pydivert-dependent modules so main.py imports on any OS.
# ---------------------------------------------------------------------------

def _install_pydivert_stub():
    saved = {k: sys.modules.get(k) for k in ("pydivert", "fake_tcp",
                                             "injecter", "monitor_connection")}

    pydivert = types.ModuleType("pydivert")
    class _Packet:  # noqa: D401 - placeholder
        pass
    pydivert.Packet = _Packet
    pydivert.WinDivert = object
    sys.modules["pydivert"] = pydivert

    return saved


def _restore_modules(saved):
    for k, v in saved.items():
        if v is not None:
            sys.modules[k] = v
        else:
            sys.modules.pop(k, None)
    sys.modules.pop("main", None)


class _DummyInjector:
    """Stand-in for FakeTcpInjector that never touches WinDivert."""

    def __init__(self, *a, **k):
        self.w = types.SimpleNamespace(close=lambda: None)

    def run(self):
        # idle forever-ish; the daemon thread is harmless and dies with proc
        import time
        time.sleep(30)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProxyStartupTest(unittest.TestCase):
    def setUp(self):
        self._saved = _install_pydivert_stub()
        import main as main_mod
        self.main = main_mod
        # neutralise the real injector (needs WinDivert + admin)
        self._saved_injector = main_mod.FakeTcpInjector
        main_mod.FakeTcpInjector = _DummyInjector

    def tearDown(self):
        try:
            self.main.FakeTcpInjector = self._saved_injector
        except Exception:
            pass
        _restore_modules(self._saved)

    def _cfg(self, port):
        return {
            "LISTEN_HOST": "127.0.0.1",
            "LISTEN_PORT": port,
            "FAKE_SNI": "www.example.com",
            "CONNECT_IP": "127.0.0.1",   # loopback so interface detection works
            "CONNECT_PORT": 443,
            "gaming_mode": False,
        }

    def test_start_blocks_until_listening_then_socket_is_accepting(self):
        port = _free_port()
        srv = self.main.ProxyServer(self._cfg(port))
        logs = []
        srv.on_log = logs.append
        try:
            ok = srv.start()
            self.assertTrue(ok, f"start() failed: {srv._start_error!r}")
            # start() returned True ⇒ the socket must ALREADY be accepting now
            # (no sleep/poll). This is the core guarantee the engine relies on.
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as c:
                c.settimeout(2)
                c.connect(("127.0.0.1", port))  # must not raise
        finally:
            srv.stop()

    def test_start_reports_failure_on_busy_port(self):
        port = _free_port()
        # occupy the port so the spoofer's bind() fails
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        blocker.bind(("127.0.0.1", port))
        blocker.listen(1)
        try:
            srv = self.main.ProxyServer(self._cfg(port))
            logs = []
            srv.on_log = logs.append
            ok = srv.start()
            self.assertFalse(ok)
            self.assertIsNotNone(srv._start_error)
            self.assertTrue(any("گوش" in m for m in logs))
        finally:
            srv.stop()
            blocker.close()

    def test_start_reports_failure_when_injector_cannot_open(self):
        # Simulate "WinDivert missing / no admin": injector ctor raises.
        class _BoomInjector:
            def __init__(self, *a, **k):
                raise RuntimeError("WinDivert handle open failed")

        self.main.FakeTcpInjector = _BoomInjector
        port = _free_port()
        srv = self.main.ProxyServer(self._cfg(port))
        logs = []
        srv.on_log = logs.append
        try:
            ok = srv.start()
            self.assertFalse(ok)
            self.assertIsNotNone(srv._start_error)
            self.assertTrue(any("WinDivert" in m for m in logs))
        finally:
            srv.stop()


if __name__ == "__main__":
    unittest.main()
