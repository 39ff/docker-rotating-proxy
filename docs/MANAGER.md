# Web UI (manager)

`manager/` is an optional web UI for this repository. It is meant for people who would rather not edit
`proxyList.txt` over SSH: one command on a fresh server, then everything happens in the browser.

- First-run wizard: admin password → allow your address → paste your proxies → start and test.
- Pasted proxies are recognised in the formats providers hand out and converted to `proxyList.txt` lines.
- **Apply** runs `setup/generate.py` and `docker compose up -d`; **Test** shows the exit IP of port 3128 and of every
  static port.
- Allowlist, OpenVPN configs and the public proxy list can be managed from the same page.
- English and Japanese.

It is a small Python standard-library server (`manager/server.py`) with a static page; there is no database. The UI
edits the same files the README describes, so the command-line flow keeps working next to it.

## Install

On a Linux server, as root:

```sh
curl -fsSL https://raw.githubusercontent.com/39ff/docker-rotating-proxy/master/install.sh | sudo sh
```

![install.sh output](images/manager/install.png)

`install.sh`:

1. installs Docker through `https://get.docker.com` when `docker` is missing (set `RPX_NO_DOCKER_INSTALL=1` to fail
   instead), and git when it is missing;
2. clones this repository to `/opt/docker-rotating-proxy` (or runs `git pull --ff-only` there if it exists) and makes the
   directory `0700`, because upstream credentials end up in it;
3. writes a one-time setup token to `.manager/setup_token` and the UI settings to `manager/.env`;
4. builds and starts the UI with `docker compose -f manager/compose.yml up -d --build`;
5. checks that containers may use `nofile=65536` (the generated compose file asks for it) and prints the setup link.

| variable | default | meaning |
|---|---|---|
| `RPX_DIR` | `/opt/docker-rotating-proxy` | install directory |
| `RPX_REPO` / `RPX_REF` | this repository / `master` | what to clone |
| `RPX_PORT` | `8080` | web UI port |
| `RPX_BIND` | `0.0.0.0` | address the UI listens on; `127.0.0.1` = reachable through an SSH tunnel only |
| `RPX_PUBLIC_HOST` | detected public IP | host shown in the connection examples |
| `RPX_SETUP_TOKEN` | random | choose the setup token yourself (cloud-init) |
| `RPX_NO_DOCKER_INSTALL` | | `1` = do not install Docker |

Running `install.sh` again updates the checkout and restarts the UI; your settings are kept. Because the UI code is
read from the checkout (the image only contains Python and the docker CLI), `git pull` followed by
`docker restart rotating_proxy_manager` is enough to update it by hand.

### Without SSH: cloud-init

Most VPS providers accept "user data" when a server is created. With your own token you know the setup link in
advance:

```yaml
#cloud-config
runcmd:
  - [sh, -c, "curl -fsSL https://raw.githubusercontent.com/39ff/docker-rotating-proxy/master/install.sh | RPX_SETUP_TOKEN=use-a-long-random-string sh"]
```

Then open `http://<server-ip>:8080/setup?token=use-a-long-random-string` once the server is up (installing Docker takes
a minute or two).

### Existing checkout

```sh
cat > manager/.env <<EOF
RPX_ROOT=$PWD
RPX_BIND=127.0.0.1
EOF
docker compose -f manager/compose.yml up -d --build
docker logs rotating_proxy_manager      # prints the setup link
```

`RPX_ROOT` must be an absolute path: the checkout is mounted at the same path inside the container, because
`docker compose` (run from inside) hands bind-mount paths such as `./config` to the host's Docker daemon.

## First run

| step | |
|---|---|
| 1. Admin password | needs the token from the setup link; at least 10 characters |
| 2. Who may use it | your current address (as seen by the server) is pre-selected; add more addresses or networks |
| 3. Upstream proxies | paste, check the preview, choose the type for lines that do not name one; optionally add an `.ovpn` |
| 4. Start | runs Apply, then Test, and shows connection examples (curl, Python, Playwright, MCP) |

| paste | started |
|---|---|
| ![wizard: paste](images/manager/wizard-upstreams.png) | ![wizard: started](images/manager/wizard-started.png) |

### Accepted proxy formats

| pasted | written to `proxyList.txt` |
|---|---|
| `203.0.113.10:1080:user:pass` | `203.0.113.10:1080:<type>:user:pass` |
| `user:pass@203.0.113.10:1080` | `203.0.113.10:1080:<type>:user:pass` |
| `203.0.113.10:1080@user:pass` | `203.0.113.10:1080:<type>:user:pass` |
| `socks5://user:p%40ss@host:1080` (`socks5h://`, `socks://`, `http://`, `https://`) | `host:1080:socks5:user:p@ss` (percent-decoded) |
| `203.0.113.10:1080` | `203.0.113.10:1080:<type>` |
| `host:port[:scheme[:user:pass]]` | unchanged |

