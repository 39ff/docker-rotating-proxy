#!/usr/bin/env bash
# End-to-end test for the generated gost/compose setup.
#
# Topology (all on the docker network rp-e2e, 172.30.99.0/24):
#   origin  172.30.99.1:18080  python http.server on the host (bridge
#                              gateway) that answers with the client IP,
#                              i.e. the IP of the upstream that reached it
#   up1     172.30.99.11:1080  socks5 user1:pass1
#   up2     172.30.99.12:1080  socks5 user2:pa:ss2  (':' inside the password)
#   up3     172.30.99.13:8080  http   user3:pass3
#   up4     172.30.99.14:3128  http without auth, via publicProxyList.txt
#   gost    attached to rp-e2e by ci/e2e.override.yml
#
# Because gost itself can reach the origin directly, a direct (fail-open)
# connection would be visible as gost's own IP in the answer.
#
# Usage: ci/e2e.sh            (needs docker, docker compose, curl, python3)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/rp-e2e.XXXXXX")"
PROJECT=rpe2e
NET=rp-e2e
SUBNET=172.30.99.0/24
GW=172.30.99.1
ORIGIN_PORT=18080
ORIGIN="http://${GW}:${ORIGIN_PORT}/"
UP1=172.30.99.11 UP2=172.30.99.12 UP3=172.30.99.13 UP4=172.30.99.14
OUTSIDER=172.30.99.20
IMAGE=gogost/gost:3.3.1
RELOAD_WAIT=50   # seconds; gost reloads data files every 30s
ORIGIN_PID=""

log() { printf '\n=== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

# E2E_EXTRA_COMPOSE: optional extra override file, e.g. to lower ulimits on
# hosts whose docker daemon cannot grant nofile=65536.
compose() {
  local extra=()
  if [ -n "${E2E_EXTRA_COMPOSE:-}" ]; then extra=(-f "$E2E_EXTRA_COMPOSE"); fi
  docker compose -p "$PROJECT" --project-directory "$WORK" \
    -f "$WORK/docker-compose.yml" -f "$WORK/e2e.override.yml" "${extra[@]}" "$@"
}

dump_logs() {
  set +e
  echo "----- docker compose ps"; compose ps -a
  echo "----- gost logs"; docker logs --tail 200 dockergost_rotate 2>&1
  for c in up1 up2 up3 up4; do
    echo "----- $c logs"; docker logs --tail 50 "rp-e2e-$c" 2>&1
  done
  echo "----- origin log"; tail -n 50 "$WORK/origin.log"
  echo "----- config/gost.yaml (credentials are test values)"
  cat "$WORK/config/gost.yaml"
  echo "----- config/nodes.json"; cat "$WORK/config/nodes.json"
  echo "----- config/allowed_ip.txt"; cat "$WORK/config/allowed_ip.txt"
}

cleanup() {
  local status=$?
  set +e
  if [ "$status" -ne 0 ]; then dump_logs; fi
  compose down -v -t 2 >/dev/null 2>&1
  docker rm -f rp-e2e-up1 rp-e2e-up2 rp-e2e-up3 rp-e2e-up4 >/dev/null 2>&1
  docker network rm "$NET" >/dev/null 2>&1
  [ -n "$ORIGIN_PID" ] && kill "$ORIGIN_PID" 2>/dev/null
  rm -rf "$WORK"
  if [ "$status" -eq 0 ]; then echo "E2E PASSED"; else echo "E2E FAILED"; fi
}
trap cleanup EXIT

# curl through a local gost port; prints the origin's answer (upstream IP).
# --noproxy '' makes curl ignore NO_PROXY/no_proxy from the environment.
via() {
  local port=$1; shift
  curl -sS --noproxy '' --max-time 15 -x "http://127.0.0.1:${port}" "$@" "$ORIGIN"
}
# HTTP status code only (000 when the connection failed).
status_via() {
  local port=$1; shift
  curl -s -o /dev/null -w '%{http_code}' --noproxy '' --max-time 15 \
    -x "http://127.0.0.1:${port}" "$@" "$ORIGIN" || true
}
# Wait until the given command succeeds, up to $1 seconds.
wait_for() {
  local timeout=$1; shift
  local start=$SECONDS
  until "$@"; do
    if (( SECONDS - start >= timeout )); then return 1; fi
    sleep 2
  done
}

log "work dir $WORK"
cp -r "$REPO/setup" "$REPO/template" "$WORK/"
cp "$REPO/ci/e2e.override.yml" "$WORK/e2e.override.yml"
mkdir -p "$WORK/config" "$WORK/openvpn"

log "origin + upstreams"
python3 - "$ORIGIN_PORT" >"$WORK/origin.log" 2>&1 <<'PY' &
import http.server, sys
class Echo(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        body = (self.client_address[0] + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.client_address[0], fmt % args))
        sys.stderr.flush()
http.server.ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), Echo).serve_forever()
PY
ORIGIN_PID=$!

