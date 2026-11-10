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

# Build-time credential for the ayla-ai-core dependency (DRF-2996).
#
# ayla-ai-core is private again since 20.10.2026 (it was public 04.09–20.10,
# OPEN_DECISIONS.md §22), so `pip install` of the git+ pin needs a token.
# Until DRF-2996 the token travelled as `ARG GH_DEPLOY_TOKEN` and
# `git config --global` wrote it to /root/.gitconfig: the value showed in
# `docker history`, in `docker buildx history inspect` and in the image
# itself (single stage, no USER — the app could read the file). The
# comment that said "not baked into the runtime image" was wrong.
#
# Now the token is a BuildKit secret mounted for THIS RUN only
# (`--mount=type=secret,id=gh_deploy_token`, supplied by compose
# `build.secrets`) and reaches git through GIT_CONFIG_COUNT/KEY/VALUE —
# environment of one process, nothing on disk, nothing in a layer. pip
# spawns `git clone` as a subprocess and the subprocess inherits the
# variables. Without the secret (an empty or missing file) the clone is
# anonymous and fails on the private pin: an honest failure, not a quiet
# fallback. The log line says whether the secret was there, never its value.
#
# No `# syntax=` directive: it would pull a frontend image from Docker Hub,
# which the pilot box reaches badly (see the top of this file); the frontend
# built into Docker already supports secret mounts.
# Guard: tests/test_ai_core_token_build_secret.py.
COPY requirements.txt .
RUN --mount=type=secret,id=gh_deploy_token \
    if [ -s /run/secrets/gh_deploy_token ]; then \
      echo "gh_deploy_token: present"; \
      export GIT_CONFIG_COUNT=1 \
        GIT_CONFIG_KEY_0="url.https://x-access-token:$(tr -d '\r\n' < /run/secrets/gh_deploy_token)@github.com/.insteadOf" \
        GIT_CONFIG_VALUE_0="https://github.com/"; \
    else \
      echo "gh_deploy_token: absent — fetching ayla-ai-core anonymously"; \
    fi; \
    pip install --no-cache-dir -r requirements.txt

COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# DRF-2409 — каталог отчётов разбора услуг.
#
# `/app` принадлежит root, а контейнер идёт под `user:` (по умолчанию
# 1000:1000, docker-compose, DRF-1677), поэтому создать `/app/var` на ходу
# процесс НЕ может: на стенде `map_salon_services --store` падал
# PermissionError, а ручное создание каталога не пережило пересоздание
# контейнера. Каталог заводится в образе и отдаётся тому же uid.
#
# APP_UID/APP_GID — те же значения, что в compose; машина с другим
# владельцем дерева задаёт их при сборке ИЛИ переносит отчёты настройкой
# MAPPING_REPORT_DIR, не правя образ.
ARG APP_UID=1000
ARG APP_GID=1000
RUN mkdir -p /app/var/mapping_reports && chown -R ${APP_UID}:${APP_GID} /app/var

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
