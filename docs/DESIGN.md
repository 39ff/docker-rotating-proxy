# Design: Squid 7 front door + one gost v3 + gluetun ("Plan B")

This document explains how the rotating proxy is put together after the
modernisation, why Squid stays in front, what that costs compared with a
gost-only design, and what was actually verified.

## 1. Architecture

```
                  compose network 172.16.231.0/24 (PROXY_SUBNET), ports on ${BIND_ADDR:-0.0.0.0}

 clients ─ :3128 ─▶ squid (dockersquid_rotate, b4tman/squid:7.5)
 (allowlisted)        acl Allowed_IP, never_direct, round-robin, header scrubbing
                      ├─ cache_peer HOST:PORT                public, httpsquid (login=) ──────▶ HTTP proxy
                      ├─ cache_peer HOST:PORT tls login=     https (issue #23) ───────────────▶ HTTPS proxy
                      ├─ cache_peer dockergost_rotate:3100k  socks5, http ───┐
                      └─ cache_peer dockervpn_N:8888         VPN ────────────┼──────────┐
                                                                             │          │
                    gost (dockergost_rotate, gogost/gost:3.3.1)              │          │
                      peer-k   :3100k  internal, no admission ◀──────────────┘          │
                         └─ chain-k ─ node HOST:PORT (socks5 / http / tls+http) ──────────────▶ SOCKS5 / HTTP / HTTPS proxy
 clients ─ :3000k ─▶  static-k :3000k  admission "allowlist" ─ chain-k (same chain)     │
 (allowlisted)        static-m :3000m  admission "allowlist" ─ chain-m ─ dockervpn_N:8888 ─┐
                                                                                        │  │
                    gluetun (dockervpn_N, qmcgaw/gluetun:v3.41.3)                       │  │
                      HTTP proxy :8888 (not published) ◀────────────────────────────────┴──┘
                      OpenVPN + firewall kill switch ─────────────────────────────────────────▶ VPN server

 Host-local clients reach the published ports through Docker's port forwarding and
 appear with the gateway address 172.16.231.1, which template/allowed_ip.txt allows.
```

| request | path | allowlist enforced by |
|---|---|---|
| `:3128` → public / `httpsquid` upstream | Squid → upstream | Squid `acl Allowed_IP` |
| `:3128` → `https` upstream | Squid ⇒(TLS) upstream | Squid |
| `:3128` → `socks5` / `http` upstream | Squid → gost `peer-k` → upstream | Squid |
| `:3128` → VPN | Squid → gluetun HTTP proxy → tunnel | Squid |
| `:3000k` → upstream k | gost `static-k` → upstream | gost admission (`whitelist: true`, file reloaded every 30 s) |
| `:3000m` → VPN | gost `static-m` → gluetun → tunnel | gost admission |

Port rule (unchanged from the PHP version): static ports start at 30000, first one per
`socks5`/`http`/`https` line in `proxyList.txt` order, then one per VPN directory (sorted).
The internal gost port of upstream k is `static port + 1000` (31000+), so `gost3` in
Squid's logs is the upstream behind static port 30002. `https` upstreams have a static
port but no internal port, because Squid talks TLS to them directly.

Files: `setup/generate.py` reads `proxyList.txt`, `publicProxyList.txt`, `openvpn/*/`,
`template/squid.conf` and `template/allowed_ip.txt`, and writes `config/squid.conf`,
`config/gost.yaml` (0600), `config/allowed_ip.txt`, `docker-compose.yml`,
`openvpn/<name>/gluetun.env` (0600) and, when needed, `openvpn/<name>/gluetun.conf`.
`config/` is mounted read-only as a directory into Squid (`/etc/squid/rotating`) and gost
(`/etc/gost`), so files replaced atomically by the generator are visible without a
remount.

## 2. Why Squid stays in front

1. **Hot reload of many peers.** Public lists have hundreds of entries that change hourly.
   `squid -k reconfigure` swaps the whole peer set inside the running process; the cron path
   is verified in CI without a container restart. gost v3 has no reliable in-place reload
   (go-gost/gost#754), so a gost-only design must recreate the container (dropping open
   connections) or drive gost's web API.
2. **Forwarding semantics that already work.** `connect-fail-limit` marks dead peers,
   Squid retries a request on another peer after a connect failure, a 502/504 and, with
   `retry_on_error on` (now in the template), a 403/500/501/503 from a peer
   (`forward_max_tries`, default 25), and the access log shows which peer served each
   request (`TCP_MISS:ROUNDROBIN_PARENT`). Header scrubbing (`Via`, `X-Forwarded-For`,
   `Cache-Control`) is explicit config rather than an implementation detail.