docker network create --subnet "$SUBNET" --gateway "$GW" "$NET" >/dev/null
docker run -d --name rp-e2e-up1 --network "$NET" --ip "$UP1" "$IMAGE" \
  -L 'socks5://user1:pass1@:1080' >/dev/null
docker run -d --name rp-e2e-up2 --network "$NET" --ip "$UP2" "$IMAGE" \
  -L 'socks5://user2:pa:ss2@:1080' >/dev/null
docker run -d --name rp-e2e-up3 --network "$NET" --ip "$UP3" "$IMAGE" \
  -L 'http://user3:pass3@:8080' >/dev/null
docker run -d --name rp-e2e-up4 --network "$NET" --ip "$UP4" "$IMAGE" \
  -L 'http://:3128' >/dev/null
origin_up() { curl -sf --noproxy '*' --max-time 2 "$ORIGIN" >/dev/null; }
wait_for 20 origin_up || fail "origin did not start"

log "generate"
cat >"$WORK/proxyList.txt" <<EOF
# e2e upstreams (comment, blank line and CRLF on purpose)

${UP1}:1080:socks5:user1:pass1
${UP2}:1080:socks5:user2:pa:ss2
${UP3}:8080:http:user3:pass3
this line is invalid and must be skipped
EOF
printf '%s\r\n' "${UP3}:8080:http:user3:pass3" >>"$WORK/proxyList.txt"  # duplicate
python3 "$WORK/setup/generate.py"
compose config -q

log "docker compose up"
compose up -d
healthy() {
  [ "$(docker inspect -f '{{.State.Health.Status}}' dockergost_rotate 2>/dev/null)" = healthy ]
}
wait_for 90 healthy || fail "gost container did not become healthy"
wait_for 30 bash -c "curl -sf --noproxy '' --max-time 5 -x http://127.0.0.1:3128 $ORIGIN >/dev/null" \
  || fail "rotating port never answered"

log "rotation on :3128 (one request per connection)"
seq=()
for _ in 1 2 3 4 5 6; do seq+=("$(via 3128)"); done
echo "answers: ${seq[*]}"
for i in 0 1 2; do
  [ "${seq[$i]}" = "${seq[$((i + 3))]}" ] || fail "not round-robin: ${seq[*]}"
done
distinct=$(printf '%s\n' "${seq[@]}" | sort -u | tr '\n' ' ')
[ "$distinct" = "$UP1 $UP2 $UP3 " ] || fail "expected $UP1 $UP2 $UP3, got $distinct"

log "rotation per request on one keep-alive connection"
pair=$(curl -sS --noproxy '' --max-time 15 -x http://127.0.0.1:3128 "$ORIGIN" "$ORIGIN" | tr '\n' ' ')
echo "answers: $pair"
read -r a b <<<"$pair"
[ -n "$a" ] && [ -n "$b" ] && [ "$a" != "$b" ] || fail "same upstream on keep-alive: $pair"

log "rotation with CONNECT tunnels (curl -p)"
seq=()
for _ in 1 2 3; do seq+=("$(via 3128 -p)"); done
echo "answers: ${seq[*]}"
[ "$(printf '%s\n' "${seq[@]}" | sort -u | wc -l)" -eq 3 ] || fail "CONNECT not rotating: ${seq[*]}"

log "static ports are sticky"
for port_ip in "30000 $UP1" "30001 $UP2" "30002 $UP3"; do
  read -r port ip <<<"$port_ip"
  for _ in 1 2 3; do
    got=$(via "$port"); [ "$got" = "$ip" ] || fail "port $port answered $got, expected $ip"
    got=$(via "$port" -p); [ "$got" = "$ip" ] || fail "port $port (CONNECT) answered $got"
  done
  echo "port $port -> $ip (x6)"
done
code=$(status_via 30003)
[ "$code" = 000 ] || fail "port 30003 should not exist, got $code"

GOST_E2E_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NET\").IPAddress}}" dockergost_rotate)
echo "gost IP on $NET: $GOST_E2E_IP"
outsider() {
  docker run --rm --network "$NET" --ip "$OUTSIDER" --entrypoint wget \
    -e "http_proxy=http://${GOST_E2E_IP}:$1" "$IMAGE" -q -T 10 -O - "$ORIGIN"
}

