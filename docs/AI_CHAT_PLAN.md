# AI Chat Endpoint — Implementation Plan

> Status: PLANNED · Author: Andrey + Claude · Date: 2026-04-26 (rev. 2 — aligned to API Spec v2.0)
> Foundation: `ai/services/llm_client.py` + `ai/redaction.py` (PR #26, merged)
> Target: M5 Penza pilot (2026-07-15)
> **Spec source of truth:** Notion → "API Specification v2.0 — Ayla" §🤖 AI ASSISTANT

## Scope

Conversational AI assistant для 🟢 Ayla (client app). Пользователь пишет
свободный запрос — LLM возвращает текст + опционально структурированное
действие. Подтверждение действия идёт через отдельный endpoint, который
выполняет side-effect (например, создаёт `Appointment` с `source=ai`).

### Что входит в MVP
- `Conversation` + `Message` модели с FK на `User` или `AnonymousSession`
- 5 endpoints (per spec v2.0 §AI ASSISTANT)
- 5 action types: `show_specialists`, `show_slots`, `confirm_booking`, `show_appointments`, `ask_clarification`
- PII redaction перед каждым OpenAI вызовом
- Rate limit (DRF scope) + daily token guardrail + anonymous message cap

### Что НЕ входит (deferred per M4 scope reduction, memory `project_m4_scope_reduction.md`)
- `voice_mode` / `voice_response` action — Phase 7+ (Voice token deferred)
- `collect_context` action — Phase 6 (UserPersonalContext deferred)
- Streaming (SSE) — sync ответ
- Multi-LLM router (только OpenAI gpt-4o-mini)
- Long-term user memory (persona / preferences storage)

Enum `AIActionType` остаётся расширяемым (DB stores raw string), чтобы
будущие action types не требовали миграций.

## Locked decisions (2026-04-26)

| # | Decision | Choice |
|---|---|---|
| 1 | History retention | Храним все Message в БД, в LLM context передаём последние 10 |
| 2 | Specialist context source | Top-20 по городу клиента, фильтр rating ≥ 4.0 |
| 3 | Anonymous chat | Разрешён, лимит 5 user-messages до `verify-otp` (merge сессии при OTP-входе) |
| 4 | Streaming | Sync only в MVP, SSE — Phase 6 |
| 5 | confirm_booking flow | Booking создаётся **на бэке** через `/ai/chat/{id}/action/`, **не** прямым POST /appointments/ с фронта (audit + idempotency + source=ai) |

## Spec compliance map

| Spec endpoint | Реализация в плане |
|---|---|
| `POST /ai/chat` | `POST /api/v1/ai/chat/` |
| `POST /ai/chat/{conversation_id}/action` | `POST /api/v1/ai/chat/{conversation_id}/action/` |
| `GET /ai/conversations` | `GET /api/v1/ai/conversations/` |
| `GET /ai/conversations/{id}` | `GET /api/v1/ai/conversations/{id}/` |
| `DELETE /ai/conversations/{id}` | `DELETE /api/v1/ai/conversations/{id}/` |

**Trailing slashes** — Django convention, per API Contract Audit 2026-04-13 §M8.
**Response envelope** — все ответы wrapped в `{data: {...}}` через
`success_response()` (per audit §M3).

## Models

### `ai/models.py`

```python
class Conversation(BaseModel):
    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE,
        null=True, blank=True, related_name="conversations",
    )
    anonymous_session = models.ForeignKey(
        "users.AnonymousSession", on_delete=models.CASCADE,
        null=True, blank=True, related_name="conversations",
    )
    is_active = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-last_message_at"]),
            models.Index(fields=["anonymous_session", "-last_message_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=(models.Q(user__isnull=False) | models.Q(anonymous_session__isnull=False)),
                name="conversation_owner_required",
            ),
        ]


class Message(BaseModel):
    class Role(models.TextChoices):
        USER = "user"
        ASSISTANT = "assistant"
        TOOL = "tool"
        SYSTEM = "system"  # reserved for future

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    # action attached to this assistant message (per spec AIMessageFull.action)
    action_type = models.CharField(max_length=32, blank=True, default="")
    action_data = models.JSONField(null=True, blank=True)
    # raw LLM tool_call (for debugging / audit)
    tool_call = models.JSONField(null=True, blank=True)
    tool_call_id = models.CharField(max_length=64, blank=True, default="")
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    latency_ms = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["conversation", "created_at"])]
        ordering = ["created_at"]
```

**Migration:** `ai/migrations/0001_initial.py` — auto-generated.

## Endpoints

| Method | Path | Auth | Throttle | Description |
|---|---|---|---|---|
| POST | `/api/v1/ai/chat/` | JWT (user OR anon) | `ai_chat` | Send message, get response + optional action |
| POST | `/api/v1/ai/chat/{id}/action/` | JWT (owner) | `ai_chat` | Confirm/reject AI action; performs side-effect |
| GET | `/api/v1/ai/conversations/` | JWT (user only, не anon) | `user` | List conversations (paginated) |
| GET | `/api/v1/ai/conversations/{id}/` | JWT (owner) | `user` | Full conversation with embedded messages |
| DELETE | `/api/v1/ai/conversations/{id}/` | JWT (owner) | `user` | Delete conversation |

**X-App-Type:** `client` only. Pro app не использует AI chat → 403 `WRONG_APP_TYPE`.

### POST /api/v1/ai/chat/

**Request:**
```json
{
  "conversation_id": "uuid (optional — creates new if missing)",
  "message": "Хочу записаться на маникюр в эту субботу",
  "context": {
    "location": {"lat": 53.2007, "lon": 45.0046},
    "preferred_date": "2026-05-02",
    "preferred_time": "afternoon"
  }
}
```

`context` — все поля optional. Используется как hint для системного промпта
и `SpecialistContextBuilder`. `voice_mode` принимаем (для backward compat
с фронтом, который читает spec v2.0), но игнорируем — vision/voice deferred.

**Response 200:**
```json
{
  "data": {
    "conversation_id": "uuid",
    "message": {
      "id": "uuid",
      "role": "assistant",
      "content": "Нашла 3 мастеров с рейтингом 4.5+ в субботу...",
      "created_at": "2026-04-26T14:00:00Z"
    },
    "action": {
      "type": "show_specialists",
      "data": {
        "specialists": [
          {
            "specialist": { "...": "SpecialistListItem" },
            "match_score": 92,
            "match_reasons": ["Высокий рейтинг", "Свободна в субботу"]
          }
        ],
        "explanation": "Топ по рейтингу + свободны в выбранный день"
      }
    }
  }
}
```

### POST /api/v1/ai/chat/{conversation_id}/action/

**Request:**
```json
{
  "action_type": "confirm_booking",
  "confirmed": true,
  "data": {
    "specialist_id": "uuid",
    "service_id": "uuid",
    "datetime": "2026-05-02T14:00:00Z"
  }
}
```

**Response 200 (confirm_booking success):**
```json
{
  "data": {
    "success": true,
    "result": {
      "appointment": { "...": "Appointment" },
      "next_message": {
        "id": "uuid",
        "role": "assistant",
        "content": "Записала вас на 2 мая, 14:00. Я отправлю напоминание за час.",
        "created_at": "..."
      }
    }
  }
}
```

**Response 200 (other action types):**
```json
{
  "data": {
    "success": true,
    "result": {
      "next_message": { "...": "AIMessage" },
      "next_action": { "...": "AIAction" }
    }
  }
}
```

**Action handlers:**

| `action_type` | Side-effect | Result |
|---|---|---|
| `confirm_booking` (confirmed=true) | Вызывает `CreateBookingService` с `source=ai`, idempotency_key = `f"ai-{conversation_id}-{datetime.isoformat()}"` | `appointment` + `next_message` |
| `confirm_booking` (confirmed=false) | Сохраняет user-decline сообщение, LLM возвращает альтернативу | `next_message` + `next_action` |
| `show_specialists` (selected_specialist_id) | Записывает выбор в conversation context, LLM предлагает услугу/слот | `next_message` + `next_action` (likely `show_slots`) |
| `ask_clarification` (answer) | Сохраняет ответ как user message, LLM продолжает диалог | `next_message` + `next_action?` |

### GET /api/v1/ai/conversations/

**Query:** `?page=1&page_size=20`

**Response 200:**
```json
{
  "data": {
    "results": [
      {
        "id": "uuid",
        "preview": "Хочу записаться на маникюр...",
        "messages_count": 6,
        "last_message_at": "2026-04-26T14:05:00Z",
        "created_at": "2026-04-26T13:50:00Z"
      }
    ],
    "count": 12,
    "page": 1,
    "page_size": 20
  }
}
```

`preview` — first user message, truncated to 80 chars.

**Anonymous users:** 403 (per spec — list только для authenticated).

### GET /api/v1/ai/conversations/{id}/

**Response 200 (per spec — embedded messages, не paginated):**
```json
{
  "data": {
    "id": "uuid",
    "messages": [
      {
        "id": "uuid",
        "role": "user",
        "content": "Хочу маникюр в субботу",
        "created_at": "..."
      },
      {
        "id": "uuid",
        "role": "assistant",
        "content": "Нашла 3 мастеров...",
        "action": { "type": "show_specialists", "data": {} },
        "created_at": "..."
      }
    ],
    "created_at": "..."
  }
}
```

**Trim policy:** если messages > 100 → возвращаем последние 100 + `truncated: true`. Полная пагинация — TODO Phase 6.

### DELETE /api/v1/ai/conversations/{id}/

**Response 204** — soft delete (`is_active=false`, `deleted_at=now()`). Hard delete — Phase 6 cleanup task.

### Errors (per spec v2.0 + audit conventions)

| HTTP | error.code | When |
|---|---|---|
| 400 | `INVALID_ACTION_TYPE` | unknown action_type в `/action/` |
| 401 | `NOT_AUTHENTICATED` | no JWT |
| 403 | `WRONG_APP_TYPE` | `X-App-Type: pro` |
| 403 | `NOT_OWNER` | conversation принадлежит другому юзеру |
| 404 | `CONVERSATION_NOT_FOUND` | id не существует |
| 409 | `SLOT_NOT_AVAILABLE` | при `confirm_booking` слот занят (forwarded из CreateBookingService) |
| 429 | `RATE_LIMITED` | minute throttle ИЛИ daily limit ИЛИ anon cap (различаем через `details.reason`) |
| 503 | `AI_UNAVAILABLE` | `OPENAI_API_KEY` пуст или OpenAI down |

**`details.reason`** для 429:
- `"minute_throttle"` — DRF scope `ai_chat`
- `"daily_token_limit"` — Redis daily counter
- `"anon_message_limit"` — anonymous > 5 messages

## Pipeline

### `ai/application/services/chat_service.py` (POST /ai/chat/)

```
ChatService.send_message(actor, conversation_id|None, message_text, context)
  ├── 1. resolve_conversation()
  │      → existing OR create new (FK на user или anonymous_session)
  │
  ├── 2. check_anonymous_limit()        → 429 RATE_LIMITED (anon_message_limit)
  │      anonymous + count(messages where role=user) >= 5
  │
  ├── 3. check_daily_limit()            → 429 RATE_LIMITED (daily_token_limit)
  │      Redis: ai:tokens:{actor_id}:{YYYY-MM-DD} >= AI_MAX_TOKENS_PER_USER_PER_DAY
  │
  ├── 4. save_user_message(content=raw)  # raw в БД, redacted в OpenAI
  │
  ├── 5. build_llm_context()
  │      ├── recent_messages = last 10 from this conversation
  │      ├── specialist_context = SpecialistContextBuilder(actor, ctx).top(20)
  │      │     → city + rating ≥ 4.0, optionally next-available date filter
  │      └── system_prompt = render_template(specialist_context, actor, today)
  │
  ├── 6. call_llm()
  │      ├── messages = [system] + [redact_pii(m) for m in recent_messages] + [redacted_user_msg]
  │      ├── tools = TOOL_DEFINITIONS  (5 tools)
  │      └── resp = client.chat.completions.create(model=OPENAI_MODEL, messages, tools)
  │
  ├── 7. parse_response()
  │      ├── if resp.tool_calls:
  │      │     tool_call = resp.tool_calls[0]
  │      │     action_data = ToolHandler.handle(tool_call, actor)  # validates, no side-effect
  │      └── content = resp.choices[0].message.content
  │
  ├── 8. save_assistant_message(content, action_type, action_data, tokens, latency)
  │
  ├── 9. update_counters()
  │      ├── conversation.last_message_at = now
  │      └── redis.incrby(daily_tokens_key, tokens_total, ex=86400)
  │
  └── 10. return ChatResponseDTO(message, action)
```

### `ai/application/services/action_service.py` (POST /ai/chat/{id}/action/)

```
ActionService.execute(conversation, action_type, confirmed, data)
  ├── 1. validate_owner(conversation, actor)  → 403 NOT_OWNER if mismatch
  │
  ├── 2. dispatch by action_type:
  │
  │   confirm_booking + confirmed=true:
  │     ├── idempotency_key = f"ai-{conversation.id}-{datetime}"
  │     ├── appointment = CreateBookingService.execute(
  │     │       client_id=actor.id,
  │     │       specialist_id=data.specialist_id,
  │     │       service_id=data.service_id,
  │     │       start_datetime=data.datetime,
  │     │       source="ai",
  │     │       idempotency_key=idempotency_key,
  │     │   )
  │     │   → 409 SLOT_NOT_AVAILABLE forwarded from domain
  │     ├── save_user_message("✓ Подтверждаю запись")
  │     ├── follow_up = generate_followup(conversation, appointment)
  │     │   → calls LLM with confirmation context, returns short message
  │     └── return {appointment, next_message: follow_up}
  │
  │   confirm_booking + confirmed=false:
  │     ├── save_user_message("Нет, не подходит")
  │     └── follow_up = generate_alternatives(conversation)  → next LLM turn
  │         return {next_message, next_action: maybe show_slots(other date)}
  │
  │   show_specialists + selected_specialist_id:
  │     ├── save_user_message(f"Выбираю: {specialist.name}")
  │     └── follow_up = generate_service_selection(conversation, specialist)
  │         return {next_message, next_action: show_slots(specialist, services)}
  │
  │   ask_clarification + answer:
  │     ├── save_user_message(answer)
  │     └── follow_up = chat_service.continue_conversation(conversation)
  │         return {next_message, next_action?}
  │
  └── 3. update conversation.last_message_at
```

## Tools (`ai/tools.py`)

5 tool definitions для OpenAI `chat.completions.create(tools=[...])`:

```python
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "show_specialists",
            "description": "Show recommended specialists with match reasons.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialist_ids": {"type": "array", "items": {"type": "string"}},
                    "match_scores": {"type": "array", "items": {"type": "integer"}},
                    "match_reasons": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                    "explanation": {"type": "string"},
                },
                "required": ["specialist_ids", "explanation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_slots",
            "description": "Show available time slots for a specialist + service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialist_id": {"type": "string"},
                    "service_id": {"type": "string"},
                    "date": {"type": "string", "format": "date"},
                },
                "required": ["specialist_id", "service_id", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_booking",
            "description": "Show booking confirmation card. Does NOT create the appointment — user confirms via separate action endpoint.",
            "parameters": {
                "type": "object",
                "properties": {
                    "specialist_id": {"type": "string"},
                    "service_id": {"type": "string"},
                    "datetime": {"type": "string", "format": "date-time"},
                },
                "required": ["specialist_id", "service_id", "datetime"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_appointments",
            "description": "Show user's existing appointments.",
            "parameters": {
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "enum": ["upcoming", "past", "all"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_clarification",
            "description": "Ask user a clarifying question with optional suggested answers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
            },
        },
    },
]
```

### Tool handlers (`ai/tools_handlers.py`)

Каждый handler — **только валидация и data shaping**, без side-effect.

- `handle_show_specialists(args, actor)` — проверяет, что все `specialist_ids` существуют и видимы для actor (city + rating filter), enriches с `SpecialistListItem` shape (per spec).
- `handle_show_slots(args, actor)` — вызывает `AvailabilityQueryService.get_available_slots(...)` и возвращает `slots[]` per `ShowSlotsData` shape.
- `handle_confirm_booking(args, actor)` — резолвит `specialist_name`, `service_name`, `address`, `price` из БД, возвращает `ConfirmBookingData` shape. **НЕ создаёт booking.**
- `handle_show_appointments(args, actor)` — вызывает `Appointment.objects.filter(client=actor, ...)` с фильтром per `args.filter`.
- `handle_ask_clarification(args, actor)` — pass-through (just shape).

## System prompt (`ai/prompts.py`)

```python
SYSTEM_PROMPT_TEMPLATE = """\
Ты — Ayla, AI-ассистент для записи к мастерам красоты.

КОНТЕКСТ:
- Город клиента: {city}
- Имя: {first_name}
- Сегодня: {today}
- Геолокация: {location_hint}

ДОСТУПНЫЕ МАСТЕРА (top-20 в городе, rating ≥ 4.0):
{specialists_summary}

ПРАВИЛА:
1. Отвечай на русском, дружелюбно, кратко (2-3 предложения).
2. Задавай уточняющие вопросы через `ask_clarification` если запрос неясен.
3. Используй `show_specialists` чтобы показать список.
4. Используй `show_slots` чтобы показать слоты.
5. Используй `confirm_booking` ТОЛЬКО когда клиент явно выбрал мастера + услугу + время.
6. После confirm_booking ЖДИ подтверждения — не создавай запись сам.
7. Используй `show_appointments` если клиент спрашивает "когда у меня запись".
8. НИКОГДА не выдумывай мастеров вне списка.
9. Если запрос вне beauty-домена — вежливо переориентируй.
10. НЕ запрашивай телефон/email — они уже у нас.
"""
```

## Guardrails

### Throttling

В `djangoProject/settings/base.py` → `DEFAULT_THROTTLE_RATES`:
```python
'ai_chat': '30/min',  # per user OR per anonymous_session
```

### Daily token cap

`AI_MAX_TOKENS_PER_USER_PER_DAY = 50000` (env override).
Redis key `ai:tokens:{actor_id}:{YYYY-MM-DD}`, TTL 24h.

### Anonymous message cap

5 user-role сообщений на `AnonymousSession` total.
`Message.objects.filter(conversation__anonymous_session=..., role=USER).count() >= 5`
→ 429 `RATE_LIMITED` с `details.reason="anon_message_limit"`.

При OTP-входе (`/auth/verify-otp` с `anonymous_token`) сессия мерджится:
все `Conversation.anonymous_session=X` → `Conversation.user=Y, anonymous_session=null`.
Это уже описано в spec v2.0 §POST /auth/verify-otp.

### PII redaction

`ai.redaction.redact_pii()` применяется к КАЖДОМУ user-сообщению перед
отправкой в OpenAI. В БД хранится оригинал. Известный gap: фамилии
(см. `project_ai_foundation.md` follow-ups).

### Empty API key

Если `settings.OPENAI_API_KEY == ""` → 503 `AI_UNAVAILABLE` сразу,
не пытаемся вызвать OpenAI client.

## Files touched

| Type | Path | Status |
|---|---|---|
| Model | `ai/models.py` | NEW |
| Migration | `ai/migrations/0001_initial.py` | NEW (auto) |
| Service | `ai/application/services/chat_service.py` | NEW |
| Service | `ai/application/services/action_service.py` | NEW |
| Service | `ai/application/services/specialist_context_builder.py` | NEW |
| Module | `ai/prompts.py` | NEW |
| Module | `ai/tools.py` (TOOL_DEFINITIONS) | NEW |
| Module | `ai/tools_handlers.py` (validation + shaping) | NEW |
| Serializers | `ai/serializers.py` | NEW |
| Views | `ai/views.py` (5 views) | NEW |
| URLs | `ai/urls.py` | NEW |
| URLs | `djangoProject/urls.py` | EDIT (include ai.urls) |
| Settings | `djangoProject/settings/base.py` | EDIT (throttle scope, AI_MAX_TOKENS_PER_USER_PER_DAY) |
| Admin | `ai/admin.py` | NEW (Conversation + Message read-only) |
| Tests | `ai/tests/test_chat_service.py` | NEW |
| Tests | `ai/tests/test_action_service.py` | NEW |
| Tests | `ai/tests/test_views.py` | NEW |
| Tests | `ai/tests/test_tools_handlers.py` | NEW |
| Tests | `ai/tests/factories.py` | NEW |

**Total:** ~17 новых файлов, 2 правки. Оценка ~180-220 минут CC (увеличилось из-за action endpoint + 5 tools вместо 3).

## Tests

### Unit (chat_service)
- `test_creates_new_conversation_if_no_id`
- `test_uses_existing_conversation`
- `test_anonymous_under_limit_works`
- `test_anonymous_at_5_messages_returns_429_with_anon_reason`
- `test_redacts_pii_before_openai_call` (mock OpenAI, assert redacted phone+email)
- `test_saves_raw_user_message_to_db` (assert БД содержит оригинал, OpenAI получил redacted)
- `test_handles_tool_call_show_specialists_enriches_with_match_data`
- `test_handles_tool_call_show_slots_calls_availability_service`
- `test_handles_tool_call_confirm_booking_does_not_create_appointment`
- `test_handles_tool_call_show_appointments`
- `test_handles_tool_call_ask_clarification`
- `test_empty_openai_key_returns_503`
- `test_daily_token_limit_exceeded_returns_429_with_daily_reason`
- `test_specialist_context_filters_by_city_and_rating`
- `test_context_param_overrides_profile_location`
- `test_voice_mode_in_request_is_silently_ignored` (forward-compat)

### Unit (action_service)
- `test_confirm_booking_true_creates_appointment_with_source_ai`
- `test_confirm_booking_uses_idempotency_key_from_conversation_id`
- `test_confirm_booking_false_does_not_create_appointment`
- `test_confirm_booking_slot_taken_returns_409`
- `test_show_specialists_selected_advances_conversation`
- `test_ask_clarification_answer_advances_conversation`
- `test_action_on_other_users_conversation_returns_403`
- `test_invalid_action_type_returns_400`

### Integration (views)
- `test_post_chat_unauthenticated_returns_401`
- `test_post_chat_authenticated_creates_conversation_and_returns_message`
- `test_post_chat_anonymous_works_with_anon_jwt`
- `test_post_chat_response_envelope_is_data_wrapped`
- `test_post_action_creates_appointment_returns_201_shape`
- `test_get_conversations_list_returns_paginated_with_preview`
- `test_get_conversations_list_anonymous_returns_403`
- `test_get_conversation_detail_returns_embedded_messages`
- `test_get_conversation_detail_other_user_returns_404`
- `test_delete_conversation_soft_deletes`
- `test_throttle_31st_request_returns_429_with_minute_reason`
- `test_pro_app_type_returns_403_wrong_app_type`

### Mocking strategy
- `unittest.mock.patch("ai.services.llm_client.get_openai_client")` returns Mock
- Mock возвращает `ChatCompletion` с заранее заданным content + optional tool_calls
- `CreateBookingService` — реальный (через factories), не mock — для action tests

## Rollout

1. Локально: `make migrate` → `make test-app APP=ai`
2. PR в dev — review + merge
3. Dev VPS: уже есть `OPENAI_API_KEY` + `OPENAI_PROXY` (smoke-test PR #26 прошёл)
4. Smoke-test через `curl` 5 endpoints с реальным JWT
5. Mobile интеграция (Phase 3 UI) — отдельным PR в beautygo-mobile

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| OpenAI rate-limit 429 на org level | Med | Sentry alert + fallback "AI временно недоступен" |
| LLM галлюцинирует мастеров | High | `handle_show_specialists` валидирует ID на бэке, отбрасывает invalid |
| LLM создаёт фейковую запись | High | `confirm_booking` tool НЕ создаёт; booking — только через `/action/` endpoint |
| LLM вызывает `confirm_booking` без подтверждения юзера | Med | в action_service `confirmed: bool` обязателен в request body |
| px6 proxy down | Low-Med | 503 + Sentry; fallback на YandexGPT — Phase 6 |
| Anonymous abuse (5 → reset session → 5...) | Med | rate limit per IP на `/auth/anonymous` уже есть; fingerprinting — Phase 6 |
| Cost runaway | Med | daily token cap + Sentry alert если daily total > $10 |
| PII leak в OpenAI | Med | `redact_pii()` mandatory; last-name redaction — known gap |
| Race condition на `confirm_booking` (двойной POST) | Med | idempotency_key детерминирован: `ai-{conversation_id}-{datetime}` |
| Frontend шлёт `voice_mode=true` ожидая TTS | Low | принимаем поле, ignored в MVP; ответ всегда text |

## Open follow-ups (post-MVP)

- [ ] Last-name PII redaction
- [ ] LLM Router (OpenAI primary / YandexGPT fallback for PII-adjacent flows)
- [ ] `voice_mode` / `voice_response` action — Phase 7+ (Voice token)
- [ ] `collect_context` action — Phase 6 (UserPersonalContext)
- [ ] User memory / persona storage — Phase 6
- [ ] Streaming SSE — Phase 6
- [ ] Conversation summarization когда история > 50 сообщений (compaction)
- [ ] Hard-delete cleanup task для soft-deleted conversations (Celery beat, 30 дней)
- [ ] Pagination для GET /ai/conversations/{id}/ когда messages > 100
- [ ] A/B test gpt-4o-mini vs gpt-4o vs YandexGPT 5.1 Pro по качеству ответов в RU
- [ ] Cost dashboard (daily $ per user, p50/p95 latency, tokens/conversation)
- [ ] Hint для frontend: чтобы он не использовал `POST /appointments/` напрямую после AI flow — booking идёт через `/action/`

## Spec deviations (документировать в Notion)

После merge надо обновить Notion API Spec v2.0 §AI ASSISTANT, добавив раздел
"Implementation Notes" (per audit Strategy 2 рекомендация):

1. `voice_mode` принимается, но игнорируется в MVP (deferred to Phase 7+)
2. `voice_response` / `collect_context` action types — defined в spec, **не имплементированы** в MVP
3. `GET /ai/conversations/{id}/` — embedded messages с trim до 100 (не paginated)
4. `confirm_booking` через `/action/` endpoint создаёт `Appointment.source = ai`,
   idempotency_key = `f"ai-{conversation_id}-{datetime}"`
5. 429 `RATE_LIMITED` с детализацией в `details.reason`: minute_throttle / daily_token_limit / anon_message_limit

## Review gates

Перед merge:
- [ ] All tests green (`make test-app APP=ai`)
- [ ] Coverage по `ai/` ≥ 85%
- [ ] Sentry breadcrumb на каждый LLM call (latency, tokens, model)
- [ ] X-App-Type middleware блокирует pro
- [ ] PII redaction unit-тест с phone+email в сообщении
- [ ] confirm_booking idempotency test (двойной POST с тем же conversation_id+datetime → один appointment)
- [ ] Manual smoke-test на dev VPS с реальным OpenAI

---

*Last updated: 2026-04-26 (rev. 2 — spec v2.0 alignment)*
