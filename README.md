# Docker Rotating Proxy
- HTTP/HTTPS Support
-  socks5 with Authorization Proxy to HTTP(S) proxy convert compatible

```
               Docker Container
               -------------------------------------
                      <-> Delegate <-> Socks5 Proxy with Authorization
Client <---->  Squid  <-> HTTP/HTTPS Proxies
                      <-> Delegate <-> Socks5 Proxy
                :3128
```


## Usage Example

### Configuring IPs to allow the use of the Rotating Proxy
If you want to use it from outside, please specify the **your** IP address to Allowed_IP.txt

http://httpbin.org/ip

Example:
```
93.184.216.34
108.62.57.53
```

### Create your proxyList.txt
Search FreeProxy List or Paid/Subscribe ProxyService Provider.

example : https://github.com/clarketm/proxy-list

#### Format
```
IPAddress:Port:Type(socks5 or http):Username:Password
IPAddress:Port:Type(socks5 or http)
IPAddress:Port
```

### proxyList.txt Example1
If you would like to add a lot of http/https proxies,please use :openproxy flag that's not use delegate.

```
127.0.0.1:1080:socks5:yourUsername:yourPassword
127.0.0.1:44129:http:mysquidproxy:mysquidpassword
127.0.0.1:29128:http:rotatingserviceUsername:password
169.254.0.1:1080:socks5:paidsocksUsername:paidsocksPassword
127.0.0.1:80
172.31.22.222:8080
proxy.ipredator.se:8080
```

## proxyList.txt Example2
Here are some practical examples.

using NordVPN,TorGuard,Luminati

```
89.187.161.86:80:http:yourNordVPNEmail@example.com:NordVPNPassword
173.254.222.146:1080:socks5:yourTorGuardUsername:Password
zproxy.lum-superproxy.io:22225:http:yourLuminatiUsername:Password
```



## Start docker container

```
docker build -t 39ff/rotate-proxy .
docker run -p 3128:3128 -d 39ff/rotate-proxy
```

or 
```
docker pull confact/rotate-proxy:latest
docker run -it -t -d -p127.0.0.1:3128:3128 --name testproxy confact/rotate-proxy:latest
docker exec -it testproxy /bin/bash
```

want to have your proxylist outside the docker? do this:
```
docker pull confact/rotate-proxy:latest
docker run -it -t -d -p127.0.0.1:3128:3128 -v /proxylist:/home/delegate/proxylist --name testproxy confact/rotate-proxy:latest
docker exec -it testproxy /bin/bash
```




## Now try it out
```
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
```

## WARN
USE OF PUBLIC PROXIES WILL BE LEAKING DATA, DO NOT USE FOR SOCIAL/SHOPPING


## Why not using Polipo or Privoxy?
Because polipo can't possible to forward to upstream proxy with socks5 authorization.

As is well known,VPN/Proxy Provider offered socks5 proxy with Username:Password Authorization.

- Updated 2020-05 , SOCKS5 username/password support https://sourceforge.net/p/ijbswa/patches/141/

## Pull request welcome
- Need refactoring
- Use HAProxy etc
