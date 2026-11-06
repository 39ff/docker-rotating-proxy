#!/usr/bin/env bash
# End-to-end test of the web UI (manager/): starts the manager container from
# manager/compose.yml, then drives its HTTP API the way the browser does:
# first-run setup, pasting upstreams in provider formats, the allowlist,
# "Apply" (generate.py + docker compose up), "Test" (probes through 3128 and
# the static ports), deleting an upstream, and the public list refresh.
#
# Run it from a disposable checkout: it overwrites proxyList.txt, .env,
# manager/.env and template/allowed_ip.txt, and starts containers.
#   E2E_EXTRA_COMPOSE=path.yml   extra compose file for the proxy stack
#   E2E_MANAGER_BUILD=0          use an existing rotating-proxy-manager:local image
#   E2E_KEEP=1                   keep everything running afterwards
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
unset http_proxy HTTP_PROXY https_proxy HTTPS_PROXY all_proxy ALL_PROXY no_proxy NO_PROXY
PORT="${E2E_MANAGER_PORT:-18080}"
BASE="http://127.0.0.1:$PORT"
NET=172.16.231
JAR="$(mktemp)"
MCOMPOSE=(docker compose -f manager/compose.yml)

step() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
ok() { printf 'ok: %s\n' "$*"; }
# .manager/ is created by the manager, which runs as root, with mode 0700:
# look at it from inside the container, as an unprivileged runner cannot.
mexec() { "${MCOMPOSE[@]}" exec -T manager "$@"; }

on_exit() {
  rc=$?
  if [ "$rc" -ne 0 ]; then
    step "diagnostics (exit $rc)"
    "${MCOMPOSE[@]}" logs --no-color --tail=100 || true
    docker compose ps -a || true
    docker compose logs --no-color --tail=60 || true
  fi
  if [ "${E2E_KEEP:-0}" != 1 ]; then
    docker compose down -v --remove-orphans >/dev/null 2>&1 || true
    "${MCOMPOSE[@]}" down >/dev/null 2>&1 || true
  fi
  rm -f "$JAR"
  exit "$rc"
}
trap on_exit EXIT

# api METHOD PATH [JSON] -> response body; fails on HTTP errors
api() {
  local method=$1 path=$2 body='{}'
  [ $# -ge 3 ] && body=$3
  if [ "$method" = GET ]; then
    curl -sS --fail-with-body -b "$JAR" -c "$JAR" "$BASE$path"
  else
    curl -sS --fail-with-body -b "$JAR" -c "$JAR" -X "$method" -H 'Content-Type: application/json' \
      -H 'X-RPX-Request: 1' --data "$body" "$BASE$path"
  fi
}
json() { python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {"d": d}))' "$1"; }

wait_job() { # wait_job JOB_JSON -> prints the final job, fails unless it succeeded
  local id status
  id=$(echo "$1" | json 'd["id"]')
  for _ in $(seq 300); do
    job=$(api GET "/api/jobs/$id")
    status=$(echo "$job" | json 'd["status"]')
    [ "$status" != running ] && break
    sleep 1
  done
  echo "$job" | json 'd["log"]'
  [ "$status" = succeeded ] || fail "job $id ended with status $status"
}

