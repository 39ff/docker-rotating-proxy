#!/bin/sh
# One-line installer: docker-rotating-proxy plus its web UI on a Linux server.
#
#   curl -fsSL https://raw.githubusercontent.com/39ff/docker-rotating-proxy/master/install.sh | sudo sh
#
# Installs Docker when it is missing (get.docker.com), clones this repository
# to /opt/docker-rotating-proxy, starts the web UI (manager/compose.yml) and
# prints the one-time setup link. Running it again updates the checkout
# (git pull --ff-only) and restarts the UI; your settings are kept.
#
# Settings (environment variables):
#   RPX_DIR          install directory              (default /opt/docker-rotating-proxy)
#   RPX_REPO         git repository                 (default https://github.com/39ff/docker-rotating-proxy.git)
#   RPX_REF          branch or tag                  (default master)
#   RPX_PORT         web UI port                    (default 8080)
#   RPX_BIND         address the web UI listens on  (default 0.0.0.0; 127.0.0.1 = SSH tunnel only)
#   RPX_PUBLIC_HOST  host/IP shown in the examples  (default: detected public IP)
#   RPX_SETUP_TOKEN  choose the setup token yourself, e.g. in cloud-init user-data
#   RPX_NO_DOCKER_INSTALL=1  fail instead of installing Docker
set -eu

RPX_DIR="${RPX_DIR:-/opt/docker-rotating-proxy}"
RPX_REPO="${RPX_REPO:-https://github.com/39ff/docker-rotating-proxy.git}"
RPX_REF="${RPX_REF:-master}"

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root, e.g.: curl -fsSL <url> | sudo sh"
case "${RPX_PORT:-8080}" in *[!0-9]*) die "RPX_PORT must be a number" ;; esac

install_pkg() { # install_pkg NAME
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$1"
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q "$1"
  elif command -v yum >/dev/null 2>&1; then yum install -y -q "$1"
  elif command -v apk >/dev/null 2>&1; then apk add --no-cache "$1"
  else return 1
  fi
}

# --- Docker -----------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  [ "${RPX_NO_DOCKER_INSTALL:-}" = 1 ] && die "docker is not installed (RPX_NO_DOCKER_INSTALL=1)"
  command -v curl >/dev/null 2>&1 || install_pkg curl || die "curl is needed to install Docker"
  say "Installing Docker (https://get.docker.com)"
  curl -fsSL https://get.docker.com | sh
fi
docker info >/dev/null 2>&1 || {
  command -v systemctl >/dev/null 2>&1 && systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running"
}
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing (package docker-compose-plugin)"

# --- Checkout ---------------------------------------------------------------
command -v git >/dev/null 2>&1 || { say "Installing git"; install_pkg git || die "git is needed"; }
if [ -d "$RPX_DIR/.git" ]; then
  say "Updating $RPX_DIR"
  if ! git -C "$RPX_DIR" pull --ff-only --quiet; then
    warn "git pull failed (local changes in tracked files?); keeping the current version"
  fi
elif [ -e "$RPX_DIR" ] && [ -n "$(ls -A "$RPX_DIR" 2>/dev/null)" ]; then
  die "$RPX_DIR exists and is not a git checkout; set RPX_DIR to another directory"
else
  say "Cloning $RPX_REPO ($RPX_REF) to $RPX_DIR"
  git clone --quiet --depth 1 --branch "$RPX_REF" "$RPX_REPO" "$RPX_DIR"
fi
# Upstream credentials end up in this directory.
chmod 700 "$RPX_DIR"

# Settings: environment variable, else the value from an earlier install
# (manager/.env), else the default. Re-running without RPX_BIND must not turn
# an SSH-only UI into a public one.
envf="$RPX_DIR/manager/.env"
old_env() { if [ -f "$envf" ]; then sed -n "s/^$1=//p" "$envf" | tail -n 1; fi; }
RPX_BIND="${RPX_BIND:-$(old_env RPX_BIND)}"
RPX_BIND="${RPX_BIND:-0.0.0.0}"
RPX_PORT="${RPX_PORT:-$(old_env RPX_PORT)}"
RPX_PORT="${RPX_PORT:-8080}"
RPX_PUBLIC_HOST="${RPX_PUBLIC_HOST:-$(old_env RPX_PUBLIC_HOST)}"
case "$RPX_PORT" in *[!0-9]*) die "RPX_PORT must be a number" ;; esac

