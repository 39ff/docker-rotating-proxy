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
# 1b. CachePeer.cc  –  free socks_user / socks_pass in destructor
# ---------------------------------------------------------------------------
CACHE_PEER_CC="${SQUID_SRC}/src/CachePeer.cc"
echo "==> Patching ${CACHE_PEER_CC}"
[ -f "${CACHE_PEER_CC}" ] || die "CachePeer.cc not found"

if ! grep -q 'socks_user' "${CACHE_PEER_CC}"; then
    # Insert xfree calls next to existing xfree(login) in the destructor
    sed -i '/xfree(login);/a\
\
    xfree(socks_user);\
    xfree(socks_pass);' "${CACHE_PEER_CC}"
    grep -q 'socks_user' "${CACHE_PEER_CC}" || die "Failed to patch CachePeer.cc destructor"
fi

echo "    CachePeer.cc patched OK"

# ---------------------------------------------------------------------------
# 1c. comm/Connection.h  –  add socksNegotiated flag (SOCKS anti-reuse guard)
# ---------------------------------------------------------------------------
# A SOCKS-negotiated cache_peer connection is a raw tunnel bound to ONE target
# (request->url.host():port).  The persistent-connection pool is keyed by peer,
# NOT target, so a pooled SOCKS connection could otherwise be reused for a
# different target (silent mis-routing) or receive a second SOCKS greeting
# injected into a live stream.  This per-connection flag lets the FwdState /
# tunnel hooks detect and refuse such reuse.
CONNECTION_H="${SQUID_SRC}/src/comm/Connection.h"
echo "==> Patching ${CONNECTION_H}"
[ -f "${CONNECTION_H}" ] || die "comm/Connection.h not found"

if ! grep -q 'socksNegotiated' "${CONNECTION_H}"; then
    python3 - "${CONNECTION_H}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]
with open(filepath, 'r') as f:
    content = f.read()

# Locate the *definition* of class Connection (skip forward declarations like
# "class Connection;").  A definition has an opening "{" before any ";".
match = None
brace_start = -1
for mm in re.finditer(r'class\s+Connection\b', content):
    rest = content[mm.end():]
    brace = rest.find('{')
    semi = rest.find(';')
    if brace != -1 and (semi == -1 or brace < semi):
        match = mm
        brace_start = mm.end() + brace
        break

if match is None:
    print("ERROR: class Connection definition not found in Connection.h", file=sys.stderr)
    sys.exit(1)

# Brace-match to find the closing "}" of the class body.
depth = 1
pos = brace_start + 1
while pos < len(content) and depth > 0:
    if content[pos] == '{': depth += 1
    elif content[pos] == '}': depth -= 1
    pos += 1

# pos is right after the closing "}"; insert the field just before it.
field = ('\npublic:\n'
         '    /* SOCKS peer support: true once SocksPeerConnector has\n'
         '     * negotiated a tunnel on this fd.  Prevents a pooled SOCKS\n'
         '     * tunnel (bound to one target) from being reused for another. */\n'
         '    bool socksNegotiated = false;\n')
insert_at = pos - 1
content = content[:insert_at] + field + content[insert_at:]

with open(filepath, 'w') as f:
    f.write(content)
print("    Inserted socksNegotiated flag into class Connection")
PYEOF
    grep -q 'socksNegotiated' "${CONNECTION_H}" || die "Failed to add socksNegotiated to Connection.h"
fi

echo "    comm/Connection.h patched OK"

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

# The code to insert. Starts with " else if" (no leading "}") and closes
# the final branch with "}".  The insertion point is right after the "}"
# that closes the anchor's if-block, so " else if" continues the chain.
socks_code = ''' else if (!strcmp(token, "socks4")) {
            p->socks_type = 4;
        } else if (!strcmp(token, "socks5")) {
            p->socks_type = 5;
        } else if (!strncmp(token, "socks-user=", 11)) {
            safe_free(p->socks_user);
            p->socks_user = xstrdup(token + 11);
        } else if (!strncmp(token, "socks-pass=", 11)) {
            safe_free(p->socks_pass);
            p->socks_pass = xstrdup(token + 11);
        }'''

# Find the anchor in a strcmp/strncmp context
# Try matching "else if" variant first (most options), then plain "if" (first option)
for pat_template in [
    r'else\s+if\s*\(!(?:strcmp|strncmp)\(token,\s*"' + re.escape(anchor) + r'"',
    r'if\s*\(!(?:strcmp|strncmp)\(token,\s*"' + re.escape(anchor) + r'"',
]:
    pat = re.compile(pat_template)
    match = pat.search(content)
    if match:
        break

if not match:
    print(f"ERROR: Could not find '{anchor}' in cache_cf.cc", file=sys.stderr)
    sys.exit(1)

