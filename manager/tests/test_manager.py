"""Unit tests for manager/ (standard library only).

  python3 -m unittest discover -s manager/tests -v
"""

import http.client
import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "manager"))
sys.path.insert(0, str(REPO / "setup"))

import formats  # noqa: E402
import server  # noqa: E402

server.PBKDF2_ITERATIONS = 1000  # keep the tests fast

FAKE_DOCKER = textwrap.dedent("""\
    #!/usr/bin/env python3
    # Stand-in for the docker CLI: records calls, reports squid and gost healthy.
    import json, os, sys
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as fh:
        fh.write(" ".join(sys.argv[1:]) + "\\n")
    if sys.argv[1:3] == ["compose", "ps"]:
        for svc in ("squid", "gost"):
            print(json.dumps({"Service": svc, "Name": "c-" + svc, "State": "running",
                              "Health": "healthy", "Status": "Up"}))
    sys.exit(0)
""")


def make_root(tmp: Path) -> Path:
    """A minimal checkout: generator, templates and an example proxyList.txt."""
    root = tmp / "root"
    (root / "setup").mkdir(parents=True)
    shutil.copy2(REPO / "setup" / "generate.py", root / "setup" / "generate.py")
    shutil.copytree(REPO / "template", root / "template")
    (root / "openvpn").mkdir()
    (root / "config").mkdir()
    (root / "proxyList.txt").write_text("# my upstreams\n203.0.113.1:1080:socks5:a:b\n# keep me\n")
    return root


