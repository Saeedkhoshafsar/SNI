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

    def test_fire_and_forget_handle_relays_without_server_ack(self):
        """fake_ttl/wrong_checksum must NOT block waiting for a server ACK.

        We drive ``_handle`` against a real loopback echo server. A stub
        injector simulates the WinDivert send-thread: it marks the fake as
        sent and fires ``t2a_event`` with ``fake_sent_no_ack`` (exactly what
        the real injector now does for fire-and-forget strategies) — and
        crucially it NEVER produces a ``fake_data_ack_recv``. The relay must
        still start and echo bytes, proving the 5s ACK-timeout no longer
        applies to fire-and-forget techniques.
        """
        import asyncio
        import threading

        # a tiny loopback echo server to stand in for the upstream endpoint
        echo = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        echo.bind(("127.0.0.1", 0))
        echo.listen(1)
        echo_port = echo.getsockname()[1]

        def _echo_accept():
            try:
                conn, _ = echo.accept()
                data = conn.recv(65536)
                if data:
                    conn.sendall(data)
                conn.close()
            except OSError:
                pass

        threading.Thread(target=_echo_accept, daemon=True).start()

        cfg = self._cfg(_free_port())
        cfg["CONNECT_IP"] = "127.0.0.1"
        cfg["CONNECT_PORT"] = echo_port
        srv = self.main.ProxyServer(cfg)
        srv.bypass_method = "fake_ttl"  # a fire-and-forget strategy

        # Stub the injector behaviour: instead of WinDivert, when _handle
        # registers a connection we immediately simulate "fake injected, no
        # ACK expected" on a tiny timer.
        orig_set = self.main.FakeInjectiveConnection

        captured = {}

        def _wrap_conn(*a, **k):
            conn = orig_set(*a, **k)
            captured["conn"] = conn
            return conn

        self.main.FakeInjectiveConnection = _wrap_conn

        async def _drive():
            loop = asyncio.get_running_loop()
            srv._loop = loop
            # a real loopback TCP pair (emulating xray ↔ spoofer). AF_UNIX
            # socketpair() can't take TCP_NODELAY, which _configure_sock sets.
            lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lst.bind(("127.0.0.1", 0))
            lst.listen(1)
            lp = lst.getsockname()[1]
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(("127.0.0.1", lp))
            server_side, _ = lst.accept()
            lst.close()
            client.setblocking(False)
            server_side.setblocking(False)

            async def _simulate_injector():
                # wait until _handle has built the FakeInjectiveConnection
                for _ in range(500):
                    if "conn" in captured:
                        break
                    await asyncio.sleep(0.001)
                conn = captured["conn"]
                conn.fake_sent = True
                conn.t2a_msg = "fake_sent_no_ack"
                conn.t2a_event.set()

            handle_task = asyncio.create_task(
                srv._handle(server_side, ("127.0.0.1", 12345)))
            inj_task = asyncio.create_task(_simulate_injector())

            # once relay is up, send through the client side and read the echo
            await asyncio.sleep(0.2)
            await loop.sock_sendall(client, b"ping")
            echoed = b""
            try:
                echoed = await asyncio.wait_for(loop.sock_recv(client, 64), 3)
            except asyncio.TimeoutError:
                pass
            client.close()
            await asyncio.gather(handle_task, inj_task,
                                  return_exceptions=True)
            return echoed

        try:
            result = asyncio.run(_drive())
            self.assertEqual(result, b"ping",
                             "fire-and-forget relay did not echo data "
                             "(it likely blocked on the missing server ACK)")
        finally:
            self.main.FakeInjectiveConnection = orig_set
            echo.close()

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
