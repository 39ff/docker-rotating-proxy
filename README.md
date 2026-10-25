# Docker Rotating Proxy

A self-hosted rotating proxy, configured from a plain text list.

- **Port 3128** rotates every request across all upstreams (round-robin), powered by [Squid](https://www.squid-cache.org/) 7.
- **Ports 30000, 30001, ...** each stick to one upstream, for browser automation (Selenium, Puppeteer, Playwright) that needs the same exit IP for a whole session.
- Upstreams can be public HTTP proxies, HTTP/HTTPS proxies with or without credentials, SOCKS5 proxies (converted to HTTP by one [gost v3](https://gost.run/) container) and OpenVPN configs (each run in a [gluetun](https://github.com/qdm12/gluetun) container).
- Access is limited by client IP address on every published port, so programs can use the proxy without credentials.
- Request headers that reveal the proxy (`Via`, `X-Forwarded-For`) are removed; nothing is cached.

```
client ── :3128 ───▶ Squid ── round-robin ──┬──▶ public / httpsquid / https proxies ──┐
(allowlisted)                               ├──▶ gost ──▶ socks5 / http proxies ─────┤
                                            └──▶ gluetun (OpenVPN) ──────────────────┼──▶ Internet
client ── :30000+ ─▶ gost: one port = one upstream or one VPN ───────────────────────┘
(allowlisted)
```

See [docs/DESIGN.md](docs/DESIGN.md) for the design, the trade-offs and what is verified by CI.

## Quick start

Requirements: Docker with the Compose plugin (`docker compose`), and Python 3.8+ or Docker to run the generator.

```sh
git clone https://github.com/39ff/docker-rotating-proxy
cd docker-rotating-proxy

$EDITOR proxyList.txt            # add your upstreams (format below)
$EDITOR template/allowed_ip.txt  # add the IP addresses of your clients

python3 setup/generate.py
# or, without a local Python:
# docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/app" -w /app python:3.13-alpine python3 setup/generate.py

docker compose up -d --remove-orphans
docker compose ps                # squid and gost should become "healthy"

curl -x http://127.0.0.1:3128 https://api.ipify.org    # a different upstream per request
curl -x http://127.0.0.1:30000 https://api.ipify.org   # always the first socks5/http/https upstream
```

The generator prints which static port belongs to which upstream:

```
static port 30000 -> socks5 203.0.113.10:1080 (proxyList.txt:2)
static port 30001 -> vpn openvpn/my-vpn (dockervpn_1)
```

After **any** change to `proxyList.txt`, `openvpn/` or the templates, run the same two commands again:
`python3 setup/generate.py && docker compose up -d --remove-orphans`. Only containers whose configuration
changed are recreated (the generator labels each service with a hash of its config files). Changes to
`publicProxyList.txt` are applied without a restart by `public_proxy_cron.sh` (see below).

## proxyList.txt

One upstream per line: `host:port[:scheme[:username:password]]`. Lines starting with `#` and blank lines are ignored;
Windows line endings are fine.

| scheme | upstream | rotating port 3128 | static port |
|---|---|---|---|
| *(empty)* | public HTTP proxy without auth | Squid `cache_peer` | no |
| `httpsquid` | HTTP proxy, credentials sent by Squid (`login=`) | Squid `cache_peer` | no |
| `http` | HTTP proxy | Squid → gost → upstream | yes |
| `socks5` | SOCKS5 proxy (hostnames are resolved by the upstream) | Squid → gost → upstream | yes |
| `https` | HTTP proxy that is reached over TLS (issue #23) | Squid `cache_peer ... tls` | yes (gost, TLS) |

```
203.0.113.10:1080:socks5:yourUsername:yourPassword
203.0.113.11:8080:http:yourUsername:yourPassword
proxy.example.com:443:https:yourUsername:yourPassword
zproxy.example.net:22225:httpsquid:yourUsername:yourPassword
198.51.100.7:3128
[2001:db8::1]:1080:socks5:yourUsername:yourPassword
```

- IPv6 addresses must be written in brackets.
- Ports must be 1-65535. The password is everything after the fourth `:` and may itself contain `:`.
- Hosts and credentials must not contain whitespace, quotes or `#`. Invalid lines are skipped with a warning that
  names the line number.
- `https` upstreams are verified against the system CA store, using the host name from the line, so use the name that
  is in the proxy's certificate (not its IP address).
- Static ports are assigned in file order: first every `socks5`/`http`/`https` line, then every VPN directory.
  Adding a line in the middle shifts the following static ports.

## OpenVPN (gluetun)

Each directory under `openvpn/` becomes one gluetun container (`dockervpn_1`, `dockervpn_2`, ... in alphabetical
order of the directory names) that offers an HTTP proxy to Squid and gost. Nothing of gluetun is published on the host.

```
openvpn/<name>/<anything>.ovpn   # OpenVPN client config (the first one, alphabetically)
openvpn/<name>/secret            # optional: line 1 username, line 2 password
```

Copy an example to get started: `cp -r examples/openvpn/jp454.nordvpn.com.tcp443.ovpn openvpn/` and put your
service credentials (not your account login) into `secret`.

- The credentials are written to `openvpn/<name>/gluetun.env` (mode 0600, gitignored) and passed with `env_file`;
  they never appear in `docker-compose.yml`.
- Your `.ovpn` file is mounted read-only and never modified. gluetun's custom provider (v3.41.3) only accepts IP
  addresses in `remote` lines, because DNS is blocked until the tunnel is up. If your file uses a host name, the
  generator resolves it and writes `openvpn/<name>/gluetun.conf` (gitignored), which is mounted instead. The
  chosen address is recorded in that file and reused on later runs, so round-robin DNS does not recreate the VPN
  container every time you run the generator. Run `python3 setup/generate.py --refresh-dns` when the provider
  retired that server (gluetun then fails to connect and the VPN's static port answers 503). gluetun uses only
  one `remote` line, so `remote-random` has no effect.
- Directory and file names may contain letters, digits, `.`, `_` and `-`.

## Allowlist

`template/allowed_ip.txt` lists the client addresses (IPv4/IPv6 or CIDR) that may use port 3128 **and** the static
ports. The generator validates it and writes `config/allowed_ip.txt`, which both Squid and gost read.

Clients on the Docker host itself reach the containers through Docker's port forwarding, so the proxy sees the
gateway of the compose network as their address. The compose network uses the fixed subnet `172.16.231.0/24`, and the
template therefore allows `172.16.231.1`. If you change `PROXY_SUBNET`, change that line as well.

The old template also allowed every private network (`localnet`). That is no longer the case: add your LAN range
(e.g. `192.168.1.0/24`) if machines on your LAN should use the proxy.

## Settings (`.env`)

Docker Compose reads `.env` next to `docker-compose.yml`:

| variable | default | meaning |
|---|---|---|
| `BIND_ADDR` | `0.0.0.0` | host address the ports are published on; `127.0.0.1` keeps the proxy local (IPv6: `[::1]`) |
| `PROXY_SUBNET` | `172.16.231.0/24` | subnet of the compose network; change it if it collides with your network |
| `SQUID_IMAGE` | `b4tman/squid:7.5` | Squid image (must be built from [b4tman/docker-squid](https://github.com/b4tman/docker-squid)) |

Local additions to the compose file belong in `docker-compose.override.yml`, which Docker Compose merges automatically.

## Public proxy lists (cron)

`public_proxy_cron.sh` downloads a list of public proxies (`host:port` per line, default
[clarketm/proxy-list](https://github.com/clarketm/proxy-list)) into `publicProxyList.txt`, regenerates only
`config/squid.conf`, checks it with `squid -k parse` and applies it with `squid -k reconfigure`, without restarting
any container. `proxyList.txt` is never touched.

```
0 * * * * /path/to/docker-rotating-proxy/public_proxy_cron.sh >> /tmp/rotating-proxy-cron.log 2>&1
```

Set `PUBLIC_PROXY_URL` to use a different list, or to an empty string to apply a hand-edited
`publicProxyList.txt` without downloading (`PUBLIC_PROXY_URL= ./public_proxy_cron.sh`). Public proxies only take
part in the rotation on port 3128.

## Security notes

- Every published port checks the client address. Squid answers `403` to other clients; gost closes the connection.
  Keep `BIND_ADDR=127.0.0.1` if only local programs use the proxy.
- The allowlist only works if the proxy sees real client addresses. Docker Desktop (macOS/Windows) and some
  rootless Docker setups rewrite the source address of published ports; there, publish on `127.0.0.1` only.
- There is no username/password authentication for clients yet (see docs/DESIGN.md).
- Upstream credentials are stored in `config/squid.conf` (`httpsquid`/`https`, must be readable by the Squid user
  inside the container) and `config/gost.yaml` (mode 0600). Both are gitignored. Restrict access to the checkout
  directory (`chmod 700`) on shared machines.
- The internal gost ports (31000+) have no allowlist, but they are only reachable from containers on the compose
  network and are not published.
- **Squid version:** `b4tman/squid:7.5` is the newest exact tag on Docker Hub (checked 2026-10-23). It fixes
  SQUID-2025:2 (Squid < 7.2) but **not SQUID-2026:6** (request smuggling, fixed in Squid 7.6). Until a 7.6+ tag is
  published, you can build the current b4tman/docker-squid master (Squid 7.7) yourself and point `SQUID_IMAGE` at it:

  ```sh
  docker build -t local/squid:7.7 https://github.com/b4tman/docker-squid.git#0f45802d489023afd439a0dfa649373b61f877a0
  echo 'SQUID_IMAGE=local/squid:7.7' >> .env
  docker compose up -d
  ```

## Migrating from the PHP version

What changed:

| | before | now |
|---|---|---|
| generator | `setup/generate.php`, PHP 7.4 + composer + symfony/yaml | `setup/generate.py`, Python 3 standard library |
| Squid | `b4tman/squid:5.8` (2023) | `b4tman/squid:7.5` |
| SOCKS/HTTP conversion | one `ginuerzh/gost:latest` (v2) container per upstream | one `gogost/gost:3.3.1` container |
| VPN | `qmcgaw/gluetun` (unpinned) | `qmcgaw/gluetun:v3.41.3` |
| static ports | gost/gluetun ports published without any access control | gost checks `allowed_ip.txt` on every static port |
| VPN credentials | written into `docker-compose.yml` | `openvpn/<name>/gluetun.env` (0600) |
| `.ovpn` files | `remote` host names rewritten in place | never modified (`gluetun.conf` is derived instead) |
| allowlist | `allowed_ip.txt` + every RFC 1918 network | `allowed_ip.txt` only |
| public proxies (cron) | overwrote `proxyList.txt`, `docker run -it`, SIGHUP | separate `publicProxyList.txt`, `squid -k parse` + `squid -k reconfigure` |
| healthcheck | request to httpbin.org through the rotation every 5 min | local checks only (Squid cache manager, gost metrics) |
| HTTPS upstream proxies | not supported | `https` scheme (Squid `tls` cache_peer, gost TLS dialer) |
| shadowsocks port 50000+ | published, but disabled in gluetun | removed |
| dead socks/http upstream on 3128 | its 503 reached the client | retried on the next peer (`retry_on_error on`) |

Static ports keep the old rule (socks/http upstreams in file order, then VPNs). VPN containers are now numbered
by VPN directory (`dockervpn_1`, `dockervpn_2`, ...) instead of continuing the proxy line counter.

Steps:

```sh
docker compose down --remove-orphans   # while the old docker-compose.yml is still there
git pull                               # keep your own lines if git reports a conflict in proxyList.txt
rm -rf setup/vendor                    # composer leftovers
# add 172.16.231.1 (and your LAN range, if needed) to template/allowed_ip.txt
python3 setup/generate.py
docker compose up -d --remove-orphans
```

The example VPN configs moved from `openvpn/` to `examples/openvpn/`, so a fresh checkout no longer starts two VPN
containers with placeholder credentials.

## Development

```sh
python3 -m unittest discover -s setup/tests -v   # unit tests (stdlib only)
.github/e2e/run.sh                               # end-to-end test; overwrites proxyList.txt, use a scratch checkout
```

CI (`.github/workflows/ci.yml`) runs the unit tests, generates a sample configuration and checks it with
`docker compose config`, `gost -O json` and `squid -k parse` in the pinned Squid image, and runs the end-to-end test
with two SOCKS5 upstreams, an HTTP and an HTTPS upstream, the allowlist and the cron reload path.

## License

Public domain ([The Unlicense](LICENSE)).
