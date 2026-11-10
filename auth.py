"""Signed login tokens and a login-attempt limiter (standard library only).

Tokens are signed with SESSION_SECRET instead of being kept in server memory, so people stay
logged in even when a free host puts the app to sleep and wakes it again.
"""
import hashlib
import hmac
import threading
import time


def _sign(secret, expires):
    return hmac.new(secret.encode(), f"wsm1:{expires}".encode(), hashlib.sha256).hexdigest()


def make_token(secret, ttl, now=None):
    now = time.time() if now is None else now
    expires = int(now + ttl)
    return f"{expires}.{_sign(secret, expires)}"


def verify_token(secret, token, now=None):
    if not isinstance(token, str):
        return False
    now = time.time() if now is None else now
    expires_text, dot, sig = token.partition(".")
    # ASCII digits only: int() would also accept other scripts' digits
    if not dot or not expires_text.isascii() or not expires_text.isdigit():
        return False
    expires = int(expires_text)
    if expires < now:
        return False
    return hmac.compare_digest(sig, _sign(secret, expires))


class LoginThrottle:
    """Blocks a client after too many wrong passwords within a time window."""

    def __init__(self, max_fails=8, window=600):
        self.max_fails = max_fails
        self.window = window
        self._fails = {}
        self._lock = threading.Lock()

    def _recent(self, key, now):
        times = [t for t in self._fails.get(key, []) if t > now - self.window]
        if times:
            self._fails[key] = times
        else:
            self._fails.pop(key, None)  # release memory for clients that went quiet
        return times

    def blocked(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            return len(self._recent(key, now)) >= self.max_fails

    def fail(self, key, now=None):
        now = time.time() if now is None else now
        with self._lock:
            times = self._recent(key, now)
            times.append(now)
            self._fails[key] = times
            if len(self._fails) > 10_000:  # many different clients: drop the expired ones
                for k in list(self._fails):
                    self._recent(k, now)
