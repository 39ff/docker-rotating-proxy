"""Unit tests for setup/generate.py (standard library only).

Run: python3 -m unittest discover -s setup/tests
"""

import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import generate as g  # noqa: E402

IP_OVPN = """client
dev tun
proto tcp
remote 89.187.161.54 443
remote-random
auth-user-pass secret
"""

HOST_OVPN = """client\r
dev tun\r
proto tcp\r
remote vpn.example.com 1443\r
remote second.example.com 443 tcp\r
# remote commented.example.com 443\r
remote-random\r
"""

DEFAULT_ALLOW = "127.0.0.1\n172.16.0.0/12\n"


def fake_resolver(table):
    def resolve(host):
        return list(table.get(host, []))
    return resolve


class Repo:
    """A throw-away repository root with the generator inputs."""

    def __init__(self, proxies="", allow=DEFAULT_ALLOW, users=None,
                 public=None):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "template").mkdir()
        (self.root / "config").mkdir()
        (self.root / "openvpn").mkdir()
        self.write("proxyList.txt", proxies)
        if allow is not None:
            self.write("template/allowed_ip.txt", allow)
        if users is not None:
            self.write("template/users.txt", users)
        if public is not None:
            self.write("publicProxyList.txt", public)
        self.warnings = []
        self.infos = []

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
        return path

    def read(self, rel):
        return (self.root / rel).read_text(encoding="utf-8")

    def exists(self, rel):
        return (self.root / rel).exists()

    def vpn(self, name, ovpn=IP_OVPN, secret="user\npass\n", filename="vpn.ovpn"):
        self.write("openvpn/{}/{}".format(name, filename), ovpn)
        if secret is not None:
            self.write("openvpn/{}/secret".format(name), secret)

    def run(self, **kwargs):
        kwargs.setdefault("resolver", fake_resolver({}))
        return g.generate(self.root, warn=self.warnings.append,
                          info=self.infos.append, **kwargs)

    def nodes(self):
        return json.loads(self.read("config/nodes.json"))

    def close(self):
        self._tmp.cleanup()


class RepoTestCase(unittest.TestCase):
    def repo(self, *args, **kwargs):
        repo = Repo(*args, **kwargs)
        self.addCleanup(repo.close)
        return repo


