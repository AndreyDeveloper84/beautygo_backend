# AI Chat Endpoint — Implementation Plan

> Status: REVISED · Author: Andrey + Claude · Date: 2026-04-28 (rev. 3 — reuse strategy aligned to ayla-ai-core)
> Foundation: `ai/services/llm_client.py` + `ai/redaction.py` (PR #26, merged) + **ayla-ai-core 0.5.0** (DRF-236..239)
> Target: M5 Penza pilot (2026-07-15)
> **Spec source of truth:** Notion → "API Specification v2.0 — Ayla" §🤖 AI ASSISTANT
> **Audit reference:** `docs/BOT_CODE_AUDIT_2026-04.md` — определил что 80% уже готово в ayla-ai-core

---

## Reuse Strategy ⚡

> **Ключевое изменение (rev. 3):** AI Chat строится не с нуля, а поверх `ayla-ai-core` —
> shared Python package, экстрагированного из production бота Formula Tela (30+ дней в проде).
> Оценка сокращается с **~17 файлов / 4 спринта → ~5 файлов / 1.5 спринта**.

### Что приходит из ayla-ai-core (готово, тестировано)

| Компонент | Класс / функция | Тикет |
|---|---|---|
| Главный AI pipeline | `AIConcierge` | DRF-237 |
| Контекст специалистов + anti-hallucination | `SpecialistContext[UUID]` | DRF-238 |
| Tool JSON schemas для OpenAI | `build_tool_definitions("string")` | DRF-237 |
| Роутер tool calls + ID-валидация | `dispatch_tool_call()` | DRF-237 |
| Brand voice конфиг | `AYLA_MARKETPLACE_VOICE` | DRF-239 |
| Рендер системного промпта | `render_system_prompt()` | DRF-239 |

### Что строит Ayla сама (~5 файлов)

| Компонент | Файл | Тикет |
|---|---|---|
| Conversation + Message модели с tenant_id | `ai/models.py` | DRF-240 |
| Django migration | `ai/migrations/0001_initial.py` | DRF-240 |
| REST views (5 endpoints) | `ai/views.py` | DRF-241 |
| Serializers | `ai/serializers.py` | DRF-241 |
| Ayla-specific SpecialistContextBuilder | `ai/services/specialist_context_builder.py` | DRF-241 |
| URL routing | `ai/urls.py` | DRF-241 |
| Admin | `ai/admin.py` | DRF-240 |

### Anti-hallucination — критически важно ⚠️

`ayla-ai-core` содержит двухслойную защиту от галлюцинаций (отсутствовала в оригинальном плане):

- **Слой 1:** `SpecialistContext` передаёт LLM только ID из реальной БД (frozenset)
- **Слой 2:** `dispatch_tool_call()` перепроверяет каждый returned ID — если LLM выдумал
  несуществующий specialist_id (бывает в ~3% запросов), автоматически возвращается
  `ask_clarification` вместо ошибки. Клиент никогда не видит сломанную карточку.

Не нужно реализовывать отдельно — это уже в package.

### Именование tools — выравнивание

`ayla-ai-core` использует именование из бота:

| ayla-ai-core (wire format) | Оригинальный план | Ayla фронт |
|---|---|---|
| `show_masters` / `ActionType.SHOW_MASTERS` | `show_specialists` | `show_specialists` |
| `show_my_bookings` / `ActionType.SHOW_MY_BOOKINGS` | `show_appointments` | `show_appointments` |

**Решение:** использовать `ayla-ai-core` wire-format как-есть (изменить plan/spec, не package).
Фронт получает `action_type = "show_masters"` — адаптировать в мобильном клиенте.
Альтернатива: переименовать в ayla-ai-core (minor version bump) — обсудить с командой.

---

## Scope

Conversational AI assistant для 🟢 Ayla (client app). Пользователь пишет
свободный запрос — LLM возвращает текст + опционально структурированное
действие. Подтверждение действия идёт через отдельный endpoint, который
выполняет side-effect (например, создаёт `Appointment` с `source=ai`).

### Что входит в MVP
- `Conversation` + `Message` модели с FK на `User` и `tenant_id` (DRF-240)
- 5 endpoints (per spec v2.0 §AI ASSISTANT) поверх `AIConcierge` из ayla-ai-core (DRF-241)
- 5 action types из ayla-ai-core: `show_masters`, `show_slots`, `confirm_booking`, `show_my_bookings`, `ask_clarification`
- PII redaction перед каждым OpenAI вызовом (уже в `ai/redaction.py`, PR #26)
- Rate limit (DRF scope) + daily token guardrail + anonymous message cap

### Что НЕ входит (deferred per M4 scope reduction)
- `voice_mode` / `voice_response` action — Phase 7+ (Voice token deferred)
- `collect_context` action — Phase 6 (UserPersonalContext deferred)
- Streaming (SSE) — sync ответ
- Multi-LLM router (только OpenAI gpt-4o-mini)
- Long-term user memory (persona / preferences storage)

Enum `AIActionType` остаётся расширяемым (DB stores raw string), чтобы
будущие action types не требовали миграций.

## Locked decisions (2026-04-26, подтверждены rev. 3)

| # | Decision | Choice |
|---|---|---|
| 1 | History retention | Храним все Message в БД, в LLM context передаём последние 10 |
| 2 | Specialist context source | Top-20 по городу клиента, фильтр rating ≥ 4.0 |
| 3 | Anonymous chat | Разрешён, лимит 5 user-messages до `verify-otp` (merge сессии при OTP-входе) |
| 4 | Streaming | Sync only в MVP, SSE — Phase 6 |
| 5 | confirm_booking flow | Booking создаётся **на бэке** через `/ai/chat/{id}/action/`, **не** прямым POST /appointments/ с фронта |
| 6 | AI pipeline | `AIConcierge` из ayla-ai-core, DI-инжектируется Ayla-specific зависимостями |
| 7 | ID type | UUID (ayla-ai-core поддерживает через `SpecialistContext[UUID]`) |
| 8 | Tool wire names | Использовать ayla-ai-core naming (`show_masters`, `show_my_bookings`) |

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

### `ai/models.py` (DRF-240)

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
    tenant = models.ForeignKey(
        "tenants.Tenant", on_delete=models.CASCADE,
        related_name="conversations",
    )
    is_active = models.BooleanField(default=True)
    last_message_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["user", "-last_message_at"]),
            models.Index(fields=["tenant", "is_active", "-last_message_at"]),
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
        SYSTEM = "system"

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages",
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    action_type = models.CharField(max_length=32, blank=True, default="")
    action_data = models.JSONField(null=True, blank=True)
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
      "type": "show_masters",
      "data": {
        "masters": [
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

## Pipeline (с ayla-ai-core)

### DI-конфигурация AIConcierge в Ayla

```python
# ai/concierge_factory.py
from ayla_ai_core import AIConcierge, AYLA_MARKETPLACE_VOICE, build_tool_definitions
from ai.services.specialist_context_builder import build_specialist_context
from ai.stores import DjangoConversationStore

def get_concierge() -> AIConcierge:
    return AIConcierge(
        openai_client=get_async_openai_client(),   # ai/services/llm_client.py
        store=DjangoConversationStore(),            # обёртка над Conversation/Message models
        context_builder=build_specialist_context,  # Ayla multi-tenant query, Top-20
        brand_voice=AYLA_MARKETPLACE_VOICE,
        tool_definitions=build_tool_definitions("string"),  # UUID-based
    )
```

### `ai/application/services/chat_service.py` (POST /ai/chat/)

```
ChatService.send_message(actor, conversation_id|None, message_text, context)
  ├── 1. check_anonymous_limit()        → 429 RATE_LIMITED (anon_message_limit)
  ├── 2. check_daily_limit()            → 429 RATE_LIMITED (daily_token_limit)
  ├── 3. redact_pii(message_text)       → redacted_text (БД хранит оригинал)
  ├── 4. concierge.send_message(       ← AIConcierge из ayla-ai-core делает всё остальное:
  │       conversation_id,             │   resolve_conversation → save_user_msg →
  │       redacted_text,               │   build_context → load_history →
  │       actor_id,                    │   render_prompt → call_openai →
  │   )                                │   dispatch_tool_call → save_assistant_msg
  ├── 5. update_daily_token_counter()  → redis.incrby(daily_tokens_key, tokens_total)
  └── 6. return ChatResponseDTO
```

### `ai/application/services/specialist_context_builder.py`

Единственная Ayla-специфичная функция — multi-tenant SQL-запрос Top-N специалистов:

```python
async def build_specialist_context(conversation_id: UUID) -> SpecialistContext[UUID]:
    """Ayla-specific: top-20 specialists for tenant, rating ≥ 4.0."""
    conversation = await Conversation.objects.aget(id=conversation_id)
    specialists = await (
        SpecialistProfile.objects
        .filter(tenant=conversation.tenant, is_active=True, rating__gte=4.0)
        .order_by("-rating")[:20]
        .prefetch_related("services")
        .aall()  # actually: sync_to_async wrapping
    )
    return build_specialist_context_from_candidates([
        SpecialistCandidate(
            id=s.id,
            name=s.display_name,
            specialization=s.specialization,
            services=[
                SpecialistService(id=svc.id, name=svc.name, price=svc.price)
                for svc in s.services.all()
            ],
        )
        for s in specialists
    ], tenant_id=str(conversation.tenant_id))
```

## Tools (из ayla-ai-core)

5 tool definitions предоставляются `build_tool_definitions("string")` из `ayla-ai-core`.
Ayla не определяет их самостоятельно — импортирует готовые.

Tool handlers (`dispatch_tool_call`) тоже из ayla-ai-core.
Anti-hallucination: автоматически (candidate_ids frozenset + fallback к ask_clarification).

## System prompt (из ayla-ai-core)

`render_system_prompt(context, AYLA_MARKETPLACE_VOICE)` — из ayla-ai-core.
`AYLA_MARKETPLACE_VOICE` определяет assistant_name="Ayla", business_name="Ayla Marketplace",
domain="beauty services marketplace", off_topic_redirect.

## Guardrails

### Throttling

```python
'ai_chat': '30/min',  # per user OR per anonymous_session
```

### Daily token cap

`AI_MAX_TOKENS_PER_USER_PER_DAY = 50000` (env override).
Redis key `ai:tokens:{actor_id}:{YYYY-MM-DD}`, TTL 24h.

### Anonymous message cap

5 user-role сообщений на `AnonymousSession` total → 429 `RATE_LIMITED` с `details.reason="anon_message_limit"`.

### PII redaction

`ai.redaction.redact_pii()` применяется к каждому user-сообщению перед передачей в AIConcierge.
В БД хранится оригинал. В OpenAI уходит redacted.

## Files touched (rev. 3 — reuse strategy)

| Type | Path | Status | Источник |
|---|---|---|---|
| Model | `ai/models.py` | NEW | Ayla (DRF-240) |
| Migration | `ai/migrations/0001_initial.py` | NEW (auto) | Ayla (DRF-240) |
| Admin | `ai/admin.py` | NEW | Ayla (DRF-240) |
| Factory | `ai/concierge_factory.py` | NEW | Ayla (DRF-241) |
| Store | `ai/stores.py` | NEW | Ayla (DRF-241) — Django adapter для ConversationStore |
| Context builder | `ai/services/specialist_context_builder.py` | NEW | Ayla (DRF-241) |
| Serializers | `ai/serializers.py` | NEW | Ayla (DRF-241) |
| Views | `ai/views.py` (5 views) | NEW | Ayla (DRF-241) |
| URLs | `ai/urls.py` | NEW | Ayla (DRF-241) |
| URLs | `djangoProject/urls.py` | EDIT | Ayla (DRF-241) |
| Settings | `djangoProject/settings/base.py` | EDIT | Ayla (DRF-241) — throttle + AI_MAX_TOKENS |
| Tests | `ai/tests/test_chat_service.py` | NEW | Ayla (DRF-241) |
| Tests | `ai/tests/test_views.py` | NEW | Ayla (DRF-241) |

**Итого:** ~11 файлов (было 17) + `ayla-ai-core` берёт на себя pipeline, tools, handlers, prompts.
**Оценка:** ~1.5 спринта (было 4). Сокращение ~60%.

### Что НЕ нужно писать (есть в ayla-ai-core)

- ~~`ai/tools.py`~~ → `from ayla_ai_core import build_tool_definitions`
- ~~`ai/tools_handlers.py`~~ → `from ayla_ai_core import dispatch_tool_call`
- ~~`ai/prompts.py`~~ → `from ayla_ai_core import render_system_prompt, AYLA_MARKETPLACE_VOICE`
- ~~`ai/application/services/chat_service.py` (core pipeline)~~ → `AIConcierge.send_message()`
- ~~Anti-hallucination logic~~ → встроена в `dispatch_tool_call()`

## Tests

### Unit (chat_service)
- `test_creates_new_conversation_if_no_id`
- `test_uses_existing_conversation`
- `test_anonymous_under_limit_works`
- `test_anonymous_at_5_messages_returns_429_with_anon_reason`
- `test_redacts_pii_before_concierge_call`
- `test_saves_raw_user_message_to_db`
- `test_daily_token_limit_exceeded_returns_429_with_daily_reason`
- `test_specialist_context_filters_by_tenant_city_and_rating`
- `test_empty_openai_key_returns_503`

### Integration (views)
- `test_post_chat_unauthenticated_returns_401`
- `test_post_chat_authenticated_creates_conversation_and_returns_message`
- `test_post_chat_pro_app_type_returns_403`
- `test_post_chat_response_envelope_is_data_wrapped`
- `test_get_conversations_list_returns_paginated`
- `test_get_conversations_list_anonymous_returns_403`
- `test_get_conversation_detail_returns_embedded_messages`
- `test_get_conversation_detail_other_user_returns_404`
- `test_delete_conversation_soft_deletes`

### Mocking strategy
- `unittest.mock.patch("ayla_ai_core.orchestrator.AsyncOpenAI")` — mock OpenAI client
- Mock возвращает `ChatCompletion` с заданным content + optional tool_calls
- `SpecialistContextBuilder` — реальный (через test factories), не mock

## Rollout

1. Локально: `pip install -e ../ayla-ai-core` → `make migrate` → `make test-app APP=ai`
2. PR в dev — review + merge
3. Dev VPS: `pip install ayla-ai-core==0.5.x` + `OPENAI_API_KEY` (уже есть, PR #26)
4. Smoke-test через `curl` 5 endpoints с реальным JWT
5. Mobile интеграция (Phase 3 UI) — отдельным PR в beautygo-mobile

## Risks & mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| OpenAI rate-limit 429 | Med | Sentry alert + fallback "AI временно недоступен" |
| LLM галлюцинирует specialist_ids | High | **Закрыто ayla-ai-core** — dispatch_tool_call() фильтрует невалидные ID |
| LLM создаёт фейковую запись | High | confirm_booking tool НЕ создаёт; booking — только через `/action/` endpoint |
| ayla-ai-core breaking change | Low | Pin minor version (`==0.5.x`), bump только после review |
| Несовместимость tool names с фронтом | Med | Согласовать `show_masters` vs `show_specialists` с mobile-командой до интеграции |
| Cost runaway | Med | daily token cap + Sentry alert |
| Race condition на confirm_booking | Med | idempotency_key = `f"ai-{conversation_id}-{datetime}"` |

## Open follow-ups (post-MVP)

- [ ] Согласовать tool wire names с mobile-командой (`show_masters` vs `show_specialists`)
- [ ] Last-name PII redaction
- [ ] LLM Router (OpenAI primary / YandexGPT fallback)
- [ ] `voice_mode` / `voice_response` — Phase 7+
- [ ] Streaming SSE — Phase 6
- [ ] Conversation summarization когда история > 50 сообщений
- [ ] Hard-delete cleanup task (Celery beat, 30 дней)
- [ ] A/B test gpt-4o-mini vs gpt-4o vs YandexGPT 5.1 Pro

## Related tickets

| Тикет | Статус | Scope |
|---|---|---|
| DRF-236 | Done | Initial ayla-ai-core scaffold |
| DRF-237 | Done | AIConcierge + tools + handlers |
| DRF-238 | Done | SpecialistContext generics + UUID |
| DRF-239 | Done | BrandVoiceConfig + render_system_prompt |
| DRF-240 | Backlog | Conversation + Message models (due 2026-05-08) |
| DRF-241 | Backlog | REST endpoints (due 2026-05-13) |
| DRF-242 | Backlog | Multi-tenant scoping (due 2026-05-15) |
| DRF-243 | Backlog | Bot migration на ayla-ai-core (due 2026-05-22) |

---

*Last updated: 2026-04-28 (rev. 3 — reuse strategy, ayla-ai-core integration, ~60% scope reduction)*
