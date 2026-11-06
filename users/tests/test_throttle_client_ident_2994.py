"""DRF-2994: the throttle key must not be a value the client writes.

With ``NUM_PROXIES`` unset DRF keyed every per-IP throttle by the whole
``X-Forwarded-For`` header. The client sends that header, nginx only appends
the real address to it, so a new header per request was a new bucket: the
``anon`` and ``auth`` limits (login, OTP) could be walked around.
"""

from django.conf import settings
from rest_framework.test import APIRequestFactory
from rest_framework.throttling import AnonRateThrottle

REAL_CLIENT = "203.0.113.7"


def _ident(xff: str | None, remote_addr: str = "127.0.0.1") -> str:
    """Throttle key for a request that reached the app through our nginx."""
    extra = {"REMOTE_ADDR": remote_addr}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    request = APIRequestFactory().get("/", **extra)
    return AnonRateThrottle().get_ident(request)


def test_one_trusted_proxy_is_configured():
    assert settings.REST_FRAMEWORK["NUM_PROXIES"] == 1


def test_spoofed_prefix_does_not_change_the_key():
    """nginx appends the real address last; what the client put before it
    must not reach the key."""
    keys = {
        _ident(f"{spoof}, {REAL_CLIENT}")
        for spoof in ("1.1.1.1", "2.2.2.2", "10.0.0.1, 10.0.0.2", "garbage")
    }
    assert keys == {REAL_CLIENT}


def test_client_without_its_own_header_is_keyed_by_nginx_entry():
    assert _ident(REAL_CLIENT) == REAL_CLIENT


def test_direct_request_without_header_is_keyed_by_peer():
    """Local runs and container-to-container calls without nginx."""
    assert _ident(None, remote_addr="172.18.0.3") == "172.18.0.3"