class FormatTests(unittest.TestCase):
    def conv(self, raw, scheme="http"):
        return formats.convert_line(raw, scheme)

    def test_provider_formats(self):
        cases = {
            "1.2.3.4:8080:alice:pw": ("1.2.3.4:8080:http:alice:pw", "host:port:user:pass"),
            "1.2.3.4:8080:alice:p:w:x": ("1.2.3.4:8080:http:alice:p:w:x", "host:port:user:pass"),
            "alice:pw@1.2.3.4:8080": ("1.2.3.4:8080:http:alice:pw", "user:pass@host:port"),
            "alice:p@w@1.2.3.4:8080": ("1.2.3.4:8080:http:alice:p@w", "user:pass@host:port"),
            "1.2.3.4:8080@alice:pw": ("1.2.3.4:8080:http:alice:pw", "host:port@user:pass"),
            "socks5h://bob:p%40ss%3Aw@proxy.example.com:1080/": ("proxy.example.com:1080:socks5:bob:p@ss:w", "url"),
            "https://proxy.example.com:443": ("proxy.example.com:443:https", "url"),
            "[2001:db8::1]:1080:dave:x": ("[2001:db8::1]:1080:http:dave:x", "host:port:user:pass"),
            "1.2.3.4:8080": ("1.2.3.4:8080:http", "host:port"),
            "zp.example.net:22225:httpsquid:u:p": ("zp.example.net:22225:httpsquid:u:p", "proxyList.txt"),
        }
        for raw, (line, fmt) in cases.items():
            with self.subTest(raw=raw):
                c = self.conv(raw)
                self.assertTrue(c.ok, c.error)
                self.assertEqual(c.line, line)
                self.assertEqual(c.fmt, fmt)

    def test_default_scheme(self):
        self.assertEqual(self.conv("1.2.3.4:8080", "socks5").line, "1.2.3.4:8080:socks5")
        self.assertEqual(self.conv("1.2.3.4:8080", "").line, "1.2.3.4:8080")  # public
        # credentials cannot go to a public proxy: they fall back to http
        self.assertEqual(self.conv("1.2.3.4:8080:u:p", "").line, "1.2.3.4:8080:http:u:p")
        with self.assertRaises(ValueError):
            self.conv("1.2.3.4:8080", "ftp")

    def test_rejected(self):
        for raw in ("socks4://1.2.3.4:1080", "1.2.3.4:99999", "just text", "2001:db8::1:1080",
                    "1.2.3.4:80:http:us er:pw", "a:b@c"):
            with self.subTest(raw=raw):
                c = self.conv(raw)
                self.assertFalse(c.ok)
                self.assertTrue(c.error)

    def test_skips_comments_and_blank(self):
        self.assertIsNone(self.conv("   "))
        self.assertIsNone(self.conv("# 1.2.3.4:8080"))
        self.assertEqual(len(formats.convert_text("a:1\n\n# x\n1.2.3.4:80\n")), 2)

    def test_describe_hides_password(self):
        up = self.conv("1.2.3.4:8080:alice:secret").upstream
        self.assertNotIn("secret", json.dumps(formats.describe(up)))


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = make_root(self.tmp)
        self.cfg = server.Config(self.root)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_add_upstreams_appends_and_dedupes(self):
        res = server.add_upstreams(self.cfg, "203.0.113.1:1080:socks5:a:b\n5.6.7.8:80:u:p\nbad", "http")
        self.assertEqual(res["added"], 1)
        self.assertEqual(res["duplicates"], 1)
        self.assertEqual(len(res["errors"]), 1)
        self.assertEqual(self.cfg.proxy_list.read_text(),
                         "# my upstreams\n203.0.113.1:1080:socks5:a:b\n# keep me\n5.6.7.8:80:http:u:p\n")
        backups = list((self.cfg.state_dir / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)

    def test_list_and_delete_by_id(self):
        server.add_upstreams(self.cfg, "5.6.7.8:80:u:p", "http")
        data = server.list_upstreams(self.cfg)
        self.assertEqual([e["static_port"] for e in data["entries"]], [30000, 30001])
        self.assertNotIn('"p"', json.dumps(data))
        target = data["entries"][0]["id"]
        self.assertEqual(server.delete_lines(self.cfg, self.cfg.proxy_list, [target]), 1)
        self.assertEqual(self.cfg.proxy_list.read_text(), "# my upstreams\n# keep me\n5.6.7.8:80:http:u:p\n")
        with self.assertRaises(server.ApiError) as ctx:  # id from the old file content
            server.delete_lines(self.cfg, self.cfg.proxy_list, [target])
        self.assertEqual(ctx.exception.code, "stale")

    def test_invalid_lines_are_listed_masked(self):
        self.cfg.proxy_list.write_text("1.2.3.4:80:nope:user:secret\n")
        data = server.list_upstreams(self.cfg)
        self.assertEqual(len(data["invalid"]), 1)
        self.assertNotIn("secret", data["invalid"][0]["text"])

    def test_preview_never_returns_password(self):
        rows = server.preview_upstreams("1.2.3.4:8080:alice:s3cr:et\nsocks5://bob:hunter2@5.6.7.8:1", "http")
        dump = json.dumps(rows)
        self.assertNotIn("s3cr", dump)
        self.assertNotIn("hunter2", dump)

    def test_allowlist(self):
        server.add_allowlist(self.cfg, "198.51.100.0/24", "office\nmultiline")
        self.assertIn("198.51.100.0/24          # office multiline", self.cfg.allowlist.read_text())
        self.assertEqual(server.add_allowlist(self.cfg, "198.51.100.7/24", "")["added"], 0)  # same network
        self.assertEqual(server.add_allowlist(self.cfg, "::ffff:192.0.2.9", "")["value"], "192.0.2.9")
        for bad in ("0.0.0.0/0", "::/0", "not-an-ip"):
            with self.subTest(bad=bad), self.assertRaises(server.ApiError):
                server.add_allowlist(self.cfg, bad, "")
        entries = server.list_allowlist(self.cfg)["entries"]
        system = [e for e in entries if e["system"]]
        self.assertEqual(sorted(e["value"] for e in system), ["127.0.0.1", "172.16.231.1"])
        with self.assertRaises(server.ApiError) as ctx:
            server.delete_allowlist(self.cfg, [system[0]["id"]])
        self.assertEqual(ctx.exception.code, "system_entry")
        mine = [e["id"] for e in entries if e["value"] == "192.0.2.9"]
        self.assertEqual(server.delete_allowlist(self.cfg, mine), 1)

    def test_system_entry_follows_proxy_subnet(self):
        (self.root / ".env").write_text("PROXY_SUBNET=10.99.0.0/24\n")
        self.assertEqual(server.system_allowlist_entries(self.cfg), ["10.99.0.1", "127.0.0.1"])

    def test_ip_allowed(self):
        self.assertTrue(server.ip_allowed("198.51.100.7", ["198.51.100.0/24"]))
        self.assertFalse(server.ip_allowed("198.51.101.7", ["198.51.100.0/24"]))
        self.assertFalse(server.ip_allowed("garbage", ["0.0.0.0/0"]))

    def test_vpn_add_and_delete(self):
        ovpn = "client\nremote vpn.example.com 443 tcp\n"
        res = server.add_vpn(self.cfg, "jp-1", "My Config (1).ovpn", ovpn, "user", "pa ss")
        self.assertEqual(res["file"], "My_Config__1_.ovpn")
        secret = self.cfg.openvpn_dir / "jp-1" / "secret"
        self.assertEqual(secret.read_text(), "user\npa ss\n")
        self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
        vpns = server.list_vpns(self.cfg)["vpns"]
        self.assertEqual(vpns[0]["static_port"], 30001)  # after the socks5 upstream
        self.assertEqual(vpns[0]["remote_hosts"], ["vpn.example.com"])
        cases = [("../x", ovpn, "", ""), ("jp-1", ovpn, "", ""), ("jp-2", "client\n", "", ""),
                 ("jp-3", ovpn, "user", ""), ("jp-4", ovpn, "us\ner", "pw")]
        for name, content, user, pw in cases:
            with self.subTest(name=name), self.assertRaises(server.ApiError):
                server.add_vpn(self.cfg, name, "vpn.ovpn", content, user, pw)
        server.delete_vpn(self.cfg, "jp-1")
        self.assertFalse((self.cfg.openvpn_dir / "jp-1").exists())
        self.assertTrue(list((self.cfg.state_dir / "backups").glob("openvpn-jp-1.*.tar.gz")))
        with self.assertRaises(server.ApiError):
            server.delete_vpn(self.cfg, "..")

    def test_inputs_digest_tracks_apply_inputs(self):
        before = server.inputs_digest(self.cfg)
        (self.root / "publicProxyList.txt").write_text("1.2.3.4:80\n")
        self.assertEqual(server.inputs_digest(self.cfg), before)  # applied by the cron path
        server.add_upstreams(self.cfg, "5.6.7.8:80:u:p", "http")
        self.assertNotEqual(server.inputs_digest(self.cfg), before)

    def test_mask_line(self):
        self.assertEqual(server.mask_line("h:1:socks5:u:p:w"), "h:1:socks5:u:***")
        self.assertEqual(server.mask_line("h:1:socks5"), "h:1:socks5")


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = server.Config(make_root(self.tmp))
        self.auth = server.Auth(self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_setup_flow(self):
        token = self.auth.setup_token()
        self.assertEqual(stat.S_IMODE(self.auth.token_file.stat().st_mode), 0o600)
        self.assertEqual(self.auth.setup_token(), token)  # stable until used
        with self.assertRaises(server.ApiError):
            self.auth.complete_setup("wrong", "long enough password")
        with self.assertRaises(server.ApiError):
            self.auth.complete_setup(token, "short")
        session = self.auth.complete_setup(token, "long enough password")
        self.assertTrue(self.auth.valid(session))
        self.assertFalse(self.auth.token_file.exists())
        self.assertEqual(stat.S_IMODE(self.auth.auth_file.stat().st_mode), 0o600)
        self.assertNotIn("long enough", self.auth.auth_file.read_text())
        with self.assertRaises(server.ApiError):  # only once
            self.auth.complete_setup(token, "another long password")
        self.auth.logout(session)
        self.assertFalse(self.auth.valid(session))

    def test_login_rate_limit(self):
        self.auth.complete_setup(self.auth.setup_token(), "long enough password")
        for _ in range(5):
            with self.assertRaises(server.ApiError) as ctx:
                self.auth.login("wrong", "198.51.100.1")
            self.assertEqual(ctx.exception.code, "bad_password")
        with self.assertRaises(server.ApiError) as ctx:  # blocked now, even with the right password
            self.auth.login("long enough password", "198.51.100.1")
        self.assertEqual(ctx.exception.code, "rate_limited")
        self.assertTrue(self.auth.valid(self.auth.login("long enough password", "198.51.100.2")))

    def test_password_check_does_not_block_sessions(self):
        # Regression: PBKDF2 used to run under Auth.lock, which valid() (every
        # authenticated request) also takes; failed logins could stall the UI.
        session = self.auth.complete_setup(self.auth.setup_token(), "long enough password")
        started, release = threading.Event(), threading.Event()
        real_verify = server.verify_password

        def slow_verify(password, record):
            started.set()
            release.wait(5)
            return real_verify(password, record)

        server.verify_password = slow_verify
        try:
            results = []
            workers = [threading.Thread(target=lambda: results.append(self._try_login("wrong")))
                       for _ in range(server.LOGIN_CONCURRENCY + 1)]
            for w in workers:
                w.start()
            self.assertTrue(started.wait(5))
            t0 = time.monotonic()
            self.assertTrue(self.auth.valid(session))
            self.assertLess(time.monotonic() - t0, 0.5)
            time.sleep(0.2)  # let the extra login hit the concurrency limit
            release.set()
            for w in workers:
                w.join(5)
        finally:
            server.verify_password = real_verify
        self.assertEqual(sorted(results), sorted(["bad_password"] * server.LOGIN_CONCURRENCY + ["busy_login"]))

    def _try_login(self, password):
        try:
            self.auth.login(password, "198.51.100.3")
            return "ok"
        except server.ApiError as exc:
            return exc.code


class HttpTests(unittest.TestCase):
    """The HTTP layer, with a fake docker CLI."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = make_root(self.tmp)
        fake = self.tmp / "docker"
        fake.write_text(FAKE_DOCKER)
        fake.chmod(0o755)
        self.docker_log = self.tmp / "docker.log"
        os.environ["FAKE_DOCKER_LOG"] = str(self.docker_log)
        cfg = server.Config(self.root, docker=str(fake))
        self.httpd, self.state = server.make_server(cfg, "127.0.0.1:0")
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.cookie = ""

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp)

    def request(self, method, path, body=None, headers=None, conn=None):
        own = conn is None
        conn = conn or http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"X-RPX-Request": "1", "Content-Type": "application/json"} if method == "POST" else {}
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        hdrs.update(headers or {})
        data = json.dumps(body if body is not None else {}).encode() if method == "POST" else None
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        for value in resp.headers.get_all("Set-Cookie") or []:
            self.cookie = value.split(";", 1)[0]
        if own:
            conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return resp.status, payload, resp.headers

    def login(self):
        token = self.state.auth.setup_token()
        status, _, headers = self.request("POST", "/api/setup", {"token": token, "password": "long enough password"})
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie")
        for attr in ("HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(attr, cookie)

    def test_static_and_headers(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Rotating Proxy", body)
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        for path in ("/static/../server.py", "/static/%2e%2e/server.py", "/static/nope.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.assertEqual(self.request("GET", "/healthz")[1], b"ok\n")

    def test_csrf_and_auth(self):
        self.assertEqual(self.request("POST", "/api/setup", {}, headers={"X-RPX-Request": ""})[0], 403)
        self.assertEqual(self.request("POST", "/api/setup", {}, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)
        self.assertEqual(self.request("GET", "/api/jobs/x")[0], 401)
        self.login()
        status, ov, _ = self.request("GET", "/api/overview")
        self.assertEqual(status, 200)
        self.assertTrue(ov["pending"])
        self.assertEqual(self.request("POST", "/api/logout")[0], 200)
        self.assertEqual(self.request("GET", "/api/overview")[0], 401)

    def test_keep_alive_after_post_without_reading_body(self):
        # Regression: a POST whose handler ignores the body used to leave "{}"
        # on the connection, which then broke the next request ("{}GET").
        self.login()
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.assertEqual(self.request("POST", "/api/logout", {"x": 1}, conn=conn)[0], 200)
        self.assertEqual(self.request("GET", "/api/session", conn=conn)[0], 200)
        conn.close()

    def test_apply_job_with_fake_docker(self):
        self.login()
        status, job, _ = self.request("POST", "/api/apply")
        self.assertEqual(status, 202)
        for _ in range(100):
            status, job, _ = self.request("GET", "/api/jobs/" + job["id"])
            if job["status"] != "running":
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "succeeded", job["log"])
        self.assertTrue((self.root / "docker-compose.yml").is_file())
        self.assertIn("compose up -d --remove-orphans", self.docker_log.read_text())
        self.assertFalse(self.request("GET", "/api/overview")[1]["pending"])
        self.request("POST", "/api/upstreams/add", {"text": "5.6.7.8:80:u:p", "default_scheme": "http"})
        self.assertTrue(self.request("GET", "/api/overview")[1]["pending"])

    def test_forwarded_client_address(self):
        self.state.cfg.trust_forwarded = True
        self.login()
        hdrs = {"X-Forwarded-For": "203.0.113.1, 198.51.100.77", "CF-Connecting-IP": "192.0.2.99"}
        self.assertEqual(self.request("GET", "/api/session", headers=hdrs)[1]["client_ip"], "198.51.100.77")
        self.assertEqual(self.request("GET", "/api/session", headers={"CF-Connecting-IP": "192.0.2.99"})[1]["client_ip"],
                         "127.0.0.1")
        self.state.cfg.trust_forwarded = False
        self.assertEqual(self.request("GET", "/api/session", headers=hdrs)[1]["client_ip"], "127.0.0.1")

    def test_bad_inputs(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/upstreams/preview", {"text": "x", "default_scheme": "ftp"})[0], 400)
        self.assertEqual(self.request("POST", "/api/upstreams/delete", {"ids": "nope"})[0], 400)
        self.assertEqual(self.request("POST", "/api/public/refresh", {"url": "gopher://x"})[0], 400)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", "/api/upstreams/add", body=b"not json",
                     headers={"X-RPX-Request": "1", "Cookie": self.cookie, "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()


if __name__ == "__main__":
    unittest.main()
