#!/usr/bin/env php
<?php
/**
 * Docker Rotating Proxy Configuration Generator
 * Generates docker-compose.yml and squid configuration from proxy list
 */

require __DIR__.'/vendor/autoload.php';
use Symfony\Component\Yaml\Yaml;

// Configuration
$config = [
    'start_port' => 30000,
    'start_shadowsocks_port' => 50000,
    'gluetun_http_port' => 8888,
    'squid_default_options' => 'no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only',
];

// Load user config if exists
if (file_exists(__DIR__.'/config.php')) {
    $userConfig = require __DIR__.'/config.php';
    $config = array_merge($config, $userConfig);
}

// Initialize
copy(__DIR__.'/../template/squid.conf', __DIR__.'/squid.conf');
$dockerCompose = Yaml::parseFile(__DIR__.'/../template/docker-compose.yml');

// Process proxy list
$proxies = parseProxyList(__DIR__.'/../proxyList.txt');
$state = [
    'counter' => 1,
    'port' => $config['start_port'],
    'shadowsocks_port' => $config['start_shadowsocks_port'],
];

foreach ($proxies as $proxy) {
    processProxy($proxy, $dockerCompose, $state, $config);
}

// Process OpenVPN configurations
processOpenVPN($dockerCompose, $state, $config);

// Write output files
file_put_contents(__DIR__.'/../docker-compose.yml', Yaml::dump($dockerCompose, 4, 4));
rename(__DIR__.'/squid.conf', __DIR__.'/../config/squid.conf');
copy(__DIR__.'/../template/allowed_ip.txt', __DIR__.'/../config/allowed_ip.txt');

echo "✓ Generated docker-compose.yml\n";
echo "✓ Generated config/squid.conf\n";
echo "✓ Processed " . ($state['counter'] - 1) . " proxy entries\n";

/**
 * Parse proxy list file with flexible format support
 *
 * Supports header line to define column order:
 * # format: host,port,scheme,user,pass
 * or
 * # format: scheme,host,port,user,pass
 *
 * Default format (no header): host:port:scheme:user:pass
 */
