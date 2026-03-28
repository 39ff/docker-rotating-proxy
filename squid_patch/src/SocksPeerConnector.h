/*
 * SocksPeerConnector.h - SOCKS4/SOCKS5 negotiation for Squid cache_peer
 *
 * Performs synchronous SOCKS handshake on an established TCP connection.
 * After negotiation, the connection acts as a direct tunnel to the target.
 *
 * Reference: https://wiki.squid-cache.org/Features/Socks
 * SOCKS4:  RFC 1928 predecessor (de facto standard)
 * SOCKS4a: Extension for hostname resolution by proxy
 * SOCKS5:  RFC 1928 + RFC 1929 (username/password auth)
 */

#ifndef SQUID_SRC_SOCKS_PEER_CONNECTOR_H
#define SQUID_SRC_SOCKS_PEER_CONNECTOR_H

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <fcntl.h>
#include <unistd.h>
#include <cstring>
#include <cerrno>
#include <string>

enum SocksPeerType {
    SOCKS_NONE = 0,
    SOCKS_V4 = 4,
    SOCKS_V5 = 5
};

namespace SocksPeerConnector {

/* ---- low-level helpers ------------------------------------------------ */

static inline bool syncSend(int fd, const void *buf, size_t len)
{
    const char *p = static_cast<const char *>(buf);
    size_t sent = 0;
    while (sent < len) {
        ssize_t n = ::send(fd, p + sent, len - sent, MSG_NOSIGNAL);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        sent += static_cast<size_t>(n);
    }
    return true;
}

static inline bool syncRecv(int fd, void *buf, size_t len)
{
    char *p = static_cast<char *>(buf);
    size_t got = 0;
    while (got < len) {
        ssize_t n = ::recv(fd, p + got, len - got, 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (n == 0) return false;
        got += static_cast<size_t>(n);
    }
    return true;
}

/* ---- SOCKS4 / SOCKS4a ------------------------------------------------ */

static inline bool socks4Connect(int fd,
                                  const std::string &host, uint16_t port,
                                  const std::string &user)
{
    struct in_addr addr;
    bool useSocks4a = (inet_pton(AF_INET, host.c_str(), &addr) != 1);

    if (useSocks4a) {
        /* SOCKS4a: set IP to 0.0.0.x (x != 0) and append hostname */
        addr.s_addr = htonl(0x00000001);
    }

    /* Bounds check: 8 (header) + userid + 1 (null) + hostname + 1 (null) */
    const size_t needed = 8 + user.size() + 1 + (useSocks4a ? host.size() + 1 : 0);
    uint8_t req[600];
    if (needed > sizeof(req))
        return false;

    size_t pos = 0;

    req[pos++] = 0x04;                          /* VN  = 4             */
    req[pos++] = 0x01;                          /* CD  = CONNECT       */
    req[pos++] = static_cast<uint8_t>((port >> 8) & 0xFF);
    req[pos++] = static_cast<uint8_t>(port & 0xFF);
    std::memcpy(req + pos, &addr.s_addr, 4);    /* DSTIP               */
    pos += 4;

    /* USERID */
    if (!user.empty()) {
        std::memcpy(req + pos, user.c_str(), user.length());
        pos += user.length();
    }
    req[pos++] = 0x00;                          /* NULL terminator     */

    /* SOCKS4a hostname */
    if (useSocks4a) {
        std::memcpy(req + pos, host.c_str(), host.length());
        pos += host.length();
        req[pos++] = 0x00;
    }

    if (!syncSend(fd, req, pos))
        return false;

    uint8_t resp[8];
    if (!syncRecv(fd, resp, 8))
        return false;

    return (resp[1] == 0x5A);                   /* 0x5A = granted      */
}

/* ---- SOCKS5  (RFC 1928 + RFC 1929) ----------------------------------- */

static inline bool socks5Connect(int fd,
                                  const std::string &host, uint16_t port,
                                  const std::string &user,
                                  const std::string &pass)
{
    const bool hasAuth = (!user.empty() && !pass.empty());

    /* --- greeting ---------------------------------------------------- */
    uint8_t greeting[4];
    size_t gLen;
    if (hasAuth) {
        greeting[0] = 0x05;   /* VER                     */
        greeting[1] = 0x02;   /* NMETHODS                */
        greeting[2] = 0x00;   /* NO AUTHENTICATION       */
        greeting[3] = 0x02;   /* USERNAME / PASSWORD      */
        gLen = 4;
    } else {
        greeting[0] = 0x05;
        greeting[1] = 0x01;
        greeting[2] = 0x00;
        gLen = 3;
    }

    if (!syncSend(fd, greeting, gLen))
        return false;

    uint8_t gResp[2];
    if (!syncRecv(fd, gResp, 2))
        return false;

    if (gResp[0] != 0x05)
        return false;

    /* --- authentication (RFC 1929) ----------------------------------- */
    if (gResp[1] == 0x02) {
        if (!hasAuth)
            return false;

        /* RFC 1929: username and password are each max 255 bytes */
        if (user.length() > 255 || pass.length() > 255)
            return false;

        uint8_t auth[515];
        size_t aPos = 0;
        auth[aPos++] = 0x01;  /* sub-negotiation VER */
        auth[aPos++] = static_cast<uint8_t>(user.length());
        std::memcpy(auth + aPos, user.c_str(), user.length());
        aPos += user.length();
        auth[aPos++] = static_cast<uint8_t>(pass.length());
        std::memcpy(auth + aPos, pass.c_str(), pass.length());
        aPos += pass.length();

        if (!syncSend(fd, auth, aPos))
            return false;

        uint8_t aResp[2];
        if (!syncRecv(fd, aResp, 2))
            return false;

        if (aResp[0] != 0x01 || aResp[1] != 0x00)
            return false;   /* auth failed or wrong sub-negotiation version */

    } else if (gResp[1] == 0x00) {
        /* no auth required */
    } else {
        return false;       /* unsupported or unacceptable method (includes 0xFF) */
    }

    /* --- connect request --------------------------------------------- */
    uint8_t connReq[263];
    size_t cPos = 0;

    connReq[cPos++] = 0x05;   /* VER                  */
    connReq[cPos++] = 0x01;   /* CMD = CONNECT        */
    connReq[cPos++] = 0x00;   /* RSV                  */

    /* Detect address type: IPv4, IPv6, or domain name */
    struct in_addr ipv4;
    struct in6_addr ipv6;
    if (inet_pton(AF_INET, host.c_str(), &ipv4) == 1) {
        connReq[cPos++] = 0x01;   /* ATYP = IPv4 */
        std::memcpy(connReq + cPos, &ipv4, sizeof(ipv4));
        cPos += sizeof(ipv4);
    } else if (inet_pton(AF_INET6, host.c_str(), &ipv6) == 1) {
        connReq[cPos++] = 0x04;   /* ATYP = IPv6 */
        std::memcpy(connReq + cPos, &ipv6, sizeof(ipv6));
        cPos += sizeof(ipv6);
    } else {
        if (host.length() > 255)
            return false;
        connReq[cPos++] = 0x03;   /* ATYP = DOMAINNAME    */
        connReq[cPos++] = static_cast<uint8_t>(host.length());
        std::memcpy(connReq + cPos, host.c_str(), host.length());
        cPos += host.length();
    }

    connReq[cPos++] = static_cast<uint8_t>((port >> 8) & 0xFF);
    connReq[cPos++] = static_cast<uint8_t>(port & 0xFF);

    if (!syncSend(fd, connReq, cPos))
        return false;

    /* --- connect response -------------------------------------------- */
    uint8_t cResp[4];
    if (!syncRecv(fd, cResp, 4))
        return false;

    if (cResp[0] != 0x05 || cResp[1] != 0x00)
        return false;   /* connection failed */

    /* drain the BND.ADDR + BND.PORT */
    switch (cResp[3]) {
    case 0x01: {            /* IPv4  */
        uint8_t skip[6];    /* 4 addr + 2 port */
        if (!syncRecv(fd, skip, 6)) return false;
        break;
    }
    case 0x03: {            /* DOMAINNAME */
        uint8_t dLen;
        if (!syncRecv(fd, &dLen, 1)) return false;
        uint8_t skip[258];
        if (!syncRecv(fd, skip, dLen + 2)) return false;
        break;
    }
    case 0x04: {            /* IPv6 */
        uint8_t skip[18];   /* 16 addr + 2 port */
        if (!syncRecv(fd, skip, 18)) return false;
        break;
    }
    default:
        return false;
    }

    return true;
}

/* ---- public entry point ---------------------------------------------- */

/**
 * Perform SOCKS negotiation on an established TCP connection.
 *
 * Temporarily switches the socket to blocking mode, performs the
 * SOCKS handshake (with a 10-second timeout), and restores the
 * original socket flags.
 *
 * @return true on success; the fd is then a tunnel to targetHost:targetPort
 */
static inline bool negotiate(int fd, SocksPeerType type,
                              const std::string &targetHost,
                              uint16_t targetPort,
                              const std::string &user = "",
                              const std::string &pass = "")
{
    if (type == SOCKS_NONE)
        return true;

    /* save original flags */
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0)
        return false;

    /* switch to blocking for the handshake */
    if (fcntl(fd, F_SETFL, flags & ~O_NONBLOCK) < 0)
        return false;

    /* save original timeouts and set a 10 s limit for the handshake */
    struct timeval origRecvTv = {0, 0}, origSendTv = {0, 0};
    socklen_t tvLen = sizeof(struct timeval);
    if (getsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &origRecvTv, &tvLen) < 0 ||
        getsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &origSendTv, &tvLen) < 0) {
        fcntl(fd, F_SETFL, flags);
        return false;
    }

    struct timeval tv;
    tv.tv_sec  = 10;
    tv.tv_usec = 0;
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv)) < 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) < 0) {
        fcntl(fd, F_SETFL, flags);
        return false;
    }

    bool ok = false;
    if (type == SOCKS_V4)
        ok = socks4Connect(fd, targetHost, targetPort, user);
    else if (type == SOCKS_V5)
        ok = socks5Connect(fd, targetHost, targetPort, user, pass);

    /* restore original flags and timeouts (best-effort, log-worthy but not fatal) */
    int restoreOk = 0;
    restoreOk |= fcntl(fd, F_SETFL, flags);
    restoreOk |= setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &origRecvTv, sizeof(origRecvTv));
    restoreOk |= setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &origSendTv, sizeof(origSendTv));
    if (restoreOk < 0 && ok)
        return false;   /* negotiation succeeded but socket is in bad state */

    return ok;
}

}  /* namespace SocksPeerConnector */

#endif /* SQUID_SRC_SOCKS_PEER_CONNECTOR_H */
