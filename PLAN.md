# Ayla Backend — Implementation Plan

## Current State
- Django project in `djangoProject/` (CLAUDE.md expects `config/`)
- 1 app `users/` with User (BigAutoField), Service, Profile
- Basic JWT auth at `/api/auth/`
- Docker + CI/CD + pytest ready
- No Redis, Celery, external APIs

## Plan: 6 Phases

---

### Phase 1: Foundation Refactoring
Align existing code with CLAUDE.md architecture.

1. **Restructure project layout**
   - Rename `djangoProject/` → `config/` (Django settings package)
   - Create `apps/` directory, move `users/` → `apps/users/`
   - Update all imports, INSTALLED_APPS, manage.py, wsgi/asgi

2. **Switch to UUID primary keys**
   - Add `apps/core/` app with `BaseModel(models.Model)` — UUID pk, created_at, updated_at
   - Migrate User model to UUID pk
   - Migrate Service, Profile models

3. **Update URL structure**
   - Change from `/api/auth/` to `/api/v1/` prefix
   - Reorganize URL routing per CLAUDE.md spec

4. **Add X-App-Type middleware**
   - `apps/core/middleware.py` — validate X-App-Type header
   - Map endpoints to allowed app types

5. **Add core utilities**
   - `apps/core/exceptions.py` — standardized error responses
   - `apps/core/permissions.py` — IsClient, IsSpecialist, IsAdmin
   - `apps/core/pagination.py` — custom pagination with meta
   - `apps/core/mixins.py` — shared view mixins

6. **Update requirements**
   - Add: redis, celery, anthropic, yookassa, firebase-admin, requests
   - Split into `requirements/base.txt` + `requirements/dev.txt`

7. **Update docker-compose.yml**
   - Add Redis service
   - Add Celery worker + beat services

8. **Fix tests** — update all existing tests for new structure

---

### Phase 2: User System & Auth
Complete authentication per CLAUDE.md.

1. **Refactor User model**
   - UUID pk (from BaseModel)
   - phone as unique identifier (not username)
   - role: client / specialist / admin
   - is_verified field
   - Remove username-based auth

2. **Add ClientProfile model**
   - OneToOne to User (role=client)
   - Preferences, favorite specialists (M2M)

3. **Add OTPCode model**
   - phone, code, expires_at, is_used
   - Service: generate, verify, rate-limit

4. **OTP Auth flow**
   - POST `/api/v1/auth/login/` — send OTP to phone
   - POST `/api/v1/auth/verify-otp/` — verify → JWT tokens
   - POST `/api/v1/auth/register/` — register + send OTP
   - POST `/api/v1/auth/token/refresh/`
   - POST `/api/v1/auth/logout/`

5. **SMS integration** (SMS.RU)
   - `apps/users/services.py` — SMSService for OTP delivery

6. **User endpoints**
   - GET/PATCH `/api/v1/users/me/`
   - DELETE `/api/v1/users/me/`

7. **Tests** for all auth flows

---

### Phase 3: Specialists & Services
Core business domain.

1. **SpecialistProfile model**
   - OneToOne to User
   - display_name, bio, avatar, rating, reviews_count
   - latitude, longitude, address, city
   - is_accepting_clients

2. **Schedule model**
   - specialist FK, day_of_week, start_time, end_time
   - is_active flag

3. **ScheduleException model**
   - specialist FK, date, start_time, end_time
   - exception_type: day_off / custom_hours

4. **PortfolioItem model**
   - specialist FK, image, description

5. **ServiceCategory model**
   - name, slug, icon, parent (tree)

6. **Refactor Service model**
   - specialist FK → SpecialistProfile
   - category FK → ServiceCategory
   - duration_minutes, price, is_active

7. **Favorite model** (M2M through)
   - client FK, specialist FK, created_at

8. **Specialist endpoints**
   - GET `/api/v1/specialists/` — list with filters (city, category, rating, distance)
   - GET `/api/v1/specialists/{id}/` — detail
   - GET `/api/v1/specialists/{id}/services/`
   - GET `/api/v1/specialists/{id}/slots/` — available slots
   - GET `/api/v1/specialists/{id}/reviews/`
   - POST/DELETE `/api/v1/specialists/{id}/favorite/`

