#!/usr/bin/env bash
curl -sSf "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt" > proxyList.txt
docker run --rm -it -v "$(pwd):/app/" php:7.4-cli php /app/setup/generate.php
docker kill --signal=SIGHUP dockersquid_rotate