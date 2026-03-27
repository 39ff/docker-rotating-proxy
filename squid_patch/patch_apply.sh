#!/bin/bash
#
# patch_apply.sh - Apply SOCKS cache_peer support patches to Squid source
#
# Usage: patch_apply.sh <patch_src_dir> <squid_src_dir>
#
# Modifies the Squid 6.x source tree to add SOCKS4/SOCKS5 support for
# cache_peer directives.  Uses pattern-based modifications (sed + Python)
# so that the script is tolerant of minor changes across 6.x point releases.
#
set -euo pipefail

PATCH_SRC="${1:?Usage: $0 <patch_src_dir> <squid_src_dir>}"
SQUID_SRC="${2:?Usage: $0 <patch_src_dir> <squid_src_dir>}"

die() { echo "PATCH ERROR: $*" >&2; exit 1; }

echo "==> Copying SocksPeerConnector.h into ${SQUID_SRC}/src/"
cp "${PATCH_SRC}/SocksPeerConnector.h" "${SQUID_SRC}/src/SocksPeerConnector.h" \
    || die "Failed to copy SocksPeerConnector.h"

# ---------------------------------------------------------------------------
# 1. CachePeer.h  –  add socks_type / socks_user / socks_pass fields
# ---------------------------------------------------------------------------
CACHE_PEER_H="${SQUID_SRC}/src/CachePeer.h"
echo "==> Patching ${CACHE_PEER_H}"
[ -f "${CACHE_PEER_H}" ] || die "CachePeer.h not found"

grep -q 'class CachePeer' "${CACHE_PEER_H}" || die "CachePeer class not found"

# Do NOT include SocksPeerConnector.h here – it pulls in POSIX headers
# that must come after squid.h.  Use plain int/char* for the fields.
if ! grep -q 'socks_type' "${CACHE_PEER_H}"; then
    if grep -q '} options;' "${CACHE_PEER_H}" 2>/dev/null; then
        sed -i '/} options;/a\
\
    /* SOCKS proxy support for cache_peer (0=none, 4=SOCKS4, 5=SOCKS5) */\
    int socks_type = 0;\
    char *socks_user = nullptr;\
    char *socks_pass = nullptr;' "${CACHE_PEER_H}"
    else
        sed -i '/^};/i\
\
    /* SOCKS proxy support for cache_peer (0=none, 4=SOCKS4, 5=SOCKS5) */\
    int socks_type = 0;\
    char *socks_user = nullptr;\
    char *socks_pass = nullptr;\
' "${CACHE_PEER_H}"
    fi
fi

echo "    CachePeer.h patched OK"

# ---------------------------------------------------------------------------
# 2. cache_cf.cc  –  parse socks4 / socks5 / socks-user= / socks-pass=
# ---------------------------------------------------------------------------
CACHE_CF="${SQUID_SRC}/src/cache_cf.cc"
echo "==> Patching ${CACHE_CF}"
[ -f "${CACHE_CF}" ] || die "cache_cf.cc not found"

if ! grep -q 'socks_type' "${CACHE_CF}"; then
    ANCHOR=""
    for pattern in 'proxy-only' 'no-digest' 'no-query' 'round-robin' 'originserver'; do
        if grep -q "\"${pattern}\"" "${CACHE_CF}"; then
            ANCHOR="${pattern}"
            break
        fi
    done

    [ -n "${ANCHOR}" ] || die "Could not find peer option parsing anchor in cache_cf.cc"
    echo "    Using anchor: '${ANCHOR}'"

    python3 - "${CACHE_CF}" "${ANCHOR}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]
anchor = sys.argv[2]

with open(filepath, 'r') as f:
    content = f.read()

# Use xstrdup for string allocation (Squid's malloc wrapper)
socks_code = '''
        } else if (!strcmp(token, "socks4")) {
            p->socks_type = 4;
        } else if (!strcmp(token, "socks5")) {
            p->socks_type = 5;
        } else if (!strncmp(token, "socks-user=", 11)) {
            safe_free(p->socks_user);
            p->socks_user = xstrdup(token + 11);
        } else if (!strncmp(token, "socks-pass=", 11)) {
            safe_free(p->socks_pass);
            p->socks_pass = xstrdup(token + 11);
'''

