"""Validate and normalise a user-entered third-party relay base URL.

A relay (``第三方中转站``) is the one BYOK provider whose upstream host the user
chooses, so the host is attacker-controllable and the string it arrives as is
untrusted.  This module reduces that string to a single canonical ``https``
origin + normalised path (or rejects it), so the only thing sealed into the
credential is a host the egress proxy can also police.  SSRF defence proper
belongs at the squid egress layer (see ``infra/egress-squid.conf``); this is the
application-side half that refuses IP literals, loopback, internal TLDs,
userinfo, query, fragment and odd ports before a host is ever sealed.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import unquote, urlsplit

# Total serialized length ceiling.  The sealed claim and the ``base_url`` wire
# field are both bounded to 255; keep normalisation inside that envelope.
MAX_RELAY_URL_LENGTH = 255

# Hostnames whose right-most label (or whole name) resolves to infrastructure a
# public relay has no business being: link-local, private LAN discovery and the
# RFC 8375 home network zone.  Matched case-insensitively against the punycode
# host and every suffix of it.
_FORBIDDEN_HOST_SUFFIXES = (
    "localhost",
    "local",
    "internal",
    "intranet",
    "lan",
    "home.arpa",
    "in-addr.arpa",
    "ip6.arpa",
)

# A single DNS label: 1-63 chars, alphanumeric with internal hyphens, no
# leading/trailing hyphen.  Applied to the punycode (A-label) form.
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class RelayUrlError(ValueError):
    """Raised with a stable ``code`` when a relay base URL is rejected."""

    code = "relay_url_invalid"


def _reject(reason: str) -> RelayUrlError:
    # The reason is for tests/logs only; the public error code never widens.
    error = RelayUrlError(reason)
    return error


def _is_ip_literal(host: str) -> bool:
    """True if *host* parses as an IPv4/IPv6 address in any numeric form.

    Covers dotted-decimal, bare decimal (``2130706433``), octal/hex octets
    (``0177.0.0.1``), short forms (``127.1``) and bracket-stripped IPv6.  Python's
    :mod:`ipaddress` rejects the ambiguous short/octal forms, so those are also
    caught structurally below; this is the strict parse.
    """

    candidate = host
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate)
        return True
    except ValueError:
        return False


def _looks_numeric_host(host: str) -> bool:
    """Catch IP literals that :mod:`ipaddress` refuses to parse but a resolver
    may still treat as an address: a bare decimal integer, dotted octal/hex, or
    an all-numeric dotted form (``127.0.0.1`` variants, ``0x7f.1``)."""

    stripped = host.strip("[]")
    if not stripped:
        return False
    # Pure integer (decimal/hex/octal) — a classic SSRF bypass (``2130706433``).
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", stripped):
        return True
    if re.fullmatch(r"[0-9]+", stripped):
        return True
    parts = stripped.split(".")
    if len(parts) >= 2 and all(part for part in parts) and all(
        re.fullmatch(r"0[xX][0-9a-fA-F]+|0[0-7]*|[0-9]+", part) for part in parts
    ):
        # Any dotted form whose every label is a number (decimal/octal/hex) is
        # an address literal attempt, not a DNS name.
        if any(re.fullmatch(r"0[xX][0-9a-fA-F]+|0[0-9]+", part) for part in parts):
            return True
        if all(re.fullmatch(r"[0-9]+", part) for part in parts):
            return True
    return False


def _normalise_path(path: str) -> str:
    """Collapse ``.``/``..`` and duplicate slashes; never escape the root.

    The input has already been percent-decoded once so ``%2e%2e`` traversal is
    visible here.  A path that tries to climb above root is rejected rather than
    silently clamped, because a relay base should be a clean prefix.
    """

    if not path or path == "/":
        return ""
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not segments:
                raise _reject("relay path escapes root")
            segments.pop()
            continue
        segments.append(segment)
    if not segments:
        return ""
    return "/" + "/".join(segments)


def normalise_relay_base_url(raw: str) -> tuple[str, str]:
    """Return ``(normalised_base_url, host)`` or raise :class:`RelayUrlError`.

    The returned base URL is ``https://<host>[:<port>][<path>]`` with a trailing
    ``/`` and a trailing ``/chat/completions`` stripped, suitable to seal into a
    BYOK credential and to append ``/models`` or ``/chat/completions`` to.  The
    host is the punycode A-label form, lower-cased, for the issue response and
    for host-only logging.
    """

    value = str(raw or "").strip()
    if not value:
        raise _reject("relay base url is empty")
    if len(value) > MAX_RELAY_URL_LENGTH:
        raise _reject("relay base url is too long")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise _reject("relay base url contains control characters")
    if "\\" in value:
        raise _reject("relay base url contains a backslash")

    parts = urlsplit(value)
    if parts.scheme != "https":
        raise _reject("relay base url must be https")
    if parts.query:
        raise _reject("relay base url must not carry a query string")
    if parts.fragment:
        raise _reject("relay base url must not carry a fragment")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _reject("relay base url must not carry userinfo")

    host = parts.hostname
    if not host:
        raise _reject("relay base url has no host")

    # IDNA -> punycode.  ``urlsplit`` already lower-cases the host; encode so a
    # unicode homograph is pinned to its ASCII A-label before any suffix check.
    try:
        host_ascii = host.encode("idna").decode("ascii")
    except (UnicodeError, UnicodeDecodeError) as exc:
        raise _reject("relay host is not a valid IDNA name") from exc
    host_ascii = host_ascii.lower().rstrip(".")
    if not host_ascii:
        raise _reject("relay host is empty after normalisation")

    if _is_ip_literal(host) or _is_ip_literal(host_ascii) or _looks_numeric_host(host_ascii):
        raise _reject("relay host must be a DNS name, not an IP literal")

    labels = host_ascii.split(".")
    if len(labels) < 2:
        raise _reject("relay host must be a fully-qualified domain name")
    for label in labels:
        if not _LABEL.fullmatch(label):
            raise _reject("relay host has an invalid label")
    # A host whose whole name or right-most labels are an internal/discovery
    # zone is refused even though it is syntactically a DNS name.
    for suffix in _FORBIDDEN_HOST_SUFFIXES:
        suffix_labels = suffix.split(".")
        if labels[-len(suffix_labels):] == suffix_labels:
            raise _reject("relay host is an internal or loopback name")

    # Port: absent or exactly 443.  ``parts.port`` raises for out-of-range.
    try:
        port = parts.port
    except ValueError as exc:
        raise _reject("relay base url has an invalid port") from exc
    if port not in (None, 443):
        raise _reject("relay base url port must be 443")

    # Percent-decode once so traversal and encoded control bytes are visible,
    # then re-collapse.  A second encoded layer (``%252e``) stays literal and is
    # harmless because it can never match a real path segment downstream.
    decoded_path = unquote(parts.path)
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in decoded_path):
        raise _reject("relay path contains control characters")
    normalised_path = _normalise_path(decoded_path)
    # After decoding, a path may only hold plain URL path characters: an encoded
    # ``%3F``/``%23`` must not come back as a query or fragment once the server
    # appends ``/models`` or ``/chat/completions``.
    if not re.fullmatch(r"[A-Za-z0-9._~/-]*", normalised_path):
        raise _reject("relay path contains characters outside the allowed set")
    # A path ending in the OpenAI-compatible completions suffix is stripped so a
    # user may paste either ``https://relay.example.com`` or the full endpoint.
    if normalised_path.endswith("/chat/completions"):
        normalised_path = normalised_path[: -len("/chat/completions")]

    base = f"https://{host_ascii}"
    if port == 443:
        # Normalise away the default port so sealed bases are canonical.
        pass
    if normalised_path:
        base = base + normalised_path
    if len(base) > MAX_RELAY_URL_LENGTH:
        raise _reject("normalised relay base url is too long")
    return base, host_ascii
