# Social Auth API — Документация для фронтенда

> **Версия:** 1.0
> **Дата:** 2026-03-30
> **Base URL:** `/api/v1/auth`

---

## Содержание

1. [Обзор](#1-обзор)
2. [Авторизация через соцсеть](#2-авторизация-через-соцсеть)
3. [Привязка телефона](#3-привязка-телефона)
4. [Какой токен отправлять](#4-какой-токен-отправлять)
5. [Логика навигации на фронте](#5-логика-навигации-на-фронте)
6. [Коды ошибок](#6-коды-ошибок)
7. [Формат ответов](#7-формат-ответов)

---

## 1. Обзор

BeautyGO поддерживает авторизацию через 4 социальных провайдера:

| Провайдер | URL-параметр | SDK |
|-----------|-------------|-----|
| ВКонтакте | `vk` | VK SDK |
| Google | `google` | Google Sign-In |
| Apple | `apple` | Sign in with Apple |
| Яндекс | `yandex` | Yandex OAuth |

**Обязательный заголовок для всех запросов:**

```
X-App-Type: client    # 🟢 BeautyGO
X-App-Type: pro       # 🟣 BeautyGO Pro
```

Заголовок определяет роль нового пользователя:
- `client` → роль `client`
- `pro` → роль `specialist`

---

## 2. Авторизация через соцсеть

### `POST /api/v1/auth/social/{provider}/`

**Доступ:** Публичный (без авторизации)

### Параметры URL

| Параметр | Тип | Значения |
|----------|-----|----------|
| `provider` | string | `vk`, `google`, `apple`, `yandex` |

### Тело запроса

```json
{
  "token": "oauth_access_token_или_id_token",
  "device_id": "уникальный-id-устройства",
  "first_name": "Иван",
  "last_name": "Петров"
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|:------------:|----------|
| `token` | string | ✅ | OAuth token от провайдера (см. [раздел 4](#4-какой-токен-отправлять)) |
| `device_id` | string | ❌ | UUID устройства, до 255 символов. Сохраняется в JWT для идентификации |
| `first_name` | string | ❌ | Имя пользователя (макс. 150 символов). Используется для Apple — см. примечание ниже |
| `last_name` | string | ❌ | Фамилия пользователя (макс. 150 символов) |

> **Apple Sign In:** Apple отдаёт имя пользователя **только при первом входе**. Поэтому `first_name` и `last_name` необходимо передавать из ответа Apple SDK. При повторных входах Apple эти данные не возвращает.

### Успешный ответ — `200 OK`

```json
{
  "data": {
    "access": "eyJhbGciOiJIUzI1NiIs...",
    "refresh": "eyJhbGciOiJIUzI1NiIs...",
    "user": {
      "id": "550e8400-e29b-41d4-a716-446655440000",
      "phone": "+79001234567",
      "role": "client",
      "is_verified": true
    },
    "is_new_user": false,
    "phone_required": false
  }
}
```

| Поле | Тип | Описание |
|------|-----|----------|
| `access` | string | JWT access token (время жизни: 15 минут) |
| `refresh` | string | JWT refresh token (время жизни: 90 дней) |
| `user.id` | UUID | Уникальный ID пользователя |
| `user.phone` | string \| null | Телефон (null если не привязан) |
| `user.role` | string | `"client"` или `"specialist"` |
| `user.is_verified` | boolean | Подтверждён ли аккаунт |
| `is_new_user` | boolean | `true` — только что создан, `false` — существующий |
| `phone_required` | boolean | `true` — нужно привязать телефон |

### Пример запроса — VK

```bash
curl -X POST https://api.beautygo.ru/api/v1/auth/social/vk/ \
  -H "Content-Type: application/json" \
  -H "X-App-Type: client" \
  -d '{
    "token": "vk1.a.abc123...",
    "device_id": "550e8400-e29b-41d4-a716-446655440000"
  }'
```

### Пример запроса — Apple

```bash
curl -X POST https://api.beautygo.ru/api/v1/auth/social/apple/ \
  -H "Content-Type: application/json" \
  -H "X-App-Type: client" \
  -d '{
    "token": "eyJraWQiOiJX...",
    "device_id": "550e8400-e29b-41d4-a716-446655440000",
    "first_name": "Иван",
    "last_name": "Петров"
  }'
```

---

## 3. Привязка телефона

Вызывается когда `phone_required: true` в ответе social auth.

### Шаг 1: Отправить OTP на телефон

#### `POST /api/v1/auth/send-code/`

**Доступ:** Публичный

```json
{
  "phone": "+79001234567"
}
```

**Ответ `200 OK`:**
```json
{
  "data": {
    "message": "OTP sent"
  }
}
```

> Повторный запрос возможен через **60 секунд** (rate limit).

### Шаг 2: Подтвердить и привязать телефон

#### `POST /api/v1/auth/bind-phone/`

**Доступ:** Требуется авторизация (`Authorization: Bearer <access_token>`)

```json
{
  "phone": "+79001234567",
  "code": "123456"
}
```

| Поле | Тип | Обязательное | Описание |
|------|-----|:------------:|----------|
| `phone` | string | ✅ | Формат: `+7XXXXXXXXXX` или `8XXXXXXXXXX` (автоматически нормализуется в `+7`) |
| `code` | string | ✅ | OTP код, 4–6 цифр |

**Ответ `200 OK`:**
```json
{
  "data": {
    "message": "Phone bound successfully"
  }
}
```

### Пример полного flow

```bash
# 1. Запросить OTP
curl -X POST https://api.beautygo.ru/api/v1/auth/send-code/ \
  -H "Content-Type: application/json" \
  -H "X-App-Type: client" \
  -d '{"phone": "+79001234567"}'

# 2. Привязать номер (с токеном из social auth)
curl -X POST https://api.beautygo.ru/api/v1/auth/bind-phone/ \
  -H "Content-Type: application/json" \
  -H "X-App-Type: client" \
  -H "Authorization: Bearer eyJhbGciOiJIUzI1NiIs..." \
  -d '{"phone": "+79001234567", "code": "123456"}'
```

---

## 4. Какой токен отправлять

| Провайдер | Поле `token` | Откуда получить |
|-----------|-------------|-----------------|
| **VK** | `access_token` | VK SDK → `VKAccessToken.accessToken` |
| **Google** | `id_token` | Google Sign-In → `GIDAuthentication.idToken` |
| **Apple** | `id_token` | ASAuthorization → `appleIDCredential.identityToken` (JWT) |
| **Yandex** | `oauth_token` | Yandex SDK → `YandexLoginResult.token` |

---

## 5. Логика навигации на фронте

```
POST /auth/social/{provider}/
         │
         ▼
   Получен ответ
         │
         ├── phone_required: true
         │         │
         │         ▼
         │   Экран "Привязка телефона"
         │   POST /auth/send-code/
         │   POST /auth/bind-phone/
         │         │
         │         ▼
         │   is_new_user: true? ──► Экран "Завершение регистрации"
         │         │                 (заполнить имя/профиль)
         │         │ false
         │         ▼
         │   Главный экран
         │
         ├── is_new_user: true, phone_required: false
         │         │
         │         ▼
         │   Экран "Завершение регистрации"
         │         │
         │         ▼
         │   Главный экран
         │
         └── is_new_user: false, phone_required: false
                   │
                   ▼
             Главный экран
```

### Таблица решений

| `is_new_user` | `phone_required` | Действие |
|:-:|:-:|---|
| `false` | `false` | → Главный экран |
| `true` | `false` | → Экран завершения регистрации (имя, аватар) |
| `false` | `true` | → Экран привязки телефона → Главный экран |
| `true` | `true` | → Экран привязки телефона → Завершение регистрации |

---

## 6. Коды ошибок

### Social Auth

| Код | HTTP | Когда |
|-----|:----:|-------|
| `INVALID_PROVIDER` | 400 | Неизвестный провайдер (не vk/google/apple/yandex) |
| `SOCIAL_TOKEN_INVALID` | 401 | Невалидный или истекший токен от провайдера |
| `SOCIAL_AUTH_ERROR` | 400 | Общая ошибка social auth |

### Привязка телефона

| Код | HTTP | Когда |
|-----|:----:|-------|
| `PHONE_ALREADY_BOUND` | 400 | Номер уже привязан к другому аккаунту |
| `INVALID_OTP` | 400 | Неверный OTP код |
| `OTP_EXPIRED` | 400 | OTP код истёк (время жизни: 5 минут) |
| `MAX_ATTEMPTS_EXCEEDED` | 429 | Превышено кол-во попыток ввода кода (макс. 3) |
| `RATE_LIMITED` | 429 | Слишком частая отправка OTP (интервал: 60 сек) |

### Общие

| Код | HTTP | Когда |
|-----|:----:|-------|
| `VALIDATION_ERROR` | 400 | Невалидные данные в запросе (подробности в `details`) |

---

## 7. Формат ответов

### Успешный ответ

```json
{
  "data": { ... }
}
```

### Ошибка

```json
{
  "error": {
    "code": "ERROR_CODE",
    "message": "Описание ошибки",
    "details": {
      "field_name": ["Описание ошибки поля"]
    }
  }
}
```

> Поле `details` присутствует только при `VALIDATION_ERROR`.

### Пример ошибки валидации

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input",
    "details": {
      "token": ["This field is required."],
      "phone": ["Invalid phone format. Use +7XXXXXXXXXX"]
    }
  }
}
```

### Пример ошибки провайдера

```json
{
  "error": {
    "code": "SOCIAL_TOKEN_INVALID",
    "message": "Invalid or expired provider token"
  }
}
```
