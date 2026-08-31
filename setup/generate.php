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

// Add web authentication system if enabled
if (!empty($config['enable_web_auth'])) {
    addWebAuthServices($dockerCompose, $config);
}

// Write output files
file_put_contents(__DIR__.'/../docker-compose.yml', Yaml::dump($dockerCompose, 4, 4));
rename(__DIR__.'/squid.conf', __DIR__.'/../config/squid.conf');
copy(__DIR__.'/../template/allowed_ip.txt', __DIR__.'/../config/allowed_ip.txt');

echo "✓ Generated docker-compose.yml\n";
echo "✓ Generated config/squid.conf\n";
echo "✓ Processed " . ($state['counter'] - 1) . " proxy entries\n";

if (!empty($config['enable_web_auth'])) {
    echo "✓ Web authentication enabled (http://localhost:" . $config['web_auth']['web_port'] . ")\n";
}

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

/**
 * Add web authentication services to docker-compose
 * Integrates squid-db-auth-web and squid-db-auth-ip for user management
 */
function addWebAuthServices(&$dockerCompose, $config) {
    $webAuth = $config['web_auth'];

    // Generate Laravel APP_KEY if not provided
    $appKey = $webAuth['app_key'];
    if (empty($appKey)) {
        $appKey = 'base64:' . base64_encode(random_bytes(32));
    }

    // Add MySQL service
    $dockerCompose['services']['db'] = [
        'image' => 'mysql:8.0',
        'container_name' => 'dockersquid_mysql',
        'restart' => 'unless-stopped',
        'ports' => [$webAuth['db_port'] . ':3306'],
        'environment' => [
            'MYSQL_ROOT_PASSWORD=' . $webAuth['db_root_password'],
            'MYSQL_DATABASE=' . $webAuth['db_name'],
            'MYSQL_USER=' . $webAuth['db_user'],
            'MYSQL_PASSWORD=' . $webAuth['db_password'],
            'TZ=UTC',
        ],
        'volumes' => ['db-store:/var/lib/mysql'],
        'command' => '--default-authentication-plugin=mysql_native_password',
    ];

    // Add Redis service
    $dockerCompose['services']['redis'] = [
        'image' => 'redis:6.2-alpine',
        'container_name' => 'dockersquid_redis',
        'restart' => 'unless-stopped',
        'ports' => [$webAuth['redis_port'] . ':6379'],
        'volumes' => ['redis-store:/data'],
    ];

    // Add Laravel application service
    $dockerCompose['services']['app'] = [
        'image' => 'ghcr.io/39ff/squid-db-auth-web:latest',
        'container_name' => 'dockersquid_app',
        'restart' => 'unless-stopped',
        'depends_on' => ['db', 'redis'],
        'environment' => [
            'APP_NAME=SquidUserManager',
            'APP_ENV=production',
            'APP_KEY=' . $appKey,
            'APP_DEBUG=' . $webAuth['app_debug'],
            'APP_URL=' . $webAuth['app_url'],
            'DB_CONNECTION=mysql',
            'DB_HOST=db',
            'DB_PORT=3306',
            'DB_DATABASE=' . $webAuth['db_name'],
            'DB_USERNAME=' . $webAuth['db_user'],
            'DB_PASSWORD=' . $webAuth['db_password'],
            'REDIS_HOST=redis',
            'REDIS_PASSWORD=null',
            'REDIS_PORT=6379',
            'CACHE_DRIVER=redis',
            'SESSION_DRIVER=redis',
            'QUEUE_CONNECTION=sync',
        ],
    ];

    // If source path is provided, mount it
    if (!empty($webAuth['web_source_path'])) {
        $dockerCompose['services']['app']['volumes'] = [
            $webAuth['web_source_path'] . ':/app'
        ];
    }

    // Add Nginx web server
    $dockerCompose['services']['web'] = [
        'image' => 'nginx:alpine',
        'container_name' => 'dockersquid_web',
        'restart' => 'unless-stopped',
        'ports' => [$webAuth['web_port'] . ':80'],
        'depends_on' => ['app'],
        'volumes' => [
            './config/nginx.conf:/etc/nginx/conf.d/default.conf:ro',
        ],
    ];

    // Update Squid service to depend on db and include auth script
    if (isset($dockerCompose['services']['squid'])) {
        if (!isset($dockerCompose['services']['squid']['depends_on'])) {
            $dockerCompose['services']['squid']['depends_on'] = [];
        }
        $dockerCompose['services']['squid']['depends_on'][] = 'db';

        // Add volume for auth script
        if (!isset($dockerCompose['services']['squid']['volumes'])) {
            $dockerCompose['services']['squid']['volumes'] = [];
        }
        $dockerCompose['services']['squid']['volumes'][] = './config/basic_db_ip_auth.php:/etc/squid/basic_db_ip_auth.php:ro';
    }

    // Add volumes for persistence
    if (!isset($dockerCompose['volumes'])) {
        $dockerCompose['volumes'] = [];
    }
    $dockerCompose['volumes']['db-store'] = [
        'driver' => 'local',
        'driver_opts' => ['type' => 'none', 'o' => 'bind', 'device' => './volumes/mysql'],
    ];
    $dockerCompose['volumes']['redis-store'] = [
        'driver' => 'local',
        'driver_opts' => ['type' => 'none', 'o' => 'bind', 'device' => './volumes/redis'],
    ];

    // Create volumes directory structure
    @mkdir(__DIR__.'/../volumes', 0755, true);
    @mkdir(__DIR__.'/../volumes/mysql', 0755, true);
    @mkdir(__DIR__.'/../volumes/redis', 0755, true);

    // Download and save auth IP script
    downloadAuthScript($webAuth['auth_ip_script_url'], __DIR__.'/../config/basic_db_ip_auth.php');

    // Generate nginx configuration
    generateNginxConfig(__DIR__.'/../config/nginx.conf');

    // Generate auth-enabled squid configuration
    generateAuthSquidConfig(__DIR__.'/squid.conf', $webAuth);

    echo "Setting up web authentication services...\n";
    echo "- MySQL database\n";
    echo "- Redis cache\n";
    echo "- Laravel application\n";
    echo "- Nginx web server\n";
}

