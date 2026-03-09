# CLAUDE.md — BeautyGo Backend

## Project Overview

BeautyGo — платформа для бронирования бьюти-услуг. Django 5.2 + Django REST Framework + JWT-аутентификация. Клиенты ищут специалистов и бронируют услуги, специалисты управляют своими услугами, расписанием и записями.

## Tech Stack

- **Python 3**, **Django 5.2**, **Django REST Framework 3.16**
- **Auth**: JWT via `djangorestframework-simplejwt`
- **Filtering**: `django-filter`
- **Database**: SQLite (dev), PostgreSQL (prod-ready)

## Project Structure

```
beautygo_backend/
├── manage.py                  # Entry point (uses djangoProject.settings)
├── requirements.txt           # Python dependencies
├── db.sqlite3                 # Dev database (gitignored)
├── .gitignore
│
├── djangoProject/             # Django project config (ACTIVE)
│   ├── settings.py            # Settings: apps, auth, DRF, DB
│   ├── urls.py                # Root URLs → api/auth/ → users.urls
│   ├── wsgi.py / asgi.py
│
├── users/                     # Main app: all business logic
│   ├── models.py              # User, Category, SpecialistProfile, Service, Schedule, Booking, Review
│   ├── views.py               # ViewSets, permissions, filters
│   ├── serializers.py         # DRF serializers
│   ├── urls.py                # API routes (DefaultRouter + auth endpoints)
│   ├── admin.py               # All models registered
│   ├── migrations/
│
├── backend/                   # Legacy/alternate Django config (NOT USED)
│
└── pm-skills/                 # Claude Code PM plugin collection (external)
```

## Models & Relationships

```
User (AbstractUser)
  ├── role: 'client' | 'specialist'
  ├── phone: optional
  │
  ├─→ SpecialistProfile (1:1, auto-created on specialist registration)
  │     ├── bio, specialization, city, address, avatar
  │     ├── experience_years, is_available
  │     ├── categories (M2M → Category)
  │     └── @average_rating, @reviews_count (computed properties)
  │
  ├─→ Service (1:N, specialist creates services)
  │     ├── name, description, price, duration_minutes, is_active
  │     └── category (FK → Category, nullable)
  │
  ├─→ Schedule (1:N, specialist's weekly availability)
  │     ├── weekday (0=Mon..6=Sun), start_time, end_time
  │     └── unique_together: (specialist, weekday, start_time)
  │
  ├─→ Booking (client ←→ specialist through service)
  │     ├── date, start_time, end_time (auto-calculated)
  │     ├── status: pending → confirmed → completed | cancelled
  │     └── comment
  │
  └─→ Review (client reviews specialist after completed booking)
        ├── rating (1-5), text
        └── booking (1:1, must be status='completed')

Category
  ├── name (unique), description, icon
  ├── M2M → SpecialistProfile.categories
  └── 1:N → Service.category
```

## API Endpoints

All endpoints are prefixed with `/api/auth/`.

### Authentication (no auth required)
| Method | Path | Description |
|--------|------|-------------|
| POST | `/register/` | Register user (client or specialist) |
| POST | `/login/` | Get JWT access + refresh tokens |
| POST | `/refresh/` | Refresh access token |

### Categories (read: public, write: admin only)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/categories/` | List all categories |
| POST | `/categories/` | Create category (admin) |

### Specialists (read: public, write: specialist owner)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/specialists/` | List profiles (filter: city, specialization, category, is_available) |
| GET/PUT/PATCH | `/specialists/{id}/` | View/update profile |

### Services (read: public, write: specialist owner)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/services/` | List active services (filter: name, min_price, max_price, category, specialist, duration_minutes) |
| POST | `/services/` | Create service (specialist auto-assigned) |
| PUT/PATCH/DELETE | `/services/{id}/` | Manage own service |

### Schedules (read: public, write: specialist owner)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/schedules/?specialist={id}` | View specialist's schedule |
| POST | `/schedules/` | Add schedule slot |

### Bookings (authenticated users only)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/bookings/` | List own bookings (client sees theirs, specialist sees theirs) |
| POST | `/bookings/` | Create booking (end_time auto-calculated from service duration) |
| PATCH | `/bookings/{id}/confirm/` | Specialist confirms |
| PATCH | `/bookings/{id}/cancel/` | Client or specialist cancels |
| PATCH | `/bookings/{id}/complete/` | Specialist completes |

### Reviews (read: public, write: authenticated client)
| Method | Path | Description |
|--------|------|-------------|
| GET | `/reviews/?specialist={id}` | List reviews |
| POST | `/reviews/` | Create review (only for own completed bookings) |

## Permissions

| Class | Rule |
|-------|------|
| `IsSpecialist` | `user.is_authenticated and user.role == 'specialist'` |
| `IsClient` | `user.is_authenticated and user.role == 'client'` |

- **Public endpoints** (list/retrieve): categories, specialists, services, schedules, reviews
- **Specialist-only**: create/update/delete services, schedules; confirm/complete bookings
- **Client actions**: create bookings, create reviews
- **Admin-only**: manage categories

## Key Conventions

### Code Style
- All response messages in Russian (`"Регистрация прошла успешно"`, `"Вы можете редактировать только свой профиль."`)
- ViewSets with `DefaultRouter` for CRUD endpoints
- FilterSets defined inline in `views.py` (not separate file)
- Permissions defined inline in `views.py`
- All serializers in `serializers.py`, all models in `models.py`
- Single app architecture (`users/`) — all business logic lives here

### Auto-assignment Patterns
- `specialist` field auto-set from `request.user` in `perform_create()`
- `client` field auto-set from `request.user` in booking creation
- `end_time` auto-calculated from `service.duration_minutes`
- `SpecialistProfile` auto-created when specialist registers

### Queryset Filtering
- Specialists see only their own data for write operations
- Clients see only their own bookings
- Public endpoints show only `is_active=True` services

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py makemigrations
python manage.py migrate

# Run dev server
python manage.py runserver

# Create superuser
python manage.py createsuperuser

# Django system check
python manage.py check
```

## Settings Notes

- `AUTH_USER_MODEL = 'users.User'` — custom user model, cannot be changed after initial migration
- `DEBUG = True` — change for production
- `SECRET_KEY` is hardcoded — use env variable in production
- `ALLOWED_HOSTS = []` — configure for production
- SQLite for dev; switch to PostgreSQL for production

## pm-skills Directory

External Claude Code plugin collection (65 PM skills across 8 suites). Not part of the Django application. Contains product management tools: strategy, discovery, GTM, research, analytics, execution, marketing, toolkit.
