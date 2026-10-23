#!/usr/bin/env bash
# Refresh the pool of public proxies without restarting anything.
#
# Run it hourly from cron, for example:
#   0 * * * * /path/to/docker-rotating-proxy/public_proxy_cron.sh >>/var/log/rotating-proxy-cron.log 2>&1
#
# 1. Downloads the list to publicProxyList.txt (proxyList.txt is never touched;
#    a failed or empty download keeps the previous list).
# 2. Regenerates only config/nodes.json (proxyList.txt + publicProxyList.txt +
#    openvpn/). gost re-reads it every 30 seconds, so no container restart.
#
# Override the source with PUBLIC_PROXY_LIST_URL (one host:port per line).
set -euo pipefail

cd "$(dirname "$0")"

url="${PUBLIC_PROXY_LIST_URL:-https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt}"
target=publicProxyList.txt

tmp="$(mktemp "${target}.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

curl -fsSL --max-time 60 --retry 3 --retry-delay 5 -o "$tmp" "$url"

# Refuse to replace a working list with an empty or broken download.
if ! grep -Eq '^[^#[:space:]]+:[0-9]+' "$tmp"; then
  echo "public_proxy_cron.sh: no proxy found at $url, keeping the previous $target" >&2
  exit 1
fi
chmod 644 "$tmp"
mv -f "$tmp" "$target"

if command -v python3 >/dev/null 2>&1 &&
  python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  python3 setup/generate.py --nodes-only
else
  docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/app" -w /app \
    python:3.13-alpine python3 setup/generate.py --nodes-only
fi