/**
 * Download authentication script from GitHub
 */
function downloadAuthScript($url, $destination) {
    echo "Downloading auth script from $url...\n";

    $content = @file_get_contents($url);
    if ($content === false) {
        echo "Warning: Could not download auth script. Using placeholder.\n";
        $content = "<?php\n// Auth script placeholder\n// Download manually from: $url\n";
    }

    file_put_contents($destination, $content);
    echo "✓ Saved auth script to $destination\n";
}

/**
 * Generate Nginx configuration for Laravel
 */
function generateNginxConfig($destination) {
    $config = <<<'NGINX'
server {
    listen 80;
    server_name _;
    root /app/public;

    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";

    index index.php;

    charset utf-8;

    location / {
        try_files $uri $uri/ /index.php?$query_string;
    }

    location = /favicon.ico { access_log off; log_not_found off; }
    location = /robots.txt  { access_log off; log_not_found off; }

    error_page 404 /index.php;

    location ~ \.php$ {
        fastcgi_pass app:9000;
        fastcgi_param SCRIPT_FILENAME $realpath_root$fastcgi_script_name;
        include fastcgi_params;
    }

    location ~ /\.(?!well-known).* {
        deny all;
    }
}
NGINX;

    file_put_contents($destination, $config);
    echo "✓ Generated nginx config\n";
}

/**
 * Update squid configuration to use database authentication
 */
function generateAuthSquidConfig($squidConfPath, $webAuth) {
    // Add auth configuration to squid.conf
    $authConfig = <<<SQUID

# Database authentication configuration
auth_param basic program /etc/squid/basic_db_ip_auth.php --dsn "mysql:dbname={$webAuth['db_name']};host=db;charset=utf8mb4" --user {$webAuth['db_user']} --password {$webAuth['db_password']}
auth_param basic children 20 startup=5 idle=1
auth_param basic realm Squid Proxy
auth_param basic credentialsttl 2 hours

# ACL for authenticated users
acl authenticated_users proxy_auth REQUIRED
http_access allow authenticated_users

SQUID;

    // Prepend auth config to existing squid.conf (before first cache_peer line)
    $existingConfig = file_get_contents($squidConfPath);

    // Insert auth config before first cache_peer line only
    if (preg_match('/^cache_peer/m', $existingConfig, $matches, PREG_OFFSET_CAPTURE)) {
        $insertPos = $matches[0][1];
        $existingConfig = substr_replace($existingConfig, $authConfig . "\n", $insertPos, 0);
        file_put_contents($squidConfPath, $existingConfig);
    } else {
        // No cache_peer lines, append to end
        file_put_contents($squidConfPath, $authConfig, FILE_APPEND);
    }

    echo "✓ Added database authentication to squid config\n";
}
