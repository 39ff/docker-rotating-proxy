#!/bin/sh
set -e

SQUID_CONFIG_FILE="${SQUID_CONFIG_FILE:-/etc/squid/squid.conf}"

# Initialize cache directory if needed
if [ ! -d /var/cache/squid/00 ]; then
    echo "Initializing Squid cache..."
    squid -z -N -f "${SQUID_CONFIG_FILE}" 2>&1 || echo "Warning: squid -z failed (may be OK if cache is unused)"
fi

# Ensure proper ownership
chown -R squid:squid /var/cache/squid /var/log/squid /var/run/squid 2>/dev/null || true

# Remove stale PID file left by squid -z (created as root)
rm -f /var/run/squid.pid /var/run/squid/squid.pid 2>/dev/null || true

# Support both direct squid invocation and arbitrary commands.
# If the first argument is "squid", drop it to avoid "squid squid ..." duplication.
if [ "$#" -gt 0 ] && [ "$1" = "squid" ]; then
    shift
fi

# If there are no arguments, or the first arg starts with "-", treat them as squid options.
if [ "$#" -eq 0 ] || [ "${1#-}" != "$1" ]; then
    echo "Starting Squid with config: ${SQUID_CONFIG_FILE}"
    exec gosu squid squid -N -f "${SQUID_CONFIG_FILE}" "$@"
fi

# Otherwise, run the provided command as-is (e.g., a shell or another tool).
exec "$@"
