"""Unit tests for setup/generate.py (stdlib unittest).

Run from the repository root:  python3 -m unittest discover -s setup/tests -v
"""

import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE.parent))

import generate as g  # noqa: E402

try:  # optional: only used to double-check the hand-written YAML
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

OPTS = g.PEER_OPTIONS


def peer(host, port, name, *extra):
    return " ".join(["cache_peer %s parent %d 0 %s name=%s" % (host, port, OPTS, name)] + list(extra))


def rfc1738_unescape(text):
    """Python port of Squid's rfc1738_unescape() (lib/rfc1738.c), on bytes."""
    s = text.encode("utf-8")
    out, i = bytearray(), 0
    hexdigits = b"0123456789abcdefABCDEF"
    while i < len(s):
        if s[i:i + 2] == b"%%":
            out += b"%"
            i += 2
        elif s[i:i + 1] == b"%" and len(s[i + 1:i + 3]) == 2 and all(c in hexdigits for c in s[i + 1:i + 3]) \
                and int(s[i + 1:i + 3], 16) > 0:
            out.append(int(s[i + 1:i + 3], 16))
            i += 3
        else:
            out.append(s[i])
            i += 1
    return out.decode("utf-8")


def quiet_reporter():
    return g.Reporter(stream=io.StringIO(), quiet=True)


class ParseLineTest(unittest.TestCase):
    def parse(self, line):
        return g.parse_proxy_line(line, "proxyList.txt:1")

    def test_blank_and_comment(self):
        for line in ("", "   ", "\r", "# 1.2.3.4:80", "   # comment", "﻿"):
            self.assertIsNone(self.parse(line), line)

    def test_public(self):
        up = self.parse("1.2.3.4:8080")
        self.assertEqual((up.host, up.port, up.scheme, up.user, up.password), ("1.2.3.4", 8080, "", "", ""))

    def test_each_scheme(self):
        for scheme in ("socks5", "http", "https", "httpsquid"):
            up = self.parse("proxy.example.com:1080:%s:user:pw" % scheme)
            self.assertEqual(up.scheme, scheme)
            self.assertEqual((up.user, up.password), ("user", "pw"))

    def test_scheme_without_auth_and_case(self):
        up = self.parse("Proxy.Example.COM:1080:SOCKS5")
        self.assertEqual((up.host, up.scheme, up.has_auth), ("proxy.example.com", "socks5", False))

    def test_crlf_and_bom(self):
        up = self.parse("﻿1.2.3.4:1080:socks5:u:p\r\n")
        self.assertEqual((up.host, up.password), ("1.2.3.4", "p"))

    def test_password_may_contain_colon(self):
        up = self.parse("1.2.3.4:1080:socks5:user:pa:ss:word")
        self.assertEqual(up.password, "pa:ss:word")

    def test_username_with_empty_password(self):
        up = self.parse("1.2.3.4:3128:httpsquid:apikey:")
        self.assertEqual((up.user, up.password, up.has_auth), ("apikey", "", True))

    def test_ipv6(self):
        up = self.parse("[2001:DB8::1]:1080:socks5:u:p")
        self.assertEqual((up.host, up.port, up.hostport), ("2001:db8::1", 1080, "[2001:db8::1]:1080"))

    def test_invalid_lines(self):
        bad = {
            "2001:db8::1:8080": "brackets",
            "[1.2.3.4]:80": "IPv6",
            "[2001:db8::1]8080": "':port'",
            "[2001:db8::1:80": "']'",
            "1.2.3.4": "host:port",
            "1.2.3.4:0": "out of range",
            "1.2.3.4:65536": "out of range",
            "1.2.3.4:http": "invalid port",
            "1.2.3.4:80:ftp": "unknown scheme",
            "1.2.3.4:80::user:pass": "need a scheme",
            "1.2.3.4:80:http::pass": "without username",
            "1.2.3.4:80:http:us er:pass": "username",
            "1.2.3.4:80:http:user:pa ss": "password",
            "1.2.3.4:80:http:user:pa\"ss": "password",
            "1.2.3.4:80:http:user:pa'ss": "password",
            "1.2.3.4:80:http:user:pa#ss": "password",
            "1.2.3.4:80:http:user:pa\tss": "password",
            "bad host:80": "invalid host",
            "ho\"st:80": "invalid host",
            "host#1:80": "invalid host",
            "-host:80": "invalid host",
            "a..b:80": "invalid host",
        }
        for line, needle in bad.items():
            with self.assertRaises(g.LineError, msg=line) as ctx:
                self.parse(line)
            self.assertIn(needle, str(ctx.exception), line)