function parseProxyList($filepath) {
    if (!file_exists($filepath)) {
        echo "Warning: proxyList.txt not found\n";
        return [];
    }

    $lines = file($filepath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    $proxies = [];
    $format = ['host', 'port', 'scheme', 'user', 'pass']; // Default format
    $delimiter = ':'; // Default delimiter

    foreach ($lines as $lineNum => $line) {
        $line = trim($line);

        // Skip comments (but parse format header)
        if (strpos($line, '#') === 0) {
            // Check for format header
            if (preg_match('/^#\s*format:\s*(.+)$/i', $line, $matches)) {
                $formatStr = trim($matches[1]);

                // Detect delimiter (comma, colon, pipe, tab, space)
                if (strpos($formatStr, ',') !== false) {
                    $delimiter = ',';
                } elseif (strpos($formatStr, '|') !== false) {
                    $delimiter = '|';
                } elseif (strpos($formatStr, "\t") !== false) {
                    $delimiter = "\t";
                } elseif (strpos($formatStr, ' ') !== false) {
                    $delimiter = ' ';
                } else {
                    $delimiter = ':';
                }

                $format = array_map('trim', explode($delimiter, $formatStr));
                echo "Using custom format: " . implode($delimiter, $format) . "\n";
            }
            continue;
        }

        // Skip empty lines
        if (empty($line)) {
            continue;
        }

        // Parse proxy entry
        $parts = array_map('trim', explode($delimiter, $line, count($format)));
        $parts = array_pad($parts, count($format), ''); // Pad with empty strings

        $proxy = array_combine($format, $parts);

        // Validate required fields
        if (empty($proxy['host']) || empty($proxy['port'])) {
            echo "Warning: Skipping invalid proxy on line " . ($lineNum + 1) . ": $line\n";
            continue;
        }

        // Normalize scheme
        if (!empty($proxy['scheme'])) {
            $proxy['scheme'] = strtolower($proxy['scheme']);
        }

        $proxies[] = $proxy;
    }

    return $proxies;
}

/**
 * Process a single proxy entry
 */
function processProxy($proxy, &$dockerCompose, &$state, $config) {
    $squidConf = [];
    $serviceName = 'proxy' . $state['counter'];

    // Build squid cache_peer template
    $squidTemplate = sprintf(
        'cache_peer %%s parent %%d 0 %s name=%%s',
        $config['squid_default_options']
    );

    if (empty($proxy['scheme'])) {
        // Direct HTTP proxy (no authentication, no container needed)
        $squidConf[] = sprintf($squidTemplate, $proxy['host'], $proxy['port'], 'public' . $state['counter']);
    }
    elseif ($proxy['scheme'] === 'httpsquid') {
        // HTTP proxy with potential authentication
        $squidConf[] = sprintf($squidTemplate, $proxy['host'], $proxy['port'], 'private' . $state['counter']);

        if (!empty($proxy['user']) && !empty($proxy['pass'])) {
            $squidConf[] = sprintf(
                'login=%s:%s',
                urlencode($proxy['user']),
                urlencode($proxy['pass'])
            );
        }
    }
    else {
        // SOCKS5/HTTP/HTTPS proxy - needs Gost container
        $containerName = 'dockergost_' . $state['counter'];
        $port = $state['port'];

        // Build credentials
        $cred = '';
        if (!empty($proxy['user']) && !empty($proxy['pass'])) {
            $cred = sprintf(
                '%s:%s@',
                urlencode($proxy['user']),
                urlencode($proxy['pass'])
            );
        }

        // Create Gost service
        $dockerCompose['services'][$serviceName] = [
            'image' => 'ginuerzh/gost:latest',
            'container_name' => $containerName,
            'restart' => 'unless-stopped',
            'ports' => [$port . ':' . $port],
            'command' => sprintf(
                '-L=:%d -F=%s://%s%s:%d',
                $port,
                $proxy['scheme'],
                $cred,
                $proxy['host'],
                $proxy['port']
            ),
        ];

        // Add to squid config
        $squidConf[] = sprintf($squidTemplate, $containerName, $port, 'gost' . $state['counter']);

        $state['port']++;
    }

    // Write squid configuration
    if (!empty($squidConf)) {
        file_put_contents(
            __DIR__.'/squid.conf',
            PHP_EOL . implode(' ', $squidConf),
            FILE_APPEND
        );
    }

    $state['counter']++;
}

/**
 * Process OpenVPN configurations
 */
function processOpenVPN(&$dockerCompose, &$state, $config) {
    $openvpnDir = __DIR__.'/../openvpn';

    if (!file_exists($openvpnDir)) {
        return;
    }

    $squidTemplate = sprintf(
        'cache_peer %%s parent %%d 0 %s name=%%s',
        $config['squid_default_options']
    );

    foreach (glob($openvpnDir . '/*', GLOB_ONLYDIR) as $configDir) {
        $ovpnFiles = glob($configDir . '/*.ovpn');

        if (empty($ovpnFiles[0])) {
            echo "Warning: No .ovpn file found in " . basename($configDir) . "\n";
            continue;
        }

        $ovpnFile = realpath($ovpnFiles[0]);
        $secretFile = $configDir . '/secret';

        // Resolve hostname to IP (prevent DNS leaks)
        resolveOpenVPNHostname($ovpnFile);

        // Prepare environment variables
        $env = [
            'VPN_SERVICE_PROVIDER=custom',
            'VPN_TYPE=openvpn',
            'OPENVPN_CUSTOM_CONFIG=/gluetun/' . basename($ovpnFile),
            'HTTPPROXY=on',
            'HTTPPROXY_USER=',
            'HTTPPROXY_PASSWORD=',
            'HTTPPROXY_STEALTH=on',
        ];

        // Add credentials if secret file exists
        if (file_exists($secretFile)) {
            $credentials = file($secretFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
            if (count($credentials) >= 2) {
                $env[] = 'OPENVPN_USER=' . $credentials[0];
                $env[] = 'OPENVPN_PASSWORD=' . $credentials[1];
            }
        }

        // Create VPN service
        $serviceName = 'vpn' . $state['counter'];
        $containerName = 'dockervpn_' . $state['counter'];

        $dockerCompose['services'][$serviceName] = [
            'image' => 'qmcgaw/gluetun',
            'container_name' => $containerName,
            'restart' => 'unless-stopped',
            'devices' => ['/dev/net/tun:/dev/net/tun'],
            'cap_add' => ['NET_ADMIN'],
            'ports' => [
                $state['port'] . ':' . $config['gluetun_http_port'] . '/tcp',
                $state['shadowsocks_port'] . ':8388',
            ],
            'volumes' => ['./openvpn/' . basename($configDir) . ':/gluetun'],
            'environment' => $env,
        ];

        // Add to squid config
        file_put_contents(
            __DIR__.'/squid.conf',
            PHP_EOL . sprintf($squidTemplate, $containerName, $config['gluetun_http_port'], 'vpn' . $state['counter']),
            FILE_APPEND
        );

        $state['counter']++;
        $state['port']++;
        $state['shadowsocks_port']++;
    }
}

/**
 * Resolve OpenVPN hostname to IP address to prevent DNS leaks
 */
function resolveOpenVPNHostname($ovpnFile) {
    $lines = file($ovpnFile, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
    $modified = false;

    foreach ($lines as $key => $line) {
        if (preg_match('/^remote\s+([\w.-]+)(?:\s+(\d+))?$/', $line, $matches)) {
            $hostname = $matches[1];
            $ip = gethostbyname($hostname);

            // Only replace if DNS resolution succeeded (IP != hostname)
            if ($ip !== $hostname) {
                $remote = 'remote ' . $ip;
                if (isset($matches[2])) {
                    $remote .= ' ' . $matches[2];
                }
                $lines[$key] = $remote;
                $modified = true;
                echo "Resolved VPN hostname: $hostname -> $ip\n";
            }
            break;
        }
    }

    if ($modified) {
        file_put_contents($ovpnFile, implode(PHP_EOL, $lines));
    }
}
