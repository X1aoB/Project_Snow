"""Unit tests for the relay base-URL validator/normaliser (variant B)."""

from __future__ import annotations

import pytest

from backend.snow_app.relay_url import (
    MAX_RELAY_URL_LENGTH,
    RelayUrlError,
    normalise_relay_base_url,
)


@pytest.mark.parametrize(
    "raw, expected_base, expected_host",
    [
        ("https://relay.example.com", "https://relay.example.com", "relay.example.com"),
        ("https://relay.example.com/", "https://relay.example.com", "relay.example.com"),
        ("https://relay.example.com:443/v1", "https://relay.example.com/v1", "relay.example.com"),
        ("https://relay.example.com/v1/", "https://relay.example.com/v1", "relay.example.com"),
        # trailing /chat/completions is stripped
        ("https://relay.example.com/v1/chat/completions", "https://relay.example.com/v1", "relay.example.com"),
        ("https://relay.example.com/chat/completions", "https://relay.example.com", "relay.example.com"),
        # path traversal collapses without escaping root
        ("https://relay.example.com/a/../v1", "https://relay.example.com/v1", "relay.example.com"),
        ("https://relay.example.com//v1//openai/", "https://relay.example.com/v1/openai", "relay.example.com"),
        # IDN -> punycode
        ("https://中转.example.com/v1", "https://xn--fiqq40n.example.com/v1", "xn--fiqq40n.example.com"),
        # uppercase host is lowercased
        ("https://Relay.Example.COM/V1", "https://relay.example.com/V1", "relay.example.com"),
    ],
)
def test_accepts_and_normalises(raw, expected_base, expected_host):
    base, host = normalise_relay_base_url(raw)
    assert base == expected_base
    assert host == expected_host


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "http://relay.example.com",            # not https
        "ftp://relay.example.com",
        "relay.example.com",                   # no scheme
        "https://relay.example.com?x=1",       # query
        "https://relay.example.com#frag",      # fragment
        "https://user:pass@relay.example.com", # userinfo
        "https://user@relay.example.com",
        "https://relay.example.com:8443/v1",   # non-443 port
        "https://relay.example.com:80",
        "https://localhost/v1",                # loopback name
        "https://api.local/v1",                # .local
        "https://svc.internal/v1",             # .internal
        "https://box.lan/v1",                  # .lan
        "https://x.home.arpa/v1",              # home.arpa
        "https://singlelabel",                 # not FQDN
        # IP literals, various forms
        "https://127.0.0.1/v1",
        "https://10.0.0.5/v1",
        "https://192.168.1.1",
        "https://169.254.169.254",
        "https://[::1]/v1",
        "https://[fe80::1]/v1",
        "https://2130706433/v1",               # decimal IP
        "https://0x7f.0.0.1/v1",               # hex octet
        "https://0177.0.0.1/v1",               # octal octet
        "https://127.1/v1",                    # short form
        # path traversal above root
        "https://relay.example.com/../../etc/passwd",
        # encoded query/fragment/space must not come back once decoded
        "https://relay.example.com/v1%3Fx=1",
        "https://relay.example.com/v1%23frag",
        "https://relay.example.com/v1%20x",
        "https://relay.example.com/v1;x",
        # backslash
        "https://relay.example.com\\@evil.com",
        # control char
        "https://relay.example.com/\x00",
    ],
)
def test_rejects(raw):
    with pytest.raises(RelayUrlError):
        normalise_relay_base_url(raw)


def test_rejects_overlong():
    long_host = "a" * 250
    with pytest.raises(RelayUrlError):
        normalise_relay_base_url(f"https://{long_host}.example.com")


def test_error_code_is_stable():
    try:
        normalise_relay_base_url("http://relay.example.com")
    except RelayUrlError as exc:
        assert exc.code == "relay_url_invalid"
    else:  # pragma: no cover
        raise AssertionError("expected RelayUrlError")


def test_length_boundary_accepts_just_under_cap():
    # host that keeps the whole URL within the cap is accepted
    host = "relay." + "a" * 40 + ".example.com"
    base, _ = normalise_relay_base_url(f"https://{host}")
    assert len(base) <= MAX_RELAY_URL_LENGTH