class ParseFileTest(unittest.TestCase):
    def test_skips_invalid_lines_with_line_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "proxyList.txt"
            path.write_bytes(b"# header\r\n1.2.3.4:1080:socks5:u:p\r\n\r\nnot a proxy\r\n5.6.7.8:99999\r\n9.9.9.9:80\r\n")
            rep = quiet_reporter()
            ups = g.parse_proxy_file(path, rep)
            self.assertEqual([u.hostport for u in ups], ["1.2.3.4:1080", "9.9.9.9:80"])
            self.assertEqual([u.source for u in ups], ["proxyList.txt:2", "proxyList.txt:6"])
            self.assertEqual(len(rep.warnings), 2)
            self.assertIn("proxyList.txt:4: skipped", rep.warnings[0])
            self.assertIn("proxyList.txt:5: skipped", rep.warnings[1])

    def test_public_list_only_takes_host_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "publicProxyList.txt"
            path.write_text("1.1.1.1:80\n2.2.2.2:1080:socks5\n")
            rep = quiet_reporter()
            ups = g.parse_proxy_file(path, rep, public_only=True)
            self.assertEqual([u.hostport for u in ups], ["1.1.1.1:80"])
            self.assertIn("publicProxyList.txt:2", rep.warnings[0])

    def test_dedupe(self):
        rep = quiet_reporter()
        a = g.Upstream("1.1.1.1", 80, "", source="proxyList.txt:1")
        b = g.Upstream("1.1.1.1", 80, "", source="proxyList.txt:2")
        c = g.Upstream("2.2.2.2", 80, "socks5", source="proxyList.txt:3")
        p1 = g.Upstream("2.2.2.2", 80, "", source="publicProxyList.txt:1")
        p2 = g.Upstream("3.3.3.3", 80, "", source="publicProxyList.txt:2")
        p3 = g.Upstream("3.3.3.3", 80, "", source="publicProxyList.txt:3")
        ups, pub = g.dedupe([a, b, c], [p1, p2, p3], rep)
        self.assertEqual(ups, [a, c])
        self.assertEqual(pub, [p2])


class SquidLoginTest(unittest.TestCase):
    def test_round_trip_through_squid_unescape(self):
        cases = [("user", "pass"), ("alice", "p@ss:w%rd!"), ("bob", "a+b/c=d&e;f"), ("u", "${service_name}"),
                 ("u", "100%"), ("u", "ümläut"), ("u", "back\\slash"), ("u", "")]
        for user, password in cases:
            value = g.squid_login(user, password)
            self.assertRegex(value, r"^[A-Za-z0-9_.~%-]+:[A-Za-z0-9_.~%-]*$")
            self.assertEqual(rfc1738_unescape(value), "%s:%s" % (user, password))

    def test_space_is_not_plus(self):
        # PHP's urlencode() produced '+', which Squid does not decode.
        self.assertEqual(g.squid_login("a b", "c d"), "a%20b:c%20d")

    def test_specials(self):
        self.assertEqual(g.squid_login("carol", "c%l:o@n+e/="), "carol:c%25l%3Ao%40n%2Be%2F%3D")
        self.assertEqual(g.squid_login("u", "$x"), "u:%24x")


