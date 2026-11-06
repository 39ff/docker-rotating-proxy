"""Convert proxy lines in the formats proxy providers hand out into
proxyList.txt lines (host:port[:scheme[:user:pass]]).

Accepted input, one proxy per line:

  host:port[:scheme[:user:pass]]   proxyList.txt format, kept as is
  host:port:user:pass              common provider format -> default scheme
  host:port                        -> default scheme
  user:pass@host:port              -> default scheme
  host:port@user:pass              -> default scheme
  scheme://[user[:pass]@]host:port URL; socks5h:// and socks:// mean socks5,
                                   user and password are percent-decoded

IPv6 hosts must be in brackets ([2001:db8::1]:1080). Every converted line is
checked with setup/generate.py's parse_proxy_line, so the web UI accepts
exactly what the generator accepts.
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import List, Optional, Tuple

import generate  # setup/generate.py, put on sys.path by the caller

# Types offered for lines that do not name one. "" = public HTTP proxy.
DEFAULT_SCHEMES = ("http", "socks5", "https", "httpsquid", "")

URL_SCHEMES = {
    "http": "http",
    "https": "https",
    "socks5": "socks5",
    "socks5h": "socks5",
    "socks": "socks5",
}

_PORT = re.compile(r"^[0-9]{1,5}$")


@dataclass
class Converted:
    raw: str  # input line, stripped
    line: Optional[str] = None  # proxyList.txt line, None when skipped or invalid
    upstream: Optional[generate.Upstream] = None
    fmt: str = ""  # which input format matched
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.line is not None


def _split_host(text: str) -> Tuple[str, str]:
    """'host:rest' or '[v6]:rest' -> (host as written, rest). Raises ValueError."""
    if text.startswith("["):
        end = text.find("]")
        if end < 0 or text[end + 1:end + 2] != ":":
            raise ValueError("expected [IPv6]:port")
        return text[:end + 1], text[end + 2:]
    host, sep, rest = text.partition(":")
    if not sep:
        raise ValueError("expected host:port")
    return host, rest


def _looks_like_hostport(text: str) -> bool:
    try:
        _, rest = _split_host(text)
    except ValueError:
        return False
    return bool(_PORT.match(rest))


def _native(host: str, port: str, scheme: str, user: str = "", password: str = "") -> str:
    line = "%s:%s" % (host, port)
    if scheme or user:
        line += ":" + scheme
    if user:
        line += ":%s:%s" % (user, password)
    return line


def _from_url(text: str) -> Tuple[str, str]:
    scheme_raw, _, rest = text.partition("://")
    scheme = URL_SCHEMES.get(scheme_raw.lower())
    if scheme is None:
        raise ValueError("unsupported scheme %r (use http, https or socks5)" % scheme_raw)
    rest = rest.rstrip("/")
    userinfo, at, hostport = rest.rpartition("@")
    if not at:
        hostport = rest
    host, port = _split_host(hostport)
    user, _, password = userinfo.partition(":")
    user = urllib.parse.unquote(user)
    password = urllib.parse.unquote(password)
    if password and not user:
        raise ValueError("password without username")
    return _native(host, port, scheme, user, password), "url"


def _convert(text: str, default_scheme: str) -> Tuple[str, str]:
    if "://" in text:
        return _from_url(text)
    if "@" in text:
        left, _, right = text.rpartition("@")
        if _looks_like_hostport(right):  # user:pass@host:port
            creds, hostport, fmt = left, right, "user:pass@host:port"
        elif _looks_like_hostport(left):  # host:port@user:pass
            creds, hostport, fmt = right, left, "host:port@user:pass"
        else:
            raise ValueError("cannot tell credentials from host:port around '@'")
        user, _, password = creds.partition(":")
        host, port = _split_host(hostport)
        return _native(host, port, default_scheme or "http", user, password), fmt
    host, rest = _split_host(text)
    fields = rest.split(":", 3)
    if len(fields) == 1:
        return _native(host, fields[0], default_scheme), "host:port"
    if fields[1].strip().lower() in generate.SCHEMES:
        return text, "proxyList.txt"
    if len(fields) == 3:  # host:port:user:pass (the password may contain ':')
        return _native(host, fields[0], default_scheme or "http", fields[1], fields[2]), "host:port:user:pass"
    if len(fields) == 4:
        # host:port:user:pa:ss - everything after the user is the password
        return (_native(host, fields[0], default_scheme or "http", fields[1], fields[2] + ":" + fields[3]),
                "host:port:user:pass")
    raise ValueError("unknown scheme %r (use socks5, http, https, httpsquid or leave empty)" % fields[1])


def convert_line(raw: str, default_scheme: str = "http") -> Optional[Converted]:
    """Convert one pasted line. Returns None for blank lines and comments."""
    text = raw.strip().lstrip("\ufeff")
    if not text or text.startswith("#"):
        return None
    if default_scheme not in DEFAULT_SCHEMES:
        raise ValueError("unknown default scheme %r" % default_scheme)
    result = Converted(raw=text)
    try:
        line, result.fmt = _convert(text, default_scheme)
        upstream = generate.parse_proxy_line(line)
    except (ValueError, generate.LineError) as exc:
        result.error = str(exc)
        return result
    result.line = line
    result.upstream = upstream
    return result


def convert_text(text: str, default_scheme: str = "http", limit: int = 5000) -> List[Converted]:
    out: List[Converted] = []
    for raw in text.splitlines():
        conv = convert_line(raw, default_scheme)
        if conv is None:
            continue
        out.append(conv)
        if len(out) >= limit:
            break
    return out


def describe(up: generate.Upstream) -> dict:
    """Upstream as shown in the UI. Never contains the password."""
    kinds = {
        "": "public",
        "httpsquid": "httpsquid",
        "http": "http",
        "socks5": "socks5",
        "https": "https",
    }
    return {
        "scheme": up.scheme,
        "kind": kinds[up.scheme],
        "host": up.host,
        "port": up.port,
        "hostport": up.hostport,
        "user": up.user,
        "has_password": bool(up.password),
        "static": up.scheme in ("socks5", "http", "https"),
    }
