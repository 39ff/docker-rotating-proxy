#!/usr/bin/env bash
# End-to-end test of the generated stack (used by .github/workflows/ci.yml).
#
# Run it from a disposable checkout: it overwrites proxyList.txt, deletes
# openvpn/*/ and publicProxyList.txt, then runs `docker compose up`.
#   E2E_SQUID_PORT / E2E_STATIC_BASE  host ports of Squid / first static port
#   E2E_KEEP=1                        keep the containers running afterwards
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
# The test talks to 127.0.0.1 and 172.16.231.0/24 directly; ambient proxy
# settings (NO_PROXY applies even to curl -x) must not interfere.
unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:.github/e2e/compose.e2e.yml}"
SQUID_PORT="${E2E_SQUID_PORT:-3128}"
STATIC_BASE="${E2E_STATIC_BASE:-30000}"
ORIGIN=172.16.231.210
NET=172.16.231

step() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok: %s\n' "$*"; }

on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    step "diagnostics (exit $rc)"
    docker compose ps -a || true
    docker compose logs --no-color --tail=150 || true
    echo "--- config/squid.conf (cache_peer lines)"; grep '^cache_peer' config/squid.conf | sed -E 's/login=[^ ]+/login=***/' || true
  fi
  if [ "${E2E_KEEP:-0}" != 1 ]; then docker compose down -v --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$rc"
}
trap on_exit EXIT

via() { # via PORT URL [curl args...] -> response body
  local port=$1 url=$2; shift 2
  curl -sS --fail --max-time 15 -x "http://127.0.0.1:${port}" "$@" "$url"
}

collect() { # collect PORT URL COUNT [curl args...] -> one origin-seen IP per line
  local port=$1 url=$2 count=$3; shift 3
  for _ in $(seq "$count"); do via "$port" "$url" "$@" || echo "request-failed"; done
}

step "test certificates"
rm -rf .e2e && mkdir -p .e2e/certs && cd .e2e/certs
openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=rotating-proxy e2e CA" \
  -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout ca.key -out ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=secure-upstream.test" -keyout server.key -out server.csr 2>/dev/null