`<type>` is the "type for lines that do not say" selector: HTTP or SOCKS5 or HTTPS (each gets a static port),
HTTP rotation-only (`httpsquid`), or public (no login, rotation only). Every converted line is checked with
`generate.parse_proxy_line`, so the UI accepts exactly what the generator accepts. Exact duplicates are skipped.

## Dashboard

![dashboard](images/manager/overview.png)

- **Overview**: counts, the rotating endpoint, the static port table with the exit IP and latency of each port, the
  state of `squid`, `gost` and the VPN containers, and copy-paste examples.
- **Upstreams**: paste more, delete lines, see lines the generator would skip, download a public list.
- **Allowlist**: "allow my address", add an address or CIDR with a note, delete. `127.0.0.1` and the compose gateway
  (`172.16.231.1`, or `.1` of `PROXY_SUBNET`) are locked: the stack itself needs them.
- **VPN**: upload an `.ovpn` with the service username and password, delete (a `.tar.gz` backup is kept).

Saving and starting are separate: after a change a banner says "saved but not running yet" with **Apply now**.

![upstreams page](images/manager/upstreams.png)

## How it works

```
browser ──HTTP──▶ manager (host network, :8080) ──▶ proxyList.txt, template/allowed_ip.txt, openvpn/<name>/
                     │                                   │
                     │ Apply                             ▼
                     ├──▶ python3 setup/generate.py ──▶ config/, docker-compose.yml
                     ├──▶ docker compose up -d --remove-orphans   (via /var/run/docker.sock)
                     │ Test
                     └──▶ 127.0.0.1:3128 and :30000+ ──▶ squid / gost ──▶ upstreams ──▶ RPX_PROBE_URL
```

- **Files are the source of truth.** Adding appends lines, deleting removes exactly the selected lines (identified by
  line number and content; if the file changed meanwhile the UI asks you to reload). Comments and lines you wrote by
  hand stay. Every write keeps the previous file in `.manager/backups/` (last 30 per file).
- **Apply** runs the generator and `docker compose up -d --remove-orphans --quiet-pull` in the checkout, then waits
  up to two minutes for `squid` and `gost` to report healthy. The log is shown live. "Pending changes" means the
  digest of the inputs (`proxyList.txt`, the templates, `.env`, `openvpn/*/{*.ovpn,secret}`) differs from the one
  recorded after the last successful Apply.
