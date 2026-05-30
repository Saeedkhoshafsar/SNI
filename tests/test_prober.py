"""Unit tests for core.prober (Auto-Prober: probe → rank → select → health).

The network is injected via a fake probe_fn, so the entire ranking / selection
/ health / fallback logic runs deterministically with no sockets — works on any
OS including the sandbox.

Run:  python tests/test_prober.py
"""
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.prober import (
    ERROR, OK, RST, TIMEOUT, AutoProber, Candidate, HealthWindow, ProbeResult,
    build_candidates,
)


# ---------------------------------------------------------------------------
#  fake probe functions
# ---------------------------------------------------------------------------

def _scripted_probe(script):
    """Return a probe_fn that yields outcomes from a dict {strategy_key: (outcome, ms)}."""
    def fn(candidate, host, port, timeout):
        outcome, ms = script.get(candidate.key, (ERROR, 0.0))
        return ProbeResult(candidate, outcome, latency_ms=ms)
    return fn


def _always(outcome, ms=10.0):
    def fn(candidate, host, port, timeout):
        return ProbeResult(candidate, outcome, latency_ms=ms)
    return fn


# ---------------------------------------------------------------------------
#  Candidate
# ---------------------------------------------------------------------------

def test_candidate_key_plain():
    assert Candidate("wrong_seq").key == "wrong_seq"


def test_candidate_key_with_fragmentation():
    c = Candidate("wrong_seq", fragment_tcp=True, fragment_tls=True, tls_chunk=48)
    assert c.key == "wrong_seq+ftcp+ftls48"


# ---------------------------------------------------------------------------
#  ProbeResult scoring
# ---------------------------------------------------------------------------

def test_probe_result_success_score_latency_aware():
    fast = ProbeResult(Candidate("a"), OK, latency_ms=0.0)
    slow = ProbeResult(Candidate("a"), OK, latency_ms=1000.0)
    assert fast.score() == 1.0
    assert abs(slow.score() - 0.5) < 1e-9
    assert fast.score() > slow.score()


def test_probe_result_failure_scores_zero():
    for outcome in (RST, TIMEOUT, ERROR):
        assert ProbeResult(Candidate("a"), outcome, latency_ms=10).score() == 0.0
        assert ProbeResult(Candidate("a"), outcome).success is False
    assert ProbeResult(Candidate("a"), OK).success is True


# ---------------------------------------------------------------------------
#  HealthWindow
# ---------------------------------------------------------------------------

def test_health_window_bounded():
    w = HealthWindow(size=3)
    for _ in range(5):
        w.record(ProbeResult(Candidate("a"), OK, latency_ms=0))
    assert w.samples == 3


def test_health_window_success_rate_and_health():
    w = HealthWindow(size=4)
    w.record(ProbeResult(Candidate("a"), OK))
    w.record(ProbeResult(Candidate("a"), OK))
    w.record(ProbeResult(Candidate("a"), RST))
    w.record(ProbeResult(Candidate("a"), RST))
    assert w.success_rate == 0.5
    assert w.healthy is True            # >= 0.5
    w.record(ProbeResult(Candidate("a"), RST))   # window slides → [OK,RST,RST,RST]
    assert w.success_rate == 0.25
    assert w.healthy is False


def test_health_window_empty_is_unhealthy():
    w = HealthWindow()
    assert w.samples == 0
    assert w.healthy is False
    assert w.mean_score == 0.0


# ---------------------------------------------------------------------------
#  candidate ordering by static prior
# ---------------------------------------------------------------------------

def test_candidate_order_by_prior():
    cands = [Candidate("a"), Candidate("b"), Candidate("c")]
    scores = {"a": 0.5, "b": 0.9, "c": 0.7}
    order = AutoProber.candidate_order(scores, cands)
    assert [c.strategy for c in order] == ["b", "c", "a"]


# ---------------------------------------------------------------------------
#  probe_all + select_best
# ---------------------------------------------------------------------------

def test_run_selects_only_successful_candidate():
    cands = [Candidate("a"), Candidate("b"), Candidate("c")]
    script = {"a": (RST, 0), "b": (OK, 20), "c": (TIMEOUT, 0)}
    p = AutoProber(cands, _scripted_probe(script))
    best = p.run("host", 443)
    assert best is not None
    assert best.strategy == "b"
    assert p.selected.strategy == "b"


def test_run_picks_lowest_latency_among_successes():
    cands = [Candidate("slow"), Candidate("fast")]
    script = {"slow": (OK, 800), "fast": (OK, 50)}
    p = AutoProber(cands, _scripted_probe(script))
    best = p.run("h", 1)
    assert best.strategy == "fast"     # higher score == lower latency


def test_run_returns_none_when_all_fail():
    cands = [Candidate("a"), Candidate("b")]
    p = AutoProber(cands, _always(RST))
    assert p.run("h", 1) is None
    assert p.selected is None


def test_tie_break_is_randomized_but_deterministic_with_seed():
    cands = [Candidate("a"), Candidate("b")]
    script = {"a": (OK, 0), "b": (OK, 0)}     # identical scores → tie
    chosen = set()
    for seed in range(20):
        p = AutoProber(cands, _scripted_probe(script), rng=random.Random(seed))
        chosen.add(p.run("h", 1).strategy)
    # over many seeds both candidates should get picked at least once
    assert chosen == {"a", "b"}


