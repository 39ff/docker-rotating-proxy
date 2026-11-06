# Runtime for manager/server.py: Python 3, the docker CLI and the compose
# plugin (to run setup/generate.py and `docker compose up`), bash and curl
# (for public_proxy_cron.sh). The code is read from the mounted checkout, so
# `git pull` updates the web UI without rebuilding this image.
FROM python:3.13-alpine
RUN apk add --no-cache bash curl docker-cli docker-cli-compose
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
