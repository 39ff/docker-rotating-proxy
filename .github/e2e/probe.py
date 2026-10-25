#!/usr/bin/env python3
"""probe.py PROXY_HOST PROXY_PORT URL -> prints "<status> <body>" or "ERR <reason>".

Sends one plain-HTTP request through an HTTP proxy. Used from a container
whose address is not on the allowlist.
"""
import http.client
import sys
from urllib.parse import urlsplit

host, port, url = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    conn = http.client.HTTPConnection(host, port, timeout=10)
    conn.request("GET", url, headers={"Host": urlsplit(url).netloc})
    resp = conn.getresponse()
    body = resp.read().decode(errors="replace").strip().splitlines()
    print(resp.status, body[0][:100] if body else "")
except Exception as exc:  # noqa: BLE001 - any failure means "no service"
    print("ERR", type(exc).__name__, exc)
