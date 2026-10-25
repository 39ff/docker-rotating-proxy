#!/usr/bin/env bash
# CI helper: write sample inputs that cover every proxyList.txt scheme, the
# public list and both example VPN directories. Overwrites proxyList.txt.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."

cat > proxyList.txt <<'LIST'
# CI sample: every scheme
203.0.113.10:1080:socks5:alice:p@ss:w%rd!
203.0.113.11:8080:http:bob:b0b-secret
203.0.113.12:8080:http
proxy.example.com:443:https:carol:c$rol/pw
[2001:db8::10]:1080:socks5:dave:d4ve
zproxy.example.net:22225:httpsquid:erin:e%r+in
198.51.100.7:3128
[2001:db8::20]:3128
this line is invalid and must be skipped
LIST
printf '198.51.100.8:80\r\n198.51.100.9:8080\r\n198.51.100.7:3128\r\n' > publicProxyList.txt
cp -r examples/openvpn/. openvpn/
