# Docker Rotating Proxy
-  socks5 with Authorization Proxy to HTTP(S) proxy convert compatible  


```
               Docker Container
               -------------------------------------
                      <-> Delegate <-> Socks5 Proxy
                      <-> Delegate <-> Socks5 Proxy with Authorization
Client <---->  Squid  <-> Delegate <-> Tor Proxy
                      <-> Your HTTP Proxy
                      <-> Your HTTPS Proxy
                      <-> Public Proxies
```



## Why not using Polipo?
Because polipo can't possible to forward to upstream proxy with socks5 authorization.

As is well known,Usually VPN/Proxy Provider offered socks5 only proxy with Username:Password Authorization.

