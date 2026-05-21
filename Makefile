.DEFAULT_GOAL := help

# Python interpreter — override with: make test PYTHON=python3
PYTHON ?= python
MANAGE  = $(PYTHON) manage.py
DC      = docker compose

# Local-dev compose invocation. Pulls in docker-compose.dev.yml (issue
# #421) which strips VPS-only bind mounts and switches the web/celery
# services to settings.dev. Bare `docker compose` (DC alone) keeps the
# VPS-tuned base file for CI deploy + production parity checks.
DC_DEV  = $(DC) -f docker-compose.yml -f docker-compose.dev.yml

# ─── Help ────────────────────────────────────────────────────────────────────

.PHONY: help
help:
	@echo ""
	@echo "BeautyGO — available commands:"
	@echo ""
	@echo "  Local dev:"
	@echo "    make run            Run dev server (localhost:8000)"
	@echo "    make shell          Django interactive shell"
	@echo "    make superuser      Create Django superuser"
	@echo ""
	@echo "  Database:"
	@echo "    make migrate        Apply migrations"
	@echo "    make migrations     Create new migrations"
	@echo "    make migrations APP=<app>  Create migrations for specific app"
	@echo ""
	@echo "  Testing & linting:"
	@echo "    make test           Run all tests"
	@echo "    make test-cov       Run tests with coverage report"
	@echo "    make test-app APP=<app>  Run tests for specific app"
	@echo "    make lint           Run flake8 linter"
	@echo ""
	@echo "  Docker (prod / VPS):"
	@echo "    make up             Start all containers"
	@echo "    make down           Stop all containers"
	@echo "    make build          Build Docker image"
	@echo "    make restart        Restart web container"
	@echo "    make logs           Follow all logs"
	@echo "    make logs-web       Follow web container logs"
	@echo ""
	@echo "  Docker (local dev — uses docker-compose.dev.yml override):"
	@echo "    make up-dev         Start full local-dev stack (web + db + redis + minio + worker + beat)"
	@echo "    make down-dev       Stop local-dev stack"
	@echo "    make worker         Start/restart celery_worker, follow logs"
	@echo "    make beat           Start/restart celery_beat, follow logs"
	@echo "    make logs-worker    Follow celery_worker logs"
	@echo "    make logs-beat      Follow celery_beat logs"
	@echo "    make celery-ping    Sanity-check: celery inspect ping against running worker"
	@echo ""

# ─── Local dev ───────────────────────────────────────────────────────────────

.PHONY: run
run:
	DJANGO_SETTINGS_MODULE=djangoProject.settings.dev $(MANAGE) runserver

.PHONY: shell
shell:
	DJANGO_SETTINGS_MODULE=djangoProject.settings.dev $(MANAGE) shell

.PHONY: superuser
superuser:
	DJANGO_SETTINGS_MODULE=djangoProject.settings.dev $(MANAGE) createsuperuser

# ─── Database ────────────────────────────────────────────────────────────────

.PHONY: migrate
migrate:
	DJANGO_SETTINGS_MODULE=djangoProject.settings.dev $(MANAGE) migrate

.PHONY: migrations
migrations:
	DJANGO_SETTINGS_MODULE=djangoProject.settings.dev $(MANAGE) makemigrations $(APP)

# ─── Testing & linting ───────────────────────────────────────────────────────

.PHONY: test
test:
	$(PYTHON) -m pytest -x -q

.PHONY: test-cov
test-cov:
	$(PYTHON) -m pytest --cov=. --cov-report=term-missing --cov-report=html -q

.PHONY: test-app
test-app:
	$(PYTHON) -m pytest $(APP)/tests/ -v

.PHONY: lint
lint:
	$(PYTHON) -m flake8 .

# ─── Docker ──────────────────────────────────────────────────────────────────

.PHONY: up
up:
	$(DC) up -d

.PHONY: down
down:
	$(DC) down

.PHONY: build
build:
	$(DC) build

.PHONY: restart
restart:
	$(DC) restart web

.PHONY: logs
logs:
	$(DC) logs -f

.PHONY: logs-web
logs-web:
	$(DC) logs -f web

# ─── Docker (local dev) ──────────────────────────────────────────────────────
#
# Targets below use DC_DEV (base + docker-compose.dev.yml). The override file
# lives in #421 — until that PR lands, these targets require the file to be
# present locally. CI / VPS deploy never invokes these targets.

.PHONY: up-dev
up-dev:
	$(DC_DEV) up -d

.PHONY: down-dev
down-dev:
	$(DC_DEV) down

# `make worker` brings up celery_worker (recreating it so a settings or
# env change is picked up) and then tails its logs in the same shell. Ctrl-C
# detaches from the log stream — the container keeps running until `down`.
.PHONY: worker
worker:
	$(DC_DEV) up -d --force-recreate celery_worker
	$(DC_DEV) logs -f celery_worker

.PHONY: beat
beat:
	$(DC_DEV) up -d --force-recreate celery_beat
	$(DC_DEV) logs -f celery_beat

.PHONY: logs-worker
logs-worker:
	$(DC_DEV) logs -f celery_worker

.PHONY: logs-beat
logs-beat:
	$(DC_DEV) logs -f celery_beat

# Sanity check the running worker actually answers Celery's inspect protocol.
# Requires `make up-dev` (or at least `make worker`) running. Output should
# list the worker's hostname under "OK".
.PHONY: celery-ping
celery-ping:
	$(DC_DEV) exec celery_worker celery -A djangoProject inspect ping
