# План: Исправление Auth-модуля

## Контекст
Auth-модуль сейчас использует username+password (стандартный Django), а спека требует
phone+OTP и social auth. Начинаем с мелких правок, заканчиваем крупными.

---

## Шаг 1: Мелкие правки (без миграций, без новых моделей)

### 1.1 — Добавить endpoint `POST /auth/logout/`
- Инвалидирует refresh token (используем `rest_framework_simplejwt.token_blacklist`)
- Добавить `rest_framework_simplejwt.token_blacklist` в INSTALLED_APPS
- View: принимает `refresh` token, добавляет в blacklist
- URL: `path('logout/', LogoutView.as_view())`

### 1.2 — Разнести URL-маршруты по смыслу
Сейчас services и profile живут под `/api/v1/auth/` — это неправильно.
- `POST /api/v1/auth/register/` — оставить
- `POST /api/v1/auth/login/` — оставить
- `POST /api/v1/auth/refresh/` — оставить
- `POST /api/v1/auth/logout/` — добавить
- `GET/PATCH /api/v1/users/me/` — перенести из auth
- `GET /api/v1/users/{id}/` — перенести из auth (profile)
- Services — временно оставить, позже переедут в отдельное приложение

**Изменения файлов:**
- `djangoProject/urls.py` — добавить `path('api/v1/users/', include('users.urls_users'))`
- `users/urls.py` — оставить только auth endpoints
- `users/urls_users.py` (новый) — profile endpoints

### 1.3 — Формат ответов по спеке
Сейчас: `{"message": "..."}` или голый DRF.
Нужно: `{"data": {...}}` для успеха, `{"error": {"code": "...", "message": "..."}}` для ошибок.
- Создать `core/` app с exception handler и response wrapper
- Настроить `EXCEPTION_HANDLER` в REST_FRAMEWORK

---

## Шаг 2: Модель User — phone как primary identifier

### 2.1 — Сделать phone обязательным и unique
- `phone = CharField(max_length=20, unique=True)` — основной идентификатор
- `USERNAME_FIELD = 'phone'`
- `REQUIRED_FIELDS = ['first_name']`
- Убрать обязательность username (сгенерировать автоматически или убрать)
- Новая миграция

### 2.2 — Модель OTPCode
```python
class OTPCode(models.Model):
    phone = CharField(max_length=20, db_index=True)
    code = CharField(max_length=6)
    created_at = DateTimeField(auto_now_add=True)
    expires_at = DateTimeField()
    is_used = BooleanField(default=False)
    attempts = PositiveIntegerField(default=0)  # защита от перебора
```

---

## Шаг 3: OTP Auth Flow (основная логика)

### 3.1 — Endpoint `POST /auth/send-otp/`
Объединяет register + login:
- Принимает `{"phone": "+79001234567"}`
- Генерирует 6-значный код
- Сохраняет OTPCode
- Отправляет SMS (заглушка на dev, SMS.RU на prod)
- Rate limit: max 3 запроса в минуту на номер
- Ответ: `{"data": {"retry_after": 60}}`

### 3.2 — Endpoint `POST /auth/verify-otp/`
- Принимает `{"phone": "+79001234567", "code": "123456"}`
- Проверяет код (не истёк, не использован, attempts < 5)
- Если юзера нет — создаёт (role определяется позже или через query param)
- Возвращает JWT (access + refresh)
- Помечает OTP как is_used=True

### 3.3 — Обновить `POST /auth/register/`
Оставить для явной регистрации с указанием роли:
- Принимает `{"phone": "...", "code": "...", "role": "client|specialist", "first_name": "..."}`
- Проверяет OTP
- Создаёт User с ролью
- Возвращает JWT

### 3.4 — SMS сервис (абстракция)
```python
class BaseSMSService(ABC):
    def send(self, phone: str, message: str) -> bool: ...

class ConsoleSMSService(BaseSMSService):  # dev
class SMSRuService(BaseSMSService):       # prod
```

---

## Шаг 4: Social Auth

### 4.1 — Endpoint `POST /auth/social/{provider}/`
- Providers: `vk`, `google`, `apple`, `yandex`
- Принимает `{"access_token": "...", "role": "client"}` (role при первом входе)
- Валидирует token через API провайдера
- Создаёт/находит User
- Возвращает JWT + `{"is_new_user": true/false}`

### 4.2 — Модель SocialAccount
```python
class SocialAccount(models.Model):
    user = ForeignKey(User)
    provider = CharField(choices=['vk','google','apple','yandex'])
    provider_id = CharField()  # ID у провайдера
    extra_data = JSONField(default=dict)

    class Meta:
        unique_together = ('provider', 'provider_id')
```

### 4.3 — Связка аккаунтов
- Если юзер залогинен и привязывает соцсеть: `POST /auth/social/{provider}/link/`
- Если phone из соцсети совпадает с существующим User — автоматический merge
- Apple Sign In: сохранять email при первом входе (потом Apple его не отдаёт)

---

## Шаг 5: Защита и безопасность

### 5.1 — Rate limiting
- django-ratelimit или DRF throttling
- `/auth/send-otp/`: 3 req/min per phone, 10 req/hour per IP
- `/auth/verify-otp/`: 5 попыток на код, затем блокировка на 30 мин
- `/auth/social/`: 10 req/min per IP

### 5.2 — Token management
- `POST /auth/token/refresh/` — уже есть, rotate включен ✅
- Blacklist после ротации — уже настроен ✅
- Добавить проверку device fingerprint (опционально, позже)

---

## Порядок реализации

| # | Задача | Сложность | Зависимости |
|---|--------|-----------|-------------|
| 1 | Logout endpoint | Мелкая | — |
| 2 | Разнести URL routes | Мелкая | — |
| 3 | Core app + response format | Мелкая | — |
| 4 | User model: phone as primary | Средняя | Миграция |
| 5 | OTPCode модель | Средняя | #4 |
| 6 | SMS сервис (заглушка) | Мелкая | — |
| 7 | send-otp + verify-otp endpoints | Средняя | #4, #5, #6 |
| 8 | Обновить register | Средняя | #7 |
| 9 | SocialAccount модель | Средняя | #4 |
| 10 | Social auth endpoints | Средняя | #9 |
| 11 | Rate limiting | Мелкая | #7, #10 |

---

## Что НЕ трогаем сейчас
- Services CRUD — переедет в отдельный app позже
- Profile → SpecialistProfile/ClientProfile — отдельная задача
- X-App-Type middleware — отдельная задача
- UUID primary keys — слишком много миграций, отдельная задача
