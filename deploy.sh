#!/bin/bash
set -euo pipefail

PROJECT_DIR="/home/taximeter/beautygo/dev"
cd "$PROJECT_DIR"

echo "=== Pulling latest code ==="
git fetch origin dev
git reset --hard origin/dev

echo "=== Building and restarting containers ==="
docker compose build
docker compose up -d

echo "=== Waiting for services ==="
sleep 5
docker compose ps

echo "=== Checking application health ==="
curl -sf http://127.0.0.1:8000/api/docs/ > /dev/null && echo "App is healthy!" || echo "WARNING: App may not be responding yet"

echo "=== Deploy complete ==="
