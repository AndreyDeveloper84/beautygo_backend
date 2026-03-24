.DEFAULT_GOAL := help

# Python interpreter — override with: make test PYTHON=python3
PYTHON ?= python
MANAGE  = $(PYTHON) manage.py
DC      = docker compose

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
	@echo "  Docker (prod):"
	@echo "    make up             Start all containers"
	@echo "    make down           Stop all containers"
	@echo "    make build          Build Docker image"
	@echo "    make restart        Restart web container"
	@echo "    make logs           Follow all logs"
	@echo "    make logs-web       Follow web container logs"
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
