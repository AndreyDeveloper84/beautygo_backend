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

# OPTIONAL build-time credential for the ayla-ai-core dep.
#
# ayla-ai-core is PUBLIC (owner's decision 04.09.2026, recorded in
# OPEN_DECISIONS.md §22 in the workspace root, outside this repo), so this
# build needs no token: with the ARG empty the `if` below is skipped and
# pip clones the pinned SHA anonymously. Verified 04.09.2026 by an
# unauthenticated fetch of the pin.
#
# The token path is kept for the day the visibility is closed again (the
# decision says public "for now"): pass via
# `docker compose build --build-arg GH_DEPLOY_TOKEN=...` or set it in
# compose under web.build.args, and the URL-rewrite makes pip's git clone
# authenticate transparently. Consumed at build time only and NOT baked
# into the final image (no COPY of secret files; the .gitconfig that
# `git config --global` writes lives in /root/.gitconfig but is not
# user-facing at runtime).
ARG GH_DEPLOY_TOKEN=""
RUN if [ -n "$GH_DEPLOY_TOKEN" ]; then \
      git config --global url."https://${GH_DEPLOY_TOKEN}@github.com/".insteadOf "https://github.com/"; \
    fi

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
