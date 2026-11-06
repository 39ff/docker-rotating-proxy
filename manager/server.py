#!/usr/bin/env python3
"""Web UI for docker-rotating-proxy.

A small, standard-library-only HTTP server that edits the same files the
README describes (proxyList.txt, template/allowed_ip.txt, openvpn/<name>/),
runs setup/generate.py and `docker compose up -d`, and shows the state of
the stack. The files stay the source of truth: the UI appends and removes
single lines, so hand-written comments and the command line keep working.

  python3 manager/server.py [--root DIR] [--listen 127.0.0.1:8080]

Normally started by install.sh through manager/compose.yml. State of the UI
itself (password hash, setup token, backups) lives in <root>/.manager/.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import io
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "setup"))
sys.path.insert(0, str(HERE))

import formats  # noqa: E402
import generate  # noqa: E402

STATIC_DIR = HERE / "static"
SESSION_COOKIE = "rpx_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 10
MAX_BODY = 2 * 1024 * 1024
MAX_OVPN = 256 * 1024
BACKUPS_KEPT = 30
CSRF_HEADER = "X-RPX-Request"
ROTATION_PROBES = 6  # requests through port 3128 per test
LOGIN_CONCURRENCY = 2  # password checks running at the same time
MAX_TRACKED_FAILURES = 10000  # addresses remembered for the login rate limit

DEFAULT_PROBE_URL = "https://api.ipify.org"
_IP_IN_TEXT = re.compile(r"[0-9A-Fa-f:.]{3,45}")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


class Config:
    def __init__(self, root: Path, docker: str = "docker", probe_url: str = DEFAULT_PROBE_URL,
                 proxy_host: str = "127.0.0.1", trust_forwarded: bool = False,
                 python: str = sys.executable or "python3", public_host: str = ""):
        self.root = root
        self.docker = docker
        self.probe_url = probe_url
        self.proxy_host = proxy_host
        self.trust_forwarded = trust_forwarded
        self.python = python
        self.public_host = public_host  # host shown in the connection examples

    @property
    def state_dir(self) -> Path:
        return self.root / ".manager"

    @property
    def proxy_list(self) -> Path:
        return self.root / "proxyList.txt"

    @property
    def public_list(self) -> Path:
        return self.root / "publicProxyList.txt"

    @property
    def allowlist(self) -> Path:
        return self.root / "template" / "allowed_ip.txt"

    @property
    def openvpn_dir(self) -> Path:
        return self.root / "openvpn"


def read_dotenv(path: Path) -> Dict[str, str]:
    """Minimal .env reader (KEY=value, optional quotes); enough for BIND_ADDR/PROXY_SUBNET."""
    values: Dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, UnicodeDecodeError):
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def system_allowlist_entries(cfg: Config) -> List[str]:
    """Allowlist entries the stack itself needs (host-local clients, healthchecks)."""
    subnet = read_dotenv(cfg.root / ".env").get("PROXY_SUBNET", generate.DEFAULT_SUBNET)
    entries = ["127.0.0.1"]
    try:
        net = ipaddress.ip_network(subnet, strict=False)
        entries.insert(0, str(net.network_address + 1))
    except ValueError:
        pass
    return entries


# --------------------------------------------------------------------------
# Password, setup token and sessions
# --------------------------------------------------------------------------


def hash_password(password: str, salt: Optional[bytes] = None, iterations: Optional[int] = None) -> Dict:
    salt = salt or secrets.token_bytes(16)
    iterations = iterations or PBKDF2_ITERATIONS
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"algorithm": "pbkdf2_sha256", "iterations": iterations,
            "salt": base64.b64encode(salt).decode(), "hash": base64.b64encode(digest).decode()}


def verify_password(password: str, record: Dict) -> bool:
    try:
        salt = base64.b64decode(record["salt"])
        expected = base64.b64decode(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


class Auth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.sessions: Dict[str, float] = {}
        self.failures: Dict[str, Tuple[int, float]] = {}  # ip -> (count, blocked until)
        self.verify_slots = threading.BoundedSemaphore(LOGIN_CONCURRENCY)

    @property
    def auth_file(self) -> Path:
        return self.cfg.state_dir / "auth.json"

    @property
    def token_file(self) -> Path:
        return self.cfg.state_dir / "setup_token"

    def ensure_state_dir(self) -> None:
        self.cfg.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.chmod(self.cfg.state_dir, 0o700)
        except PermissionError:
            pass

    def setup_required(self) -> bool:
        return not self.auth_file.is_file()

    def setup_token(self) -> str:
        """The one-time token for the first-run page; created when missing."""
        self.ensure_state_dir()
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
        except FileNotFoundError:
            pass
        token = secrets.token_urlsafe(24)
        generate.write_file(self.token_file, token + "\n", 0o600)
        return token

    def complete_setup(self, token: str, password: str) -> str:
        with self.lock:
            if not self.setup_required():
                raise ApiError(HTTPStatus.CONFLICT, "setup_done", "The admin password is already set.")
            if not hmac.compare_digest(token.encode(), self.setup_token().encode()):
                raise ApiError(HTTPStatus.FORBIDDEN, "bad_token",
                               "The setup token is wrong. Use the URL printed by install.sh "
                               "(or `docker logs rotating_proxy_manager`).")
            check_password_strength(password)
            self.ensure_state_dir()
            generate.write_file(self.auth_file, json.dumps(hash_password(password), indent=2) + "\n", 0o600)
            try:
                self.token_file.unlink()
            except FileNotFoundError:
                pass
            return self._new_session()

    def login(self, password: str, client_ip: str) -> str:
        with self.lock:
            count, until = self.failures.get(client_ip, (0, 0.0))
            if until > time.time():
                raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited",
                               "Too many failed logins. Try again in %d s." % int(until - time.time() + 1))
        try:
            record = json.loads(self.auth_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            raise ApiError(HTTPStatus.CONFLICT, "setup_required", "Run the first-time setup first.") from None
        # PBKDF2 takes a noticeable time: never under self.lock, which every
        # authenticated request needs, and only a few at once.
        if not self.verify_slots.acquire(blocking=False):
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "busy_login", "Too many logins at once. Try again.")
        try:
            ok = verify_password(password, record)
        finally:
            self.verify_slots.release()
        with self.lock:
            if not ok:
                now = time.time()
                count, _ = self.failures.get(client_ip, (0, 0.0))
                count += 1
                delay = 0 if count < 5 else min(900, 2 ** (count - 4))
                self.failures[client_ip] = (count, now + delay)
                if len(self.failures) > MAX_TRACKED_FAILURES:
                    self.failures = {k: v for k, v in self.failures.items() if v[1] > now - 3600}
                raise ApiError(HTTPStatus.UNAUTHORIZED, "bad_password", "Wrong password.")
            self.failures.pop(client_ip, None)
            return self._new_session()

    def _new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v > now}
        self.sessions[token] = now + SESSION_TTL
        return token

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        with self.lock:
            expires = self.sessions.get(token)
            if expires is None:
                return False
            if expires < time.time():
                del self.sessions[token]
                return False
            return True

    def logout(self, token: Optional[str]) -> None:
        with self.lock:
            self.sessions.pop(token or "", None)


def check_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ApiError(HTTPStatus.BAD_REQUEST, "weak_password",
                       "Use at least %d characters." % MIN_PASSWORD)


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def read_text_lines(path: Path) -> List[str]:
    if not path.is_file():
        return []
    return generate.read_lines(path)


def line_id(lineno: int, text: str) -> str:
    return "%d-%s" % (lineno, hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()[:10])


def backup(cfg: Config, path: Path) -> None:
    if not path.is_file():
        return
    bdir = cfg.state_dir / "backups"
    bdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = bdir / ("%s.%s.%s" % (path.name, stamp, secrets.token_hex(2)))
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    old = sorted(p for p in bdir.iterdir() if p.name.startswith(path.name + "."))
    for p in old[:-BACKUPS_KEPT]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def write_lines(cfg: Config, path: Path, lines: List[str], mode: Optional[int] = None) -> None:
    if mode is None:
        mode = (path.stat().st_mode & 0o777) if path.exists() else 0o644
    backup(cfg, path)
    content = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    generate.write_file(path, content, mode)


def quiet_reporter() -> generate.Reporter:
    return generate.Reporter(stream=io.StringIO(), quiet=True, limit=10 ** 6)


def compute_plan(cfg: Config) -> Dict:
    """What generate.py would produce from the current files, without writing anything."""
    rep = quiet_reporter()
    upstreams = generate.parse_proxy_file(cfg.proxy_list, rep)
    public = generate.parse_proxy_file(cfg.public_list, rep, public_only=True)
    upstreams, public = generate.dedupe(upstreams, public, rep)
    vpns = generate.discover_vpns(cfg.openvpn_dir, rep)
    allow = generate.parse_allowlist(cfg.allowlist, rep)
    plan = generate.build_plan(upstreams, public, vpns)
    static_by_source: Dict[str, int] = {}
    n = 0
    for up in upstreams:
        if up.scheme in ("socks5", "http", "https"):
            static_by_source[up.source] = generate.STATIC_BASE_PORT + n
            n += 1
    vpn_ports = {}
    for vpn in vpns:
        vpn_ports[vpn.name] = generate.STATIC_BASE_PORT + n
        n += 1
    return {
        "upstreams": upstreams,
        "public": public,
        "vpns": vpns,
        "allowlist": allow,
        "plan": plan,
        "static_by_source": static_by_source,
        "vpn_ports": vpn_ports,
        "warnings": rep.warnings,
    }


def inputs_digest(cfg: Config) -> str:
    """Digest of everything a full apply reads (publicProxyList.txt excluded:
    it is applied by the refresh job / cron without a full apply)."""
    digest = hashlib.sha256()
    paths = [cfg.proxy_list, cfg.allowlist, cfg.root / "template" / "squid.conf", cfg.root / ".env"]
    if cfg.openvpn_dir.is_dir():
        for d in sorted(cfg.openvpn_dir.iterdir()):
            if d.is_dir():
                paths.extend(sorted(p for p in d.iterdir() if p.is_file() and (p.suffix == ".ovpn" or p.name == "secret")))
    for p in paths:
        digest.update(str(p.relative_to(cfg.root)).encode())
        digest.update(b"\0")
        try:
            digest.update(p.read_bytes())
        except FileNotFoundError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


# --------------------------------------------------------------------------
# Upstreams
# --------------------------------------------------------------------------


def list_upstreams(cfg: Config) -> Dict:
    info = compute_plan(cfg)
    lines = read_text_lines(cfg.proxy_list)
    entries = []
    invalid = []
    for lineno, text in enumerate(lines, 1):
        source = "proxyList.txt:%d" % lineno
        try:
            up = generate.parse_proxy_line(text, source)
        except generate.LineError as exc:
            invalid.append({"id": line_id(lineno, text), "line": lineno, "error": str(exc),
                            "text": mask_line(text)})
            continue
        if up is None:
            continue
        item = formats.describe(up)
        item.update({"id": line_id(lineno, text), "line": lineno,
                     "static_port": info["static_by_source"].get(source)})
        entries.append(item)
    public_count = len(info["public"])
    return {"entries": entries, "invalid": invalid, "public_count": public_count,
            "static_ports": len(info["plan"].static_ports)}


def mask_line(text: str) -> str:
    """Hide everything after the fourth ':' (the password) of a proxy line."""
    parts = text.strip().split(":", 4)
    if len(parts) == 5 and parts[4]:
        parts[4] = "***"
    return ":".join(parts)[:200]


def preview_upstreams(text: str, default_scheme: str) -> List[Dict]:
    rows = []
    for conv in formats.convert_text(text, default_scheme):
        # The raw input may be host:port:user:pass, where mask_line() would
        # not hide the password; show the converted line instead.
        row = {"input": mask_line(conv.line) if conv.ok else conv.raw[:200], "ok": conv.ok,
               "format": conv.fmt, "error": conv.error}
        if conv.ok:
            row.update(formats.describe(conv.upstream))
        rows.append(row)
    return rows


def add_upstreams(cfg: Config, text: str, default_scheme: str) -> Dict:
    converted = formats.convert_text(text, default_scheme)
    lines = read_text_lines(cfg.proxy_list)
    existing = set()
    for text_line in lines:
        try:
            up = generate.parse_proxy_line(text_line)
        except generate.LineError:
            continue
        if up is not None:
            existing.add((up.host, up.port, up.scheme, up.user, up.password))
    added, duplicates, errors = [], 0, []
    for conv in converted:
        if not conv.ok:
            errors.append({"input": conv.raw[:200], "error": conv.error})
            continue
        up = conv.upstream
        key = (up.host, up.port, up.scheme, up.user, up.password)
        if key in existing:
            duplicates += 1
            continue
        existing.add(key)
        added.append(conv.line)
    if added:
        write_lines(cfg, cfg.proxy_list, lines + added)
    return {"added": len(added), "duplicates": duplicates, "errors": errors}


def delete_lines(cfg: Config, path: Path, ids: List[str]) -> int:
    lines = read_text_lines(path)
    wanted = set(ids)
    keep, removed = [], 0
    for lineno, text in enumerate(lines, 1):
        if line_id(lineno, text) in wanted:
            removed += 1
            wanted.discard(line_id(lineno, text))
            continue
        keep.append(text)
    if wanted:
        raise ApiError(HTTPStatus.CONFLICT, "stale",
                       "The file changed since the page was loaded. Reload and try again.")
    if removed:
        write_lines(cfg, path, keep)
    return removed


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------


def normalize_ip(value: str) -> str:
    value = value.strip()
    if "/" in value:
        net = ipaddress.ip_network(value, strict=False)
        return str(net)
    addr = ipaddress.ip_address(value)
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        return str(addr.ipv4_mapped)
    return str(addr)


def ip_allowed(ip: str, entries: List[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in entries:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def list_allowlist(cfg: Config) -> Dict:
    system = set(system_allowlist_entries(cfg))
    entries = []
    seen = set()
    for lineno, text in enumerate(read_text_lines(cfg.allowlist), 1):
        value, _, comment = text.partition("#")
        value = value.strip()
        if not value:
            continue
        try:
            norm = normalize_ip(value)
        except ValueError:
            entries.append({"id": line_id(lineno, text), "line": lineno, "value": value,
                            "comment": comment.strip(), "invalid": True, "system": False})
            continue
        entries.append({"id": line_id(lineno, text), "line": lineno, "value": norm,
                        "comment": comment.strip(), "invalid": False, "system": norm in system,
                        "duplicate": norm in seen})
        seen.add(norm)
    return {"entries": entries}


def add_allowlist(cfg: Config, value: str, comment: str) -> Dict:
    try:
        norm = normalize_ip(value)
    except ValueError:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_ip", "%r is not an IP address or CIDR." % value[:60])
    if norm in ("0.0.0.0/0", "::/0"):
        raise ApiError(HTTPStatus.BAD_REQUEST, "open_proxy",
                       "%s would let everyone on the internet use the proxy." % norm)
    comment = re.sub(r"[\r\n]+", " ", comment).strip()[:80]
    current = [e["value"] for e in list_allowlist(cfg)["entries"] if not e["invalid"]]
    if norm in current:
        return {"added": 0}
    lines = read_text_lines(cfg.allowlist)
    line = norm if not comment else "%-24s # %s" % (norm, comment)
    write_lines(cfg, cfg.allowlist, lines + [line])
    return {"added": 1, "value": norm}


def delete_allowlist(cfg: Config, ids: List[str]) -> int:
    entries = {e["id"]: e for e in list_allowlist(cfg)["entries"]}
    for i in ids:
        if i in entries and entries[i]["system"]:
            raise ApiError(HTTPStatus.BAD_REQUEST, "system_entry",
                           "%s is needed by the stack itself (clients on this host, healthchecks)."
                           % entries[i]["value"])
    return delete_lines(cfg, cfg.allowlist, ids)


# --------------------------------------------------------------------------
# VPN
# --------------------------------------------------------------------------


def list_vpns(cfg: Config) -> Dict:
    info = compute_plan(cfg)
    items = []
    for vpn in info["vpns"]:
        items.append({"name": vpn.name, "file": vpn.ovpn.name, "container": vpn.container,
                      "service": vpn.service, "has_credentials": vpn.user is not None,
                      "remote_hosts": vpn.remote_hosts, "static_port": info["vpn_ports"].get(vpn.name)})
    return {"vpns": items}


def add_vpn(cfg: Config, name: str, filename: str, content: str, username: str, password: str) -> Dict:
    name = name.strip()
    if not generate._SAFE_NAME.match(name) or len(name) > 64:
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_name",
                       "Use letters, digits, '.', '_' and '-' for the name (max 64).")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", Path(filename or "vpn.ovpn").name)
    if not filename.endswith(".ovpn"):
        filename += ".ovpn"
    if not generate._SAFE_NAME.match(filename):
        filename = "vpn.ovpn"
    if len(content.encode("utf-8")) > MAX_OVPN:
        raise ApiError(HTTPStatus.BAD_REQUEST, "too_large", "The .ovpn file is larger than 256 KiB.")
    if not re.search(r"^\s*remote\s+\S+", content, re.M):
        raise ApiError(HTTPStatus.BAD_REQUEST, "no_remote", "This does not look like an OpenVPN client "
                       "config (no `remote` line).")
    if (username or password) and not (username and password):
        raise ApiError(HTTPStatus.BAD_REQUEST, "half_credentials", "Give both username and password, or neither.")
    for value in (username, password):
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_credentials", "Credentials must be one line each.")
    vdir = cfg.openvpn_dir / name
    if vdir.exists():
        raise ApiError(HTTPStatus.CONFLICT, "exists", "openvpn/%s already exists." % name)
    vdir.mkdir(parents=True, mode=0o755)
    generate.write_file(vdir / filename, content.replace("\r\n", "\n"), 0o644)
    if username:
        generate.write_file(vdir / "secret", "%s\n%s\n" % (username, password), 0o600)
    return {"name": name, "file": filename}


def delete_vpn(cfg: Config, name: str) -> None:
    if not generate._SAFE_NAME.match(name or ""):
        raise ApiError(HTTPStatus.BAD_REQUEST, "bad_name", "Bad VPN name.")
    vdir = cfg.openvpn_dir / name
    if not vdir.is_dir() or vdir.parent.resolve() != cfg.openvpn_dir.resolve():
        raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "openvpn/%s does not exist." % name)
    bdir = cfg.state_dir / "backups"
    bdir.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.make_archive(str(bdir / ("openvpn-%s.%s" % (name, time.strftime("%Y%m%d-%H%M%S")))),
                        "gztar", root_dir=str(cfg.openvpn_dir), base_dir=name)
    shutil.rmtree(vdir)


# --------------------------------------------------------------------------
# Docker / jobs
# --------------------------------------------------------------------------


def compose_ps(cfg: Config) -> Dict:
    if not (cfg.root / "docker-compose.yml").is_file():
        return {"generated": False, "services": [], "error": ""}
    try:
        proc = subprocess.run([cfg.docker, "compose", "ps", "--all", "--format", "json"], cwd=str(cfg.root),
                              capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"generated": True, "services": [], "error": str(exc)}
    if proc.returncode != 0:
        return {"generated": True, "services": [], "error": (proc.stderr or proc.stdout).strip()[-500:]}
    services = []
    text = proc.stdout.strip()
    items: List[Dict] = []
    if text.startswith("["):
        try:
            items = json.loads(text)
        except ValueError:
            items = []
    else:
        for line in text.splitlines():
            try:
                items.append(json.loads(line))
            except ValueError:
                continue
    for item in items:
        services.append({"service": item.get("Service", ""), "name": item.get("Name", ""),
                         "state": item.get("State", ""), "health": item.get("Health", ""),
                         "status": item.get("Status", "")})
    services.sort(key=lambda s: (s["service"] != "squid", s["service"] != "gost", s["service"]))
    return {"generated": True, "services": services, "error": ""}


class Job:
    def __init__(self, kind: str):
        self.id = secrets.token_hex(6)
        self.kind = kind
        self.status = "running"
        self.started = time.time()
        self.finished: Optional[float] = None
        self.lines: List[str] = []
        self.lock = threading.Lock()

    def log(self, text: str) -> None:
        with self.lock:
            for line in text.rstrip("\n").splitlines() or [""]:
                self.lines.append(line)
            if len(self.lines) > 4000:
                self.lines = self.lines[-4000:]

    def to_json(self) -> Dict:
        with self.lock:
            return {"id": self.id, "kind": self.kind, "status": self.status, "log": "\n".join(self.lines),
                    "started": self.started, "finished": self.finished}


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.jobs: Dict[str, Job] = {}
        self.running: Optional[Job] = None

    def start(self, kind: str, fn: Callable[[Job], bool]) -> Job:
        with self.lock:
            if self.running is not None and self.running.status == "running":
                raise ApiError(HTTPStatus.CONFLICT, "busy", "Another job is still running.",
                               job=self.running.id)
            job = Job(kind)
            self.jobs[job.id] = job
            self.running = job
            if len(self.jobs) > 50:
                for old in sorted(self.jobs.values(), key=lambda j: j.started)[:-50]:
                    self.jobs.pop(old.id, None)

        def run():
            ok = False
            try:
                ok = fn(job)
            except Exception:  # pragma: no cover - logged for the user
                job.log(traceback.format_exc())
            job.status = "succeeded" if ok else "failed"
            job.finished = time.time()

        threading.Thread(target=run, name="job-" + kind, daemon=True).start()
        return job

    def get(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "no_job", "Unknown job.")
        return job


def run_logged(job: Job, cmd: List[str], cwd: Path, env: Optional[Dict[str, str]] = None,
               timeout: int = 900) -> int:
    job.log("$ " + " ".join(cmd))
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, env=full_env, bufsize=1)
    except OSError as exc:
        job.log("error: %s" % exc)
        return 127
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        proc.kill()

    timer = threading.Timer(timeout, kill)  # also fires when the command prints nothing
    timer.start()
    try:
        with proc:
            assert proc.stdout is not None
            for line in proc.stdout:
                job.log(line)
            rc = proc.wait()
    finally:
        timer.cancel()
    if timed_out.is_set():
        job.log("error: timed out after %d s" % timeout)
    return rc


def apply_job(cfg: Config, state: "State") -> Callable[[Job], bool]:
    def fn(job: Job) -> bool:
        digest = inputs_digest(cfg)
        rc = run_logged(job, [cfg.python, str(cfg.root / "setup" / "generate.py"), "--root", str(cfg.root)],
                        cfg.root)
        if rc != 0:
            job.log("generate.py failed (exit %d); nothing was started." % rc)
            return False
        rc = run_logged(job, [cfg.docker, "compose", "up", "-d", "--remove-orphans", "--quiet-pull"], cfg.root)
        if rc != 0:
            job.log("docker compose up failed (exit %d)." % rc)
            return False
        job.log("waiting for squid and gost to become healthy ...")
        deadline = time.time() + 120
        while True:
            ps = compose_ps(cfg)
            core = [s for s in ps["services"] if s["service"] in ("squid", "gost")]
            pending = [s for s in core if s["health"] not in ("healthy", "") or s["state"] != "running"]
            if core and not pending:
                break
            if time.time() > deadline:
                for s in pending:
                    job.log("not healthy: %s (%s %s)" % (s["service"], s["state"], s["health"]))
                return False
            time.sleep(2)
        for s in compose_ps(cfg)["services"]:
            job.log("%-8s %s %s" % (s["service"], s["state"], s["health"]))
        state.mark_applied(digest)
        job.log("done.")
        return True
    return fn


def public_refresh_job(cfg: Config, url: Optional[str]) -> Callable[[Job], bool]:
    def fn(job: Job) -> bool:
        env = {}
        if url is not None:
            env["PUBLIC_PROXY_URL"] = url
        rc = run_logged(job, ["bash", str(cfg.root / "public_proxy_cron.sh")], cfg.root, env=env, timeout=300)
        return rc == 0
    return fn


# --------------------------------------------------------------------------
# Probes
# --------------------------------------------------------------------------


class _AlwaysProxy(urllib.request.BaseHandler):
    """Send every request through one HTTP proxy. urllib's ProxyHandler would
    skip the proxy for hosts matched by the NO_PROXY environment variable."""

    def __init__(self, hostport: str):
        self.hostport = hostport

    def http_request(self, req):
        req.set_proxy(self.hostport, "http")
        return req

    https_request = http_request


def probe_once(cfg: Config, port: int, timeout: float = 10.0) -> Dict:
    host = "[%s]" % cfg.proxy_host if ":" in cfg.proxy_host else cfg.proxy_host
    # ProxyHandler({}) disables the proxies from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _AlwaysProxy("%s:%d" % (host, port)))
    started = time.monotonic()
    try:
        with opener.open(urllib.request.Request(cfg.probe_url, headers={"User-Agent": "rotating-proxy-manager"}),
                         timeout=timeout) as resp:
            body = resp.read(256).decode("utf-8", "replace")
        ms = int((time.monotonic() - started) * 1000)
        match = _IP_IN_TEXT.search(body)
        return {"ok": True, "ip": match.group(0) if match else body.strip()[:64], "ms": ms}
    except urllib.error.HTTPError as exc:
        hint = " (is the manager's address on the allowlist?)" if exc.code == 403 else ""
        return {"ok": False, "error": "HTTP %d%s" % (exc.code, hint)}
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, socket.timeout):
            return {"ok": False, "error": "timeout"}
        return {"ok": False, "error": str(reason)[:120]}
    except (socket.timeout, TimeoutError):
        return {"ok": False, "error": "timeout"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": str(exc)[:120]}


def run_probe(cfg: Config) -> Dict:
    info = compute_plan(cfg)
    targets: List[Tuple[str, int, str]] = [("rotating", generate.SQUID_PORT, "rotation")] * ROTATION_PROBES
    for sp in info["plan"].static_ports:
        targets.append(("static", sp.port, sp.description))
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda t: probe_once(cfg, t[1]), targets))
    rotating = [r for t, r in zip(targets, results) if t[0] == "rotating"]
    static = []
    for t, r in zip(targets, results):
        if t[0] == "static":
            item = {"port": t[1], "description": t[2]}
            item.update(r)
            static.append(item)
    ips = sorted({r["ip"] for r in rotating if r.get("ok")})
    return {"at": time.time(), "probe_url": cfg.probe_url,
            "rotating": {"port": generate.SQUID_PORT, "ok": sum(1 for r in rotating if r.get("ok")),
                         "total": len(rotating), "ips": ips,
                         "ms": [r["ms"] for r in rotating if r.get("ok")],
                         "errors": sorted({r["error"] for r in rotating if not r.get("ok")})},
            "static": static}


# --------------------------------------------------------------------------
# State shared by the handlers
# --------------------------------------------------------------------------


class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth = Auth(cfg)
        self.jobs = Jobs()
        self.files_lock = threading.Lock()
        self.last_probe: Optional[Dict] = None

    @property
    def applied_file(self) -> Path:
        return self.cfg.state_dir / "applied.json"

    def mark_applied(self, digest: str) -> None:
        self.auth.ensure_state_dir()
        generate.write_file(self.applied_file, json.dumps({"inputs": digest, "at": time.time()}) + "\n", 0o600)

    def applied(self) -> Dict:
        try:
            return json.loads(self.applied_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def overview(self, client_ip: str) -> Dict:
        cfg = self.cfg
        info = compute_plan(cfg)
        plan = info["plan"]
        applied = self.applied()
        ps = compose_ps(cfg)
        env = read_dotenv(cfg.root / ".env")
        return {
            "pending": applied.get("inputs") != inputs_digest(cfg) or not ps["generated"],
            "applied_at": applied.get("at"),
            "compose": ps,
            "counts": plan.counts,
            "peers": len(plan.squid_peers),
            "rotating_port": generate.SQUID_PORT,
            "static_ports": [{"port": sp.port, "description": sp.description} for sp in plan.static_ports],
            "warnings": info["warnings"][:50],
            "warnings_total": len(info["warnings"]),
            "client_ip": client_ip,
            "client_allowed": ip_allowed(client_ip, info["allowlist"]),
            "bind_addr": env.get("BIND_ADDR", "0.0.0.0"),
            "public_host": cfg.public_host,
            "last_probe": self.last_probe,
            "job": self.jobs.running.to_json() if self.jobs.running else None,
        }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def client_ip_of(handler: "Handler", cfg: Config) -> str:
    peer = handler.client_address[0]
    if cfg.trust_forwarded:
        try:
            loopback = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            loopback = False
        if loopback:
            # Only the last X-Forwarded-For entry, which the proxy in front
            # appended itself (Caddy, nginx and Cloudflare all do). Headers
            # such as CF-Connecting-IP are passed through unchanged by other
            # proxies, so a client could set them to dodge the rate limit.
            xff = handler.headers.get("X-Forwarded-For") or ""
            forwarded = xff.split(",")[-1].strip()
            if forwarded:
                peer = forwarded
    try:
        return normalize_ip(peer)
    except ValueError:
        return peer


class Handler(BaseHTTPRequestHandler):
    server_version = "rotating-proxy-manager"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 120  # per connection, so idle keep-alive clients do not pin threads
    state: State  # set on the subclass by make_server

    # ---- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):  # quieter, no request bodies
        if os.environ.get("RPX_ACCESS_LOG"):
            sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))

    def _common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def send_json(self, status: int, obj, cookies: Optional[List[str]] = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> None:
        """Read the whole request body up front: a body left unread on a
        keep-alive connection would be parsed as the start of the next request."""
        self._body = b""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_length", "Bad Content-Length.")
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "Request too large.")
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ApiError(HTTPStatus.LENGTH_REQUIRED, "length_required", "Send a Content-Length.")
        if length:
            self._body = self.rfile.read(length)

    def read_json(self) -> Dict:
        raw = getattr(self, "_body", b"")
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "Body is not JSON.")
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_json", "Body must be a JSON object.")
        return data

    def session_token(self) -> Optional[str]:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie") or "")
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else None

    def is_https(self) -> bool:
        return self.state.cfg.trust_forwarded and (self.headers.get("X-Forwarded-Proto") or "") == "https"

    def session_cookie(self, token: str, max_age: int = SESSION_TTL) -> str:
        parts = ["%s=%s" % (SESSION_COOKIE, token), "Path=/", "HttpOnly", "SameSite=Strict",
                 "Max-Age=%d" % max_age]
        if self.is_https():
            parts.append("Secure")
        return "; ".join(parts)

    def check_csrf(self) -> None:
        if self.headers.get(CSRF_HEADER) != "1":
            raise ApiError(HTTPStatus.FORBIDDEN, "csrf", "Missing %s header." % CSRF_HEADER)
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            parsed = urllib.parse.urlsplit(origin)
            if parsed.netloc != host:
                raise ApiError(HTTPStatus.FORBIDDEN, "csrf", "Cross-origin request refused.")

    # ---- routing --------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        path = urllib.parse.urlsplit(self.path).path
        try:
            self._read_body()
            if method == "GET" and not path.startswith("/api/"):
                return self._static(path)
            if not path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
            if method == "POST":
                self.check_csrf()
            route = ROUTES.get((method, path))
            if route is None and method == "GET" and path.startswith("/api/jobs/"):
                route = Handler.api_job
            if route is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Unknown API route.")
            public = route in PUBLIC_ROUTES
            if not public and not self.state.auth.valid(self.session_token()):
                raise ApiError(HTTPStatus.UNAUTHORIZED, "login_required", "Log in first.")
            route(self)
        except ApiError as exc:
            body = {"error": exc.code, "message": exc.message}
            body.update(exc.extra)
            self.send_json(exc.status, body)
        except BrokenPipeError:
            pass
        except Exception:
            traceback.print_exc()
            self.send_json(500, {"error": "internal", "message": "Internal error, see the manager log."})

    def _static(self, path: str) -> None:
        if path == "/healthz":
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path in ("/", "/setup", "/login") or not path.startswith("/static/"):
            target = STATIC_DIR / "index.html"
        else:
            rel = path[len("/static/"):]
            target = (STATIC_DIR / rel).resolve()
            if STATIC_DIR.resolve() not in target.parents or not target.is_file():
                raise ApiError(HTTPStatus.NOT_FOUND, "not_found", "Not found.")
        self.send_file(target)

    # ---- API ------------------------------------------------------------

    def api_session(self):
        auth = self.state.auth
        self.send_json(200, {"setup_required": auth.setup_required(),
                             "authenticated": auth.valid(self.session_token()),
                             "client_ip": client_ip_of(self, self.state.cfg)})

    def api_setup(self):
        data = self.read_json()
        token = self.state.auth.complete_setup(str(data.get("token", "")), str(data.get("password", "")))
        self.send_json(200, {"ok": True}, cookies=[self.session_cookie(token)])

    def api_login(self):
        data = self.read_json()
        token = self.state.auth.login(str(data.get("password", "")), client_ip_of(self, self.state.cfg))
        self.send_json(200, {"ok": True}, cookies=[self.session_cookie(token)])

    def api_logout(self):
        self.state.auth.logout(self.session_token())
        self.send_json(200, {"ok": True}, cookies=[self.session_cookie("", 0)])

    def api_overview(self):
        self.send_json(200, self.state.overview(client_ip_of(self, self.state.cfg)))

    def api_upstreams(self):
        self.send_json(200, list_upstreams(self.state.cfg))

    def api_upstreams_preview(self):
        data = self.read_json()
        scheme = self._default_scheme(data)
        self.send_json(200, {"rows": preview_upstreams(str(data.get("text", "")), scheme)})

    def api_upstreams_add(self):
        data = self.read_json()
        scheme = self._default_scheme(data)
        with self.state.files_lock:
            result = add_upstreams(self.state.cfg, str(data.get("text", "")), scheme)
        self.send_json(200, result)

    def api_upstreams_delete(self):
        ids = self._ids(self.read_json())
        with self.state.files_lock:
            removed = delete_lines(self.state.cfg, self.state.cfg.proxy_list, ids)
        self.send_json(200, {"removed": removed})

    def api_allowlist(self):
        result = list_allowlist(self.state.cfg)
        result["client_ip"] = client_ip_of(self, self.state.cfg)
        self.send_json(200, result)

    def api_allowlist_add(self):
        data = self.read_json()
        with self.state.files_lock:
            result = add_allowlist(self.state.cfg, str(data.get("value", "")), str(data.get("comment", "")))
        self.send_json(200, result)

    def api_allowlist_delete(self):
        ids = self._ids(self.read_json())
        with self.state.files_lock:
            removed = delete_allowlist(self.state.cfg, ids)
        self.send_json(200, {"removed": removed})

    def api_vpns(self):
        self.send_json(200, list_vpns(self.state.cfg))

    def api_vpns_add(self):
        data = self.read_json()
        with self.state.files_lock:
            result = add_vpn(self.state.cfg, str(data.get("name", "")), str(data.get("filename", "")),
                             str(data.get("content", "")), str(data.get("username", "")),
                             str(data.get("password", "")))
        self.send_json(200, result)

    def api_vpns_delete(self):
        data = self.read_json()
        with self.state.files_lock:
            delete_vpn(self.state.cfg, str(data.get("name", "")))
        self.send_json(200, {"ok": True})

    def api_apply(self):
        job = self.state.jobs.start("apply", apply_job(self.state.cfg, self.state))
        self.send_json(202, job.to_json())

    def api_public_refresh(self):
        data = self.read_json()
        url = data.get("url")
        if url is not None:
            url = str(url).strip()
            if url and not re.match(r"^(https?|file)://", url):
                raise ApiError(HTTPStatus.BAD_REQUEST, "bad_url", "Use an http://, https:// or file:// URL.")
        job = self.state.jobs.start("public", public_refresh_job(self.state.cfg, url))
        self.send_json(202, job.to_json())

    def api_job(self):
        job_id = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
        self.send_json(200, self.state.jobs.get(job_id).to_json())

    def api_probe(self):
        result = run_probe(self.state.cfg)
        self.state.last_probe = result
        self.send_json(200, result)

    # ---- helpers --------------------------------------------------------

    @staticmethod
    def _default_scheme(data: Dict) -> str:
        scheme = str(data.get("default_scheme", "http"))
        if scheme not in formats.DEFAULT_SCHEMES:
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_scheme", "Unknown proxy type.")
        return scheme

    @staticmethod
    def _ids(data: Dict) -> List[str]:
        ids = data.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise ApiError(HTTPStatus.BAD_REQUEST, "bad_ids", "Expected a list of ids.")
        return ids


ROUTES: Dict[Tuple[str, str], Callable] = {
    ("GET", "/api/session"): Handler.api_session,
    ("POST", "/api/setup"): Handler.api_setup,
    ("POST", "/api/login"): Handler.api_login,
    ("POST", "/api/logout"): Handler.api_logout,
    ("GET", "/api/overview"): Handler.api_overview,
    ("GET", "/api/upstreams"): Handler.api_upstreams,
    ("POST", "/api/upstreams/preview"): Handler.api_upstreams_preview,
    ("POST", "/api/upstreams/add"): Handler.api_upstreams_add,
    ("POST", "/api/upstreams/delete"): Handler.api_upstreams_delete,
    ("GET", "/api/allowlist"): Handler.api_allowlist,
    ("POST", "/api/allowlist/add"): Handler.api_allowlist_add,
    ("POST", "/api/allowlist/delete"): Handler.api_allowlist_delete,
    ("GET", "/api/vpns"): Handler.api_vpns,
    ("POST", "/api/vpns/add"): Handler.api_vpns_add,
    ("POST", "/api/vpns/delete"): Handler.api_vpns_delete,
    ("POST", "/api/apply"): Handler.api_apply,
    ("POST", "/api/public/refresh"): Handler.api_public_refresh,
    ("POST", "/api/probe"): Handler.api_probe,
}
PUBLIC_ROUTES = {Handler.api_session, Handler.api_setup, Handler.api_login, Handler.api_logout}


def parse_listen(value: str) -> Tuple[str, int]:
    host, _, port = value.rpartition(":")
    host = host.strip("[]") or "0.0.0.0"
    return host, int(port)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, handler):
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, handler)

    def handle_error(self, request, client_address):
        # Clients that hang up (port scanners, TLS hellos on a plain-HTTP port)
        # are not worth a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError)):
            return
        super().handle_error(request, client_address)


def make_server(cfg: Config, listen: str) -> Tuple[Server, State]:
    state = State(cfg)
    handler = type("BoundHandler", (Handler,), {"state": state})
    host, port = parse_listen(listen)
    return Server((host, port), handler), state


def main(argv: Optional[List[str]] = None) -> int:
    default_root = Path(os.environ.get("RPX_ROOT") or HERE.parent)
    parser = argparse.ArgumentParser(description="Web UI for docker-rotating-proxy.")
    parser.add_argument("--root", type=Path, default=default_root, help="checkout to manage (default: %(default)s)")
    parser.add_argument("--listen", default=os.environ.get("RPX_LISTEN", "127.0.0.1:8080"),
                        help="address:port to listen on (default: %(default)s)")
    parser.add_argument("--docker", default=os.environ.get("RPX_DOCKER", "docker"), help="docker CLI to use")
    parser.add_argument("--probe-url", default=os.environ.get("RPX_PROBE_URL", DEFAULT_PROBE_URL),
                        help="URL that returns the caller's IP, used by 'Test' (default: %(default)s)")
    parser.add_argument("--proxy-host", default=os.environ.get("RPX_PROXY_HOST", "127.0.0.1"),
                        help="where the manager reaches the published proxy ports (default: %(default)s)")
    parser.add_argument("--trust-forwarded", action="store_true",
                        default=os.environ.get("RPX_TRUST_FORWARDED", "") not in ("", "0", "false"),
                        help="behind a reverse proxy on localhost: take the client IP from the last "
                             "X-Forwarded-For entry")
    parser.add_argument("--public-host", default=os.environ.get("RPX_PUBLIC_HOST", ""),
                        help="host name or IP shown in the connection examples (default: the host of the page)")
    args = parser.parse_args(argv)
    cfg = Config(args.root.resolve(), docker=args.docker, probe_url=args.probe_url,
                 proxy_host=args.proxy_host, trust_forwarded=args.trust_forwarded,
                 public_host=args.public_host)
    if not (cfg.root / "setup" / "generate.py").is_file():
        print("error: %s is not a docker-rotating-proxy checkout" % cfg.root, file=sys.stderr)
        return 1
    server, state = make_server(cfg, args.listen)
    host, port = server.server_address[:2]
    print("rotating-proxy manager on http://%s:%d (root %s)" % (host, port, cfg.root), flush=True)
    if state.auth.setup_required():
        shown = cfg.public_host or ("<this-server>" if host in ("0.0.0.0", "::") else host)
        if ":" in shown and not shown.startswith("["):
            shown = "[%s]" % shown
        print("first-time setup: http://%s:%d/setup?token=%s" % (shown, port, state.auth.setup_token()),
              flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
