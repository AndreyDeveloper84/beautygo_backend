# Base image, indirected through a build ARG (DRF-1431).
#
# Why this is not just `FROM python:3.12-slim`. The dev deploy builds ON
# THE VPS (ci.yml step "2/4 Build application images" runs
# `docker compose build` over SSH), and that box reaches Docker Hub badly:
# two consecutive deploys on 2026-08-31 died before a single line of our
# code was compiled, at
#     failed to authorize: failed to fetch anonymous token:
#     Get "https://auth.docker.io/token?scope=repository%3Alibrary..."
# Measured from the pilot on 2026-08-31: registry-1.docker.io/v2/ answers
# in 5.4s and auth.docker.io is intermittent, while ghcr.io/v2/ answers in
# 0.26-0.31s on every attempt. GitHub is already a hard dependency of the
# deploy (step 1/4 fetches from it), so pulling the base layer from GHCR
# adds no new point of failure — it removes one.
#
# The DEFAULT stays the upstream Docker Hub ref on purpose: a fresh clone,
# a laptop, and CI all keep working with no registry setup. Only the dev
# deploy overrides it, via docker-compose.yml's
# `PYTHON_BASE_IMAGE: ${PYTHON_BASE_IMAGE:-python:3.12-slim}`.
#
# The mirror is refreshed by .github/workflows/mirror-base-image.yml,
# which runs on a GitHub runner (good Hub connectivity) and copies the
# upstream manifest list byte-for-byte. Same digest, different registry.
ARG PYTHON_BASE_IMAGE=python:3.12-slim
FROM ${PYTHON_BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies:
# - libjpeg-dev / zlib1g-dev: Pillow image processing
# - libpq-dev: psycopg2 (PostgreSQL adapter)
# - git: pip needs it to clone git+ dependencies (e.g.
#   ``ayla-ai-core @ git+https://github.com/...``). Without git in PATH
#   pip prints "Cannot find command 'git'" and the build fails.
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    zlib1g-dev \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# DRF-979 — токена в сборке больше нет: ни build-аргумента, ни url-rewrite.
#
# Здесь стоял необязательный build-аргумент с токеном и RUN-строка, которая
# писала его в /root/.gitconfig через `url.<…>.insteadOf`, с комментарием
# «consumed at build time only and NOT baked into the final image». Оба
# утверждения неверны, и проверено это на собранном образе 11.09.2026:
#   * значение аргумента попадает в команду RUN, а команда RUN — в
#     `docker history --no-trunc` каждого слоя, который её исполнил;
#   * `git config --global` пишет /root/.gitconfig В СЛОЙ, и файл едет в
#     финальный образ — не user-facing, но он там.
# То есть каждый образ, собранный на пилоте с токеном в .env, нёс его в двух
# местах. Комментарий говорил одно, слой — другое.
#
# ayla-ai-core публичен (решение владельца 04.09.2026, OPEN_DECISIONS §22),
# pip клонирует закреплённый SHA анонимно — так было и до этой правки, аргумент
# был пуст. Если видимость когда-нибудь закроют, единственная форма, которая
# НЕ оставляет секрет в слое, — BuildKit-секрет, не аргумент сборки:
#     RUN --mount=type=secret,id=gh_token #         git config --global url."https://$(cat /run/secrets/gh_token)@github.com/".insteadOf "https://github.com/" #      && pip install --no-cache-dir -r requirements.txt #      && rm -f /root/.gitconfig
# — и даже так .gitconfig снимается в той же RUN-строке, иначе он ляжет в слой.

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