# From the match position, find the opening brace and count to the closing brace
idx = match.start()
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
# pos is now right after the closing "}" of the anchor block
content = content[:pos] + socks_code + content[pos:]

# Also add a post-parse validation: socks4/socks5 requires originserver.
# Options can appear in any order, so we validate after the while loop ends.
# findCachePeerByName is the first check after the option-parsing loop.
validation = '''
    /* Validate: SOCKS peers must use originserver */
    if (p->socks_type && !p->options.originserver)
        throw TextException(ToSBuf("cache_peer ", *p, ": socks4/socks5 requires the originserver option"), Here());

    /* Validate: socks-user/socks-pass only valid with socks5 and must be set together */
    if (p->socks_type != 5 && (p->socks_user || p->socks_pass))
        throw TextException(ToSBuf("cache_peer ", *p, ": socks-user/socks-pass options require socks5"), Here());
    if (p->socks_type == 5 && ((!p->socks_user) != (!p->socks_pass)))
        throw TextException(ToSBuf("cache_peer ", *p, ": socks-user and socks-pass must both be set or both omitted"), Here());

'''
marker = 'findCachePeerByName'
marker_idx = content.find(marker, pos)
if marker_idx > pos:
    line_start = content.rfind('\n', 0, marker_idx)
    if line_start > 0:
        content = content[:line_start] + validation + content[line_start:]
        print("    Inserted SOCKS+originserver validation after option parsing loop")
else:
    print("ERROR: Could not insert originserver validation", file=sys.stderr)
    sys.exit(1)

with open(filepath, 'w') as f:
    f.write(content)
print(f"    Inserted SOCKS parsing after '{anchor}' block")
PYEOF
fi

echo "    cache_cf.cc patched OK"

# ---------------------------------------------------------------------------
# 3. FwdState.cc  –  SOCKS negotiation at the top of dispatch()
# ---------------------------------------------------------------------------
FWD_STATE="${SQUID_SRC}/src/FwdState.cc"
echo "==> Patching ${FWD_STATE}"
[ -f "${FWD_STATE}" ] || die "FwdState.cc not found"

# Add include AFTER squid.h (squid.h MUST be the first include in every .cc)
if ! grep -q 'SocksPeerConnector.h' "${FWD_STATE}"; then
    sed -i '/#include "squid.h"/a\
#include "SocksPeerConnector.h"' "${FWD_STATE}"
    grep -q 'SocksPeerConnector.h' "${FWD_STATE}" || die "Failed to add include to FwdState.cc"
fi

if ! grep -q 'socks_type' "${FWD_STATE}"; then
    python3 - "${FWD_STATE}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# Squid 6.10 API:
#   serverConnection() returns Comm::ConnectionPointer const &
#   ->getPeer() returns CachePeer*
#   ->fd is int (public member of Comm::Connection)
#   request->url.host() returns const char*
#   request->url.port() returns unsigned short
#   retryOrBail() is a private method of FwdState
socks_hook = r'''
    /* SOCKS peer negotiation: after TCP connect, before HTTP dispatch */
    if (const auto sp = serverConnection()->getPeer()) {
        if (sp->socks_type) {
            /* Anti-reuse guard: a connection that has already been
             * SOCKS-negotiated is a pooled tunnel bound to a *previous*
             * target.  Re-negotiating would inject a second greeting into a
             * live stream, and using it as-is would silently mis-route this
             * request.  Drop it and let FwdState retry on a fresh fd. */
            if (serverConnection()->socksNegotiated) {
                /* A reused, already-negotiated pconn is a tunnel bound to a
                 * previous target.  Tear it down before retrying: noteConnection()
                 * has already set destinationReceipt and syncWithServerConn() has
                 * installed serverConn/closeHandler, so a bare retryOrBail() would
                 * re-enter noteConnection() with destinationReceipt still set and
                 * trip assert(!destinationReceipt).  Mirror serverClosed(). */
                debugs(17, 2, "SOCKS: dropping reused negotiated connection to "
                       << sp->host << "; retrying on a fresh fd");
                closeServerConnection("reused SOCKS tunnel cannot serve a new target");
                serverConn = nullptr;
                destinationReceipt = nullptr;
                retryOrBail();
                return;
            }

            /* The SOCKS tunnel is bound to (request->url.host():port).
             * The pconn pool is keyed by peer address, NOT target, so a
             * pooled SOCKS-negotiated connection would silently route the
             * next request to the WRONG destination.  Force the upstream
             * connection to close after this request to keep one tunnel
             * per target, and to guarantee the next dispatch() runs on a
             * freshly-connected fd that has not been SOCKS-negotiated yet. */
            request->flags.proxyKeepalive = false;

            const auto targetPort = static_cast<uint16_t>(request->url.port());
            debugs(17, 3, "SOCKS" << sp->socks_type
                   << " negotiation with peer " << sp->host
                   << " for " << request->url.host() << ":" << targetPort);
            if (!SocksPeerConnector::negotiate(
                    serverConnection()->fd,
                    static_cast<SocksPeerType>(sp->socks_type),
                    std::string(request->url.host()),
                    targetPort,
                    sp->socks_user ? std::string(sp->socks_user) : std::string(),
                    sp->socks_pass ? std::string(sp->socks_pass) : std::string())) {
                debugs(17, 2, "SOCKS negotiation FAILED for peer " << sp->host);
                closeServerConnection("SOCKS negotiation failed");
                serverConn = nullptr;
                destinationReceipt = nullptr;
                retryOrBail();
                return;
            }
            serverConnection()->socksNegotiated = true;
            debugs(17, 3, "SOCKS negotiation OK for peer " << sp->host);
        }
    }

'''

