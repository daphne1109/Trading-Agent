import pytest

from agent.deriv.guard import (
    RealAccountRefused,
    assert_demo_account,
    assert_demo_ws_url,
    assert_public_ws_url,
)

PUBLIC_URL = "wss://api.derivws.com/trading/v1/options/ws/public"


def test_public_guard_accepts_public_endpoint():
    assert_public_ws_url(PUBLIC_URL, {"api.derivws.com"})


@pytest.mark.parametrize(
    "url",
    [
        "wss://api.derivws.com/trading/v1/options/ws/demo?otp=abc",
        "wss://api.derivws.com/trading/v1/options/ws/real?otp=abc",
        PUBLIC_URL + "?otp=abc",  # paper mode never carries credentials
        "wss://evil.example.com/trading/v1/options/ws/public",
        "ws://api.derivws.com/trading/v1/options/ws/public",
    ],
)
def test_public_guard_rejects_everything_else(url):
    with pytest.raises(RealAccountRefused):
        assert_public_ws_url(url, {"api.derivws.com"})


def test_demo_guard_still_rejects_public_endpoint():
    with pytest.raises(RealAccountRefused):
        assert_demo_ws_url(PUBLIC_URL, {"api.derivws.com"})


PROD = {"api.derivws.com"}
DEMO_URL = "wss://api.derivws.com/trading/v1/options/ws/demo?otp=abc"


def test_guard_accepts_demo_url():
    assert_demo_ws_url(DEMO_URL, PROD)


def test_guard_accepts_demo_url_trailing_slash():
    assert_demo_ws_url("wss://api.derivws.com/trading/v1/options/ws/demo/?otp=x", PROD)


def test_guard_rejects_real_url():
    with pytest.raises(RealAccountRefused):
        assert_demo_ws_url("wss://api.derivws.com/trading/v1/options/ws/real?otp=abc", PROD)


@pytest.mark.parametrize(
    "url",
    [
        "wss://api.derivws.com/trading/v1/options/ws/public",
        "wss://api.derivws.com/trading/v1/options/ws/demo/../real?otp=x",
        "wss://api.derivws.com/ws/demo?otp=x",
        "wss://evil.example.com/trading/v1/options/ws/demo?otp=x",
        "ws://api.derivws.com/trading/v1/options/ws/demo?otp=x",  # plaintext to prod
        "https://api.derivws.com/trading/v1/options/ws/demo",
        "",
        "not a url",
    ],
)
def test_guard_rejects_unknown_or_unsafe_urls(url):
    with pytest.raises(RealAccountRefused):
        assert_demo_ws_url(url, PROD)


def test_guard_allows_local_fake_server_only_when_listed():
    local = "ws://127.0.0.1:8765/trading/v1/options/ws/demo?otp=t"
    assert_demo_ws_url(local, {"127.0.0.1"})
    with pytest.raises(RealAccountRefused):
        assert_demo_ws_url(local, PROD)


@pytest.mark.parametrize(
    "account",
    [{"is_virtual": 1}, {"is_virtual": True}, {"account_type": "demo"}, {"type": "Demo"}],
)
def test_guard_accepts_demo_account_json(account):
    assert_demo_account(account)


@pytest.mark.parametrize(
    "account",
    [
        {"account_type": "real"},
        {"type": "live"},
        {"is_virtual": 0, "account_type": "real"},
        {"type": "demo", "group": "real"},  # conflicting markers: real wins
        {"is_virtual": 1, "account_type": "real"},
        {"is_virtual": 0, "type": "demo"},
        {},
        {"account_id": "DOT123"},
    ],
)
def test_guard_rejects_real_or_unknown_account_json(account):
    with pytest.raises(RealAccountRefused):
        assert_demo_account(account)