class PlanTest(unittest.TestCase):
    def setUp(self):
        lines = [
            "9.9.9.9:3128",                                   # public
            "socks.example.com:1080:socks5:alice:p@ss:word",  # static 1, peer 31000
            "[2001:db8::7]:443:https:dave:d$ve",              # static 2, Squid TLS, no peer port
            "5.6.7.8:8080:http",                              # static 3, peer 31002
            "10.0.0.5:3128:httpsquid:carol:c%l:o",            # Squid login=
        ]
        self.ups = [g.parse_proxy_line(l, "proxyList.txt:%d" % i) for i, l in enumerate(lines, 1)]
        self.pub = [g.parse_proxy_line("8.8.8.8:80", "publicProxyList.txt:1")]
        self.vpns = [g.Vpn(1, "a-vpn", Path("a.ovpn"), []), g.Vpn(2, "b-vpn", Path("b.ovpn"), ["vpn.example.com"])]
        self.plan = g.build_plan(self.ups, self.pub, self.vpns)

    def test_exact_cache_peers(self):
        self.assertEqual(self.plan.squid_peers, [
            peer("9.9.9.9", 3128, "public1"),
            peer("dockergost_rotate", 31000, "gost1"),
            peer("2001:db8::7", 443, "https2", "tls", "tls-options=NO_SSLv3,NO_TLSv1,NO_TLSv1_1", "login=dave:d%24ve"),
            peer("dockergost_rotate", 31002, "gost3"),
            peer("10.0.0.5", 3128, "private1", "login=carol:c%25l%3Ao"),
            peer("dockervpn_1", 8888, "vpn1"),
            peer("dockervpn_2", 8888, "vpn2"),
            peer("8.8.8.8", 80, "public2"),
        ])

    def test_port_assignment(self):
        self.assertEqual([(sp.port, sp.chain) for sp in self.plan.static_ports], [
            (30000, "chain-1"), (30001, "chain-2"), (30002, "chain-3"), (30003, "chain-4"), (30004, "chain-5")])
        services = {s["name"]: s for s in self.plan.gost["services"]}
        self.assertEqual(sorted(services), ["peer-1", "peer-3", "static-1", "static-2", "static-3", "static-4",
                                            "static-5"])
        self.assertEqual(services["peer-1"]["addr"], ":31000")
        self.assertEqual(services["peer-3"]["addr"], ":31002")
        for name, svc in services.items():
            self.assertEqual(svc["handler"], {"type": "http", "chain": "chain-" + name.split("-")[1]})
            self.assertEqual(svc["listener"], {"type": "tcp"})
            if name.startswith("static-"):
                self.assertEqual(svc["admission"], "allowlist")
                self.assertEqual(int(svc["addr"][1:]), 30000 + int(name.split("-")[1]) - 1)
            else:
                self.assertNotIn("admission", svc)  # internal, not published

    def test_admission_is_whitelist(self):
        self.assertEqual(self.plan.gost["admissions"], [{
            "name": "allowlist", "whitelist": True, "reload": "30s", "file": {"path": "/etc/gost/allowed_ip.txt"}}])
        self.assertNotIn("resolvers", self.plan.gost)
        self.assertEqual(self.plan.gost["metrics"], {"addr": "127.0.0.1:9000", "path": "/metrics"})

    def test_chain_nodes(self):
        nodes = {c["name"]: c["hops"][0]["nodes"][0] for c in self.plan.gost["chains"]}
        self.assertEqual(nodes["chain-1"], {
            "name": "upstream-1", "addr": "socks.example.com:1080",
            "connector": {"type": "socks5", "auth": {"username": "alice", "password": "p@ss:word"}},
            "dialer": {"type": "tcp"}})
        self.assertEqual(nodes["chain-2"]["addr"], "[2001:db8::7]:443")
        self.assertEqual(nodes["chain-2"]["connector"], {"type": "http", "auth": {"username": "dave", "password": "d$ve"}})
        self.assertEqual(nodes["chain-2"]["dialer"], {"type": "tls", "tls": {"secure": True, "serverName": "2001:db8::7"}})
        self.assertEqual(nodes["chain-3"]["connector"], {"type": "http"})
        self.assertEqual(nodes["chain-4"]["addr"], "dockervpn_1:8888")
        self.assertEqual(nodes["chain-5"]["addr"], "dockervpn_2:8888")

    def test_compose(self):
        compose = g.build_compose(self.plan)
        self.assertNotIn("version", compose)
        svc = compose["services"]
        self.assertEqual(sorted(svc), ["gost", "squid", "vpn1", "vpn2"])
        self.assertEqual(svc["squid"]["ports"], ["${BIND_ADDR:-0.0.0.0}:3128:3128"])
        self.assertEqual(svc["gost"]["ports"], ["${BIND_ADDR:-0.0.0.0}:%d:%d" % (p, p) for p in range(30000, 30005)])
        self.assertEqual(svc["gost"]["image"], "gogost/gost:3.3.1")
        self.assertEqual(svc["gost"]["command"], ["-C", "/etc/gost/gost.yaml"])
        self.assertEqual(svc["squid"]["depends_on"], ["gost", "vpn1", "vpn2"])
        for name in ("squid", "gost", "vpn1", "vpn2"):
            self.assertEqual(svc[name]["restart"], "unless-stopped")
            self.assertNotIn("latest", json.dumps(svc[name]["image"]))
        self.assertNotIn("ports", svc["vpn1"])
        self.assertEqual(svc["vpn1"]["container_name"], "dockervpn_1")
        self.assertEqual(svc["vpn1"]["volumes"], ["./openvpn/a-vpn/a.ovpn:/gluetun/custom.conf:ro"])
        self.assertEqual(svc["vpn2"]["volumes"], ["./openvpn/b-vpn/gluetun.conf:/gluetun/custom.conf:ro"])
        self.assertNotIn("env_file", svc["vpn1"])  # no secret
        self.assertNotIn("httpbin", json.dumps(compose))
        self.assertIn("squid-internal-mgr", json.dumps(svc["squid"]["healthcheck"]))

    def test_without_static_ports_there_is_no_gost(self):
        plan = g.build_plan([g.parse_proxy_line("1.2.3.4:80")], [], [])
        compose = g.build_compose(plan)
        self.assertEqual(sorted(compose["services"]), ["squid"])
        self.assertNotIn("depends_on", compose["services"]["squid"])
        self.assertEqual(plan.gost["services"], [])


