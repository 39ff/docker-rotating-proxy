#!/usr/bin/env python3
"""Tiny HTTP/HTTPS origin for the e2e test.

GET /         -> the client IP the origin saw (i.e. which upstream was used)
GET /headers  -> request headers as JSON (to check header scrubbing)
"""
import json
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.startswith("/headers"):
            body = json.dumps({k.lower(): v for k, v in self.headers.items()}).encode()
        else:
            body = (self.client_address[0] + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("origin: %s %s\n" % (self.client_address[0], fmt % args))


def serve(port, context=None):
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    if context is not None:
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    httpd.serve_forever()


if __name__ == "__main__":
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain("/certs/server.pem", "/certs/server.key")
    threading.Thread(target=serve, args=(443, ctx), daemon=True).start()
    serve(80)