class ParseProxyLineTests(unittest.TestCase):
    def parse(self, line):
        return g.parse_proxy_line(line)

    def assertInvalid(self, line, fragment):
        with self.assertRaises(ValueError) as ctx:
            self.parse(line)
        self.assertIn(fragment, str(ctx.exception))

    def test_public_proxy_without_scheme(self):
        for line in ("1.2.3.4:8080", "1.2.3.4:8080:", "  1.2.3.4:8080  "):
            up = self.parse(line)
            self.assertEqual((up.host, up.port, up.scheme), ("1.2.3.4", 8080, ""))
            self.assertFalse(up.has_static_port)
            node = g.upstream_node("n", up)
            self.assertEqual(node["connector"], {"type": "http"})
            self.assertEqual(node["dialer"], {"type": "tcp"})

    def test_socks5_with_and_without_auth(self):
        up = self.parse("10.0.0.1:1080:socks5:alice:s3cret")
        self.assertTrue(up.has_static_port)
        node = g.upstream_node("n", up)
        self.assertEqual(node["addr"], "10.0.0.1:1080")
        self.assertEqual(node["connector"], {
            "type": "socks5", "auth": {"username": "alice", "password": "s3cret"}})
        self.assertEqual(node["dialer"], {"type": "tcp"})
        node = g.upstream_node("n", self.parse("10.0.0.1:1080:socks5"))
        self.assertEqual(node["connector"], {"type": "socks5"})

    def test_http_https_httpsquid(self):
        http = self.parse("proxy.example.com:3128:http:u:p")
        self.assertTrue(http.has_static_port)
        self.assertEqual(g.upstream_node("n", http)["connector"]["type"], "http")
        self.assertEqual(g.upstream_node("n", http)["dialer"]["type"], "tcp")

        https = self.parse("proxy.example.com:443:https:u:p")
        self.assertTrue(https.has_static_port)
        self.assertEqual(g.upstream_node("n", https)["connector"]["type"], "http")
        self.assertEqual(g.upstream_node("n", https)["dialer"]["type"], "tls")

        squid = self.parse("zproxy.lum-superproxy.io:22225:httpsquid:user-x:pw")
        self.assertFalse(squid.has_static_port)
        node = g.upstream_node("n", squid)
        self.assertEqual(node["connector"], {
            "type": "http", "auth": {"username": "user-x", "password": "pw"}})
        self.assertEqual(node["dialer"]["type"], "tcp")
        self.assertFalse(self.parse("1.2.3.4:3128:httpsquid").has_static_port)

    def test_scheme_is_case_insensitive(self):
        self.assertEqual(self.parse("1.2.3.4:1080:SOCKS5").scheme, "socks5")

    def test_password_may_contain_colons(self):
        up = self.parse("1.2.3.4:1080:socks5:user:pa:ss:word")
        self.assertEqual((up.username, up.password), ("user", "pa:ss:word"))

    def test_email_username(self):
        up = self.parse("89.187.161.86:80:httpsquid:me@example.com:pw")
        self.assertEqual(up.username, "me@example.com")

    def test_ipv6(self):
        up = self.parse("[::1]:1080:socks5:u:p")
        self.assertEqual(up.host, "::1")
        self.assertEqual(up.addr, "[::1]:1080")
        self.assertEqual(g.upstream_node("n", up)["addr"], "[::1]:1080")
        up = self.parse("[2001:DB8::0:1]:3128")
        self.assertEqual(up.addr, "[2001:db8::1]:3128")
        self.assertEqual(up.scheme, "")

    def test_bare_ipv6_is_rejected_with_hint(self):
        self.assertInvalid("::1:1080", "brackets")
        self.assertInvalid("2001:db8::1:3128:socks5", "brackets")

    def test_invalid_ipv6(self):
        self.assertInvalid("[::1:1080", "unterminated")
        self.assertInvalid("[::1]1080", "expected ':port'")
        self.assertInvalid("[::zz]:1080", "invalid IPv6")
        self.assertInvalid("[fe80::1%eth0]:1080", "zone")

    def test_comments_and_blank_lines(self):
        for line in ("", "   ", "# comment", "   # indented comment", "\r\n"):
            self.assertIsNone(self.parse(line))

    def test_crlf(self):
        up = self.parse("1.2.3.4:1080:socks5:u:p\r\n")
        self.assertEqual(up.password, "p")

    def test_invalid_ports(self):
        self.assertInvalid("1.2.3.4:0", "out of range")
        self.assertInvalid("1.2.3.4:65536", "out of range")
        self.assertInvalid("1.2.3.4:abc", "invalid port")
        self.assertInvalid("1.2.3.4:\u00b2", "invalid port")
        self.assertInvalid("1.2.3.4", "missing port")
        self.assertInvalid("1.2.3.4:", "missing port")
        self.assertEqual(self.parse("1.2.3.4:65535").port, 65535)
        self.assertEqual(self.parse("1.2.3.4:1").port, 1)

    def test_invalid_hosts(self):
        self.assertInvalid(":8080", "missing host")
        self.assertInvalid("999.1.1.1:8080", "invalid IPv4")
        self.assertInvalid("bad host:8080", "whitespace")
        self.assertInvalid('"host":8080', "quotes")
        self.assertInvalid("ho$t:8080", "invalid hostname")
        self.assertInvalid("a..b:8080", "invalid hostname")

    def test_invalid_scheme_and_credentials(self):
        self.assertInvalid("1.2.3.4:1080:socks4", "unknown scheme")
        self.assertInvalid("1.2.3.4:1080:socks5:user", "both")
        self.assertInvalid("1.2.3.4:1080:socks5::pass", "both")
        self.assertInvalid("1.2.3.4:1080::user:pass", "need a scheme")
        self.assertInvalid("1.2.3.4:1080:socks5:us er:pass", "username")
        self.assertInvalid("1.2.3.4:1080:socks5:user:pa ss", "password")
        self.assertInvalid("1.2.3.4:1080:socks5:user:pa'ss", "password")
        self.assertInvalid('1.2.3.4:1080:socks5:us"er:pass', "username")
        self.assertInvalid("1.2.3.4:1080:socks5:user:pa\\ss", "password")
        self.assertInvalid("1.2.3.4:1080:socks5:user:pa\tss", "password")

    def test_empty_credentials_mean_no_auth(self):
        up = self.parse("1.2.3.4:1080:socks5::")
        self.assertEqual((up.username, up.password), ("", ""))


