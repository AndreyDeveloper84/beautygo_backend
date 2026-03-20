# BeautyGO Backend

REST API для платформы бронирования бьюти-услуг BeautyGO.

## Технологии

- Python 3.12 + Django 5.2 + Django REST Framework 3.16
- PostgreSQL 16 (prod) / SQLite (dev)
- JWT-аутентификация (simplejwt)
- Minio (S3-совместимое хранилище для медиафайлов)
- Docker + docker-compose
- GitHub Actions CI/CD

## Быстрый старт (локально)

### 1. Клонировать репозиторий

```bash
git clone https://github.com/AndreyDeveloper84/beautygo_backend.git
cd beautygo_backend
```

### 2. Создать виртуальное окружение

```bash
python -m venv .venv

# Linux/Mac
source .venv/bin/activate

# Windows
.venv\Scripts\activate
```

### 3. Установить зависимости

```bash
pip install -r requirements.txt
```

### 4. Настроить переменные окружения

```bash
cp .env.example .env
```

### 5. Применить миграции и запустить

```bash
python manage.py migrate
python manage.py runserver
```

Сервер доступен по адресу: http://localhost:8000

## Быстрый старт (Docker)

### 1. Настроить переменные окружения

```bash
cp .env.prod.example .env
# Отредактируй .env — укажи DJANGO_SECRET_KEY и POSTGRES_PASSWORD
```

### 2. Запустить

```bash
docker-compose up -d
```

Это поднимет:
- **web** — Django + gunicorn (порт 8000)
- **db** — PostgreSQL 16
- **minio** — S3-хранилище (API: порт 9000, консоль: порт 9001)
- **minio-init** — создаёт бакет `beautygo-media` при первом запуске

### 3. Создать суперпользователя (опционально)

```bash
docker-compose exec web python manage.py createsuperuser
```

## Minio (S3-хранилище)

Все медиафайлы (аватары, фото) хранятся в Minio.

- **Веб-консоль**: http://localhost:9001
- **Логин**: minioadmin / minioadmin (dev)
- **Бакет**: `beautygo-media`

## API документация

После запуска сервера:

- **Swagger UI**: http://localhost:8000/api/docs/
- **ReDoc**: http://localhost:8000/api/redoc/
- **OpenAPI Schema**: http://localhost:8000/api/schema/

## Основные эндпоинты

```
POST   /api/v1/auth/register/       — Регистрация (телефон + роль)
POST   /api/v1/auth/login/          — Отправка OTP на телефон
POST   /api/v1/auth/verify-otp/     — Проверка OTP → JWT токены
POST   /api/v1/auth/token/refresh/  — Обновление access токена
POST   /api/v1/auth/logout/         — Выход (blacklist refresh)

GET    /api/v1/auth/profile/me/     — Мой профиль
PATCH  /api/v1/auth/profile/me/     — Обновить профиль

GET    /api/v1/auth/services/       — Список услуг
POST   /api/v1/auth/services/       — Создать услугу (мастер)

GET    /api/v1/health/              — Health check
```

Все запросы к API требуют заголовок `X-App-Type: client` или `X-App-Type: pro`.

## Тестирование

```bash
# Все тесты
pytest

# С подробным выводом
pytest -v

# Конкретный файл
pytest users/tests/test_views.py
```

## CI/CD

GitHub Actions запускается автоматически:
- **На каждый PR в main** — lint (flake8) + тесты (pytest)
- **На push в dev** — lint + тесты + деплой на dev-сервер

## Структура проекта

```
├── djangoProject/          # Настройки Django
│   ├── settings/
│   │   ├── base.py         # Общие настройки
│   │   ├── dev.py          # Development (SQLite, CORS open)
│   │   └── prod.py         # Production (PostgreSQL, security)
│   └── urls.py             # Маршрутизация
├── users/                  # Аутентификация, профили, услуги
│   ├── models.py           # User, Service, Profile, OTPCode
│   ├── views.py            # API views
│   ├── serializers.py      # DRF serializers
│   ├── services.py         # Бизнес-логика (OTP, Auth)
│   ├── middleware.py        # AppType + JWT middleware
│   └── tests/              # Тесты (pytest)
├── docs/                   # Документация API
├── docker-compose.yml      # Docker окружение
├── Dockerfile
├── requirements.txt
└── CLAUDE.md               # Инструкции для AI-ассистента
```

## Лицензия

Проприетарный код. Все права защищены.
