"""Demo-only guard. Pure functions, fail closed: anything not provably demo is refused.

Deriv's options API serves demo and real accounts on separate WebSocket endpoints:
    wss://api.derivws.com/trading/v1/options/ws/demo?otp=...
    wss://api.derivws.com/trading/v1/options/ws/real?otp=...
The agent may only ever talk to the demo one.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from urllib.parse import urlparse

DEMO_WS_PATH = "/trading/v1/options/ws/demo"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})


class RealAccountRefused(RuntimeError):
    """Raised whenever something might touch a real-money account."""


def assert_demo_ws_url(url: str, allowed_hosts: Collection[str]) -> None:
    """Refuse any WebSocket URL that is not the demo endpoint on an allowed host."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host not in allowed_hosts:
        raise RealAccountRefused(f"WebSocket host {host!r} is not allowed")
    expected_scheme = "ws" if host in LOCAL_HOSTS else "wss"
    if parsed.scheme not in {expected_scheme, "wss"}:
        raise RealAccountRefused(f"WebSocket scheme {parsed.scheme!r} is not allowed")
    if parsed.path.rstrip("/") != DEMO_WS_PATH:
        raise RealAccountRefused(f"WebSocket path {parsed.path!r} is not the demo endpoint")


_DEMO_MARKERS = {"demo", "virtual"}
_REAL_MARKERS = {"real", "live"}


def assert_demo_account(account: Mapping[str, object]) -> None:
    """Refuse an account record unless it positively identifies as demo.

    The exact field names of the new accounts endpoint are confirmed in the P0 spike
    (docs/api-notes.md). Until then this accepts the common shapes and fails closed.
    """
    if account.get("is_virtual") in (True, 1, "1"):
        return
    for key in ("account_type", "type", "environment", "group"):
        value = str(account.get(key, "")).strip().lower()
        if value in _REAL_MARKERS:
            raise RealAccountRefused(f"account {key}={value!r} is a real-money account")
        if value in _DEMO_MARKERS:
            return
    raise RealAccountRefused("account record does not identify as demo")