3. **TLS to upstream proxies** (issue #23) is a native `cache_peer ... tls` with certificate
   verification against the system CAs and the peer host name.
4. **Room to grow without new components:** caching (`cache_dir`), richer ACLs
   (`dstdomain`, time, ports), client authentication (`auth_param`) and the cache manager
   (`/squid-internal-mgr/server_list` gives per-peer counters).
5. **Continuity for existing users.** Same ports, same `proxyList.txt` format, same
   `template/squid.conf` customisation point, same container names for Squid and VPNs.

## 3. Trade-offs versus a gost-only design (Plan A)

Honest list of what Plan B costs:

| topic | Plan B (Squid + gost) | gost-only |
|---|---|---|
| components in the data path | 2 for socks/http upstreams (Squid → gost → upstream), 1 for public/httpsquid/https/VPN | 1 |
| config surfaces | `squid.conf` + `gost.yaml`, two ACL implementations that must agree (both read the same `allowed_ip.txt`) | one file |
| dead socks/http upstream on 3128 | gost accepts the TCP connection and answers 503, so `connect-fail-limit` never trips; `retry_on_error on` makes Squid retry on the next peer (measured: 1 dead of 7 peers → 6/40 failed requests with it off, 0/40 with it on, for both HTTP and CONNECT). Cost: every request to the dead upstream is a wasted round trip, and origin 403/500/503 responses are retried on other peers too | gost selector `maxFails`/`failTimeout` takes the node out of rotation |
| front-door attack surface / patch cadence | Squid (C++, regular advisories); pinned 7.5 lacks the SQUID-2026:6 fix until a 7.6+ tag exists | gost (Go, memory safe) |
| third-party image | `b4tman/squid` (single maintainer, releases lag Squid by weeks) | `gogost/gost` (upstream project's own image) |
| public list reload | `squid -k reconfigure`, no restart (verified) | recreate or API calls |
| HTTPS upstream proxies | Squid `tls` peer + gost TLS dialer | gost TLS dialer |
| header scrubbing | explicit Squid directives | gost's HTTP handler does not add `Via`/`X-Forwarded-For`, but forwards headers the client sends |
| caching / ACL language / auth | available in Squid | limited (gost has auth and bypass rules, no cache) |
| resources (idle, e2e stack) | squid 13.8 MiB + gost 21.9 MiB | gost only |
| latency | no measurable difference on loopback: 200 sequential curl requests, median 7.8–8.1 ms via 3128 vs 7.6–8.4 ms via a static port (dominated by curl start-up; not a load test) | — |

If the project wants the smallest moving parts and does not need Squid features, gost-only
is simpler. Plan B is the conservative choice: it keeps the behaviour users rely on and
isolates the change to "replace gost v2 containers by one gost v3 container".

## 4. Decisions and details

- **Generator in Python stdlib.** No PyYAML: a ~40-line emitter writes block YAML with
  JSON-quoted scalars (JSON string escapes are valid YAML), so every value, including
  passwords with `:`, `{`, `%`, `\`, is quoted. Output is deterministic and files are only
  rewritten when their content changes (atomic temp file + rename). Python 3.8+.
- **Input validation.** Lines are `host:port[:scheme[:user:pass]]`; IPv6 needs brackets
  (a bare IPv6 address is detected and reported). Ports 1–65535; host names
  `[A-Za-z0-9_.-]`; credentials must not contain whitespace, quotes, `#` or control
  characters. Invalid lines are skipped with `file:line` warnings; duplicate lines and
  public entries that repeat a `host:port` are dropped. No usable upstream → exit 2.
- **`login=` encoding.** Squid runs `rfc1738_unescape()` on the `login=` value
  (`src/cache_cf.cc` in SQUID_7_5: `p->login = xstrdup(token + 6); rfc1738_unescape(p->login);`),
  so the generator percent-encodes everything outside `[A-Za-z0-9_.~-]`. That also keeps
  `${...}` away from Squid's macro expansion. PHP's `urlencode()` used before encoded spaces
  as `+`, which Squid does not decode.
- **TLS peers.** `cache_peer HOST PORT 0 ... tls tls-options=NO_SSLv3,NO_TLSv1,NO_TLSv1_1 login=...`.
  `tls-min-version=1.2` was the obvious choice but makes Squid 7.5/7.6 log
  `ERROR: Unknown TLS option` (in `Security::PeerOptions::updateTlsVersionLimits()` the
  leading `:` is only removed when `tlsMinOptions` is *not* empty, right after it was
  cleared). gost's TLS dialer is configured with `secure: true` because gost skips
  verification by default.
- **Allowlist and the gateway.** Host-local clients reach published ports through Docker's
  proxy/NAT and appear with the network gateway as source. The generated network has a
  fixed subnet (`${PROXY_SUBNET:-172.16.231.0/24}`) so the template can allow exactly
  `172.16.231.1` instead of guessing `172.17.0.1`/`172.21.0.1`/`172.24.0.1` as before. The
  blanket `localnet` allow is gone. Manager access is limited to localhost and evaluated
  before `Allowed_IP` (allowlisted clients could otherwise read `mgr:` pages). As before,
  allowlisted clients are not limited to ports 80/443.
- **Recreate on config change.** Compose does not look at the content of bind-mounted files,
  so each generated service carries a label `rotating-proxy.config-hash` (Squid: template +
  allowlist + non-public peers; gost: `gost.yaml`; VPN: the user's `.ovpn`). `docker compose up -d`
  then recreates exactly the services whose config changed. Peers from `publicProxyList.txt`
  are excluded from Squid's hash so the hourly cron never forces a recreate.
- **`retry_on_error on`** is new. It is what makes the gost hop tolerable (see the table in
  section 3), but it changes what clients see: a 403/500/501/503 relayed from an origin is
  retried on other peers before it reaches the client. Scrapers that rely on seeing those codes
  immediately can switch it off in `template/squid.conf`.
- **Cron path.** `public_proxy_cron.sh` downloads into `publicProxyList.txt` (never
  `proxyList.txt`), runs `generate.py --squid-only`, runs `squid -k parse` inside the running
  container (a config that does not parse would make Squid exit on reconfigure), restores the
  previous `squid.conf` on failure, else runs `squid -k reconfigure`. `--squid-only` warns when
  `gost.yaml` or `docker-compose.yml` would also change.
- **gluetun and host names.** The brief assumed rewriting `remote` host names was unnecessary.
  It is necessary for gluetun v3.41.3: its custom provider rejects host names
  (`internal/openvpn/extract/extract.go`, `errHostNotIP`, with a TODO about DNS before the
  tunnel is up) and replaces all `remote` lines with one `remote IP PORT`
  (`internal/provider/custom/openvpnconf.go`). The generator therefore never modifies the
  user's file: if it uses a host name, a derived `gluetun.conf` with the resolved IPv4
  address is written and mounted at `/gluetun/custom.conf`; otherwise the user's `.ovpn` is
  mounted read-only. Credentials go to `gluetun.env` (dotenv single quotes, or double quotes
  with `$$` escaping when the value has a `'`).
- **Healthchecks without the internet.** Squid: `curl http://127.0.0.1:3128/squid-internal-mgr/info`
  (Squid 7 removed `squidclient`; the image ships curl; manager requests are excluded from the
  access log). gost: `wget http://127.0.0.1:9000/metrics` (busybox in the image). No httpbin.org.
- **No `resolvers` in gost.** Host names in chain nodes are resolved by Docker DNS (container
  names) and SOCKS5 targets are sent as names to the upstream.

## 5. What was verified, and how

All on 2026-10-23 in the development sandbox unless noted. The sandbox shares one Docker
daemon with another session, so the local e2e runs used a compose override that renamed the
containers (with network aliases `dockersquid_rotate`/`dockergost_rotate`), published on
`127.0.0.1:13128` / `13000+` and reset `ulimits` (the sandbox hard limit is 20000 < 65536).
CI runs the committed files unmodified.

| check | result |
|---|---|
| Docker Hub `b4tman/squid` tags (`/v2/repositories/b4tman/squid/tags`) | newest exact 7.x tag is `7.5` (= `7.5.0`, 2026-03-17); no `7.6`/`7.7` tag. `latest`/`master` (2026-10-03) are built from master, which is at Squid 7.7 (commit `0f45802`). `ubuntu/squid` only has `7.2-26.04_edge`. |
| b4tman image contents | `CMD /run.sh` → `squid -f "$SQUID_CONFIG_FILE" --foreground -YCd 1`, user `squid` (uid 3128), `curl` and `wget` present, no `squidclient`. |
| `squid -k parse` of the CI sample (all schemes, IPv6 peer, 2 VPNs, public list) | exit 0, no `FATAL`/`ERROR`/`WARNING` with the official `b4tman/squid:7.5` (`sha256:939bcf3e…`), a local build of b4tman master (Squid 7.7) and Alpine 3.23's `squid 7.6-r0`. |
| gost config | `gost -C config/gost.yaml -O json` (3.3.1) parses; `whitelist: true` survives; struct/field names checked in go-gost/x v0.27.1 `config/config.go`, admission semantics in `admission/admission.go` (blacklist by default). |
| end-to-end (`.github/e2e/run.sh`) | passed with `b4tman/squid:7.5`, the 7.5 build from the v7.5.0 Dockerfile and the 7.7 master build: rotation across 2× socks5 (different credentials), http-via-gost and https-via-Squid-TLS with a dead socks5 upstream in the list and no failed request; no `Via`/`X-Forwarded-For` at the origin; CONNECT rotates; static ports 30000–30003 sticky for HTTP and HTTPS; non-allowlisted container gets 403 from Squid and a closed connection from gost; cron adds a public and an `httpsquid` peer (password `c%l:o@n+e/=`) with `squid -k reconfigure`, container `StartedAt`/`RestartCount` unchanged. |
| cron rollback | a template line `acl broken nosuchtype foo` → `FATAL: Bungled …` from `squid -k parse`, script exits 1, previous `squid.conf` restored, Squid keeps serving. |
| gluetun | with the example `.ovpn` (host name) mounted: `ERROR … host is not an IP address: hk-hkg.prod.surfshark.com`; with the derived `gluetun.conf`: `[http proxy] listening on :8888` and OpenVPN connecting to the resolved IP. A real tunnel was not established (no credentials). |
| env_file quoting | `docker compose` v5.6.1 + a real container: values with `$`, `${HOME}`, `'`, `"`, `\`, `#` arrive unchanged. |
| unit tests | 36 tests pass on Python 3.8, 3.11, 3.12, 3.13. |
| GitHub Actions | all jobs green on commit `e300d2a` (run 39996692886): unit (3.10, 3.13), generate + compose config + gost parse, `squid -k parse` in `b4tman/squid:7.5`, and the e2e with the real ports 3128/30000+, container names and `ulimits`. |

Not verified: a real VPN tunnel through gluetun; IPv6 upstreams at runtime (Docker networks
have no IPv6 by default; only `squid -k parse` and the generated config were checked); arm64
images (all three are multi-arch on Docker Hub); behaviour under load.

## 6. Known limitations and follow-ups

1. **SQUID-2026:6.** Pin `b4tman/squid:7.6` (or later) as soon as an exact tag exists; until then
   `SQUID_IMAGE` can point at a self-built image (README shows the command). Alternatively the
   project could ship a two-line Dockerfile on Alpine 3.23+ (`apk add squid` = 7.6-r0) with the
   same entrypoint, which removes the dependency on a single-maintainer image.
2. **Dead socks/http upstreams on 3128** stay in rotation; each request that lands on one costs a
   503 from gost and a retry. Options: a small health checker that drops dead entries before
   generating, or letting gost pick among several nodes with `maxFails`/`failTimeout` (which
   would change the one-upstream-per-port semantics).
3. **Static ports shift** when a line is inserted in the middle of `proxyList.txt` (same as
   before). A stable mapping (for example `host:port → port` persisted in `config/`) would
   need a format extension.
4. **`https` upstreams by IP address** fail verification unless the certificate has an IP SAN;
   there is no per-line `ssldomain=` or "insecure" switch.
5. **Allowlist depends on real source addresses.** Docker Desktop, rootless Docker with the
   builtin port driver, or `"iptables": false` make every client look like the gateway.
   The README tells those users to publish on 127.0.0.1.
6. **Internal gost ports (31000+)** are unauthenticated; any container attached to the compose
   network can use them. An admission matching only Squid's address would need a fixed Squid IP.
7. **Public proxies are untrusted.** They can read and modify plain HTTP and see CONNECT
   targets; that is inherent to the feature.
8. **`ulimits.nofile: 65536`** (kept from the old template) fails on hosts whose hard limit is
   lower.
9. **Client authentication (out of scope).** Squid: `auth_param basic program
   /usr/lib/squid/basic_ncsa_auth /etc/squid/rotating/passwd`, `acl users proxy_auth REQUIRED`,
   `http_access allow users` (the image has `basic_ncsa_auth` and `htpasswd`); the
   `login=` options keep working because they authenticate Squid to the peers. gost: an
   `authers` entry (`auths:` list or `file:`) referenced as `handler.auther` on the
   `static-*` services.
10. **Caching (out of scope, disabled).** Replace `cache deny all` with a `cache_dir`, mount a
    volume at `/var/cache/squid`, and drop `proxy-only` from the generated peer options
    (`proxy-only` tells Squid not to store objects fetched from a peer). Removing
    `request_header_access Cache-Control deny all` would make caching honour client hints.
