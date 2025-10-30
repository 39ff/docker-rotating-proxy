# Docker Rotating Proxy

A flexible rotating proxy system powered by Docker, Squid, and Gost. Automatically rotates through multiple proxy sources for web scraping and privacy.

## Features

- **Multiple Proxy Types**: HTTP, HTTPS, SOCKS5, and OpenVPN
- **Flexible Format**: Customize proxy list column order and delimiters
- **Auto Rotation**: Squid automatically rotates proxies per request
- **Static IPs**: Direct port access for session persistence
- **Easy Configuration**: Simple text file configuration
- **Docker-based**: Portable and easy to deploy

## Quick Start

### 1. Clone Repository

```bash
git clone https://github.com/39ff/docker-rotating-proxy
cd docker-rotating-proxy
```

### 2. Install Dependencies

```bash
cd setup
docker run --rm -it -v "$(pwd):/app" composer install
cd ..
```

### 3. Configure Proxies

Edit `proxyList.txt` with your proxies. The format is flexible:

**Example 1: Default format (colon-separated)**
```
# format: host:port:scheme:user:pass
proxy1.example.com:1080:socks5:username:password
proxy2.example.com:8080:http:user:pass
192.168.1.100:3128
```

**Example 2: Custom format (comma-separated)**
```
# format: host,port,scheme,user,pass
159.89.206.161,2434,socks5,vpn,unlimited
142.93.68.63,2434,socks5,vpn,unlimited
```

**Example 3: Custom order (scheme first)**
```
# format: scheme|host|port|user|pass
socks5|proxy.example.com|1080|myuser|mypass
```

**Supported formats:**
- `host:port` - Simple HTTP proxy
- `host:port:scheme` - Proxy with protocol
- `host:port:scheme:user:pass` - With authentication
- Delimiters: `:` (colon), `,` (comma), `|` (pipe), tab, or space

**Supported schemes:**
- `socks5` - Creates Gost bridge container
- `http` / `https` - Creates Gost bridge container
- `httpsquid` - Direct squid integration (no container)
- *(empty)* - Direct HTTP proxy (no container)

### 4. Configure Allowed IPs

Edit `template/allowed_ip.txt` to whitelist client IPs:

```
93.184.216.34
108.62.57.53
```

Get your IP: http://httpbin.org/ip

### 5. Generate Configuration

```bash
# Remove OpenVPN examples if not needed
rm -rf ./openvpn/*

# Generate docker-compose.yml
docker run --rm -it -v "$(pwd):/app/" php:8.2-cli php /app/setup/generate.php
```

### 6. Start Services

```bash
docker-compose up -d
```

### 7. Test

```bash
# Rotating proxy (port 3128)
curl http://httpbin.org/ip --proxy http://127.0.0.1:3128

# Static proxy (port 30000+)
curl http://httpbin.org/ip --proxy http://127.0.0.1:30000
```

## Usage Modes

### Mode 1: Rotating Proxy (Port 3128)

Squid automatically rotates through all configured proxies. Each request uses a different IP.

```bash
curl http://httpbin.org/ip --proxy http://127.0.0.1:3128
# {"origin": "82.196.7.200"}

curl http://httpbin.org/ip --proxy http://127.0.0.1:3128
# {"origin": "89.187.161.56"}
```

**Best for**:
- One-time requests
- High-volume scraping
- Load distribution

### Mode 2: Static Proxy (Ports 30000+)

Direct connection to individual proxy containers. Same IP for all requests to that port.

```bash
curl http://httpbin.org/ip --proxy http://127.0.0.1:30000
# {"origin": "82.196.7.200"}

curl http://httpbin.org/ip --proxy http://127.0.0.1:30000
# {"origin": "82.196.7.200"}  # Same IP
```

**Best for**:
- Browser automation (Selenium, Puppeteer, Playwright)
- Session-based scraping
- Login flows

## Architecture

