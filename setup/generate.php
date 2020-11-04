#!/usr/bin/env php
<?php
require __DIR__.'/vendor/autoload.php';
use Symfony\Component\Yaml\Yaml;

copy(__DIR__.'/../template/squid.conf',__DIR__.'/squid.conf');
$to = Yaml::parseFile(__DIR__.'/../template/docker-compose.yml');
$proxies = fopen(__DIR__.'/../proxyList.txt','r');

$i = 1;
$port = 49152;

while ($line = fgets($proxies)){
    $line = trim($line);
    $proxyInfo = (explode(":",$line));
    $cred = '';
    if(!isset($proxyInfo[0]) && !isset($proxyInfo[1])){
        continue;
    }
    if(!isset($proxyInfo[2])){
        //open proxy server IP:Port Pattern.
        //No create gost
        //only add squid config
        file_put_contents(__DIR__.'/squid.conf',PHP_EOL.sprintf('cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=public%d',$proxyInfo[0],$proxyInfo[1],$i),FILE_APPEND);
        $i++;
        continue;
    }
    if(strcmp($proxyInfo[2],'httpsquid') === 0){

        if (isset($proxyInfo[3]) && isset($proxyInfo[4])) {
            //Username:Password Auth
            file_put_contents(__DIR__.'/squid.conf',PHP_EOL.sprintf('cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=private%d login=%s:%s',$proxyInfo[0],$proxyInfo[1],$i,urlencode($proxyInfo[3]),urlencode($proxyInfo[4])),FILE_APPEND);
        }else{
            //IP Auth
            file_put_contents(__DIR__.'/squid.conf',PHP_EOL.sprintf('cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=private%d',$proxyInfo[0],$proxyInfo[1],$i),FILE_APPEND);
        }
        $i++;
        continue;
    }
    //other proxy type ex:socks
    if (isset($proxyInfo[3]) && isset($proxyInfo[4])) {
        $cred = urlencode($proxyInfo[3]) . ':' . urlencode($proxyInfo[4]) . '@';
    }
    $to['services']['proxy' . $i] = [
        'ports' => [
            $port . ':' . $port
        ],
        'image' => 'ginuerzh/gost:latest',
        'container_name'=>'dockergost_'.$i,
        'command' => sprintf('-L=:%d -F=%s://%s%s:%d', $port, $proxyInfo[2], $cred, $proxyInfo[0], $proxyInfo[1])
    ];
    file_put_contents(__DIR__.'/squid.conf',PHP_EOL.sprintf('cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=gost%d','dockergost_'.$i,$port,$i),FILE_APPEND);

    $i++;
    $port++;
}
//openvpn support
if(file_exists(__DIR__.'/../openvpn')){
    foreach(glob(__DIR__.'/../openvpn/*') as $fileOrDir){
        if(!is_dir($fileOrDir)){
            continue;
        }

        $to['services']['vpn' . $i] = [
            'ports' => [
                $port . ':' . '3128',
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
        file_put_contents(__DIR__.'/squid.conf',PHP_EOL.sprintf('cache_peer %s parent %d 0 no-digest no-netdb-exchange connect-fail-limit=2 connect-timeout=8 round-robin no-query allow-miss proxy-only name=vpn%d','dockervpn_'.$i,'3128',$i),FILE_APPEND);

        $i++;
        $port++;

    }
}

file_put_contents(__DIR__.'/../docker-compose.yml', Yaml::dump($to,4,4));
rename(__DIR__.'/squid.conf',__DIR__.'/../config/squid.conf');
copy(__DIR__.'/../template/allowed_ip.txt',__DIR__.'/../config/allowed_ip.txt');