class YamlTest(unittest.TestCase):
    def test_scalars_are_json_quoted(self):
        text = g.dump_yaml({"a": "x: y", "b": True, "c": 3, "d": [], "e": {}, "f": ["p", "q"], "g": [{"h": "i", "j": "k"}]})
        self.assertEqual(text, 'a: "x: y"\nb: true\nc: 3\nd: []\ne: {}\nf: ["p", "q"]\ng:\n  - h: "i"\n    j: "k"\n')

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_round_trip_with_pyyaml(self):
        ups = [g.parse_proxy_line(l) for l in ("1.2.3.4:1080:socks5:u:p\\a:b{c}[d]&*!|>%@", "h.example:443:https:u:v")]
        plan = g.build_plan(ups, [], [g.Vpn(1, "v", Path("v.ovpn"), [], "user", "pw")])
        self.assertEqual(yaml.safe_load(g.render_gost_yaml(plan)), plan.gost)
        self.assertEqual(yaml.safe_load(g.render_compose(plan)), g.build_compose(plan))


class DotenvTest(unittest.TestCase):
    def test_quoting(self):
        self.assertEqual(g.dotenv_quote("plain$x"), "'plain$x'")
        self.assertEqual(g.dotenv_quote("it's $x \"q\" \\"), '"it\'s $$x \\"q\\" \\\\"')


class AllowlistTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowed_ip.txt"
            path.write_text("# c\n127.0.0.1\n10.1.2.3/8  # inline\nnot-an-ip\n::1\n127.0.0.1\n\n2001:db8::/32\n")
            rep = quiet_reporter()
            self.assertEqual(g.parse_allowlist(path, rep), ["127.0.0.1", "10.0.0.0/8", "::1", "2001:db8::/32"])
            self.assertEqual(len(rep.warnings), 1)
            self.assertIn("allowed_ip.txt:4", rep.warnings[0])

    def test_open_proxy_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowed_ip.txt"
            path.write_text("0.0.0.0/0\n")
            rep = quiet_reporter()
            g.parse_allowlist(path, rep)
            self.assertTrue(any("open to everyone" in w for w in rep.warnings))


