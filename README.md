# Docker Rotating Proxy Config Generator

- Fully Optimized for Web Scraping Usage.
- HTTP/HTTPS Support
- socks5 with Authorization Proxy to HTTP(S) proxy convert compatible by [Gost](https://github.com/ginuerzh/gost)
- You can use a VPN as an HTTP proxy.
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
version: '3.3'
services:
    squid:
        ports:
            - '3128:3128'
        image: 'b4tman/squid:latest'
        volumes:
            - './config:/etc/squid/conf.d:ro'
        container_name: dockersquid_rotate
        environment:
          - 'SQUID_CONFIG_FILE=/etc/squid/conf.d/squid.conf'
    hola1:
        ports:
            - '10021:8080'
        image: 'yarmak/hola-proxy:latest'
        command: '-country us -proxy-type peer'
    socks1:
        ports:
            - '10022:10022'
        image: 'ginuerzh/gost:latest'
        command: '-L=:10022 -F=socks5://vpn:unlimited@159.89.206.161:2434'
    proxy1:
        ports:
            - '49152:49152'
        image: 'ginuerzh/gost:latest'
        container_name: dockergost_1
        command: '-L=:49152 -F=socks5://vpn:unlimited@159.89.206.161:2434'
    proxy2:
        ports:
            - '49153:49153'
        image: 'ginuerzh/gost:latest'
        container_name: dockergost_2
        command: '-L=:49153 -F=socks5://vpn:unlimited@142.93.68.63:2434'
    proxy3:
        ports:
            - '49154:49154'
        image: 'ginuerzh/gost:latest'
        container_name: dockergost_3
        command: '-L=:49154 -F=socks5://vpn:unlimited@82.196.7.200:2434'
    vpn4:
        ports:
            - '49155:3128'
        image: curve25519xsalsa20poly1305/openvpn
        container_name: dockervpn_4
        devices:
            - '/dev/net/tun:/dev/net/tun'
        cap_add:
            - NET_ADMIN
        volumes:
            - './openvpn/hk-hkg.prod.surfshark.comsurfshark_openvpn_tcp.ovpn:/vpn:ro'
        environment:
            - OPENVPN_CONFIG=/vpn/vpn.ovpn
    vpn5:
        ports:
            - '49156:3128'
        image: curve25519xsalsa20poly1305/openvpn
        container_name: dockervpn_5
        devices:
            - '/dev/net/tun:/dev/net/tun'
        cap_add:
            - NET_ADMIN
        volumes:
            - './openvpn/jp454.nordvpn.com.tcp443.ovpn:/vpn:ro'
        environment:
            - OPENVPN_CONFIG=/vpn/vpn.ovpn
```

## Now try it out
```
port 3128 is rotation port.
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "209.197.26.75"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "185.155.98.168"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "173.245.217.48"
}
sh-4.2# curl https://httpbin.org/ip --proxy https://127.0.0.1:3128
{
  "origin": "81.171.85.49"
}
sh-4.2# 

and.. try static ip gateway

# curl httpbin.org/ip --proxy http://127.0.0.1:49152
{
  "origin": "139.99.54.109"
}
# curl httpbin.org/ip --proxy http://127.0.0.1:49152
{
  "origin": "139.99.54.109"
}

# curl httpbin.org/ip --proxy http://127.0.0.1:49153
{
  "origin": "159.89.206.161"
}
# curl httpbin.org/ip --proxy http://127.0.0.1:49153
{
  "origin": "159.89.206.161"
}
```

## Example of using a large number of public proxies with real-time updates
see [public_proxy_cron.sh](public_proxy_cron.sh)
```
0 * * * * /your_sh_path_here/public_proxy_cron.sh
```

## TODO
- [ ] Username/Password Auth for Enterprise