9. **Service endpoints**
   - GET `/api/v1/services/categories/`
   - GET `/api/v1/services/`

10. **Pro app endpoints** (X-App-Type: pro)
    - CRUD for specialist's own services
    - Schedule management
    - Portfolio management

11. **SlotCalculator service**
    - 30-min intervals, 1-hour minimum notice
    - Respects schedule + exceptions + existing appointments
    - Redis caching (60s TTL)

12. **Tests**

---

### Phase 4: Appointments & Reviews

1. **Appointment model**
   - client FK, specialist FK, service FK
   - start_datetime, end_datetime
   - status: pending → confirmed → in_progress → completed
   - price, notes, cancellation_reason

2. **BookingService**
   - Validate slot availability
   - Create appointment (transaction)
   - Status transitions with validation
   - Cancellation policy: free >24h, 50% 2-24h, 100% <2h

3. **Appointment endpoints**
   - POST `/api/v1/appointments/` — create booking
   - GET `/api/v1/appointments/{id}/`
   - POST `/api/v1/appointments/{id}/cancel/`
   - POST `/api/v1/appointments/{id}/reschedule/`
   - POST `/api/v1/appointments/{id}/complete/` (specialist only)

4. **Review model**
   - appointment FK (unique), client FK, specialist FK
   - rating (1-5), text, is_anonymous
   - Auto-update specialist.rating on save

5. **Review endpoints**
   - POST `/api/v1/reviews/`
   - Only after completed appointment

6. **Celery tasks**
   - `send_appointment_reminder` — 2h before
   - `auto_complete_appointments` — periodic
   - `update_specialist_rating` — on review

7. **Tests**

---

### Phase 5: Payments & Notifications

1. **Payment model**
   - appointment FK, amount, commission (8%)
   - status: pending / succeeded / cancelled / refunded
   - yookassa_payment_id, confirmation_url

2. **YooKassaService**
   - Create payment → return confirmation_url
   - Webhook handler (verify signature)
   - Refund logic

3. **Payment endpoints**
   - POST `/api/v1/payments/`
   - GET `/api/v1/payments/{id}/`
   - POST `/api/v1/payments/webhook/` (AllowAny)

4. **Notification model**
   - user FK, type, title, body, is_read
   - deep_link, data (JSON)

5. **DeviceToken model**
   - user FK, token, platform (ios/android), app_type (client/pro)

6. **NotificationTemplate registry**
   - Templates per CLAUDE.md spec
   - Different deep links for client vs pro

7. **PushService** (Firebase FCM)
8. **SMSService** (SMS.RU) — already in Phase 2, extend

9. **Notification endpoints**
   - GET `/api/v1/notifications/`
   - POST `/api/v1/notifications/{id}/read/`
   - POST `/api/v1/notifications/register-device/`

10. **Celery tasks** for async notification delivery

11. **Tests**

---

### Phase 6: AI Assistant

1. **Conversation model**
   - client FK, title, created_at, updated_at
   - is_active

2. **Message model**
   - conversation FK, role (user/assistant)
   - content, action_data (JSON)
   - created_at

3. **ClaudeService**
   - Anthropic SDK integration
   - System prompt with specialist context
   - Tool use: show_specialists, show_slots, confirm_booking
   - Message history management

4. **RecommendationEngine**
   - Build specialist context for Claude
   - Filter by city, category, availability
   - Format data for system prompt

5. **AI endpoints**
   - POST `/api/v1/ai/chat/` — send message, get AI response
   - GET `/api/v1/ai/conversations/`
   - GET `/api/v1/ai/conversations/{id}/`

6. **Tests** (mock Anthropic API)

---

## Execution Order

Phase 1 → Phase 2 → Phase 3 → Phase 4 → Phase 5 → Phase 6

Each phase is self-contained and produces working, tested code.
Estimated: ~30-40 files per phase, all with tests.
