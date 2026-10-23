#!/usr/bin/env bash
# Refresh publicProxyList.txt and hot-reload Squid (no container restart).
#
# crontab example (hourly):
#   0 * * * * /path/to/docker-rotating-proxy/public_proxy_cron.sh >> /tmp/rotating-proxy-cron.log 2>&1
#
# PUBLIC_PROXY_URL  list to fetch, one host:port per line (default below).
#                   Set it to an empty string to skip the download and only
#                   apply the current publicProxyList.txt:
#                     PUBLIC_PROXY_URL= ./public_proxy_cron.sh
# proxyList.txt is never touched; public proxies go to publicProxyList.txt.
set -euo pipefail

PATH="/usr/local/bin:/usr/bin:/bin:${PATH:-}"
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

PUBLIC_PROXY_URL="${PUBLIC_PROXY_URL-https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt}"
SQUID_CONF=/etc/squid/rotating/squid.conf

tmp="$(mktemp publicProxyList.txt.XXXXXX)"
backup="$(mktemp "${TMPDIR:-/tmp}/squid.conf.bak.XXXXXX")"
trap 'rm -f "$tmp" "$backup"' EXIT

if [ -n "$PUBLIC_PROXY_URL" ]; then
  curl -fsS --max-time 60 --retry 2 -o "$tmp" "$PUBLIC_PROXY_URL"
  if [ ! -s "$tmp" ]; then
    echo "public_proxy_cron: downloaded list is empty, keeping the previous one" >&2
    exit 1
  fi
  # A 200 response that is not a host:port list (an HTML error page, for
  # example) must not replace the previous list either.
  if ! grep -Eq '^[[:space:]]*(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._-]+):[0-9]{1,5}[[:space:]]*$' "$tmp"; then
    echo "public_proxy_cron: downloaded list contains no host:port lines, keeping the previous one" >&2
    exit 1
  fi
  chmod 644 "$tmp"
  mv -f "$tmp" publicProxyList.txt
fi

[ -f config/squid.conf ] && cp -p config/squid.conf "$backup"

if command -v python3 >/dev/null 2>&1; then
  python3 setup/generate.py --squid-only
else
  docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/app" -w /app python:3.13-alpine \
    python3 setup/generate.py --squid-only
fi

if [ -z "$(docker compose ps --status running -q squid 2>/dev/null)" ]; then
  echo "public_proxy_cron: the squid service is not running; config/squid.conf will be used by 'docker compose up -d'" >&2
  exit 1
fi

# A config error during reconfigure would stop Squid, so parse first.
if ! docker compose exec -T squid squid -k parse -f "$SQUID_CONF" >/dev/null 2>&1; then
  echo "public_proxy_cron: new squid.conf does not parse, restoring the previous one" >&2
  docker compose exec -T squid squid -k parse -f "$SQUID_CONF" 2>&1 | grep -E 'FATAL|ERROR' >&2 || true
  [ -s "$backup" ] && cp -p "$backup" config/squid.conf
  exit 1
fi
if ! out="$(docker compose exec -T squid squid -k reconfigure -f "$SQUID_CONF" 2>&1)"; then
  echo "$out" >&2
  # Keep the file on disk in step with the config Squid is still running.
  [ -s "$backup" ] && cp -p "$backup" config/squid.conf
  exit 1
fi
echo "public_proxy_cron: $(grep -c '^cache_peer' config/squid.conf) cache_peer(s) loaded"
