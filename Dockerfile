FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# System dependencies:
# - libjpeg-dev / zlib1g-dev: Pillow image processing
# - libpq-dev: psycopg2 (PostgreSQL adapter)
# - git: pip needs it to clone private dependencies (e.g.
#   ``ayla-ai-core @ git+https://github.com/...``). Without git in PATH
#   pip prints "Cannot find command 'git'" and the build fails.
RUN apt-get update && apt-get install -y \
    libjpeg-dev \
    zlib1g-dev \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Build-time secret for cloning private GitHub repos (ayla-ai-core).
# Pass via `docker compose build --build-arg GH_DEPLOY_TOKEN=...` or
# set in compose.yaml under web.build.args. URL-rewrite makes pip's
# git clone authenticate transparently. Token is consumed at build
# time only and is NOT baked into the final image (no COPY of secret
# files; the .gitconfig that `git config --global` writes lives in
# /root/.gitconfig but is not user-facing in runtime).
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
