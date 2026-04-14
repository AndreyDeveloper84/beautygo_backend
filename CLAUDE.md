# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## Project Overview

**beautygo_backend** is a Django 5.2 + Django REST Framework backend for a beauty-services marketplace. Users register as either `client` or `specialist`; specialists can publish `Service` offerings (name, price, duration). Authentication uses JWT via `djangorestframework-simplejwt`.

The project is in an early/prototype state: single app (`users`), SQLite, no tests, no CI.

## Tech Stack

- **Python / Django 5.2**
- **djangorestframework 3.16** with **simplejwt 5.5** for auth
- **django-filter 25.1** for query filtering
- **SQLite** (`db.sqlite3` committed at repo root) — development only
- Dependencies pinned in `requirements.txt`

Note: `requirements.txt` contains `django-restframework==0.0.1`, which is a stub/typosquat package. The real dependency is `djangorestframework`. Do not rely on `django-restframework`; leave it alone unless the user asks to clean it up.

## Repository Layout

```
beautygo_backend/
├── manage.py                  # Entry point — uses djangoProject.settings
├── requirements.txt
├── db.sqlite3                 # Dev database (committed)
├── djangoProject/             # ACTIVE Django project package
│   ├── settings.py            # DEBUG=True, SQLite, JWT auth, django_filters
│   ├── urls.py                # Mounts admin/ and api/auth/
│   ├── asgi.py / wsgi.py
│   └── __init__.py
├── users/                     # Only app — auth, users, services
│   ├── models.py              # User (AbstractUser) + Service
│   ├── serializers.py         # RegisterSerializer, ServiceSerializer
│   ├── views.py               # RegisterView, ServiceViewSet, IsSpecialist, ServiceFilter
│   ├── urls.py                # login/refresh/register + DRF router for services
│   ├── admin.py               # Registers User with default UserAdmin
│   ├── migrations/            # 0001_initial, 0002_service
│   └── tests.py               # Empty
└── backend/                   # STALE duplicate project skeleton (see below)
    ├── manage.py
    └── backend/
        ├── settings.py        # Older copy, missing django_filters
        ├── urls.py
        └── asgi.py / wsgi.py
```

### Important: two project packages

There are **two Django project packages** in the tree:

1. `djangoProject/` — **the real, active project.** `manage.py` at the repo root sets `DJANGO_SETTINGS_MODULE=djangoProject.settings`. All routes, JWT config, and `django_filters` live here.
2. `backend/backend/` — an older scaffold with `ROOT_URLCONF='backend.urls'`, no filter backend, and no `urls.py` wiring for the `users` app. It is **not** used by the root `manage.py` and appears to be leftover from a rename.

**When making changes, edit `djangoProject/` unless the user explicitly asks about `backend/`.** Keep the two in sync only if asked; otherwise treat `backend/` as dead code.

## Running the Project

```bash
pip install -r requirements.txt
python manage.py migrate
python manage.py createsuperuser        # optional, for /admin/
python manage.py runserver
```

Common management commands:

```bash
python manage.py makemigrations users
python manage.py migrate
python manage.py shell
python manage.py test                   # no tests exist yet
```

All commands must be run from the repo root so `djangoProject.settings` resolves correctly.

## API Surface

Base URL prefix: `/api/auth/` (mounted in `djangoProject/urls.py`).

| Method | Path                         | View                    | Auth            |
|--------|------------------------------|-------------------------|-----------------|
| POST   | `/api/auth/register/`        | `RegisterView`          | AllowAny        |
| POST   | `/api/auth/login/`           | `TokenObtainPairView`   | AllowAny        |
| POST   | `/api/auth/refresh/`         | `TokenRefreshView`      | AllowAny        |
| GET    | `/api/auth/services/`        | `ServiceViewSet.list`   | Specialist only |
| POST   | `/api/auth/services/`        | `ServiceViewSet.create` | Specialist only |
| GET/PUT/PATCH/DELETE | `/api/auth/services/{id}/` | `ServiceViewSet`  | Specialist only |
| *      | `/admin/`                    | Django admin            | Staff           |

### Registration payload
`username`, `password`, `email`, `phone`, `role` (`client` or `specialist`).

