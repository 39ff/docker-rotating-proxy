#!/usr/bin/env php
<?php
require __DIR__.'/vendor/autoload.php';
use Symfony\Component\Yaml\Yaml;

copy(__DIR__.'/../template/squid.conf',__DIR__.'/squid.conf');
$to = Yaml::parseFile(__DIR__.'/../template/docker-compose.yml');
$proxies = fopen(__DIR__.'/../proxyList.txt','r');

$i = 1;
$port = 49152;
$keys = ['host', 'port', 'scheme', 'user', 'pass'];
$squid_default = 'cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=%s';

while ($line = fgets($proxies)){
    $line = trim($line);
    $proxyInfo = array_combine($keys, array_pad((explode(":", $line, 5)), 5, ''));
    $squid_conf = [];
    $cred = '';
    if(!$proxyInfo['host'] && !$proxyInfo['port']){
        continue;
    }

    if(!$proxyInfo['scheme']){
        //open proxy server IP:Port Pattern.
        //No create gost
        //only add squid config
        $squid_conf[] = sprintf($squid_default, $proxyInfo['host'], $proxyInfo['port'], 'public'.$i);
    }
    elseif(strcmp($proxyInfo['scheme'], 'httpsquid') === 0){
        $squid_conf[] = sprintf($squid_default, $proxyInfo['host'], $proxyInfo['port'], 'private'.$i);
        if ($proxyInfo['user'] && $proxyInfo['pass']) {
            //Username:Password Auth
            $squid_conf[] = vsprintf('login=%s:%s', array_map('urlencode', [$proxyInfo['user'], $proxyInfo['pass']]));
        }
    }else{
        //other proxy type ex:socks
        if ($proxyInfo['user'] && $proxyInfo['pass']) {
            $cred = vsprintf('%s:%s@', array_map('urlencode', [$proxyInfo['user'], $proxyInfo['pass']]));
        }
        $to['services']['proxy' . $i] = [
            'ports' => [
                $port . ':' . $port
            ],
            'image' => 'ginuerzh/gost:latest',
            'container_name'=>'dockergost_'.$i,
            'command' => sprintf('-L=:%d -F=%s://%s%s:%d', $port, $proxyInfo['scheme'], $cred, $proxyInfo['host'], $proxyInfo['port'])
        ];
        $squid_conf[] = sprintf($squid_default, 'dockergost_'.$i, $port, 'gost'.$i);
        $port++;
    }
    if($squid_conf){
        file_put_contents(__DIR__.'/squid.conf', PHP_EOL . implode(' ', $squid_conf), FILE_APPEND);
    }
    $i++;
}
//openvpn support
if(file_exists(__DIR__.'/../openvpn')){
    foreach(glob(__DIR__.'/../openvpn/*') as $fileOrDir){
        if(!is_dir($fileOrDir)){
            continue;
        }

        $to['services']['vpn' . $i] = [
            'ports' => [
                $port . ':3128',
            ],
            'image' => 'curve25519xsalsa20poly1305/openvpn',
            'container_name'=>'dockervpn_'.$i,
            'devices'=>[
                '/dev/net/tun:/dev/net/tun'
            ],
            'cap_add'=>[
                'NET_ADMIN'
            ],
            'volumes'=>[
                './openvpn/'.basename($fileOrDir).':/vpn:ro'
            ],
            'environment'=>[
                'OPENVPN_CONFIG=/vpn/vpn.ovpn'
            ]
        ];
        file_put_contents(__DIR__.'/squid.conf', PHP_EOL . sprintf($squid_default, 'dockervpn_'.$i, '3128', 'vpn'.$i), FILE_APPEND);

        $i++;
        $port++;

    }
}

file_put_contents(__DIR__.'/../docker-compose.yml', Yaml::dump($to,4,4));
rename(__DIR__.'/squid.conf',__DIR__.'/../config/squid.conf');
copy(__DIR__.'/../template/allowed_ip.txt',__DIR__.'/../config/allowed_ip.txt');