log "allowlist (admission whitelist)"
got=$(outsider 3128) || fail "outsider should be allowed by the default allowlist"
echo "outsider via 3128 before: $got"
DEFAULT_GW=$(docker network inspect -f '{{(index .IPAM.Config 0).Gateway}}' "${PROJECT}_default")
printf '127.0.0.1\n%s\n%s\n' "$DEFAULT_GW" "$GW" >"$WORK/config/allowed_ip.txt.new"
mv "$WORK/config/allowed_ip.txt.new" "$WORK/config/allowed_ip.txt"
outsider_denied() { ! outsider 3128 >/dev/null 2>&1; }
wait_for "$RELOAD_WAIT" outsider_denied || fail "outsider still allowed after reload"
outsider_denied || fail "outsider allowed"
docker run --rm --network "$NET" --ip "$OUTSIDER" --entrypoint wget \
  -e "http_proxy=http://${GOST_E2E_IP}:30000" "$IMAGE" -q -T 10 -O - "$ORIGIN" >/dev/null 2>&1 \
  && fail "outsider allowed on static port 30000"
got=$(via 3128) || fail "host client denied"
echo "outsider denied on 3128 and 30000; host client still served by $got"

log "nodes.json hot reload (no restart)"
started=$(docker inspect -f '{{.State.StartedAt}}' dockergost_rotate)
python3 - "$WORK/config/nodes.json" "$UP2" <<'PY'
import json, os, sys
path, keep = sys.argv[1], sys.argv[2]
nodes = [n for n in json.load(open(path)) if n["addr"].startswith(keep + ":")]
open(path + ".new", "w").write(json.dumps(nodes))
os.replace(path + ".new", path)
PY
only_up2() {
  local i
  for i in 1 2 3 4; do [ "$(via 3128)" = "$UP2" ] || return 1; done
}
wait_for "$RELOAD_WAIT" only_up2 || fail "nodes.json change not picked up"
echo "pool reduced to $UP2 without restart"

log "fail-closed when the pool is empty"
echo '[]' >"$WORK/config/nodes.json.new" && mv "$WORK/config/nodes.json.new" "$WORK/config/nodes.json"
not_200() { [ "$(status_via 3128)" != 200 ]; }
wait_for "$RELOAD_WAIT" not_200 || fail "empty pool still answered 200"
for _ in 1 2 3; do
  code=$(status_via 3128); [ "$code" != 200 ] || fail "empty pool answered 200"
done
if grep -q "^${GOST_E2E_IP} " "$WORK/origin.log"; then
  fail "origin saw gost's own IP: direct connection (fail-open)"
fi
echo "empty pool -> HTTP $code, origin never saw $GOST_E2E_IP"

log "publicProxyList.txt + --nodes-only (cron flow)"
printf '# fetched list\n%s:3128\n' "$UP4" >"$WORK/publicProxyList.txt"
python3 "$WORK/setup/generate.py" --nodes-only
has_up4() {
  local i seen=""
  for i in 1 2 3 4 5 6 7 8; do seen+=" $(via 3128)"; done
  [[ "$seen" == *"$UP4"* && "$seen" == *"$UP1"* ]]
}
wait_for "$RELOAD_WAIT" has_up4 || fail "public proxy not added to the pool"
[ "$(via 30000)" = "$UP1" ] || fail "static port changed after --nodes-only"
[ "$(docker inspect -f '{{.State.StartedAt}}' dockergost_rotate)" = "$started" ] \
  || fail "gost was restarted"
echo "pool restored and extended with $UP4, gost not restarted"

log "client auth via template/users.txt"
printf '# test users\ne2euser e2epass\n' >"$WORK/template/users.txt"
python3 "$WORK/setup/generate.py"
compose up -d
wait_for 90 healthy || fail "gost not healthy after enabling auth"
[ "$(docker inspect -f '{{.State.StartedAt}}' dockergost_rotate)" != "$started" ] \
  || fail "compose did not recreate gost after gost.yaml changed"
auth_ready() { [ "$(status_via 3128 -U e2euser:e2epass)" = 200 ]; }
wait_for 30 auth_ready || fail "authenticated request failed"
for port in 3128 30000 30002; do
  code=$(status_via "$port"); [ "$code" = 407 ] || fail "port $port without credentials: $code"
  code=$(status_via "$port" -U e2euser:wrong); [ "$code" = 407 ] || fail "port $port wrong password: $code"
  code=$(status_via "$port" -U e2euser:e2epass); [ "$code" = 200 ] || fail "port $port with credentials: $code"
  echo "port $port: no creds 407, wrong 407, correct 200"
done
code=$(status_via 3128 -p -U e2euser:e2epass); [ "$code" = 200 ] || fail "CONNECT with credentials: $code"
got=$(via 30001 -U e2euser:e2epass); [ "$got" = "$UP2" ] || fail "static 30001 with auth: $got"

log "all checks passed"
