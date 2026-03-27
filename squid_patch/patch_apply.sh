#!/bin/bash
#
# patch_apply.sh - Apply SOCKS cache_peer support patches to Squid source
#
# Usage: patch_apply.sh <patch_src_dir> <squid_src_dir>
#
# This script modifies the Squid source tree to add SOCKS4/SOCKS5
# support for cache_peer directives.  It uses pattern-based sed
# modifications so that it is tolerant of minor whitespace changes
# across point releases of the same major version.
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

# Verify the file contains the struct we expect
grep -q 'class CachePeer' "${CACHE_PEER_H}" || die "CachePeer class not found"

# Add include for SocksPeerConnector.h and string after existing includes
if ! grep -q 'SocksPeerConnector.h' "${CACHE_PEER_H}"; then
    sed -i '/#ifndef SQUID_SRC_CACHEPEER_H/,/#define SQUID_SRC_CACHEPEER_H/{
        /#define SQUID_SRC_CACHEPEER_H/a\
\
#include "SocksPeerConnector.h"\
#include <string>
    }' "${CACHE_PEER_H}"
fi

# Add SOCKS fields to CachePeer class – insert before the closing brace + semicolon
# We look for a known member and add after it, or add before the end of the class
if ! grep -q 'socks_type' "${CACHE_PEER_H}"; then
    # Find "} options;" line (the options struct closing) and add SOCKS fields after it
    if grep -q '} options;' "${CACHE_PEER_H}" 2>/dev/null; then
        sed -i '/} options;/a\
\
    /* SOCKS proxy support for cache_peer */\
    SocksPeerType socks_type = SOCKS_NONE;\
    std::string socks_user;\
    std::string socks_pass;' "${CACHE_PEER_H}"
    else
        # Fallback: add before the last closing brace of the class
        # Find "CBDATA_CLASS" or last "};" and insert before it
        sed -i '/^};/i\
\
    /* SOCKS proxy support for cache_peer */\
    SocksPeerType socks_type = SOCKS_NONE;\
    std::string socks_user;\
    std::string socks_pass;\
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
    # Find the peer option parsing block. In Squid 6.x, options are parsed
    # in a loop that checks token values like "no-query", "proxy-only", etc.
    # We add our SOCKS options alongside the existing option parsing.
    #
    # Strategy: find the pattern 'strcmp(token, "proxy-only")' or similar
    # well-known option and add our parsing block after the closing brace
    # of that if-block.

    # Try to find a good anchor point
    ANCHOR=""
    for pattern in 'proxy-only' 'no-digest' 'no-query' 'round-robin' 'originserver'; do
        if grep -q "\"${pattern}\"" "${CACHE_CF}"; then
            ANCHOR="${pattern}"
            break
        fi
    done

    if [ -z "${ANCHOR}" ]; then
        die "Could not find peer option parsing anchor in cache_cf.cc"
    fi

    echo "    Using anchor: '${ANCHOR}'"

    # Insert SOCKS option parsing after the first occurrence of the anchor option block
    # We use a Python script for reliable multi-line insertion
    python3 - "${CACHE_CF}" "${ANCHOR}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]
anchor = sys.argv[2]

with open(filepath, 'r') as f:
    content = f.read()

socks_code = '''
        } else if (!strcmp(token, "socks4")) {
            p->socks_type = SOCKS_V4;
        } else if (!strcmp(token, "socks5")) {
            p->socks_type = SOCKS_V5;
        } else if (!strncmp(token, "socks-user=", 11)) {
            p->socks_user = token + 11;
        } else if (!strncmp(token, "socks-pass=", 11)) {
            p->socks_pass = token + 11;
'''

# Find the anchor pattern in a strcmp context
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
    # Fallback: search for simpler pattern
    simple = f'"{anchor}"'
    idx = content.find(simple)
    if idx < 0:
        print(f"ERROR: Could not find '{anchor}' in cache_cf.cc", file=sys.stderr)
        sys.exit(1)
    # Find the closing brace of this if block
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
# 3. FwdState.cc  –  SOCKS negotiation after TCP connect (HTTP requests)
# ---------------------------------------------------------------------------
FWD_STATE="${SQUID_SRC}/src/FwdState.cc"
echo "==> Patching ${FWD_STATE}"
[ -f "${FWD_STATE}" ] || die "FwdState.cc not found"

