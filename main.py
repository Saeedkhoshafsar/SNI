import asyncio
import os
import socket
import sys
import traceback
import threading
import json
import random
import logging

from utils.network_tools import get_default_interface_ipv4
from utils.packet_templates import ClientHelloMaker
from fake_tcp import FakeInjectiveConnection, FakeTcpInjector

logger = logging.getLogger("sni_proxy")


def get_exe_dir():
    """Returns the directory where the .exe (or script) is located."""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    else:
        return os.path.dirname(os.path.abspath(__file__))


def load_config(config_path=None):
    if config_path is None:
        config_path = os.path.join(get_exe_dir(), 'config.json')
    with open(config_path, 'r') as f:
        return json.load(f)


def save_config(config, config_path=None):
    if config_path is None:
        config_path = os.path.join(get_exe_dir(), 'config.json')
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2)


# ---------------------------------------------------------------------------
#  Bidirectional relay – no mutual cancellation, clean socket shutdown
# ---------------------------------------------------------------------------

async def _relay_pair(sock_a: socket.socket, sock_b: socket.socket,
                      on_up=None, on_down=None):
    """Relay bytes both ways.

    ``sock_a`` is the *client* (incoming) socket and ``sock_b`` the *upstream*
    (outgoing) socket. So ``a → b`` is **upload** and ``b → a`` is **download**.
    Optional ``on_up`` / ``on_down`` callbacks receive the chunk length so the
    server can keep live upload/download byte counters.
    """
    loop = asyncio.get_running_loop()
    shutdown_initiated = False

    def _shutdown_both():
        nonlocal shutdown_initiated
        if shutdown_initiated:
            return
        shutdown_initiated = True
        for s in (sock_a, sock_b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    async def _forward(src, dst, meter):
        try:
            while True:
                data = await loop.sock_recv(src, 65536)
                if not data:
                    break
                if meter is not None:
                    try:
                        meter(len(data))
                    except Exception:
                        pass
                await loop.sock_sendall(dst, data)
        except (OSError, ConnectionError, asyncio.CancelledError):
            pass
        finally:
            _shutdown_both()

    await asyncio.gather(
        _forward(sock_a, sock_b, on_up),    # client → upstream  (upload)
        _forward(sock_b, sock_a, on_down),  # upstream → client  (download)
        return_exceptions=True,
    )


# ---------------------------------------------------------------------------
#  ProxyServer – importable & controllable from CLI or GUI
# ---------------------------------------------------------------------------

class ProxyServer:
    def __init__(self, config: dict):
        self.config = config
        self.listen_host = config["LISTEN_HOST"]
        self.listen_port = config["LISTEN_PORT"]
        self.fake_sni = config["FAKE_SNI"].encode()
        self.connect_ip = config["CONNECT_IP"]
        self.connect_port = config["CONNECT_PORT"]
        self.interface_ipv4 = get_default_interface_ipv4(self.connect_ip)
        self.data_mode = "tls"
        self.bypass_method = "wrong_seq"
        self.gaming_mode = config.get("gaming_mode", False)

        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server_sock: socket.socket | None = None
        self._fake_connections: dict[tuple, FakeInjectiveConnection] = {}
        self._injector: FakeTcpInjector | None = None
        self._active_connections = 0
        self._total_connections = 0

        # live traffic accounting (cumulative bytes since start)
        self._up_bytes = 0
        self._down_bytes = 0
        self._last_up = 0
        self._last_down = 0

        # optional resilience controller handed in by the engine
        self.resilience = None

        # Startup handshake: ``start()`` blocks on this until the listen socket
        # is actually bound (or the bind failed). Without it the engine would
        # launch xray *before* the spoofer is listening on 40443, so xray's very
        # first dial hits a closed port → "connection refused" → silent failure.
        self._ready_event = threading.Event()
        self._start_error: str | None = None

        # UI callbacks (thread-safe, fire-and-forget)
        self.on_log = None
        self.on_status_change = None
        self.on_connection_count_change = None
        self.on_traffic = None   # (up_bytes, down_bytes, up_bps, down_bps)

    # ---------------------------------------------------------------- helpers

    def _log(self, msg: str, level: str = "info"):
        logger.log(getattr(logging, level.upper(), logging.INFO), msg)
        if self.on_log:
            try:
                self.on_log(msg)
            except Exception:
                pass

    def _update_conn_count(self, delta: int):
        self._active_connections += delta
        if delta > 0:
            self._total_connections += delta
        if self.on_connection_count_change:
            try:
                self.on_connection_count_change(
                    self._active_connections, self._total_connections)
            except Exception:
                pass

    # -- traffic metering (called from the relay, thread-confined to the loop)
    def _add_up(self, n: int):
        self._up_bytes += n

    def _add_down(self, n: int):
        self._down_bytes += n

    async def _traffic_ticker(self):
        """Emit cumulative bytes + a 1-second rolling rate to the UI."""
        loop = asyncio.get_running_loop()
        last_t = loop.time()
        while self._running:
            await asyncio.sleep(1.0)
            now = loop.time()
            dt = max(1e-6, now - last_t)
            up_bps = (self._up_bytes - self._last_up) / dt
            down_bps = (self._down_bytes - self._last_down) / dt
            self._last_up, self._last_down = self._up_bytes, self._down_bytes
            last_t = now
            if self.on_traffic:
                try:
                    self.on_traffic(self._up_bytes, self._down_bytes,
                                    max(0.0, up_bps), max(0.0, down_bps))
                except Exception:
                    pass

    @staticmethod
    def _configure_sock(sock: socket.socket, gaming: bool = False):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 11)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 2)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except (AttributeError, OSError):
            pass
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if gaming:
            # Small buffers → lower latency, less queuing
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 32768)
        else:
            # Large buffers → higher throughput
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 262144)

    # ---------------------------------------------------------------- handler

    async def _handle(self, incoming_sock: socket.socket, addr):
        self._update_conn_count(1)
        outgoing_sock = None
        # per-connection diagnostics: a short tag so the user's log clearly
        # shows the lifecycle of each spoofed connection (helps diagnose the
        # "connects in V2RayTun but not in our app" class of problems).
        cid = f"#{self._total_connections}"
        try:
            loop = asyncio.get_running_loop()
            self._log(f"[conn {cid}] از xray رسید ({addr[0]}:{addr[1]}) → "
                      f"اتصال به {self.connect_ip}:{self.connect_port}")

            fake_data = ClientHelloMaker.get_client_hello_with(
                os.urandom(32), os.urandom(32), self.fake_sni, os.urandom(32))

            outgoing_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            outgoing_sock.setblocking(False)
            outgoing_sock.bind((self.interface_ipv4, 0))
            self._configure_sock(outgoing_sock, self.gaming_mode)

            src_port = outgoing_sock.getsockname()[1]
            fake_conn = FakeInjectiveConnection(
                outgoing_sock, self.interface_ipv4, self.connect_ip,
                src_port, self.connect_port, fake_data,
                self.bypass_method, incoming_sock)
            self._fake_connections[fake_conn.id] = fake_conn

            try:
                await asyncio.wait_for(
                    loop.sock_connect(outgoing_sock,
                                      (self.connect_ip, self.connect_port)),
                    timeout=10)
            except Exception as exc:
                fake_conn.monitor = False
                self._fake_connections.pop(fake_conn.id, None)
                self._log(f"[conn {cid}] اتصال به {self.connect_ip}:"
                          f"{self.connect_port} ناموفق: {exc}", "error")
                return

            self._log(f"[conn {cid}] TCP وصل شد (مبدأ {self.interface_ipv4}:"
                      f"{src_port}) — منتظر تأیید ClientHello جعلی…")

            # Random micro-jitter to defeat timing-based DPI fingerprinting
            await asyncio.sleep(random.uniform(0.001, 0.008))

            try:
                await asyncio.wait_for(fake_conn.t2a_event.wait(), 5)
                if fake_conn.t2a_msg == "unexpected_close":
                    raise ValueError("unexpected close")
                if fake_conn.t2a_msg != "fake_data_ack_recv":
                    self._log(f"[conn {cid}] خطای تزریق‌کننده: "
                              f"{fake_conn.t2a_msg}", "error")
                    return
            except (asyncio.TimeoutError, ValueError) as exc:
                self._log(f"[conn {cid}] دست‌دادن جعلی شکست/تایم‌اوت "
                          f"({type(exc).__name__}: {fake_conn.t2a_msg or exc}) "
                          f"— آیا WinDivert/درایور نصب و با دسترسی Admin اجرا "
                          f"شده؟", "error")
                return
            finally:
                fake_conn.monitor = False
                self._fake_connections.pop(fake_conn.id, None)

            self._log(f"[conn {cid}] ✓ ClientHello جعلی تأیید شد — شروع رله")

            # Optimize the incoming socket too before relay
            self._configure_sock(incoming_sock, self.gaming_mode)

            # Bidirectional relay – clean shutdown, no recursion.
            # incoming = client, outgoing = upstream → up/down metering.
            up0, down0 = self._up_bytes, self._down_bytes
            await _relay_pair(incoming_sock, outgoing_sock,
                              on_up=self._add_up, on_down=self._add_down)
            self._log(f"[conn {cid}] رله پایان یافت "
                      f"(↑{self._up_bytes - up0}B ↓{self._down_bytes - down0}B)")

        except asyncio.CancelledError:
            pass
        except Exception:
            self._log(traceback.format_exc(), "error")
        finally:
            self._update_conn_count(-1)
            for s in (incoming_sock, outgoing_sock):
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass

    # ---------------------------------------------------------------- server

    async def _serve(self):
        try:
            self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_sock.setblocking(False)
            self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_sock.bind((self.listen_host, self.listen_port))
            self._configure_sock(self._server_sock, self.gaming_mode)
            self._server_sock.listen(128)
        except OSError as exc:
            # bind failed (port busy / permission) — report it and unblock the
            # caller so the engine doesn't hang waiting for a listener that will
            # never come up.
            self._start_error = (
                f"نتوانست روی {self.listen_host}:{self.listen_port} گوش دهد: "
                f"{exc}. آیا پورت {self.listen_port} توسط برنامهٔ دیگری "
                f"(مثلاً V2RayTun) اشغال شده؟")
            self._log(self._start_error, "error")
            self._running = False
            self._ready_event.set()
            return

        loop = asyncio.get_running_loop()
        self._log(f"Listening on {self.listen_host}:{self.listen_port}")
        self._running = True
        # the listen socket is now bound — release start() so xray can dial us
        self._ready_event.set()
        if self.on_status_change:
            self.on_status_change(True)

        # start the live traffic rate emitter alongside the accept loop
        ticker = asyncio.create_task(self._traffic_ticker())

        tasks: set[asyncio.Task] = set()
        try:
            while self._running:
                try:
                    incoming_sock, addr = await asyncio.wait_for(
                        loop.sock_accept(self._server_sock), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except OSError:
                    if not self._running:
                        break
                    raise
                incoming_sock.setblocking(False)
                task = asyncio.create_task(self._handle(incoming_sock, addr))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            ticker.cancel()
            for t in tasks:
                t.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.gather(ticker, return_exceptions=True)
            except Exception:
                pass
            try:
                self._server_sock.close()
            except OSError:
                pass
            self._server_sock = None
            self._running = False
            if self.on_status_change:
                self.on_status_change(False)

    # ---------------------------------------------------------------- thread

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        # Bring up the WinDivert packet injector first. Opening the WinDivert
        # handle is exactly where a missing driver / lack of Administrator
        # rights blows up — previously that exception died silently inside this
        # daemon thread, so the spoofer "ran" but never injected the decoy
        # ClientHello and every connection timed out with no explanation. We now
        # catch it, surface a clear message, and abort the start cleanly.
        w_filter = (
            f"tcp and ((ip.SrcAddr == {self.interface_ipv4}"
            f" and ip.DstAddr == {self.connect_ip})"
            f" or (ip.SrcAddr == {self.connect_ip}"
            f" and ip.DstAddr == {self.interface_ipv4}))")
        try:
            self._injector = FakeTcpInjector(w_filter, self._fake_connections)
        except Exception as exc:
            self._start_error = (
                f"راه‌اندازی WinDivert ناموفق بود: {exc}. مطمئن شوید برنامه "
                f"«با دسترسی Administrator» اجرا شده و درایور WinDivert نصب "
                f"است (در حالت SNI-spoof الزامی است).")
            self._log(self._start_error, "error")
            self._running = False
            self._ready_event.set()
            self._loop.close()
            self._loop = None
            if self.on_status_change:
                self.on_status_change(False)
            return
        threading.Thread(target=self._injector.run, daemon=True).start()
        self._log("Packet injector started")

        try:
            self._loop.run_until_complete(self._serve())
        except Exception:
            self._log(traceback.format_exc(), "error")
            self._ready_event.set()  # never leave start() hanging
        finally:
            self._loop.close()
            self._loop = None

    # ---------------------------------------------------------------- public

    def start(self) -> bool:
        """Start the spoofer and **block until it is listening (or failed)**.

        Returns ``True`` once the listen socket is bound and the injector is up,
        ``False`` if startup failed (the reason is in :attr:`_start_error` and
        has already been logged). Blocking until ready is essential: the engine
        launches xray immediately after this returns, and xray's first dial must
        land on an already-listening 40443 — otherwise it hits a closed port.
        """
        if self._thread and self._thread.is_alive():
            self._log("Already running")
            return True
        if not self.interface_ipv4:
            self._start_error = (
                "نتوانست رابط شبکه را تشخیص دهد. اتصال اینترنت/مسیر به "
                f"{self.connect_ip} را بررسی کنید.")
            self._log(self._start_error, "error")
            return False
        self._start_error = None
        self._ready_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        # wait for _serve() to bind (or _run_loop to report a startup error)
        if not self._ready_event.wait(timeout=10):
            self._start_error = "راه‌اندازی spoofer در زمان تعیین‌شده کامل نشد"
            self._log(self._start_error, "error")
            return False
        return self._running and self._start_error is None

    def stop(self):
        self._running = False
        self._log("Stopping …")
        if self._injector:
            try:
                self._injector.w.close()
            except Exception:
                pass
        if self._server_sock:
            try:
                self._server_sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None
        self._log("Stopped")
        if self.on_status_change:
            self.on_status_change(False)

    @property
    def is_running(self):
        return self._running


# ---------------------------------------------------------------------------
#  CLI entry point (still works standalone)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    cfg = load_config()
    server = ProxyServer(cfg)
    server.on_log = lambda m: print(m)

    print("اگر از این برنامه برای دسترسی به اینترنت آزاد استفاده می‌کنید حمایت فراموش نشه")
    print("پروژه‌ها و برنامه‌های زیادی برای دسترسی تمام مردم ایران به اینترنت آزاد در نظر دارم"
          " که به حمایت شما نیاز دارد")
    print("\nUSDT (BEP20): 0x76a768B53Ca77B43086946315f0BDF21156bF424\n")
    print("@patterniha")

    server.start()
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        server.stop()