# --- Setup token ------------------------------------------------------------
state="$RPX_DIR/.manager"
mkdir -p "$state"
chmod 700 "$state"
token=""
if [ ! -f "$state/auth.json" ]; then
  if [ -n "${RPX_SETUP_TOKEN:-}" ]; then
    token="$RPX_SETUP_TOKEN"
  elif [ -s "$state/setup_token" ]; then
    token="$(cat "$state/setup_token")"
  else
    token="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  fi
  (umask 077 && printf '%s\n' "$token" > "$state/setup_token")
fi

# --- Public address for the examples ----------------------------------------
public_host="${RPX_PUBLIC_HOST:-}"
if [ -z "$public_host" ]; then
  public_host="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  case "$public_host" in *[!0-9A-Fa-f:.]*|'') public_host="$(hostname -I 2>/dev/null | awk '{print $1}')" ;; esac
fi

# Other settings in manager/.env (RPX_TRUST_FORWARDED, RPX_PROBE_URL, ...) are kept.
extra=""
if [ -f "$envf" ]; then
  extra="$(grep -vE '^(#|$|RPX_ROOT=|RPX_BIND=|RPX_PORT=|RPX_PUBLIC_HOST=)' "$envf" || true)"
fi
{
  echo "# Written by install.sh; read by docker compose -f manager/compose.yml"
  echo "RPX_ROOT=$RPX_DIR"
  echo "RPX_BIND=$RPX_BIND"
  echo "RPX_PORT=$RPX_PORT"
  echo "RPX_PUBLIC_HOST=$public_host"
  if [ -n "$extra" ]; then printf '%s\n' "$extra"; fi
} > "$envf"

# --- Start the web UI -------------------------------------------------------
say "Starting the web UI"
docker compose -f "$RPX_DIR/manager/compose.yml" up -d --build --quiet-pull

check_host="$RPX_BIND"
case "$check_host" in 0.0.0.0|::|'') check_host=127.0.0.1 ;; esac
case "$check_host" in *:*) check_host="[$check_host]" ;; esac
i=0
until curl -fsS --max-time 2 "http://$check_host:$RPX_PORT/healthz" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -ge 60 ] && die "the web UI did not start; see: docker logs rotating_proxy_manager"
  sleep 1
done

# The generated compose file asks for nofile=65536; some hosts cap it lower.
if ! docker run --rm --ulimit nofile=65536:65536 rotating-proxy-manager:local true >/dev/null 2>&1; then
  warn "this host does not allow nofile=65536 for containers; Squid will not start."
  warn "raise LimitNOFILE for docker.service, or remove 'ulimits' via docker-compose.override.yml"
fi

# --- Done -------------------------------------------------------------------
shown="$public_host"
case "$shown" in *:*) shown="[$shown]" ;; esac
echo
say "docker-rotating-proxy is installed in $RPX_DIR"
path="/"
if [ -n "$token" ]; then path="/setup?token=$token"; fi
if [ "$RPX_BIND" = 127.0.0.1 ] || [ "$RPX_BIND" = ::1 ]; then
  echo "The web UI only listens on this server. From your computer, run:"
  echo "  ssh -L $RPX_PORT:127.0.0.1:$RPX_PORT <user>@${shown:-<server-ip>}"
  echo "then open:"
  echo "  http://127.0.0.1:$RPX_PORT$path"
elif [ -n "$token" ]; then
  echo "Open this link to finish the setup (the token works once):"
  echo "  http://${shown:-<server-ip>}:$RPX_PORT$path"
else
  echo "Web UI: http://${shown:-<server-ip>}:$RPX_PORT/ (already set up)"
fi
echo
echo "Open these ports in your firewall / security group:"
echo "  $RPX_PORT/tcp       web UI (best: only from your own address)"
echo "  3128/tcp       rotating proxy"
echo "  30000+/tcp     static ports, one per socks5/http/https upstream or VPN"
if [ "$RPX_BIND" != 127.0.0.1 ] && [ "$RPX_BIND" != ::1 ]; then
  echo
  warn "the web UI uses plain HTTP. For HTTPS or SSH-only access see docs/MANAGER.md."
fi