step "test certificates and inputs"
rm -rf .e2e && mkdir -p .e2e/certs && (
  cd .e2e/certs
  openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=rotating-proxy e2e CA" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout ca.key -out ca.pem 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=secure-upstream.test" -keyout server.key -out server.csr 2>/dev/null
  printf 'subjectAltName=DNS:secure-upstream.test\nextendedKeyUsage=serverAuth\n' > ext.cnf
  openssl x509 -req -in server.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 2 -extfile ext.cnf -out server.pem 2>/dev/null
  chmod 644 ./*
)
git checkout -- proxyList.txt template/allowed_ip.txt 2>/dev/null || true
rm -f publicProxyList.txt docker-compose.yml
find openvpn -mindepth 1 -maxdepth 1 -type d -exec rm -rf {} +
rm -rf .manager 2>/dev/null || docker run --rm -v "$ROOT:$ROOT" python:3.13-alpine rm -rf "$ROOT/.manager"
# The proxy stack picks up the upstream fixtures through COMPOSE_FILE in .env,
# exactly as a user's own additions would be picked up.
printf 'COMPOSE_FILE=docker-compose.yml:.github/e2e/compose.e2e.yml%s\n' "${E2E_EXTRA_COMPOSE:+:$E2E_EXTRA_COMPOSE}" > .env
cat > manager/.env <<EOF
RPX_ROOT=$ROOT
RPX_BIND=127.0.0.1
RPX_PORT=$PORT
RPX_PUBLIC_HOST=proxy.example.test
RPX_PROBE_URL=http://$NET.210/
EOF

step "start the manager"
build=--build
[ "${E2E_MANAGER_BUILD:-1}" = 0 ] && build=--no-build
"${MCOMPOSE[@]}" up -d "$build" --quiet-pull --wait --wait-timeout 120
token=$(mexec cat "$ROOT/.manager/setup_token")
[ -n "$token" ] || fail "no setup token written"
"${MCOMPOSE[@]}" logs --no-color | grep -q "setup?token=$token" || fail "setup URL not in the manager log"
ok "manager is up, setup URL is in the log"

step "setup and login"
[ "$(api GET /api/session | json 'd["setup_required"]')" = True ] || fail "setup should be required"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' --data '{}' "$BASE/api/setup")
[ "$code" = 403 ] || fail "POST without X-RPX-Request should be 403, got $code"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST -H 'Content-Type: application/json' -H 'X-RPX-Request: 1' \
  --data '{"token":"wrong","password":"0123456789abc"}' "$BASE/api/setup")
[ "$code" = 403 ] || fail "wrong token should be 403, got $code"
api POST /api/setup "{\"token\":\"$token\",\"password\":\"e2e password 123\"}" >/dev/null
mexec test ! -e "$ROOT/.manager/setup_token" || fail "setup token not removed after setup"
[ "$(mexec stat -c %a "$ROOT/.manager/auth.json")" = 600 ] || fail ".manager/auth.json should be 0600"
code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/api/overview")
[ "$code" = 401 ] || fail "overview without a session should be 401, got $code"
ok "setup token, CSRF header and session checks"

step "paste upstreams in provider formats"
text=$(python3 -c 'import json; print(json.dumps("\n".join([
  "socks5://alice:p%40ss%25w%2Frd@172.16.231.201:1080",   # URL, percent-encoded password
  "172.16.231.202:1080:bob:b0b:pw",                        # host:port:user:pass, default socks5
  "secure-upstream.test:8443:https:dave:dave-pw",          # proxyList.txt format
  "not a proxy",
])))')
preview=$(api POST /api/upstreams/preview "{\"text\":$text,\"default_scheme\":\"socks5\"}")
[ "$(echo "$preview" | json 'sum(r["ok"] for r in d["rows"])')" = 3 ] || fail "preview: expected 3 usable rows: $preview"
echo "$preview" | grep -q 'b0b' && fail "preview leaked a password"
added=$(api POST /api/upstreams/add "{\"text\":$text,\"default_scheme\":\"socks5\"}")
[ "$(echo "$added" | json 'd["added"]')" = 3 ] || fail "add: $added"
api POST /api/upstreams/add '{"text":"erin:erin-pw@172.16.231.203:8080","default_scheme":"http"}' >/dev/null
again=$(api POST /api/upstreams/add '{"text":"erin:erin-pw@172.16.231.203:8080","default_scheme":"http"}')
[ "$(echo "$again" | json 'd["duplicates"]')" = 1 ] || fail "duplicate not detected: $again"
grep -qx '172.16.231.201:1080:socks5:alice:p@ss%w/rd' proxyList.txt || fail "URL not converted"
grep -qx '172.16.231.202:1080:socks5:bob:b0b:pw' proxyList.txt || fail "host:port:user:pass not converted"
grep -qx '172.16.231.203:8080:http:erin:erin-pw' proxyList.txt || fail "user:pass@host:port not converted"
ups=$(api GET /api/upstreams)
echo "$ups" | grep -q -E 'p@ss|b0b|erin-pw|dave-pw' && fail "/api/upstreams leaked a password"
[ "$(echo "$ups" | json '[e["static_port"] for e in d["entries"]]')" = "[30000, 30001, 30002, 30003]" ] || fail "static ports: $ups"
ok "4 upstreams added, converted, no passwords in API responses"

step "allowlist"
api POST /api/allowlist/add '{"value":"198.51.100.0/24","comment":"office"}' >/dev/null
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json' -H 'X-RPX-Request: 1' \
  --data '{"value":"0.0.0.0/0"}' "$BASE/api/allowlist/add")
[ "$code" = 400 ] || fail "0.0.0.0/0 should be refused, got $code"
grep -q '^198.51.100.0/24 .*# office' template/allowed_ip.txt || fail "allowlist entry not written"
sys_id=$(api GET /api/allowlist | json '[e["id"] for e in d["entries"] if e["system"]][0]')
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$JAR" -X POST -H 'Content-Type: application/json' -H 'X-RPX-Request: 1' \
  --data "{\"ids\":[\"$sys_id\"]}" "$BASE/api/allowlist/delete")
[ "$code" = 400 ] || fail "deleting a system entry should be refused, got $code"
ok "allowlist add, open-proxy and system-entry guards"

step "apply"
[ "$(api GET /api/overview | json 'd["pending"]')" = True ] || fail "overview should report pending changes"
wait_job "$(api POST /api/apply)"
ov=$(api GET /api/overview)
[ "$(echo "$ov" | json 'd["pending"]')" = False ] || fail "still pending after apply"
echo "$ov" | json '[(s["service"], s["health"]) for s in d["compose"]["services"]]'
ok "generate.py + docker compose up via the UI"

step "test (probe) through the rotating port and the static ports"
probe=$(api POST /api/probe)
echo "$probe" | python3 -m json.tool
echo "$probe" | python3 -c '
import json, sys
p = json.load(sys.stdin)
rot = p["rotating"]
assert rot["ok"] == rot["total"] == 6, rot
assert len(rot["ips"]) >= 2, "no rotation: %s" % rot
want = {30000: "172.16.231.201", 30001: "172.16.231.202", 30002: "172.16.231.205", 30003: "172.16.231.203"}
got = {s["port"]: s.get("ip") for s in p["static"]}
assert got == want, got
'
ok "rotation and sticky static ports seen by the origin"

step "delete an upstream, apply again"
id=$(api GET /api/upstreams | json '[e["id"] for e in d["entries"] if e["host"] == "172.16.231.202"][0]')
api POST /api/upstreams/delete "{\"ids\":[\"$id\"]}" >/dev/null
grep -q '172.16.231.202' proxyList.txt && fail "line not deleted"
mexec sh -c 'ls "$1"/.manager/backups/proxyList.txt.*' _ "$ROOT" >/dev/null || fail "no backup written"
wait_job "$(api POST /api/apply)"
[ "$(api GET /api/overview | json 'len(d["static_ports"])')" = 3 ] || fail "expected 3 static ports after delete"
ok "delete + apply"

step "public list refresh (public_proxy_cron.sh inside the manager)"
printf '172.16.231.204:3128\n' > .e2e/public.txt
wait_job "$(api POST /api/public/refresh "{\"url\":\"file://$ROOT/.e2e/public.txt\"}")"
[ "$(api GET /api/upstreams | json 'd["public_count"]')" = 1 ] || fail "public list not loaded"
for i in $(seq 20); do
  seen=$(api POST /api/probe | json '" ".join(d["rotating"]["ips"])')
  case " $seen " in *" $NET.204 "*) break ;; esac
  [ "$i" = 20 ] && fail "public peer $NET.204 never used; saw $seen"
done
ok "public peer in rotation after squid -k reconfigure"

step "all manager checks passed"