class GenerateTest(unittest.TestCase):
    """Runs generate() on a temporary copy of the repository layout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(REPO / "template", self.root / "template")
        (self.root / "config").mkdir()
        self.resolved = []

    def tearDown(self):
        self.tmp.cleanup()

    def resolver(self, host):
        self.resolved.append(host)
        return "192.0.2.10" if host == "vpn.example.com" else None

    def run_gen(self, squid_only=False):
        rep = quiet_reporter()
        rc = g.generate(self.root, squid_only=squid_only, rep=rep, resolver=self.resolver)
        return rc, rep

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def add_vpn(self, name, remote, secret=None):
        ovpn = self.write("openvpn/%s/vpn.ovpn" % name, "client\nproto tcp\nremote %s 443\nremote-random\n"
                          "auth-user-pass secret\n# remote ignored.example.com\n" % remote)
        if secret is not None:
            self.write("openvpn/%s/secret" % name, secret)
        return ovpn

    def test_nothing_usable(self):
        self.write("proxyList.txt", "# only comments\n\nbad line\n")
        rc, rep = self.run_gen()
        self.assertEqual(rc, 2)
        self.assertIn("no usable upstream", rep.stream.getvalue())
        self.assertFalse((self.root / "docker-compose.yml").exists())
        rc, _ = self.run_gen(squid_only=True)
        self.assertEqual(rc, 2)

    def test_missing_template(self):
        shutil.rmtree(self.root / "template")
        self.write("proxyList.txt", "1.2.3.4:80\n")
        rc, _ = self.run_gen()
        self.assertEqual(rc, 1)

    def test_full_run(self):
        self.write("proxyList.txt", "1.2.3.4:1080:socks5:alice:s3cr3t-socks\n9.9.9.9:3128\n"
                                    "10.0.0.5:3128:httpsquid:carol:s3cr3t-squid\n")
        self.write("publicProxyList.txt", "8.8.8.8:80\r\n9.9.9.9:3128\r\n")
        ovpn_a = self.add_vpn("a-vpn", "vpn.example.com", "vpnuser\nvpn's pa$$word\n")
        ovpn_b = self.add_vpn("b-vpn", "198.51.100.1")
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ovpn_a, ovpn_b)}

        rc, rep = self.run_gen()
        self.assertEqual(rc, 0, rep.stream.getvalue())

        squid = (self.root / "config/squid.conf").read_text()
        peers = [l for l in squid.splitlines() if l.startswith("cache_peer")]
        self.assertEqual(peers, [
            peer("dockergost_rotate", 31000, "gost1"),
            peer("9.9.9.9", 3128, "public1"),
            peer("10.0.0.5", 3128, "private1", "login=carol:s3cr3t-squid"),
            peer("dockervpn_1", 8888, "vpn1"),
            peer("dockervpn_2", 8888, "vpn2"),
            peer("8.8.8.8", 80, "public2"),
        ])
        self.assertNotIn(g.PEERS_MARKER, squid.splitlines())
        self.assertIn("never_direct allow all", squid)
        self.assertIn('acl Allowed_IP src "/etc/squid/rotating/allowed_ip.txt"', squid)
        self.assertIn("visible_hostname rotating-proxy", squid)
        directives = [l.strip() for l in squid.splitlines() if l.strip() and not l.lstrip().startswith("#")]
        self.assertNotIn("http_access allow localnet", directives)
        self.assertEqual(directives[-len(peers) - 1:-len(peers)], ["logfile_rotate 0"])

        self.assertEqual((self.root / "config/allowed_ip.txt").read_text(), "172.16.231.1\n127.0.0.1\n")

        compose = (self.root / "docker-compose.yml").read_text()
        for secret in ("s3cr3t", "vpnuser", "pa$$word"):
            self.assertNotIn(secret, compose)
        self.assertNotIn("version:", compose)
        self.assertIn('env_file: ["./openvpn/a-vpn/gluetun.env"]', compose)
        self.assertNotIn("./openvpn/b-vpn/gluetun.env", compose)

        env = (self.root / "openvpn/a-vpn/gluetun.env").read_text()
        self.assertIn("OPENVPN_USER='vpnuser'\n", env)
        self.assertIn('OPENVPN_PASSWORD="vpn\'s pa$$$$word"\n', env)
        self.assertFalse((self.root / "openvpn/b-vpn/gluetun.env").exists())

        # Hostname remote -> derived config with the IP; IP remote -> mounted as is.
        derived = (self.root / "openvpn/a-vpn/gluetun.conf").read_text()
        self.assertIn("\nremote 192.0.2.10 443\n", derived)
        self.assertIn("\n# remote ignored.example.com\n", derived)
        self.assertEqual(self.resolved, ["vpn.example.com"])
        self.assertFalse((self.root / "openvpn/b-vpn/gluetun.conf").exists())
        self.assertIn("./openvpn/b-vpn/vpn.ovpn:/gluetun/custom.conf:ro", compose)
        after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ovpn_a, ovpn_b)}
        self.assertEqual(before, after, "user .ovpn files must not be modified")

        gost = (self.root / "config/gost.yaml").read_text()
        self.assertIn('password: "s3cr3t-socks"', gost)
        self.assertIn("whitelist: true", gost)

        for rel, mode in (("config/gost.yaml", 0o600), ("openvpn/a-vpn/gluetun.env", 0o600),
                          ("config/squid.conf", 0o644), ("docker-compose.yml", 0o644)):
            self.assertEqual(stat.S_IMODE(os.stat(self.root / rel).st_mode), mode, rel)

    def test_idempotent(self):
        self.write("proxyList.txt", "1.2.3.4:1080:socks5:u:p\n")
        self.add_vpn("v", "203.0.113.5", "u\np\n")
        outputs = ["config/squid.conf", "config/gost.yaml", "config/allowed_ip.txt", "docker-compose.yml",
                   "openvpn/v/gluetun.env"]
        self.assertEqual(self.run_gen()[0], 0)
        first = {p: (self.root / p).read_bytes() for p in outputs}
        mtimes = {p: os.stat(self.root / p).st_mtime_ns for p in outputs}
        self.assertEqual(self.run_gen()[0], 0)
        self.assertEqual(first, {p: (self.root / p).read_bytes() for p in outputs})
        self.assertEqual(mtimes, {p: os.stat(self.root / p).st_mtime_ns for p in outputs})

    def test_squid_only(self):
        self.write("proxyList.txt", "1.2.3.4:1080:socks5:u:p\n")
        self.assertEqual(self.run_gen()[0], 0)
        compose_before = (self.root / "docker-compose.yml").read_text()

        self.write("publicProxyList.txt", "8.8.8.8:80\n")
        rc, rep = self.run_gen(squid_only=True)
        self.assertEqual(rc, 0)
        self.assertEqual(rep.warnings, [])  # public proxies only touch squid.conf
        self.assertIn(peer("8.8.8.8", 80, "public1"), (self.root / "config/squid.conf").read_text())

        self.write("proxyList.txt", "1.2.3.4:1080:socks5:u:p\n5.6.7.8:1080:socks5\n")
        rc, rep = self.run_gen(squid_only=True)
        self.assertEqual(rc, 0)
        self.assertTrue(any("docker compose up -d" in w for w in rep.warnings))
        self.assertEqual((self.root / "docker-compose.yml").read_text(), compose_before)

    def test_config_hash_labels(self):
        self.write("proxyList.txt", "1.2.3.4:1080:socks5:u:p\n")
        self.add_vpn("v", "203.0.113.5")
        self.assertEqual(self.run_gen()[0], 0)
        compose1 = (self.root / "docker-compose.yml").read_text()
        self.assertEqual(compose1.count("rotating-proxy.config-hash:"), 3)  # squid, gost, vpn1

        # Public proxies are applied by `squid -k reconfigure`: no recreate.
        self.write("publicProxyList.txt", "8.8.8.8:80\n")
        self.assertEqual(self.run_gen()[0], 0)
        self.assertEqual((self.root / "docker-compose.yml").read_text(), compose1)

        def labels(text):
            return [l.strip() for l in text.splitlines() if "config-hash" in l]

        # A changed credential only lives in gost.yaml: the gost label must change.
        self.write("proxyList.txt", "1.2.3.4:1080:socks5:u:changed\n")
        self.assertEqual(self.run_gen()[0], 0)
        compose2 = (self.root / "docker-compose.yml").read_text()
        self.assertEqual([a == b for a, b in zip(labels(compose1), labels(compose2))], [True, False, True])

        # Editing the .ovpn recreates the VPN container.
        ovpn = self.root / "openvpn/v/vpn.ovpn"
        ovpn.write_text(ovpn.read_text() + "verb 4\n")
        self.assertEqual(self.run_gen()[0], 0)
        compose3 = (self.root / "docker-compose.yml").read_text()
        self.assertEqual([a == b for a, b in zip(labels(compose2), labels(compose3))], [True, True, False])

    def test_squid_only_writes_nothing_else(self):
        self.write("proxyList.txt", "1.2.3.4:80\n")
        self.assertEqual(self.run_gen(squid_only=True)[0], 0)
        self.assertEqual(sorted(p.name for p in (self.root / "config").iterdir()), ["squid.conf"])
        self.assertFalse((self.root / "docker-compose.yml").exists())

    def test_vpn_dirs(self):
        self.write("proxyList.txt", "")
        self.add_vpn("with-secret", "203.0.113.5", "user\npass\n")
        self.add_vpn("no-secret", "203.0.113.6")
        self.add_vpn("bad-secret", "203.0.113.7", "only-one-line\n")
        (self.root / "openvpn/empty-dir").mkdir()
        (self.root / "openvpn/.hidden").mkdir()
        (self.root / "openvpn/.gitkeep").write_text("")
        self.write("openvpn/bad name/vpn.ovpn", "remote 1.2.3.4\n")
        rc, rep = self.run_gen()
        self.assertEqual(rc, 0)
        compose = (self.root / "docker-compose.yml").read_text()
        # Sorted directory order decides dockervpn_N.
        self.assertIn('container_name: "dockervpn_1"', compose)
        self.assertIn("./openvpn/bad-secret/vpn.ovpn", compose)
        self.assertIn("./openvpn/no-secret/vpn.ovpn", compose)
        self.assertIn('env_file: ["./openvpn/with-secret/gluetun.env"]', compose)
        self.assertEqual(compose.count("env_file"), 1)
        warnings = "\n".join(rep.warnings)
        self.assertIn("openvpn/empty-dir: skipped: no .ovpn file found", warnings)
        self.assertIn("openvpn/bad name: skipped", warnings)
        self.assertIn("bad-secret/secret: expected username", warnings)
        self.assertNotIn(".hidden", warnings)
        squid = (self.root / "config/squid.conf").read_text()
        self.assertEqual([l.split()[1] for l in squid.splitlines() if l.startswith("cache_peer")],
                         ["dockervpn_1", "dockervpn_2", "dockervpn_3"])

    def test_unresolvable_vpn_host_is_kept_with_warning(self):
        self.write("proxyList.txt", "")
        self.add_vpn("v", "unknown.invalid")
        rc, rep = self.run_gen()
        self.assertEqual(rc, 0)
        self.assertIn("remote unknown.invalid 443", (self.root / "openvpn/v/gluetun.conf").read_text())
        self.assertTrue(any("cannot resolve unknown.invalid" in w for w in rep.warnings))

    def test_cli_main(self):
        self.write("proxyList.txt", "1.2.3.4:80\n")
        stderr = io.StringIO()
        old = sys.stderr
        sys.stderr = stderr
        try:
            rc = g.main(["--root", str(self.root), "--quiet"])
        finally:
            sys.stderr = old
        self.assertEqual(rc, 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertTrue((self.root / "docker-compose.yml").exists())


if __name__ == "__main__":
    unittest.main()