# Find the anchor in a strcmp context and insert after its closing brace
pattern = re.compile(
    r'(else\s+if\s*\(!strcmp\(token,\s*"' + re.escape(anchor) + r'"\)\)\s*\{[^}]*\})',
    re.DOTALL
)

match = pattern.search(content)
if match:
    insert_pos = match.end()
    content = content[:insert_pos] + socks_code + content[insert_pos:]
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"    Inserted SOCKS parsing after '{anchor}' block")
else:
    # Fallback: brace-counting approach
    simple = f'"{anchor}"'
    idx = content.find(simple)
    if idx < 0:
        print(f"ERROR: Could not find '{anchor}' in cache_cf.cc", file=sys.stderr)
        sys.exit(1)
    brace_start = content.find('{', idx)
    if brace_start < 0:
        print("ERROR: Could not find opening brace", file=sys.stderr)
        sys.exit(1)
    depth = 1
    pos = brace_start + 1
    while pos < len(content) and depth > 0:
        if content[pos] == '{': depth += 1
        elif content[pos] == '}': depth -= 1
        pos += 1
    content = content[:pos] + socks_code + content[pos:]
    with open(filepath, 'w') as f:
        f.write(content)
    print(f"    Inserted SOCKS parsing (fallback) after '{anchor}' block")
PYEOF
fi

echo "    cache_cf.cc patched OK"

# ---------------------------------------------------------------------------
# 3. FwdState.cc  –  SOCKS negotiation at the top of dispatch()
# ---------------------------------------------------------------------------
FWD_STATE="${SQUID_SRC}/src/FwdState.cc"
echo "==> Patching ${FWD_STATE}"
[ -f "${FWD_STATE}" ] || die "FwdState.cc not found"

# Add include – after the first #include line (squid.h is always first)
if ! grep -q 'SocksPeerConnector.h' "${FWD_STATE}"; then
    sed -i '0,/#include/{s/#include/#include "SocksPeerConnector.h"\n#include/}' "${FWD_STATE}"
    # Verify it was inserted
    grep -q 'SocksPeerConnector.h' "${FWD_STATE}" || die "Failed to add include to FwdState.cc"
fi

if ! grep -q 'socks_type' "${FWD_STATE}"; then
    python3 - "${FWD_STATE}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Squid 6.x API:
#   serverConnection() returns Comm::ConnectionPointer const &
#   ->getPeer() returns CachePeer*
#   ->fd is int (public member of Comm::Connection)
#   request->url.host() returns SBuf (use .c_str() for const char*)
#   request->url.port() returns unsigned short
#   retryOrBail() is a private method of FwdState
socks_hook = r'''
    /* SOCKS peer negotiation: after TCP connect, before HTTP dispatch */
    if (const auto sp = serverConnection()->getPeer()) {
        if (sp->socks_type) {
            const auto targetPort = static_cast<uint16_t>(request->url.port());
            debugs(17, 3, "SOCKS" << sp->socks_type
                   << " negotiation with peer " << sp->host
                   << " for " << request->url.host() << ":" << targetPort);
            if (!SocksPeerConnector::negotiate(
                    serverConnection()->fd,
                    static_cast<SocksPeerType>(sp->socks_type),
                    std::string(request->url.host().c_str()),
                    targetPort,
                    sp->socks_user ? std::string(sp->socks_user) : std::string(),
                    sp->socks_pass ? std::string(sp->socks_pass) : std::string())) {
                debugs(17, 2, "SOCKS negotiation FAILED for peer " << sp->host);
                retryOrBail();
                return;
            }
            debugs(17, 3, "SOCKS negotiation OK for peer " << sp->host);
        }
    }

'''

inserted = False