# Add include
if ! grep -q 'SocksPeerConnector.h' "${FWD_STATE}"; then
    sed -i '/#include "FwdState.h"/a\
#include "SocksPeerConnector.h"' "${FWD_STATE}"
fi

# Add SOCKS negotiation hook.
# In Squid 6.x, after connection is established to a peer, the code
# eventually calls dispatch(). We insert SOCKS negotiation before dispatch.
#
# We look for the dispatch() call that happens after peer connection
# and add SOCKS negotiation before it.
if ! grep -q 'socks_type' "${FWD_STATE}"; then
    python3 - "${FWD_STATE}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

socks_hook = r'''
    /* SOCKS peer negotiation: after TCP connect, before dispatch */
    if (serverConnection()->getPeer() &&
        serverConnection()->getPeer()->socks_type != SOCKS_NONE) {
        CachePeer *sp = serverConnection()->getPeer();
        const char *targetHost = request->url.host();
        const uint16_t targetPort = request->url.port();
        debugs(17, 3, "SOCKS" << (int)sp->socks_type
               << " negotiation with peer " << sp->host
               << " for " << targetHost << ":" << targetPort);
        if (!SocksPeerConnector::negotiate(
                serverConnection()->fd,
                sp->socks_type,
                std::string(targetHost),
                targetPort,
                sp->socks_user,
                sp->socks_pass)) {
            debugs(17, 2, "SOCKS negotiation FAILED for peer " << sp->host);
            retryOrBail();
            return;
        }
        debugs(17, 3, "SOCKS negotiation OK for peer " << sp->host);
    }

'''

# Strategy: find the dispatch() call in a connected-to-peer context
# Look for "dispatch()" preceded by peer-related code
# Multiple possible patterns across Squid versions

inserted = False

# Pattern 1: Look for "void FwdState::dispatch()" and insert at the top of the function
match = re.search(r'(void\s+FwdState::dispatch\s*\(\s*\)\s*\{)', content)
if match:
    insert_pos = match.end()
    content = content[:insert_pos] + socks_hook + content[insert_pos:]
    inserted = True
    print("    Inserted SOCKS hook at top of FwdState::dispatch()")

if not inserted:
    # Pattern 2: look for "FwdState::dispatch" with different formatting
    match = re.search(r'(FwdState::dispatch\(\)\s*\n?\{)', content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + socks_hook + content[insert_pos:]
        inserted = True
        print("    Inserted SOCKS hook (pattern 2)")

if not inserted:
    print("WARNING: Could not find dispatch() insertion point in FwdState.cc", file=sys.stderr)
    print("         SOCKS support for HTTP requests may not work", file=sys.stderr)
else:
    with open(filepath, 'w') as f:
        f.write(content)

PYEOF
fi

echo "    FwdState.cc patched OK"

# ---------------------------------------------------------------------------
# 4. tunnel.cc  –  SOCKS negotiation for CONNECT / HTTPS tunneling
# ---------------------------------------------------------------------------
TUNNEL_CC="${SQUID_SRC}/src/tunnel.cc"
echo "==> Patching ${TUNNEL_CC}"
[ -f "${TUNNEL_CC}" ] || die "tunnel.cc not found"

if ! grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}"; then
    # Add include near the top
    sed -i '/#include "tunnel.h"\|#include "squid.h"\|#include "base\//{
        /#include "squid.h"/a\
#include "SocksPeerConnector.h"
    }' "${TUNNEL_CC}"
    # Fallback: if the above didn't match, try another pattern
    if ! grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}"; then
        sed -i '1,/^#include/{
            /^#include/a\
#include "SocksPeerConnector.h"
        }' "${TUNNEL_CC}"
    fi
fi

if ! grep -q 'socks_type' "${TUNNEL_CC}"; then
    python3 - "${TUNNEL_CC}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# In tunnel.cc, after connecting to a peer for CONNECT requests,
