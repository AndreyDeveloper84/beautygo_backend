# Auth Endpoints — Финальная спецификация

> Роль пользователя определяется из `X-App-Type` header:
> - `X-App-Type: client` → role = `client`
> - `X-App-Type: pro` → role = `specialist`

---

## OTP Flow (основной)

### `POST /api/v1/auth/send-otp/`

Отправка OTP-кода на телефон. Единый endpoint для login и register.

**Headers:** `X-App-Type: client | pro`
**Auth:** Не требуется
**Rate limit:** 3 req/min per phone, 10 req/hour per IP

**Request:**
```json
{
  "phone": "+79001234567"
}
```

**Response 200:**
```json
{
  "data": {
    "retry_after": 60
  }
}
```

**Errors:**
| Code | HTTP | Описание |
|------|------|----------|
| `INVALID_PHONE` | 400 | Некорректный формат телефона |
| `RATE_LIMIT_EXCEEDED` | 429 | Слишком много запросов |

---

### `POST /api/v1/auth/verify-otp/`

Проверка OTP-кода. Если юзер существует — login. Если нет — создаёт User без имени, возвращает `is_new_user: true`.

**Headers:** `X-App-Type: client | pro`
**Auth:** Не требуется

**Request:**
```json
{
  "phone": "+79001234567",
  "code": "123456"
}
```

**Response 200 — существующий юзер:**
```json
{
  "data": {
    "access": "eyJ...",
    "refresh": "eyJ...",
    "is_new_user": false
  }
}
```

**Response 200 — новый юзер:**
```json
{
  "data": {
    "access": "eyJ...",
    "refresh": "eyJ...",
    "is_new_user": true
  }
}
```

При `is_new_user: true` клиент показывает экран регистрации и вызывает `POST /auth/register/`.

**Логика:**
1. Найти последний неиспользованный OTP для этого phone
2. Проверить: не истёк (5 мин), attempts < 5, is_used = false
3. Если код неверный — инкрементировать attempts
4. Если код верный — пометить is_used = true
5. Найти/создать User по phone (role из X-App-Type)
6. Выдать JWT

**Errors:**
| Code | HTTP | Описание |
|------|------|----------|
| `INVALID_CODE` | 400 | Неверный код |
| `CODE_EXPIRED` | 400 | Код истёк |
| `TOO_MANY_ATTEMPTS` | 400 | Превышено количество попыток |
| `CODE_NOT_FOUND` | 400 | Код не найден (не запрашивали) |

---

### `POST /api/v1/auth/register/`

Завершение регистрации для нового юзера. Вызывается после `verify-otp` с `is_new_user: true`.

**Headers:** `X-App-Type: client | pro`, `Authorization: Bearer <access>`
**Auth:** Требуется (JWT из verify-otp)

**Request:**
```json
{
  "first_name": "Анна",
  "last_name": "Иванова"
}
```

**Response 200:**
```json
{
  "data": {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "phone": "+79001234567",
    "first_name": "Анна",
    "last_name": "Иванова",
    "role": "client"
  }
}
```

**Логика:**
1. Роль уже задана при verify-otp из X-App-Type
2. Обновляет first_name, last_name
3. Помечает `is_verified = true`
4. Создаёт ClientProfile или SpecialistProfile в зависимости от role

**Errors:**
| Code | HTTP | Описание |
|------|------|----------|
| `ALREADY_REGISTERED` | 400 | Юзер уже завершил регистрацию |
| `VALIDATION_ERROR` | 400 | Невалидные данные |

---

## Token Management

### `POST /api/v1/auth/token/refresh/`

Обновление JWT access token.

**Auth:** Не требуется (refresh token в body)

**Request:**
```json
{
  "refresh": "eyJ..."
}
```

**Response 200:**
```json
{
  "data": {
    "access": "eyJ...",
    "refresh": "eyJ..."
  }
}
```

**Errors:**
| Code | HTTP | Описание |
|------|------|----------|
| `TOKEN_INVALID` | 401 | Невалидный refresh token |
| `TOKEN_EXPIRED` | 401 | Refresh token истёк |
| `TOKEN_BLACKLISTED` | 401 | Token в blacklist |

---

### `POST /api/v1/auth/logout/`

Инвалидация refresh token (blacklist).

**Auth:** `Authorization: Bearer <access>`

**Request:**
```json
{
  "refresh": "eyJ..."
}
```

**Response 200:**
```json
{
  "data": {
    "message": "Successfully logged out"
  }
}
```

---

## Social Auth

### `POST /api/v1/auth/social/{provider}/`

Providers: `google`, `apple`. `vk` и `yandex` — принимаются, но отвергаются с 403.