- **Test** sends 6 requests through port 3128 and one through each static port to `RPX_PROBE_URL`
  (default `https://api.ipify.org`, which answers with the caller's IP) and shows what came back. The manager uses the
  host network and connects to `127.0.0.1`, so the proxy sees the compose gateway, which is on the allowlist.
- **Public list** runs `public_proxy_cron.sh` inside the manager (download, `--squid-only`, `squid -k parse`,
  `squid -k reconfigure`). The hourly cron entry from the README still works independently.
- The UI never edits `template/squid.conf` or `.env`.

State of the UI itself lives in `.manager/` (`0700`, gitignored): `auth.json`, `setup_token` until the first login,
`applied.json` and `backups/`.

## Access and HTTPS

The UI is plain HTTP on port 8080 by default, so the admin password crosses the network unencrypted. In order of
preference:

1. **SSH tunnel only.** Install with `RPX_BIND=127.0.0.1`, then `ssh -L 8080:127.0.0.1:8080 user@server` and open
   `http://127.0.0.1:8080/`. Nothing of the UI is reachable from the internet. In this mode the UI sees you as
   `127.0.0.1`, so add the addresses of the machines that will use the proxy by hand (the wizard says so).
2. **HTTPS with a domain**, e.g. [Caddy](https://caddyserver.com/) on the same host:

   ```
   # /etc/caddy/Caddyfile
   proxy-admin.example.com {
       reverse_proxy 127.0.0.1:8080
   }
   ```

   and in `manager/.env`: `RPX_BIND=127.0.0.1`, `RPX_TRUST_FORWARDED=1`, `RPX_PUBLIC_HOST=<server IP or name>`.
   `RPX_TRUST_FORWARDED` makes the UI take the client address from `X-Forwarded-For` / `CF-Connecting-IP`, but only for
   requests that come from the loopback address, and marks the session cookie `Secure` when
   `X-Forwarded-Proto: https`.
3. **Cloudflare Tunnel** (no open port): run `cloudflared` with an ingress rule to `http://127.0.0.1:8080`, protect the
   hostname with Cloudflare Access, and use the same three settings as above. Set `RPX_PUBLIC_HOST` to the server's
   address: the proxy ports themselves are not behind the tunnel.
4. Plain HTTP on `0.0.0.0:8080`: at least limit port 8080 to your own address in the firewall / security group.

Options 2 and 3 are documented, not covered by CI.

## Security model

The manager mounts `/var/run/docker.sock`, so **whoever can log in to the UI controls the host** (root-equivalent).
The UI is designed around that:

- **Setup token.** The first-run page needs the token printed by `install.sh` (and by `docker logs
  rotating_proxy_manager`), so nobody else can claim a freshly installed server. It is compared in constant time and
  deleted after use. Pages send `Referrer-Policy: no-referrer`, and the page removes the token from the address bar.
- **Password.** PBKDF2-SHA256 with 600,000 iterations and a random salt in `.manager/auth.json` (`0600`). After 5
  failed logins from one address, further attempts are delayed exponentially (up to 15 minutes).
- **Sessions.** 256-bit random tokens kept in memory for 7 days (a restart logs everyone out), cookie `HttpOnly`,
  `SameSite=Strict`, `Secure` behind an HTTPS proxy.
- **CSRF.** Every POST needs the header `X-RPX-Request: 1` (which a cross-site form cannot send) and, when the browser
  sends `Origin`, it must match `Host`.
- **Content Security Policy** `default-src 'self'` with no inline scripts or styles; `X-Frame-Options: DENY`; the page
  builds the DOM with text nodes only.
- **Credentials of upstreams** are written to disk as before (`proxyList.txt`, `config/`, `openvpn/*/secret`) but never
  sent back to the browser: lists and previews show `user:•••`.
- **Guards.** `0.0.0.0/0` and `::/0` cannot be added to the allowlist; the entries the stack needs cannot be deleted;
  VPN names and file names are restricted to `[A-Za-z0-9._-]`; request bodies are limited to 2 MiB and `.ovpn` files to
  256 KiB; every request body is read completely before the response, so keep-alive connections cannot be confused.

Not included: several users, roles, two-factor authentication, audit log.

## API

The page uses a small JSON API; scripts can use it too. Log in with `POST /api/login` and keep the `rpx_session`
cookie; every POST needs `Content-Type: application/json` and `X-RPX-Request: 1`.

| method and path | body | does |
|---|---|---|
| `GET /api/session` | | `setup_required`, `authenticated`, `client_ip` (no login needed) |
| `POST /api/setup` | `token`, `password` | first-run setup, logs in |
| `POST /api/login` / `/api/logout` | `password` / – | |
| `GET /api/overview` | | counts, static ports, services, pending flag, last test |
| `GET /api/upstreams` | | entries (no passwords), invalid lines, public list size |
| `POST /api/upstreams/preview` / `add` | `text`, `default_scheme` | convert (and append) pasted lines |
| `POST /api/upstreams/delete` | `ids` | remove lines |
| `GET /api/allowlist`, `POST /api/allowlist/add` / `delete` | `value`, `comment` / `ids` | |
| `GET /api/vpns`, `POST /api/vpns/add` / `delete` | `name`, `filename`, `content`, `username`, `password` / `name` | |
| `POST /api/apply` | | starts a job: generate + compose up |
| `POST /api/public/refresh` | optional `url` | starts a job: `public_proxy_cron.sh` |
| `GET /api/jobs/<id>` | | job status and log |
| `POST /api/probe` | | test all ports now |

## What was verified

- `manager/tests/test_manager.py` (22 tests, standard library): format conversion, line edits and backups, allowlist
  and VPN guards, setup token, password and rate limit, CSRF, static file paths, the keep-alive regression, and an
  Apply job with a fake `docker` CLI that runs the real generator.
- `.github/e2e/manager.sh` (CI job `manager-e2e`) builds and starts the manager from `manager/compose.yml`, then drives
  the API against the e2e upstream fixtures: setup, pasting in three provider formats, allowlist guards, Apply,
  Test (all 6 rotation requests succeed with at least 2 exit IPs; each static port exits through its own upstream),
  deleting an upstream and applying again, and a public list refresh that is used after `squid -k reconfigure`.
- In the development sandbox (2026-11-06): `install.sh` with a fresh clone and with an update, the `nofile` warning
  (the sandbox's limit is 20000), and the whole wizard and dashboard driven with Playwright. The screenshots come from
  that run: the exit IPs `172.16.231.x` are the e2e fixture upstreams, and `203.0.113.10` stands for the server's
  public address. The sandbox intercepts TLS, so its manager image was built from a copy of `manager/Dockerfile` with
  the sandbox CA added; CI builds the committed Dockerfile.

Not verified: a real OpenVPN tunnel started from the UI, the Caddy and Cloudflare Tunnel setups, Docker Desktop and
rootless Docker, arm64 hosts.