# Pattern: void FwdState::dispatch()  {
for pat in [
    r'(void\s+FwdState::dispatch\s*\(\s*\)\s*\{)',
    r'(FwdState::dispatch\s*\(\s*\)\s*\n?\s*\{)',
]:
    match = re.search(pat, content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + socks_hook + content[insert_pos:]
        inserted = True
        print("    Inserted SOCKS hook at top of FwdState::dispatch()")
        break

if not inserted:
    print("ERROR: Could not find dispatch() insertion point in FwdState.cc", file=sys.stderr)
    print("       SOCKS support for HTTP requests will not work", file=sys.stderr)
    sys.exit(1)

with open(filepath, 'w') as f:
    f.write(content)
PYEOF
fi

echo "    FwdState.cc patched OK"

# ---------------------------------------------------------------------------
# 4. tunnel.cc  –  SOCKS negotiation in connectDone() for CONNECT/HTTPS
# ---------------------------------------------------------------------------
TUNNEL_CC="${SQUID_SRC}/src/tunnel.cc"
echo "==> Patching ${TUNNEL_CC}"
[ -f "${TUNNEL_CC}" ] || die "tunnel.cc not found"

# Add include – after the first #include line
if ! grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}"; then
    sed -i '0,/#include/{s/#include/#include "SocksPeerConnector.h"\n#include/}' "${TUNNEL_CC}"
    grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}" || die "Failed to add include to tunnel.cc"
fi

if ! grep -q 'socks_type' "${TUNNEL_CC}"; then
    python3 - "${TUNNEL_CC}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# tunnel.cc API (Squid 6.x):
#   TunnelStateData has: server.conn (Comm::ConnectionPointer), request (HttpRequestPointer)
#   connectDone(const Comm::ConnectionPointer &conn, ...) - called after TCP connect
#   conn->getPeer() returns CachePeer*
#   conn->fd is int
#   For originserver peers, connectDone goes to notePeerReadyToShovel() (shovels data)
#   For non-origin peers, connectDone goes to connectToPeer() (sends HTTP CONNECT)
#   SOCKS peers use originserver, so after SOCKS negotiation the tunnel is ready.
socks_tunnel_hook = r'''
    /* SOCKS peer: negotiate tunnel right after TCP connect */
    if (conn->getPeer() && conn->getPeer()->socks_type) {
        const auto sp = conn->getPeer();
        const auto targetPort = static_cast<uint16_t>(request->url.port());
        debugs(26, 3, "SOCKS" << sp->socks_type
               << " tunnel negotiation with peer " << sp->host
               << " for " << request->url.host() << ":" << targetPort);
        if (!SocksPeerConnector::negotiate(
                conn->fd,
                static_cast<SocksPeerType>(sp->socks_type),
                std::string(request->url.host().c_str()),
                targetPort,
                sp->socks_user ? std::string(sp->socks_user) : std::string(),
                sp->socks_pass ? std::string(sp->socks_pass) : std::string())) {
            debugs(26, 2, "SOCKS tunnel negotiation FAILED for " << sp->host);
            conn->close();
            return;
        }
        debugs(26, 3, "SOCKS tunnel negotiation OK for " << sp->host);
    }

'''

inserted = False

# Target: TunnelStateData::connectDone  or  tunnelConnectDone
for pat in [
    r'(void\s+TunnelStateData::connectDone\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::connectDone\s*\([^)]*\)\s*\n?\s*\{)',
    r'(void\s+tunnelConnectDone\s*\([^)]*\)\s*\{)',
    # Fallback: connectToPeer
    r'(void\s+TunnelStateData::connectToPeer\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::connectToPeer\s*\([^)]*\)\s*\n?\s*\{)',
]:
    match = re.search(pat, content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + socks_tunnel_hook + content[insert_pos:]
        inserted = True
        print(f"    Inserted SOCKS tunnel hook in {match.group(0).strip()[:70]}...")
        break

if not inserted:
    print("ERROR: Could not patch tunnel.cc - HTTPS tunneling through SOCKS peers will not work", file=sys.stderr)
    sys.exit(1)

with open(filepath, 'w') as f:
    f.write(content)
PYEOF
fi

echo "    tunnel.cc patched OK"

echo ""
echo "==> All patches applied successfully"
echo ""
echo "Modified files:"
echo "  - src/CachePeer.h            (added socks_type/user/pass fields)"
echo "  - src/cache_cf.cc            (added socks4/socks5 option parsing)"
echo "  - src/FwdState.cc            (SOCKS negotiation in dispatch())"
echo "  - src/tunnel.cc              (SOCKS negotiation in connectDone())"
echo "  - src/SocksPeerConnector.h   (new: SOCKS4/5 protocol implementation)"
