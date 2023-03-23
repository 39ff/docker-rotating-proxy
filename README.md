# Docker Rotating Proxy Config Generator

- Fully Optimized for Web Scraping Usage.
- HTTP/HTTPS Support (see wiki)
- socks5 with Authorization Proxy to HTTP(S) proxy convert compatible by [Gost](https://github.com/ginuerzh/gost)
- You can use a VPN as an HTTP proxy.(powered by [gluetun](https://github.com/qdm12/gluetun) )
- Making it IP address based authentication makes it easier to use in your program.(selenium,puppeteer etc)


```
               Docker Container
               ----------------------------------
Client <---->  Squid  <-> HTTP/HTTPS Rotate Proxies---\ 
                       |
        ---------------|-> Gost <-> Socks5 Proxy    --- Internet
                       |
        --------------<-> VPN to HTTP Proxy <--------/
                     
        

It can be used in two ways.
1.Automatically control the proxy and rotate each request -> use Squid
2.Control the proxy programmatically　-> use Gost Port

```


## Usage Example

### Configuring IPs to allow the use of the Rotating Proxy
If you want to use it from outside, please specify the **your** IP address to allowed_ip.txt

http://httpbin.org/ip

Example:
```
93.184.216.34
108.62.57.53
```

### 1. Create your proxyList.txt(If HTTP/Socks is provided)
Search FreeProxy List or Paid/Subscribe ProxyService Provider.

example : https://github.com/clarketm/proxy-list

#### Format
```
IPAddress:Port:Type(socks5 or http or https or httpsquid):Username:Password
IPAddress:Port:Type(socks5 or http or https or httpsquid)
IPAddress:Port
```

### 1.1 Create Your OpenVPN Config(If HTTP/Socks is NOT provided)
see [example](openvpn/)

### Format
```
openvpn/{name}
openvpn/{name}/{name2}.ovpn
openvpn/{name}/secret
```

### 2. Generate docker-compose.yml
```
git clone https://github.com/39ff/docker-rotating-proxy
cd docker-rotating-proxy && cd setup
docker run --rm -it -v "$(pwd):/app" composer install
cd ..
# If you don't want to set up OpenVPN, please remove it.
rm -rf ./openvpn/*
docker run --rm -it -v "$(pwd):/app/" php:7.4-cli php /app/setup/generate.php
cat docker-compose.yml
docker-compose up -d
curl https://httpbin.org/ip --proxy http://127.0.0.1:3128
```

### How to it works?
![pattern1](https://user-images.githubusercontent.com/7544687/97991581-fdc2f380-1e24-11eb-99f3-df9885d627a2.png)

- Sometimes you may need the same IP address for a series of steps.
To deal with this problem, we have built a new relay server via gost.

- Most open proxies will be unavailable in a few days.
Therefore, it is useless to build a server for every open proxy, so we use squid's cache_peer to rotate a large number of open proxies.

### proxyList.txt Example1

```
127.0.0.1:1080:socks5:yourUsername:yourPassword
127.0.0.1:44129:httpsquid:mysquidproxy:mysquidpassword
127.0.0.1:29128:httpsquid:rotatingserviceUsername:password
169.254.0.1:1080:socks5:paidsocksUsername:paidsocksPassword
127.0.0.1:80
172.31.22.222:8080
```

## proxyList.txt Example2
Here are some practical examples.

using NordVPN,TorGuard,Luminati

```
89.187.161.86:80:httpsquid:yourNordVPNEmail@example.com:NordVPNPassword
173.254.222.146:1080:socks5:yourTorGuardUsername:Password
zproxy.lum-superproxy.io:22225:httpsquid:yourLuminatiUsername:Password
```



## Generated docker-compose.yml example
```
version: '3.4'
services:
    squid:
        ports:
            - '3128:3128'
        image: 'b4tman/squid:5.8'
        volumes:
            - './config:/etc/squid/conf.d:ro'
        container_name: dockersquid_rotate
        environment:
            - SQUID_CONFIG_FILE=/etc/squid/conf.d/squid.conf
        extra_hosts:
            - 'host.docker.internal:host-gateway'
        healthcheck:
            test: [CMD-SHELL, 'export https_proxy=127.0.0.1:3128 && export http_proxy=127.0.0.1:3128 && wget -q -Y on  -O - https://checkip.amazonaws.com || exit 1']
            retries: 5
            timeout: 10s
            start_period: 10s
            interval: 300s
    proxy1:
        ports:
            - '30000:30000'
        image: 'ginuerzh/gost:latest'
        container_name: dockergost_1
        command: '-L=:30000 -F=socks5://vpn:unlimited@82.196.7.200:2434'
    vpn2:
        ports:
            - '30001:8888/tcp'
            - '50000:8388'
        image: qmcgaw/gluetun
        container_name: dockervpn_2
        devices:
            - '/dev/net/tun:/dev/net/tun'
        cap_add:
            - NET_ADMIN
        volumes:
            - './openvpn/hk-hkg.prod.surfshark.comsurfshark_openvpn_tcp.ovpn:/gluetun'
        environment:
            - VPN_SERVICE_PROVIDER=custom
            - VPN_TYPE=openvpn
            - OPENVPN_CUSTOM_CONFIG=/gluetun/vpn.ovpn
            - HTTPPROXY=on
            - HTTPPROXY_USER=
            - HTTPPROXY_PASSWORD=
            - HTTPPROXY_STEALTH=on
            - OPENVPN_USER=xxxxx
            - OPENVPN_PASSWORD=yyyyy
    vpn3:
        ports:
            - '30002:8888/tcp'
            - '50001:8388'
        image: qmcgaw/gluetun
        container_name: dockervpn_3
        devices:
            - '/dev/net/tun:/dev/net/tun'
        cap_add:
            - NET_ADMIN
        volumes:
            - './openvpn/jp454.nordvpn.com.tcp443.ovpn:/gluetun'
        environment:
            - VPN_SERVICE_PROVIDER=custom
            - VPN_TYPE=openvpn
            - OPENVPN_CUSTOM_CONFIG=/gluetun/vpn.ovpn
            - HTTPPROXY=on
            - HTTPPROXY_USER=
            - HTTPPROXY_PASSWORD=
            - HTTPPROXY_STEALTH=on
            - OPENVPN_USER=xxxxx
            - OPENVPN_PASSWORD=yyyyy
```

## Now try it out
```
port 3128 is rotation port.
Recommended for one-time requests that do not require browser rendering, such as curl

sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "82.196.7.200"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "89.187.161.56"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "84.17.37.159"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "81.171.85.49"
}
sh-4.2# 

and.. try static ip gateway
Recommended in selenium, puppeteer and playwright

# curl httpbin.org/ip --proxy http://127.0.0.1:30000
{
  "origin": "82.196.7.200"
}
# curl httpbin.org/ip --proxy http://127.0.0.1:30000
{
  "origin": "82.196.7.200"
}

# curl httpbin.org/ip --proxy http://127.0.0.1:30001
{
  "origin": "84.17.37.159"
}
# curl httpbin.org/ip --proxy http://127.0.0.1:30001
{
  "origin": "84.17.37.159"
}
```


## Warning
By default, ports can be used without authentication.
Some VPSs that are directly exposed globally may require appropriate modifications to the docker-compose.


## Example of using a large number of public proxies with real-time updates
see [public_proxy_cron.sh](public_proxy_cron.sh)
```
0 * * * * /your_sh_path_here/public_proxy_cron.sh
```

## TODO
- [ ] Username/Password Auth for Enterprise