inserted = False

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

# Add include AFTER squid.h
if ! grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}"; then
    sed -i '/#include "squid.h"/a\
#include "SocksPeerConnector.h"' "${TUNNEL_CC}"
    grep -q 'SocksPeerConnector.h' "${TUNNEL_CC}" || die "Failed to add include to tunnel.cc"
fi

if ! grep -q 'socks_type' "${TUNNEL_CC}"; then
    python3 - "${TUNNEL_CC}" << 'PYEOF'
import sys, re

filepath = sys.argv[1]

with open(filepath, 'r') as f:
    content = f.read()

# tunnel.cc API (Squid 6.10):
#   TunnelStateData has: server.conn, request (HttpRequestPointer)
#   connectDone(const Comm::ConnectionPointer &conn, ...) - after TCP connect
#   conn->getPeer() returns CachePeer*
#   conn->fd is int
#   request->url.host() returns const char*
socks_tunnel_hook = r'''
    /* SOCKS peer: negotiate tunnel right after TCP connect */
    if (conn->getPeer() && conn->getPeer()->socks_type) {
        const auto sp = conn->getPeer();
        /* Anti-reuse guard: never re-negotiate (or reuse) a connection that
         * already carries a SOCKS tunnel to a previous target. */
        if (conn->socksNegotiated) {
            /* Reused tunnel connection already bound to a previous target:
             * close the pending conn (server.conn is still nil here) before
             * retrying, matching tunnel.cc's other error paths. */
            debugs(26, 2, "SOCKS: dropping reused negotiated tunnel connection to "
                   << sp->host << "; retrying");
            closePendingConnection(conn, "reused SOCKS tunnel cannot serve a new target");
            saveError(new ErrorState(ERR_CONNECT_FAIL, Http::scBadGateway, request.getRaw(), al));
            retryOrBail("SOCKS tunnel reuse");
            return;
        }
        /* Same rationale as FwdState::dispatch(): the SOCKS tunnel is
         * bound to one target host, so prevent this connection from being
         * returned to the pconn pool where another request could pick it
         * up and silently send data into the previous target's tunnel. */
        request->flags.proxyKeepalive = false;
        const auto targetPort = static_cast<uint16_t>(request->url.port());
        debugs(26, 3, "SOCKS" << sp->socks_type
               << " tunnel negotiation with peer " << sp->host
               << " for " << request->url.host() << ":" << targetPort);
        if (!SocksPeerConnector::negotiate(
                conn->fd,
                static_cast<SocksPeerType>(sp->socks_type),
                std::string(request->url.host()),
                targetPort,
                sp->socks_user ? std::string(sp->socks_user) : std::string(),
                sp->socks_pass ? std::string(sp->socks_pass) : std::string())) {
            debugs(26, 2, "SOCKS tunnel negotiation FAILED for " << sp->host);
            closePendingConnection(conn, "SOCKS negotiation failed");
            saveError(new ErrorState(ERR_CONNECT_FAIL, Http::scBadGateway, request.getRaw(), al));
            retryOrBail("SOCKS negotiation failed");
            return;
        }
        conn->socksNegotiated = true;
        debugs(26, 3, "SOCKS tunnel negotiation OK for " << sp->host);
    }

'''

inserted = False

for pat in [
    r'(void\s+TunnelStateData::connectDone\s*\([^)]*\)\s*\{)',
    r'(TunnelStateData::connectDone\s*\([^)]*\)\s*\n?\s*\{)',
    r'(void\s+tunnelConnectDone\s*\([^)]*\)\s*\{)',
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
echo "  - src/CachePeer.cc           (added socks_user/pass cleanup in destructor)"
echo "  - src/comm/Connection.h      (added socksNegotiated anti-reuse flag)"
echo "  - src/cache_cf.cc            (added socks4/socks5 option parsing)"
echo "  - src/FwdState.cc            (SOCKS negotiation in dispatch())"
echo "  - src/tunnel.cc              (SOCKS negotiation in connectDone())"
echo "  - src/SocksPeerConnector.h   (new: SOCKS4/5 protocol implementation)"