> **DRF-1245 — `vk` и `yandex` отключены.** Оба провайдера принимали голый `access_token` от клиента, а ни VK `users.get`, ни Yandex `login.yandex.ru/info` не сообщают, какому приложению этот токен выдан. Токен, полученный любым посторонним VK/Yandex-приложением, давал полноценный JWT Ayla; а так как `mobile_phone` (VK) и `default_email` / `default_phone` (Yandex) — самозаполняемые поля профиля, злоумышленник мог подставить телефон или почту жертвы и получить её аккаунт. Google пинует `aud` к `GOOGLE_CLIENT_ID`, Apple проверяет audience + issuer подписанного JWT — их это не касается. Запрос на `vk`/`yandex` отвечает `403 SOCIAL_PROVIDER_DISABLED`. Включать обратно — только после реальной привязки токена к приложению (VK `secure.checkToken` c `app_id`, Yandex — обмен authorization code). Денилист живёт в `settings.SOCIAL_AUTH_DISABLED_PROVIDERS`; env-переменная `SOCIAL_AUTH_DISABLED_PROVIDERS` может только **расширить** его (например, аварийно снять google/apple) — снять базовый `('vk','yandex')` она не может.

**Headers:** `X-App-Type: client | pro`
**Auth:** Не требуется

**Request (VK, Google, Yandex):**
```json
{
  "access_token": "..."
}
```

**Request (Apple):**
```json
{
  "id_token": "...",
  "authorization_code": "..."
}
```

**Response 200:**
```json
{
  "data": {
    "access": "eyJ...",
    "refresh": "eyJ...",
    "is_new_user": true
  }
}
```

При `is_new_user: true` — клиент показывает экран регистрации (`POST /auth/register/`).

**Логика:**
1. Валидировать token через API провайдера
2. Найти SocialAccount по provider + provider_id
3. Если найден — login, вернуть JWT
4. Если не найден — создать User (role из X-App-Type) + SocialAccount
5. Если phone из соцсети совпадает с существующим User — связать

**Errors:**
| Code | HTTP | Описание |
|------|------|----------|
| `INVALID_TOKEN` | 400 | Невалидный token провайдера |
| `PROVIDER_ERROR` | 502 | Ошибка API провайдера |
| `SOCIAL_PROVIDER_DISABLED` | 403 | Провайдер отключён (`vk`, `yandex` — DRF-1245) |
| `ACCOUNT_LINKED_TO_OTHER` | 409 | Соцсеть привязана к другому аккаунту |

---

## Сводная таблица

| Метод | Endpoint | Auth | X-App-Type | Описание |
|-------|----------|------|------------|----------|
| POST | `/api/v1/auth/send-otp/` | — | ⚪ да | Отправить OTP |
| POST | `/api/v1/auth/verify-otp/` | — | ⚪ да | Проверить OTP → JWT |
| POST | `/api/v1/auth/register/` | Bearer | ⚪ да | Завершить регистрацию |
| POST | `/api/v1/auth/token/refresh/` | — | — | Обновить access token |
| POST | `/api/v1/auth/logout/` | Bearer | — | Logout (blacklist refresh) |
| POST | `/api/v1/auth/social/vk/` | — | ⚪ да | ❌ Отключён (DRF-1245) → 403 |
| POST | `/api/v1/auth/social/google/` | — | ⚪ да | Вход через Google |
| POST | `/api/v1/auth/social/apple/` | — | ⚪ да | Вход через Apple |
| POST | `/api/v1/auth/social/yandex/` | — | ⚪ да | ❌ Отключён (DRF-1245) → 403 |

---

## Flow-диаграммы

### Новый клиент (BeautyGO 🟢)
```
App (X-App-Type: client)
  │
  ├─ POST /auth/send-otp/  {"phone": "+7..."}
  │  └─ 200 {"retry_after": 60}
  │
  ├─ POST /auth/verify-otp/  {"phone": "+7...", "code": "123456"}
  │  └─ 200 {"access": "...", "refresh": "...", "is_new_user": true}
  │
  └─ POST /auth/register/  {"first_name": "Анна"}   [Bearer token]
     └─ 200 {"id": "...", "role": "client", ...}
```

### Существующий мастер (BeautyGO Pro 🟣)
```
App (X-App-Type: pro)
  │
  ├─ POST /auth/send-otp/  {"phone": "+7..."}
  │  └─ 200 {"retry_after": 60}
  │
  └─ POST /auth/verify-otp/  {"phone": "+7...", "code": "123456"}
     └─ 200 {"access": "...", "refresh": "...", "is_new_user": false}
```

### Social Auth — новый юзер
```
App (X-App-Type: client)
  │
  ├─ POST /auth/social/google/  {"token": "..."}   (vk/yandex → 403, DRF-1245)
  │  └─ 200 {"access": "...", "refresh": "...", "is_new_user": true}
  │
  └─ POST /auth/register/  {"first_name": "Анна"}   [Bearer token]
     └─ 200 {"id": "...", "role": "client", ...}
```