printf 'subjectAltName=DNS:secure-upstream.test,IP:%s\nextendedKeyUsage=serverAuth\n' "$ORIGIN" > ext.cnf
openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -extfile ext.cnf -out server.pem 2>/dev/null
chmod 644 ./*
cd "$ROOT"

step "inputs"
cat > proxyList.txt <<'LIST'
# e2e upstreams (see .github/e2e/compose.e2e.yml)
172.16.231.201:1080:socks5:alice:p@ss%w/rd
172.16.231.202:1080:socks5:bob:b0b:pw

172.16.231.203:8080:http:erin:erin-pw
secure-upstream.test:8443:https:dave:dave-pw
# nothing listens here: gost answers 503 and Squid must retry another peer
172.16.231.240:1080:socks5:nobody:nothing
this line is invalid and must be skipped
LIST
rm -f publicProxyList.txt
find openvpn -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
python3 setup/generate.py
docker compose config -q
[ "$(grep -c '^cache_peer' config/squid.conf)" = 5 ] || fail "expected 5 cache_peer lines"

step "start"
docker compose up -d --quiet-pull --wait --wait-timeout 180
docker compose ps

step "wait until the rotating port answers"
for i in $(seq 60); do
  if via "$SQUID_PORT" "http://$ORIGIN/" >/dev/null 2>&1; then break; fi
  [ "$i" = 60 ] && fail "Squid did not forward a request within 60s"
  sleep 1
done
ok "Squid forwards"

step "rotation over all upstreams (port $SQUID_PORT)"
seen=$(collect "$SQUID_PORT" "http://$ORIGIN/" 16 | sort | uniq -c)
echo "$seen"
for ip in 201 202 203 205; do
  echo "$seen" | grep -q " $NET.$ip\$" || fail "upstream $NET.$ip never used through port $SQUID_PORT"
done
echo "$seen" | grep -q -E 'request-failed' && fail "some requests through Squid failed (dead upstream not retried?)"
ok "requests rotate across socks5 x2, http (gost) and https (Squid TLS); the dead upstream is retried away"

step "header scrubbing"
headers=$(via "$SQUID_PORT" "http://$ORIGIN/headers")
echo "$headers"
python3 - "$headers" <<'PY'
import json, sys
h = json.loads(sys.argv[1])
bad = [k for k in ("via", "x-forwarded-for", "forwarded") if k in h]
sys.exit("leaked headers: %s" % bad if bad else 0)
PY
ok "no Via / X-Forwarded-For at the origin"

step "HTTPS (CONNECT) through the rotating port"
seen=$(collect "$SQUID_PORT" "https://$ORIGIN/" 8 -k | sort | uniq -c)
echo "$seen"
echo "$seen" | grep -q 'request-failed' && fail "CONNECT through Squid failed"
[ "$(echo "$seen" | wc -l)" -ge 2 ] || fail "CONNECT requests did not rotate"
ok "CONNECT works and rotates"

step "static ports are sticky"
expect_static() { # expect_static OFFSET IP
  local port=$((STATIC_BASE + $1)) want="$NET.$2" got
  got=$( { collect "$port" "http://$ORIGIN/" 4; collect "$port" "https://$ORIGIN/" 2 -k; } | sort -u)
  [ "$got" = "$want" ] || fail "port $port: expected only $want, got: $(echo "$got" | tr '\n' ' ')"
  ok "port $port -> $want (http + https)"
}
expect_static 0 201
expect_static 1 202
expect_static 2 203
expect_static 3 205

step "clients outside the allowlist are refused"
probe() { docker compose exec -T outsider python3 /e2e/probe.py "$@"; }
out=$(probe dockersquid_rotate 3128 "http://$ORIGIN/"); echo "squid: $out"
case "$out" in 403*) ok "Squid answers 403 to $NET.250";; *) fail "Squid did not deny $NET.250: $out";; esac
out=$(probe dockergost_rotate 30000 "http://$ORIGIN/"); echo "gost:  $out"
case "$out" in ERR*) ok "gost closes the connection from $NET.250";; *) fail "gost static port did not deny $NET.250: $out";; esac

step "cron path: new peers via proxyList.txt + publicProxyList.txt, squid -k reconfigure"
squid_id=$(docker compose ps -q squid)
before=$(docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' "$squid_id")
echo '172.16.231.206:3128:httpsquid:carol:c%l:o@n+e/=' >> proxyList.txt
printf '172.16.231.204:3128\r\nnot-a-proxy\r\n' > .e2e/public.txt
PUBLIC_PROXY_URL="file://$ROOT/.e2e/public.txt" ./public_proxy_cron.sh
[ "$(grep -c '^cache_peer' config/squid.conf)" = 7 ] || fail "expected 7 cache_peer lines after the cron run"
grep -q 'login=carol:c%25l%3Ao%40n%2Be%2F%3D' config/squid.conf || fail "login= not percent-encoded as expected"
for i in $(seq 30); do
  seen=$(collect "$SQUID_PORT" "http://$ORIGIN/" 12 | sort -u)
  if echo "$seen" | grep -qx "$NET.204" && echo "$seen" | grep -qx "$NET.206"; then break; fi
  [ "$i" = 30 ] && fail "new peers $NET.204 / $NET.206 not used after reconfigure; saw: $(echo "$seen" | tr '\n' ' ')"
  sleep 2
done
after=$(docker inspect -f '{{.State.StartedAt}} {{.RestartCount}}' "$squid_id")
[ "$before" = "$after" ] || fail "squid container restarted ($before -> $after)"
ok "public peer and httpsquid peer (special-character password) added without restart"

step "all checks passed"
