# Docker Rotating Proxy
- HTTP/HTTPS Support
-  socks5 with Authorization Proxy to HTTP(S) proxy convert compatible

```
               Docker Container
               -------------------------------------
                      <-> Delegate <-> Socks5 Proxy
                      <-> Delegate <-> Socks5 Proxy with Authorization
Client <---->  Squid  <-> Delegate <-> Tor Proxy
                :3128 <-> Delegate <-> Your HTTP/HTTPS Proxy
                      <-> Delegate <-> Public HTTP/HTTPS Socks Proxies
                      <-> HTTP/HTTPS Proxies (Recommended for OpenProxies)
```


## Usage Example
### Create your proxyList.txt
Search FreeProxy List or Paid/Subscribe ProxyService Provider.


#### Format
```
IPAddress:Port:Type(socks5 or http or https):Username:Password
```

### proxyList.txt example
If you would like to add a lot of http/https proxies,please use :openproxy flag that's not use delegate.

```
127.0.0.1:1080:socks5:yourUsername:yourPassword
127.0.0.1:44888:http::
127.0.0.1:44129:http:mysquidproxy:mysquidpassword
127.0.0.1:29128:http:rotatingserviceUsername:password
169.254.0.1:1080:socks5:paidsocksUsername:paidsocksPassword
127.0.0.1:55519:https::
127.0.0.1:41258:http::
127.0.0.1:80
127.0.0.2:openproxy
```

## Start docker container
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
get a money as rotate proxy provider :)

## WARN
USE OF PUBLIC PROXIES WILL BE LEAKING DATA, DO NOT USE FOR SOCIAL/SHOPPING

### FIREWALL
You should have this one behind a firewall or limit access to it as it is setup to allow all access to it.


## Why not using Polipo?
Because polipo can't possible to forward to upstream proxy with socks5 authorization.

As is well known,VPN/Proxy Provider offered socks5 proxy with Username:Password Authorization.


## Pull request welcome
- Need refactoring
- Use HAProxy etc