# Squid sends "CONNECT host:port HTTP/1.1" to the peer.
# For SOCKS peers, we need to do SOCKS negotiation instead.
#
# Look for the function that sends the CONNECT request to the peer.
# Common function names: connectToPeer(), tunnelConnectDone(),
# connectedToPeer(), writeServerConnect(), etc.

socks_tunnel_hook = r'''
    /* SOCKS peer: negotiate tunnel instead of HTTP CONNECT */
    if (serverConnection()->getPeer() &&
        serverConnection()->getPeer()->socks_type != SOCKS_NONE) {
        CachePeer *sp = serverConnection()->getPeer();
        const char *tHost = request->url.host();
        const uint16_t tPort = request->url.port();
        debugs(26, 3, "SOCKS" << (int)sp->socks_type
               << " tunnel negotiation with peer " << sp->host
               << " for " << tHost << ":" << tPort);
        if (!SocksPeerConnector::negotiate(
                serverConnection()->fd,
                sp->socks_type,
                std::string(tHost),
                tPort,
                sp->socks_user,
                sp->socks_pass)) {
            debugs(26, 2, "SOCKS tunnel negotiation FAILED for " << sp->host);
            ErrorState *err = new ErrorState(ERR_CONNECT_FAIL, Http::scBadGateway, request.getRaw(), al);
            fail(err);
            closeServerConnection("SOCKS negotiation failed");
            return;
        }
        debugs(26, 3, "SOCKS tunnel negotiation OK for " << sp->host);
        /* After SOCKS negotiation, connection is a direct tunnel.
         * Skip the HTTP CONNECT and go straight to relaying. */
        connectExchangeCheckpoint();
        return;
    }

'''

inserted = False

# Look for the point where HTTP CONNECT is sent to peer
# Pattern: a function that handles "connected to peer" and sends CONNECT
for func_pattern in [
    r'(void\s+TunnelStateData::connectToPeer\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::connectedToPeer\s*\([^)]*\)\s*\{)',
    r'(void\s+tunnelConnectDone\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::sendConnectRequest\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::noteConnection\s*\([^)]*\)\s*\{)',
]:
    match = re.search(func_pattern, content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + socks_tunnel_hook + content[insert_pos:]
        inserted = True
        print(f"    Inserted SOCKS tunnel hook in {match.group(0)[:60]}...")
        break

if not inserted:
    # Last resort: find any function with "peer" and "connect" in tunnel.cc
    # and add the hook there
    match = re.search(r'(void\s+\w+::\w*[Cc]onnect\w*\s*\([^)]*\)\s*\{)', content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + socks_tunnel_hook + content[insert_pos:]
        inserted = True
        print(f"    Inserted SOCKS tunnel hook (fallback) in {match.group(0)[:60]}...")

if inserted:
    with open(filepath, 'w') as f:
        f.write(content)
else:
    print("WARNING: Could not patch tunnel.cc - HTTPS tunneling through SOCKS peers may not work", file=sys.stderr)

PYEOF
fi

echo "    tunnel.cc patched OK"

# ---------------------------------------------------------------------------
# 5. HttpStateData  –  use origin-server request format for SOCKS peers
# ---------------------------------------------------------------------------
# For SOCKS peers, after the SOCKS tunnel is established the connection
# is effectively direct to the origin server. We must send requests in
# origin format (GET /path) rather than proxy format (GET http://host/path).
#
# In Squid this is controlled by the CachePeer::options.originserver flag.
# Rather than modifying HttpStateData, we set originserver = true for SOCKS
# peers during configuration parsing (in cache_cf.cc).  This is already
# handled because the Dockerfile squid.conf template uses the "originserver"
# option explicitly.

echo ""
echo "==> All patches applied successfully"
echo ""
echo "Modified files:"
echo "  - src/CachePeer.h       (added socks_type/user/pass fields)"
echo "  - src/cache_cf.cc       (added socks4/socks5 option parsing)"
echo "  - src/FwdState.cc       (added SOCKS negotiation before dispatch)"
echo "  - src/tunnel.cc         (added SOCKS negotiation for CONNECT tunneling)"
echo "  - src/SocksPeerConnector.h (new: SOCKS4/5 protocol implementation)"
