#!/usr/bin/env php
<?php
$proxies = fopen('/home/delegate/proxylist/proxyList.txt','r');

$port = 49152;
$squid = '';
$i = 0;
while ($line = fgets($proxies)){
    $line = trim($line);
    $proxyInfo = (explode(":",$line));
    $httpProxy = false;

    switch ($proxyInfo[2]){
        case 'socks5':

            //If VPN/Proxy provider offer only socks5 proxy , will create a delegate server
            $additionalArguments = 'SERVER=https'.PHP_EOL;
            $additionalArguments.= 'SOCKS='.$proxyInfo[0].':'.$proxyInfo[1].'/-r'.PHP_EOL;
            $additionalArguments.= 'CONNECT=socks'.PHP_EOL;

            if(!empty($proxyInfo[3]) && !empty($proxyInfo[4])) {
                $additionalArguments .= 'MYAUTH=' . urlencode($proxyInfo[3]) . ':' . urlencode($proxyInfo[4]) . PHP_EOL;
            }
            $additionalArguments.= '-P'.$port;

            $filename = '/home/delegate/config/proxy_'.$port.'.conf';
            file_put_contents($filename, file_get_contents('/home/delegate/delegateBase.conf') . PHP_EOL . $additionalArguments);
            shell_exec('/usr/local/bin/delegate +=' . $filename);
            $squid .= 'cache_peer 127.0.0.1 parent '.$port.' 0 no-digest no-netdb-exchange connect-fail-limit=10 connect-timeout=8 round-robin no-query allow-miss proxy-only name=proxy_'.$port.PHP_EOL;

            break;

        case 'http':
        case 'https':
            //This is required upstream authorization. example: Luminati , NordVPN
            $httpProxy = true;
            $squid .= 'cache_peer '.$proxyInfo[0].' parent '.$proxyInfo[1].' 0 no-digest no-netdb-exchange connect-fail-limit=10 connect-timeout=8 round-robin no-query allow-miss proxy-only name=proxy_'.$i.' '.'login='.urlencode($proxyInfo[3]).':'.urlencode($proxyInfo[4]).PHP_EOL;
            break;

        case 'openproxy':
            //This means an unauthenticated public proxy server
            $httpProxy = true;
            $squid .= 'cache_peer '.$proxyInfo[0].' parent '.$proxyInfo[1].' 0 no-digest no-netdb-exchange connect-fail-limit=5 connect-timeout=8 round-robin no-query allow-miss proxy-only name=proxy_'.$i.PHP_EOL;
            break;
    }

    $port++;
    $i++;
    if(!$httpProxy && $port > 65535){
        break;
    }
}
file_put_contents(
        '/home/delegate/squid.conf',
        file_get_contents("/home/delegate/acl.conf").PHP_EOL.
        file_get_contents('/home/delegate/squid.conf').PHP_EOL.
        $squid.
        file_get_contents('/home/delegate/anonsquid.conf').PHP_EOL

);

system('squid -f '.'/home/delegate/squid.conf');
while (true){
    //need public-proxy update feature
    sleep(5);
}

?>