### Services
- `ServiceViewSet.get_queryset` is scoped to the requesting specialist (`Service.objects.filter(specialist=self.request.user)`), so specialists only see their own services.
- `perform_create` auto-assigns `specialist=request.user`; `specialist` is `read_only` on the serializer.
- `IsSpecialist` permission requires `request.user.role == 'specialist'`.
- `ServiceFilter` is defined in `users/views.py` but is **not currently attached** to `ServiceViewSet` via `filterset_class`. If a task requires filtering to actually work over HTTP, wire `filterset_class = ServiceFilter` onto the viewset.

## Data Model

`users/models.py`:

- **`User(AbstractUser)`** — custom user (`AUTH_USER_MODEL = 'users.User'`)
  - `role`: `client` | `specialist`
  - `phone`: optional string
- **`Service`**
  - `specialist`: FK → `settings.AUTH_USER_MODEL` (related_name `services`)
  - `name`, `description`, `price` (Decimal 10,2), `duration_minutes` (PositiveInt)
  - `created_at` (auto)

Any change to these models requires a new migration in `users/migrations/`. Never edit existing migration files; always `makemigrations` to create a new one.

## Conventions & Gotchas

- **Custom user model is already set**; do not import `django.contrib.auth.models.User`. Use `settings.AUTH_USER_MODEL` for FKs and `get_user_model()` in code.
- **Role checks** live in `IsSpecialist` (`users/views.py`). Add new role-based permissions there rather than sprinkling `request.user.role == ...` checks throughout views.
- **JWT is the only auth class.** Session auth is not enabled for DRF. Tests/curl calls must send `Authorization: Bearer <access_token>`.
- **DEBUG is True** and `SECRET_KEY` is committed in both settings files. This is a dev-only project; do not assume production hardening. Don't introduce secrets to the repo.
- **SQLite database file (`db.sqlite3`) is committed.** Treat it as dev fixture data; avoid clobbering it unless the user asks.
- **Russian-language strings** appear in user-facing responses (e.g., `"Регистрация прошла успешно"`). Preserve existing language when editing; don't silently translate.
- **`users/tests.py` is empty.** There is no test suite yet — if you add functionality, also add tests in this file (or split into `users/tests/`).
- **No linter / formatter config** (no `pyproject.toml`, `.flake8`, `ruff.toml`, `pre-commit`). Match surrounding style: 4-space indent, single quotes are common but double quotes also appear — stay consistent within a file.
- **No `.env` / settings split.** Settings are a single file. If environment-based config becomes necessary, discuss the approach before introducing `django-environ` or similar.

## Adding Features — Typical Flow

1. Edit or add models in `users/models.py`.
2. `python manage.py makemigrations users && python manage.py migrate`.
3. Add/extend serializers in `users/serializers.py`.
4. Add views in `users/views.py` (APIView for one-off endpoints, ViewSet + DRF router for CRUD).
5. Wire URLs in `users/urls.py` — function-style `path(...)` for APIViews, `router.register(...)` for ViewSets. The file appends `router.urls` at the bottom; keep that pattern.
6. Register new models in `users/admin.py` if they should be editable via `/admin/`.
7. If you add a new Django app, create it at the repo root, add it to `INSTALLED_APPS` in `djangoProject/settings.py`, and include its `urls.py` from `djangoProject/urls.py`.

## Git Workflow

- Default development branch for Claude-authored changes: **`claude/add-claude-documentation-0Hk53`** (or whatever branch the session specifies).
- Commit with clear, imperative messages. Create new commits rather than amending.
- Push with `git push -u origin <branch>`; never force-push or push to `main`/`master` without explicit instruction.
- Do **not** open pull requests unless the user explicitly asks.

## Things Worth Flagging to the User

If a task brings you near any of these, mention them — they look like latent issues, not intentional design:

- The duplicate `backend/backend/` project package (dead code).
- `django-restframework==0.0.1` in `requirements.txt` (stub package, not the real DRF).
- `ServiceFilter` exists but isn't attached to `ServiceViewSet`.
- `ServiceViewSet.get_queryset` scopes by `specialist=request.user`, which means clients (even if `IsSpecialist` were relaxed) would never see any services — filtering/listing of services for clients is not yet implemented.
- Committed `SECRET_KEY` and `db.sqlite3`.
- Empty `users/tests.py` — no automated test coverage.
