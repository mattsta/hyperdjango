"""SessionAuth brute-force lockout + bounded attempt tracking.

Covers the per-IP login-attempt limiter (previously untested) and the
memory-bound: a distributed brute-force from many IPs must not grow the tracking
dict without limit (stale IPs are swept — a DoS guard).

Run: uv run pytest tests/test_standalone/test_login_lockout.py -q
"""

from hyperdjango.auth.sessions import _LOGIN_SWEEP_INTERVAL, SessionAuth


def _auth(**kw):
    return SessionAuth(secret="k" * 32, **kw)


def _age_attempts(auth: SessionAuth, seconds: float) -> None:
    """Rewind every recorded attempt by ``seconds`` — as if that long had passed.

    The lockout window is evaluated as ``timestamp > now - login_lockout_seconds``
    against the stored stamps, so moving the stamps back is exactly equivalent to
    waiting, and it says which side of the boundary each attempt is on. Sleeping
    a shade over a short window instead makes the answer depend on the machine:
    a loaded runner oversleeps, which is fine for "it aged out" but silently
    destroys any check that something is still INSIDE the window.
    """
    with auth._login_attempts_lock:
        for ip, stamps in auth._login_attempts.items():
            auth._login_attempts[ip] = [t - seconds for t in stamps]


def test_blocks_after_max_attempts():
    auth = _auth(max_login_attempts=3, login_lockout_seconds=300)
    ip = "1.2.3.4"
    assert auth.is_login_blocked(ip) is False
    for _ in range(3):
        auth.record_failed_login(ip)
    assert auth.is_login_blocked(ip) is True


def test_clear_resets_lockout():
    auth = _auth(max_login_attempts=2, login_lockout_seconds=300)
    ip = "1.2.3.4"
    auth.record_failed_login(ip)
    auth.record_failed_login(ip)
    assert auth.is_login_blocked(ip) is True
    auth.clear_login_attempts(ip)  # successful login clears
    assert auth.is_login_blocked(ip) is False


def test_window_expiry_unblocks():
    auth = _auth(max_login_attempts=2, login_lockout_seconds=60)
    ip = "1.2.3.4"
    auth.record_failed_login(ip)
    auth.record_failed_login(ip)
    assert auth.is_login_blocked(ip) is True
    _age_attempts(auth, 59)  # still inside the 60s window
    assert auth.is_login_blocked(ip) is True
    _age_attempts(auth, 2)  # 61s old — the window has passed
    assert auth.is_login_blocked(ip) is False


def test_disabled_when_max_zero():
    auth = _auth(max_login_attempts=0)
    ip = "1.2.3.4"
    for _ in range(100):
        auth.record_failed_login(ip)
    assert auth.is_login_blocked(ip) is False


def test_attempt_tracking_is_bounded():
    """DoS guard: a distributed attack (many IPs) must not grow the tracking
    dict without bound — aged-out IPs are swept."""
    auth = _auth(max_login_attempts=5, login_lockout_seconds=60)
    attackers = _LOGIN_SWEEP_INTERVAL + 200
    for i in range(attackers):
        auth.record_failed_login(f"10.0.{i // 256}.{i % 256}")
    assert len(auth._login_attempts) == attackers
    _age_attempts(auth, 61)  # every prior attempt is now outside the window
    for _ in range(_LOGIN_SWEEP_INTERVAL):  # trigger a sweep
        auth.record_failed_login("9.9.9.9")
    # Only the still-active IP survives — exact, because the aged attempts are
    # unambiguously outside the window rather than a sleep's worth of "probably".
    assert list(auth._login_attempts) == ["9.9.9.9"]