class ProxyListFileTests(RepoTestCase):
    def test_invalid_lines_are_skipped_with_line_numbers(self):
        repo = self.repo(
            "# header\n"
            "\n"
            "1.2.3.4:1080:socks5:u:p\r\n"
            "not a proxy\n"
            "5.6.7.8:99999\n"
            "5.6.7.8:3128\n")
        repo.run()
        self.assertEqual([n["addr"] for n in repo.nodes()],
                         ["1.2.3.4:1080", "5.6.7.8:3128"])
        joined = "\n".join(repo.warnings)
        self.assertIn("proxyList.txt:4:", joined)
        self.assertIn("proxyList.txt:5:", joined)
        self.assertNotIn("proxyList.txt:1:", joined)

    def test_warnings_are_capped(self):
        warnings = []
        path = Path(self.repo().root) / "big.txt"
        path.write_text("bad line\n" * 50)
        self.assertEqual(g.read_proxy_list(path, True, warnings.append), [])
        self.assertEqual(len(warnings), g.MAX_WARNINGS_PER_FILE + 1)
        self.assertIn("30 more", warnings[-1])

    def test_duplicates_are_dropped(self):
        repo = self.repo("1.2.3.4:1080:socks5:u:p\n1.2.3.4:1080:socks5:u:p\n",
                         public="1.2.3.4:1080:socks5:u:p\n9.9.9.9:80\n")
        repo.run()
        self.assertEqual([n["name"] for n in repo.nodes()],
                         ["proxy-1", "public-1"])
        self.assertEqual(len([w for w in repo.warnings if "duplicate" in w]), 2)

    def test_missing_proxy_list_with_vpn_is_fine(self):
        repo = self.repo()
        (repo.root / "proxyList.txt").unlink()
        repo.vpn("only")
        repo.run()
        self.assertEqual([n["name"] for n in repo.nodes()], ["vpn-1"])


