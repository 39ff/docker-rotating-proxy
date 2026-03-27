# ==========================================================================
# Custom Squid build with SOCKS4/SOCKS5 cache_peer support
#
# Pinned version: Squid 6.10
# Reference: https://wiki.squid-cache.org/Features/Socks
# ==========================================================================

ARG SQUID_VERSION=6.10

# ---------- stage 1: build ------------------------------------------------
FROM debian:bookworm-slim AS builder

ARG SQUID_VERSION
ENV SQUID_VERSION=${SQUID_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        wget \
        ca-certificates \
        pkg-config \
        autoconf \
        automake \
        libtool \
        libssl-dev \
        libcap-dev \
        libexpat1-dev \
        libltdl-dev \
        python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Download and extract Squid source (pinned version)
RUN wget -q "https://www.squid-cache.org/Versions/v6/squid-${SQUID_VERSION}.tar.xz" \
        -O squid.tar.xz \
    && tar xf squid.tar.xz \
    && rm squid.tar.xz

# Copy patch sources and apply
COPY src/    /patches/src/
COPY patch_apply.sh /patches/

RUN chmod +x /patches/patch_apply.sh \
    && bash /patches/patch_apply.sh /patches/src "/build/squid-${SQUID_VERSION}"

# Configure and build
RUN cd "squid-${SQUID_VERSION}" \
    && ./configure \
        --prefix=/usr \
        --sysconfdir=/etc/squid \
        --localstatedir=/var \
        --libexecdir=/usr/lib/squid \
        --datadir=/usr/share/squid \
        --with-openssl \
        --enable-ssl-crtd \
        --enable-delay-pools \
        --enable-removal-policies="lru,heap" \
        --enable-cache-digests \
        --enable-follow-x-forwarded-for \
        --disable-arch-native \
        --with-large-files \
        --with-default-user=squid \
        --disable-strict-error-checking \
    && make -j"$(nproc)" \
    && make install DESTDIR=/install

# ---------- stage 2: runtime ----------------------------------------------
FROM debian:bookworm-slim

ARG SQUID_VERSION=6.10
LABEL maintainer="docker-rotating-proxy"
LABEL description="Squid ${SQUID_VERSION} with SOCKS4/SOCKS5 cache_peer support"

RUN apt-get update && apt-get install -y --no-install-recommends \
        libssl3 \
        libcap2 \
        libexpat1 \
        libltdl7 \
        ca-certificates \
        curl \
        gosu \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install/ /

RUN useradd -r -M -d /var/cache/squid -s /sbin/nologin squid \
    && mkdir -p /var/cache/squid /var/log/squid /var/run/squid /etc/squid/conf.d \
    && chown -R squid:squid /var/cache/squid /var/log/squid /var/run/squid /etc/squid/conf.d

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 3128

ENTRYPOINT ["docker-entrypoint.sh"]
