#!/usr/bin/env php
<?php
require './vendor/autoload.php';
use Symfony\Component\Yaml\Yaml;

$to = Yaml::parseFile('../template/docker-compose.yml');
var_dump($to);

$proxies = fopen('../proxyList.txt','r');

$i = 1;
$port = 49152;

while ($line = fgets($proxies)){
    $line = trim($line);
    $proxyInfo = (explode(":",$line));
    $cred = '';
    if(!isset($proxyInfo[2])){
        $proxyInfo[2] = 'http';
    }
    if (isset($proxyInfo[3]) && isset($proxyInfo[4])) {
        $cred = urlencode($proxyInfo[3]) . ':' . urlencode($proxyInfo[4]) . '@';
    }
    $to['services']['proxy' . $i] = [
        'ports' => [
            $port . ':' . $port
        ],
        'image' => 'chenhw2/gost:latest',
        'environment' => [
            'ARGS' => sprintf('-L=:%d -F=%s://%s%s:%d', $port, $proxyInfo[2], $cred, $proxyInfo[0], $proxyInfo[1])
        ]
    ];
    $i++;
    $port++;
}

file_put_contents('docker-compose.yml', Yaml::dump($to));