class PortAssignmentTests(RepoTestCase):
    def test_static_ports_follow_proxy_list_then_vpn_dirs(self):
        repo = self.repo(
            "1.1.1.1:80\n"                         # public: pool only
            "2.2.2.2:1080:socks5:u:p\n"            # 30000
            "3.3.3.3:3128:httpsquid:u:p\n"         # pool only
            "4.4.4.4:8080:http\n"                  # 30001
            "5.5.5.5:443:https:u:p\n",             # 30002
            public="6.6.6.6:1080:socks5\n")        # pool only, even with scheme
        repo.vpn("b-vpn")                          # 30004 (sorted after a-vpn)
        repo.vpn("a-vpn")                          # 30003
        repo.run()
        plan = g.build_plan(repo.root, lambda m: None)
        self.assertEqual(
            [(port, node["name"], node["addr"]) for port, node, _ in plan.static],
            [(30000, "proxy-2", "2.2.2.2:1080"),
             (30001, "proxy-4", "4.4.4.4:8080"),
             (30002, "proxy-5", "5.5.5.5:443"),
             (30003, "vpn-1", "dockervpn_1:8888"),
             (30004, "vpn-2", "dockervpn_2:8888")])
        self.assertEqual(
            [n["name"] for n in repo.nodes()],
            ["proxy-1", "proxy-2", "proxy-3", "proxy-4", "proxy-5",
             "vpn-1", "vpn-2", "public-1"])
        config = g.gost_config(plan)
        services = {s["name"]: s for s in config["services"]}
        self.assertEqual(sorted(services), [
            "rotate", "static-30000", "static-30001", "static-30002",
            "static-30003", "static-30004"])
        self.assertEqual(services["rotate"]["addr"], ":3128")
        self.assertEqual(services["static-30003"]["addr"], ":30003")
        chains = {c["name"]: c for c in config["chains"]}
        self.assertEqual(
            chains["static-30003"]["hops"][0]["nodes"][0]["addr"],
            "dockervpn_1:8888")
        compose = repo.read("docker-compose.yml")
        for port in (3128, 30000, 30001, 30002, 30003, 30004):
            self.assertIn('"${{BIND_ADDR:-0.0.0.0}}:{0}:{0}"'.format(port), compose)
        self.assertNotIn("30005", compose)
        self.assertIn("container_name: dockervpn_1", compose)
        self.assertIn("./openvpn/a-vpn/vpn.ovpn:/gluetun/custom.conf:ro", compose)

    def test_too_many_static_ports(self):
        lines = "".join("10.0.{}.{}:1080:socks5\n".format(i // 250, i % 250 + 1)
                        for i in range(35537))
        repo = self.repo(lines)
        with self.assertRaises(g.InputError):
            repo.run()


class GostConfigTests(RepoTestCase):
    def config(self, repo):
        return g.gost_config(g.build_plan(repo.root, lambda m: None))

    def test_admission_is_always_a_whitelist(self):
        for users in (None, "", "# nothing\n", "alice secret\n"):
            repo = self.repo("1.2.3.4:1080:socks5\n", users=users)
            config = self.config(repo)
            self.assertEqual(len(config["admissions"]), 1)
            self.assertIs(config["admissions"][0]["whitelist"], True)
            self.assertEqual(config["admissions"][0]["file"]["path"],
                             "/etc/gost/allowed_ip.txt")
            for service in config["services"]:
                self.assertEqual(service["admission"], "allowlist")
            repo.run()
            self.assertIn("whitelist: true", repo.read("config/gost.yaml"))

    def test_users_empty_disables_auth(self):
        repo = self.repo("1.2.3.4:1080:socks5\n",
                         users="# alice secret\n\n   \n")
        repo.run()
        config = self.config(repo)
        self.assertNotIn("authers", config)
        for service in config["services"]:
            self.assertNotIn("auther", service["handler"])
        self.assertNotIn("auther", repo.read("config/gost.yaml"))
        users = repo.read("config/users.txt")
        self.assertEqual([l for l in users.splitlines() if not l.startswith("#")], [])

    def test_users_populated_enables_auth_everywhere(self):
        repo = self.repo("1.2.3.4:1080:socks5\n", users=(
            "# comment\n"
            "alice secret\n"
            "bob\tpa:ss\n"            # tab separator is normalised
            "carol\n"                 # no password: skipped
            "dave:x pw\n"             # ':' in username: skipped
            "alice other\n"           # duplicate: skipped
            "erin 'quoted'\n"))       # quote: skipped
        repo.run()
        config = self.config(repo)
        self.assertEqual(config["authers"], [{
            "name": "users", "reload": "30s",
            "file": {"path": "/etc/gost/users.txt"}}])
        for service in config["services"]:
            self.assertEqual(service["handler"]["auther"], "users")
        users = [l for l in repo.read("config/users.txt").splitlines()
                 if not l.startswith("#")]
        self.assertEqual(users, ["alice secret", "bob pa:ss"])
        self.assertEqual(len([w for w in repo.warnings if "users.txt" in w]), 4)

    def test_pool_hop_is_registered_and_fails_closed(self):
        repo = self.repo("1.2.3.4:1080:socks5\n")
        config = self.config(repo)
        hops = {h["name"]: h for h in config["hops"]}
        chains = {c["name"]: c for c in config["chains"]}
        # The chain references the top-level hop by name only; an inline hop
        # without nodes would silently become a direct connection.
        self.assertEqual(chains["pool"]["hops"], [{"name": "upstreams"}])
        hop = hops["upstreams"]
        self.assertEqual(hop["file"]["path"], "/etc/gost/nodes.json")
        self.assertEqual(hop["reload"], "30s")
        self.assertEqual(hop["selector"],
                         {"strategy": "round", "maxFails": 2, "failTimeout": "60s"})
        self.assertEqual(len(hop["nodes"]), 1)
        self.assertEqual(hop["nodes"][0]["addr"], g.FAIL_CLOSED_ADDR)
        self.assertIs(hop["nodes"][0]["metadata"]["backup"], True)
        rotate = [s for s in config["services"] if s["name"] == "rotate"][0]
        self.assertEqual(rotate["handler"]["chain"], "pool")
        for service in config["services"]:
            self.assertIn(service["handler"]["chain"], chains)
            for hop_ref in chains[service["handler"]["chain"]]["hops"]:
                self.assertTrue(hop_ref.get("nodes") or hop_ref["name"] in hops)

    def test_no_resolvers_and_metrics_local_only(self):
        repo = self.repo("1.2.3.4:1080:socks5\n")
        config = self.config(repo)
        self.assertNotIn("resolvers", config)
        self.assertNotIn("hosts", config)
        self.assertEqual(config["metrics"]["addr"], "127.0.0.1:9000")


class AllowlistTests(RepoTestCase):
    def test_normalises_and_skips_invalid(self):
        repo = self.repo("1.2.3.4:1080:socks5\n", allow=(
            "# comment\n"
            "127.0.0.1   # inline comment\n"
            "10.1.2.3/8\n"
            "::1\n"
            "2001:db8::/32\n"
            "example.com\n"
            "127.0.0.1\n"))
        repo.run()
        entries = [l for l in repo.read("config/allowed_ip.txt").splitlines()
                   if not l.startswith("#")]
        self.assertEqual(entries, ["127.0.0.1", "10.0.0.0/8", "::1", "2001:db8::/32"])
        joined = "\n".join(repo.warnings)
        self.assertIn("normalised", joined)
        self.assertIn("example.com", joined)

    def test_empty_allowlist_is_an_error(self):
        repo = self.repo("1.2.3.4:1080:socks5\n", allow="# nothing\n")
        with self.assertRaises(g.InputError):
            repo.run()
        self.assertFalse(repo.exists("config/gost.yaml"))

    def test_missing_allowlist_is_an_error(self):
        repo = self.repo("1.2.3.4:1080:socks5\n", allow=None)
        with self.assertRaises(g.InputError):
            repo.run()


class VpnTests(RepoTestCase):
    def test_vpn_with_secret_and_ip_remote(self):
        repo = self.repo()
        repo.vpn("nord", secret="  myuser \r\n\r\nmy$pa ss#\"word\r\n")
        repo.run()
        env = repo.read("openvpn/nord/gluetun.env")
        self.assertIn("OPENVPN_USER='myuser'\n", env)
        self.assertIn("OPENVPN_PASSWORD='my$pa ss#\"word'\n", env)
        mode = stat.S_IMODE(os.stat(str(repo.root / "openvpn/nord/gluetun.env")).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(repo.exists("openvpn/nord/gluetun.conf"))
        compose = repo.read("docker-compose.yml")
        self.assertIn('"./openvpn/nord/gluetun.env"', compose)
        self.assertIn('"./openvpn/nord/vpn.ovpn:/gluetun/custom.conf:ro"', compose)
        self.assertNotIn("myuser", compose)
        self.assertNotIn("my$pa", compose)
        self.assertIn("OPENVPN_CUSTOM_CONFIG: /gluetun/custom.conf", compose)
        self.assertIn('HTTPPROXY: "on"', compose)
        self.assertIn("- NET_ADMIN", compose)
        self.assertIn('- "/dev/net/tun:/dev/net/tun"', compose)
        self.assertNotIn("8388", compose)
        self.assertNotIn("8888:", compose)   # gluetun ports are not published

    def test_vpn_without_secret(self):
        repo = self.repo()
        repo.vpn("free", secret=None)
        repo.run()
        self.assertFalse(repo.exists("openvpn/free/gluetun.env"))
        self.assertNotIn("env_file", repo.read("docker-compose.yml"))
        self.assertTrue(any("no 'secret'" in w for w in repo.warnings))

    def test_bad_secret(self):
        repo = self.repo()
        repo.vpn("x", secret="onlyuser\n")
        with self.assertRaises(g.InputError):
            repo.run()
        repo = self.repo()
        repo.vpn("x", secret="user\nit's\n")
        with self.assertRaises(g.InputError):
            repo.run()

    def test_hostname_remote_is_resolved_into_a_copy(self):
        repo = self.repo()
        repo.vpn("surf", ovpn=HOST_OVPN)
        before = (repo.root / "openvpn/surf/vpn.ovpn").read_bytes()
        repo.run(resolver=fake_resolver({
            "vpn.example.com": ["198.51.100.7", "198.51.100.9"],
            "second.example.com": ["203.0.113.5"]}))
        self.assertEqual((repo.root / "openvpn/surf/vpn.ovpn").read_bytes(), before)
        conf = repo.read("openvpn/surf/gluetun.conf")
        self.assertIn("\nremote 198.51.100.7 1443\n", conf)
        self.assertIn("\nremote 203.0.113.5 443 tcp\n", conf)
        self.assertIn("# remote vpn.example.com 1443", conf)
        self.assertIn("# remote commented.example.com 443", conf)
        self.assertIn("remote-random", conf)
        self.assertNotIn("\r", conf)
        self.assertIn('"./openvpn/surf/gluetun.conf:/gluetun/custom.conf:ro"',
                      repo.read("docker-compose.yml"))
        self.assertTrue(any("gluetun only uses the first" in w for w in repo.warnings))

    def test_unresolvable_remote_is_an_error_and_writes_nothing(self):
        repo = self.repo()
        repo.vpn("surf", ovpn=HOST_OVPN)
        with self.assertRaises(g.InputError) as ctx:
            repo.run(resolver=fake_resolver({}))
        self.assertIn("vpn.example.com", str(ctx.exception))
        self.assertFalse(repo.exists("docker-compose.yml"))
        self.assertFalse(repo.exists("config/nodes.json"))

    def test_no_resolve_keeps_the_original(self):
        repo = self.repo()
        repo.vpn("surf", ovpn=HOST_OVPN)
        repo.run(resolve=False)
        self.assertFalse(repo.exists("openvpn/surf/gluetun.conf"))
        self.assertIn("vpn.ovpn:/gluetun/custom.conf", repo.read("docker-compose.yml"))
        self.assertTrue(any("--no-resolve" in w for w in repo.warnings))

    def test_dir_without_ovpn_or_remote(self):
        repo = self.repo("1.2.3.4:1080:socks5\n")
        (repo.root / "openvpn/empty").mkdir()
        with self.assertRaises(g.InputError):
            repo.run()
        repo = self.repo("1.2.3.4:1080:socks5\n")
        repo.vpn("noremote", ovpn="client\ndev tun\n")
        with self.assertRaises(g.InputError):
            repo.run()

    def test_unsafe_dir_name(self):
        repo = self.repo()
        repo.vpn("bad$name")
        with self.assertRaises(g.InputError):
            repo.run()

    def test_files_and_hidden_dirs_in_openvpn_are_ignored(self):
        repo = self.repo("1.2.3.4:1080:socks5\n")
        repo.write("openvpn/.gitkeep", "")
        repo.write("openvpn/.hidden/vpn.ovpn", IP_OVPN)
        repo.run()
        self.assertNotIn("vpn1", repo.read("docker-compose.yml"))


class OutputTests(RepoTestCase):
    PROXIES = ("159.89.206.161:2434:socks5:vpn:unlimited\n"
               "127.0.0.1:44129:httpsquid:mysquidproxy:mysquidpassword\n"
               "172.31.22.222:8080\n")

    def test_nothing_usable_is_an_error(self):
        repo = self.repo("# only comments\nnot valid\n")
        with self.assertRaises(g.InputError) as ctx:
            repo.run()
        self.assertIn("no usable upstream", str(ctx.exception))
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(g.main(["--root", str(repo.root)]), 1)
        self.assertIn("error: no usable upstream", stderr.getvalue())

    def test_idempotent_and_deterministic(self):
        first = self.repo(self.PROXIES, users="alice secret\n")
        first.vpn("v1")
        changed = first.run()
        self.assertTrue(all(changed.values()))
        snapshot = {rel: first.read(rel) for rel in changed}
        again = first.run()
        self.assertFalse(any(again.values()), again)
        self.assertEqual({rel: first.read(rel) for rel in changed}, snapshot)
        second = self.repo(self.PROXIES, users="alice secret\n")
        second.vpn("v1")
        second.run()
        for rel, text in snapshot.items():
            self.assertEqual(second.read(rel), text, rel)

    def test_compose_shape(self):
        repo = self.repo(self.PROXIES)
        repo.run()
        compose = repo.read("docker-compose.yml")
        self.assertFalse(compose.lstrip("#").startswith("version"))
        self.assertNotIn("\nversion:", compose)
        self.assertIn("image: \"gogost/gost:3.3.1@sha256:", compose)
        self.assertIn("container_name: dockergost_rotate", compose)
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn('- "./config:/etc/gost:ro"', compose)
        self.assertIn("soft: 65536", compose)
        self.assertIn("http://127.0.0.1:9000/metrics", compose)
        self.assertIn('max-size: "10m"', compose)
        self.assertNotIn("httpbin", compose)
        for secret in ("unlimited", "mysquidpassword"):
            self.assertNotIn(secret, compose)

    def test_config_hash_label_follows_gost_yaml(self):
        repo = self.repo(self.PROXIES)
        repo.run()
        label = [l for l in repo.read("docker-compose.yml").splitlines()
                 if g.HASH_LABEL in l][0]
        repo.write("template/users.txt", "alice secret\n")
        repo.run()
        label2 = [l for l in repo.read("docker-compose.yml").splitlines()
                  if g.HASH_LABEL in l][0]
        self.assertNotEqual(label, label2)

    def test_file_modes(self):
        repo = self.repo(self.PROXIES)
        repo.run()
        def mode(rel):
            return stat.S_IMODE(os.stat(str(repo.root / rel)).st_mode)
        for rel in ("config/gost.yaml", "config/nodes.json", "config/users.txt"):
            self.assertEqual(mode(rel), 0o600, rel)
        self.assertEqual(mode("config/allowed_ip.txt"), 0o644)
        self.assertEqual(mode("docker-compose.yml"), 0o644)

    def test_nodes_json_format(self):
        repo = self.repo(self.PROXIES)
        repo.run()
        text = repo.read("config/nodes.json")
        nodes = json.loads(text)
        self.assertEqual(len(text.splitlines()), len(nodes) + 2)  # one per line
        for node in nodes:
            self.assertEqual(sorted(node), ["addr", "connector", "dialer", "name"])
        self.assertEqual(len({n["name"] for n in nodes}), len(nodes))

    def test_nodes_only_merges_public_list_and_touches_nothing_else(self):
        repo = self.repo(self.PROXIES)
        repo.vpn("v1")
        repo.run()
        before = {rel: repo.read(rel) for rel in (
            "config/gost.yaml", "docker-compose.yml", "config/allowed_ip.txt",
            "config/users.txt", "openvpn/v1/gluetun.env")}
        repo.write("publicProxyList.txt", "8.8.8.8:3128\n[2001:db8::2]:8080\nbad\n")
        repo.write("template/allowed_ip.txt", "10.9.9.9\n")
        repo.warnings.clear()
        written = repo.run(nodes_only=True)
        self.assertEqual(list(written), ["config/nodes.json"])
        names = [n["name"] for n in repo.nodes()]
        self.assertEqual(names, ["proxy-1", "proxy-2", "proxy-3", "vpn-1",
                                 "public-1", "public-2"])
        self.assertEqual(repo.nodes()[-1]["addr"], "[2001:db8::2]:8080")
        for rel, text in before.items():
            self.assertEqual(repo.read(rel), text, rel)
        self.assertFalse(any("out of date" in w for w in repo.warnings))
        self.assertTrue(any("publicProxyList.txt:3" in w for w in repo.warnings))

    def test_nodes_only_warns_when_full_run_needed(self):
        repo = self.repo(self.PROXIES)
        repo.run()
        repo.write("proxyList.txt", self.PROXIES + "9.9.9.9:1080:socks5\n")
        repo.run(nodes_only=True)
        self.assertTrue(any("out of date" in w for w in repo.warnings))

    def test_nodes_only_without_previous_run(self):
        repo = self.repo(self.PROXIES)
        repo.run(nodes_only=True)
        self.assertTrue(repo.exists("config/nodes.json"))
        self.assertFalse(repo.exists("docker-compose.yml"))


try:  # optional cross-check, PyYAML is not needed by the generator
    import yaml
except ImportError:  # pragma: no cover
    yaml = None


class YamlEmitterTests(unittest.TestCase):
    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_output_round_trips_through_a_real_yaml_parser(self):
        repo = Repo(OutputTests.PROXIES + "[2001:db8::1]:1080:socks5:u:p:w\n",
                    users="alice secret\n")
        self.addCleanup(repo.close)
        repo.vpn("v1")
        repo.run()
        plan = g.build_plan(repo.root, lambda m: None)
        self.assertEqual(yaml.safe_load(repo.read("config/gost.yaml")),
                         g.gost_config(plan))
        compose = yaml.safe_load(repo.read("docker-compose.yml"))
        self.assertEqual(compose["services"]["vpn1"]["environment"]["HTTPPROXY"], "on")
        self.assertEqual(compose["services"]["gost"]["ports"][0],
                         "${BIND_ADDR:-0.0.0.0}:3128:3128")

    def test_block_style_and_quoting(self):
        text = g.to_yaml({
            "services": [{"name": "rotate", "addr": ":3128",
                          "handler": {"type": "http", "retries": 2},
                          "list": ["-C", "/etc/gost/gost.yaml"],
                          "empty": [], "none": {}}],
            "flags": {"on": "on", "yes": True, "num": "30s",
                      "port": "30000:30000", "dollar": "${BIND_ADDR:-0.0.0.0}:1:1",
                      "quote": 'a"b', "unicode": "\u00e9"},
        })
        self.assertEqual(text, (
            "services:\n"
            "  - name: rotate\n"
            "    addr: \":3128\"\n"
            "    handler:\n"
            "      type: http\n"
            "      retries: 2\n"
            "    list:\n"
            "      - \"-C\"\n"
            "      - /etc/gost/gost.yaml\n"
            "    empty: []\n"
            "    none: {}\n"
            "flags:\n"
            "  \"on\": \"on\"\n"
            "  \"yes\": true\n"
            "  num: \"30s\"\n"
            "  port: \"30000:30000\"\n"
            "  dollar: \"${BIND_ADDR:-0.0.0.0}:1:1\"\n"
            "  quote: \"a\\\"b\"\n"
            "  unicode: \"\\u00e9\"\n"))

    def test_nested_lists(self):
        self.assertEqual(g.to_yaml({"a": [[1, 2], {"b": [{"c": "d"}]}]}), (
            "a:\n"
            "  - - 1\n"
            "    - 2\n"
            "  - b:\n"
            "      - c: d\n"))


if __name__ == "__main__":
    unittest.main()