```
Client Request
    ↓
Squid (localhost:3128) - Rotating proxy mode
    ├→ Direct HTTP Proxies (no container)
    ├→ Gost Containers → SOCKS5/HTTP/HTTPS Proxies
    └→ Gluetun Containers → OpenVPN → Internet

OR

Client Request → Gost/Gluetun Port (30000+) - Static IP mode
```

## OpenVPN Support

To use VPN connections as proxies:

### 1. Create OpenVPN Config

```
openvpn/{name}/
  ├── {config}.ovpn
  └── secret  # Optional: username on line 1, password on line 2
```

**Example**:
```
openvpn/nordvpn-us/
  ├── us123.nordvpn.com.ovpn
  └── secret
```

### 2. Generate Config

The generator will automatically:
- Detect all `.ovpn` files in `openvpn/` subdirectories
- Create Gluetun containers for each VPN
- Resolve hostnames to IPs (prevents DNS leaks)
- Expose HTTP proxy on ports 30000+

## Advanced Configuration

### Custom Settings

Copy and edit the config file:

```bash
cp setup/config.php.example setup/config.php
```

**Available options**:

```php
<?php
return [
    'start_port' => 30000,                    // Starting port for Gost
    'start_shadowsocks_port' => 50000,        // Starting port for Shadowsocks
    'gluetun_http_port' => 8888,              // HTTP port on Gluetun
    'squid_default_options' => '...',         // Squid cache_peer options
];
```

### Public Proxy Auto-Update

Use the included script to automatically fetch and update free proxies:

```bash
# Edit crontab
crontab -e

# Add line (runs hourly)
0 * * * * /path/to/public_proxy_cron.sh
```

See [public_proxy_cron.sh](public_proxy_cron.sh) for details.

## Real-World Examples

### NordVPN

```
# format: host:port:scheme:user:pass
89.187.161.86:80:httpsquid:your-email@example.com:your-password
```

### TorGuard

```
173.254.222.146:1080:socks5:your-username:your-password
```

### Luminati/Bright Data

```
zproxy.lum-superproxy.io:22225:httpsquid:your-username:your-password
```

### Free Proxy Lists

```
# Simple IP:Port format
192.168.1.100:8080
172.31.22.222:3128
```

Sources: [clarketm/proxy-list](https://github.com/clarketm/proxy-list)

## Troubleshooting

### Check Container Status

```bash
docker-compose ps
```

### View Logs

```bash
# All containers
docker-compose logs

# Specific container
docker-compose logs squid
docker-compose logs proxy1
```

### Test Individual Proxy

```bash
# Test first Gost proxy
curl http://httpbin.org/ip --proxy http://127.0.0.1:30000
```

### Regenerate Configuration

```bash
cd setup
php generate.php
cd ..
docker-compose up -d
```

## Security Warning

By default, proxies are accessible without authentication. If exposing to the internet:

1. **Configure firewall rules** to restrict access
2. **Use allowed_ip.txt** to whitelist IPs
3. **Consider adding authentication** (see TODO)

## Performance Tips

- **Use `httpsquid` scheme** for HTTP proxies (no container overhead)
- **Remove unused OpenVPN configs** to reduce containers
- **Increase ulimits** if handling many connections
- **Monitor with** `docker stats` to check resource usage

## File Structure

```
.
├── setup/
│   ├── generate.php           # Main generator script
│   ├── config.php.example     # Configuration template
│   └── composer.json          # PHP dependencies
├── template/
│   ├── docker-compose.yml     # Docker compose template
│   ├── squid.conf             # Squid config template
│   └── allowed_ip.txt         # IP whitelist template
├── config/                    # Generated configs (created by script)
├── openvpn/                   # OpenVPN configurations
├── proxyList.txt              # Your proxy list
└── docker-compose.yml         # Generated compose file

```

## TODO

- [ ] Add username/password authentication for proxy access
- [ ] Web UI for proxy management
- [ ] Health check and auto-removal of dead proxies
- [ ] Proxy performance metrics

## Contributing

Issues and pull requests are welcome at: https://github.com/39ff/docker-rotating-proxy

## License

See repository for license information.