# ---------------------------------------------------------------------------
#  robustness: a throwing probe_fn must not crash the run
# ---------------------------------------------------------------------------

def test_probe_fn_exception_becomes_error_result():
    def boom(candidate, host, port, timeout):
        raise RuntimeError("net down")
    cands = [Candidate("a")]
    p = AutoProber(cands, boom)
    results = p.probe_all("h", 1)
    assert results[0].outcome == ERROR
    assert "net down" in results[0].detail
    assert p.select_best() is None


# ---------------------------------------------------------------------------
#  live health monitoring + reprobe + fallback chain
# ---------------------------------------------------------------------------

def test_record_live_and_needs_reprobe():
    cands = [Candidate("a"), Candidate("b")]
    p = AutoProber(cands, _scripted_probe({"a": (OK, 10), "b": (OK, 100)}),
                   window=4)
    p.run("h", 1)
    assert p.selected.strategy == "a"
    assert p.needs_reprobe() is False
    # the live connection starts failing for the selected candidate
    for _ in range(4):
        p.record_live(ProbeResult(p.selected, RST))
    assert p.needs_reprobe() is True


def test_needs_reprobe_true_when_nothing_selected():
    p = AutoProber([Candidate("a")], _always(RST))
    assert p.selected is None
    assert p.needs_reprobe() is True


def test_fallback_order_excludes_selected_and_sorts_by_health():
    cands = [Candidate("a"), Candidate("b"), Candidate("c")]
    script = {"a": (OK, 10), "b": (OK, 500), "c": (OK, 100)}
    p = AutoProber(cands, _scripted_probe(script))
    p.run("h", 1)
    assert p.selected.strategy == "a"         # lowest latency
    fb = p.fallback_order()
    assert [c.strategy for c in fb] == ["c", "b"]   # next best first, 'a' excluded


# ---------------------------------------------------------------------------
#  build_candidates helper
# ---------------------------------------------------------------------------

def test_build_candidates_plain():
    cs = build_candidates(["wrong_seq", "fake_ttl"])
    assert [c.key for c in cs] == ["wrong_seq", "fake_ttl"]


def test_build_candidates_with_fragmentation_adds_variant():
    cs = build_candidates(["wrong_seq"], fragment_tcp=True)
    assert [c.key for c in cs] == ["wrong_seq", "wrong_seq+ftcp"]


def test_build_candidates_empty_raises_in_prober():
    try:
        AutoProber([], _always(OK))
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("empty candidates must raise ValueError")


# ---------------------------------------------------------------------------
#  integration with the real strategy registry (priors)
# ---------------------------------------------------------------------------

def test_candidate_order_uses_real_registry_scores():
    from strategies import REGISTRY
    scores = {k: s.score() for k, s in REGISTRY.items()}
    cands = [Candidate(k) for k in REGISTRY]
    order = AutoProber.candidate_order(scores, cands)
    # wrong_seq has the highest static prior (0.8) → must come first
    assert order[0].strategy == "wrong_seq"


# ---------------------------------------------------------------------------
#  tls_probe — #1 false-positive fix (validate a real handshake, not just TCP)
# ---------------------------------------------------------------------------

def test_tls_probe_rst_when_peer_closes_during_handshake():
    """A peer that accepts the TCP connect but drops it before completing the
    TLS handshake (exactly how DPI blocks a censored SNI) must NOT be reported
    as OK — that was the green-ping-but-no-connect bug (#1)."""
    import socket
    import threading
    from core.prober import tls_probe

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _accept_then_reset():
        try:
            conn, _ = srv.accept()
            # force a RST on close so the client's TLS handshake fails hard
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                            __import__("struct").pack("ii", 1, 0))
            conn.close()
        except OSError:
            pass

    threading.Thread(target=_accept_then_reset, daemon=True).start()
    res = tls_probe(Candidate("wrong_seq"), "127.0.0.1", port, 2.0,
                    server_name="blocked.example.com")
    srv.close()
    # TCP connected but TLS never completed → must be a failure, never OK
    assert res.outcome != OK
    assert res.outcome in (RST, TIMEOUT, ERROR)


def test_tls_probe_ok_against_real_tls_server():
    """A genuine TLS endpoint (self-signed is fine — we only validate that the
    protocol got through, not the cert) must report OK."""
    import ssl
    import socket
    import threading
    import tempfile
    import os as _os
    from core.prober import tls_probe

    # generate a throwaway self-signed cert; skip if cryptography isn't present
    try:
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
    except Exception:
        import pytest
        pytest.skip("cryptography not available for self-signed TLS test")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
            .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
            .sign(key, hashes.SHA256()))
    d = tempfile.mkdtemp()
    cpath, kpath = _os.path.join(d, "c.pem"), _os.path.join(d, "k.pem")
    with open(cpath, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kpath, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cpath, kpath)
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def _serve():
        try:
            conn, _ = srv.accept()
            try:
                tls = ctx.wrap_socket(conn, server_side=True)
                tls.close()
            except OSError:
                conn.close()
        except OSError:
            pass

    threading.Thread(target=_serve, daemon=True).start()
    res = tls_probe(Candidate("wrong_seq"), "127.0.0.1", port, 3.0,
                    server_name="localhost")
    srv.close()
    assert res.outcome == OK


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  PASS  {fn.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL  {fn.__name__}: {exc!r}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
