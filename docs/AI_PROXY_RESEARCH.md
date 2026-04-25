# AI Proxy Research — OpenAI access from Russia

**Date:** 2026-04-25
**Author:** Andrey + Claude
**Status:** Research-spike (b) per pilot plan
**Decision needed:** Which path to OpenAI from production VPS (RU jurisdiction)

---

## TL;DR

| Option | Verdict | Notes |
|---|---|---|
| **ProxyAPI.ru** | ✅ Recommended primary | RF-incorporated, ruble billing, drop-in OpenAI-SDK compat |
| **YandexGPT 5.1 Pro / GigaChat** | ✅ Recommended Plan B | RF-native, 152-ФЗ compliant, function calling supported |
| **Self-hosted proxy on foreign VPS** | ❌ Reject | OpenAI bans rotating proxies, ops burden, fragile |
| **Azure OpenAI** | ❌ Blocked | EU sanctions cut RF corporate access since 2024 |
| **Direct OpenAI** | ❌ Blocked | RF IPs blocked Feb 2024, RF cards rejected |
| **laozhang.ai (international)** | ⚠️ Backup-of-backup | Cheaper (~960 vs ~2400 руб/1M tokens) but not RF-incorporated → B2B accounting awkward |

**Action:** Smoke-test ProxyAPI from production VPS before any AI code lands. ETA: 1-2 hours.

---

## 1. Constraints & risks

### 1.1 Hard blockers
- **Direct OpenAI API:** geo-blocked since Feb 2024 (RF IPs reject + RU cards rejected). No workaround.
- **Azure OpenAI:** EU sanctions (Q3 2024 update) cut Microsoft cloud services to RF corporate entities. Not viable.

### 1.2 Compliance — 152-ФЗ ("Закон о персональных данных")
PII of RF citizens (phone, email, last name, photo with face) **must** be stored on servers physically located in RF. Sending PII to OpenAI servers via ProxyAPI is **still a violation** — ProxyAPI just routes; the data lands in US.

Mitigation strategy:
- **Mandatory PII redaction layer** before any OpenAI/ProxyAPI call (regex: phone, email, last name).
- **Dual-provider routing:** YandexGPT for anything PII-adjacent (specialist recommendations, booking confirmation), OpenAI for anonymous tasks (general chit-chat, food scanner).
- **Or:** legal opinion that anonymous chat doesn't qualify as ПД processing (low-risk for pilot, must clarify before scale-out).

### 1.3 Vendor lock risk
OpenAI vendor decision is a **one-way door** for product surface (prompts, function-call schemas, model behavior). Mitigation: thin abstraction layer `apps/ai/services/llm_client.py` so use-cases don't import `openai` directly.

---

## 2. ProxyAPI.ru — primary recommendation

### Why
- Russian legal entity → invoice for ИП/ООО, ruble billing, no FX hassle.
- OpenAI-SDK drop-in: just `base_url="https://api.proxyapi.ru/openai/v1"`, same models (`gpt-4o`, `gpt-4o-mini`, etc.).
- Supports vision (`gpt-4o`), function calling, streaming, embeddings.
- Compatible with `openai` Python SDK ≥ 1.x — zero code adapter.

### Costs (rough, 2026-04)
~2400 руб per 1M tokens GPT-4o (combined I/O). ~2.5x markup on direct OpenAI.

### Pilot budget estimate (Penza, M5, 2026-07-15+)
| Workload | Volume/day | Volume/month | Cost/month |
|---|---|---|---|
| Chat (100 active users × 10 msg × ~500 tok) | 500K tok | 15M tok | ~36K руб |
| Food scanner (100 × 1 image ≈ 1K vision tok) | 100K tok | 3M tok | ~7K руб |
| Buffer / system prompts / retries | — | — | ~10K руб |
| **Total** | | | **~50–60K руб/мес** |

Acceptable for pilot. Hard cap per-user/day required (cost runaway protection).

### Risks
- **Single point of failure:** ProxyAPI outage = AI features down. Mitigation: YandexGPT fallback wired through abstraction.
- **TOS change risk:** RF regulator could pressure resellers. Mitigation: same fallback.
- **Latency markup:** +50-200ms vs direct OpenAI (RF→ProxyAPI→OpenAI hop). Acceptable for chat, marginal for streaming.

---

## 3. YandexGPT / GigaChat — Plan B

### Why
- Native RF infrastructure → 152-ФЗ compliant by default, no PII redaction concerns.
- Stable supplier (Yandex Cloud / Sber).
- Function calling supported (tools/agents).
- 32K context (YandexGPT 5.1 Pro) — enough for chat use-cases.

### Why not primary
- **Vision gap:** YandexGPT itself doesn't accept image inputs. YandexART is generation-only. Food Scanner (DRF-143) requires vision-language model — major scope hit.
- **Function-calling maturity:** less battle-tested than OpenAI's. May need more prompt engineering for tool-use stability.
- **Ecosystem lock:** OAuth2/IAM tied to Yandex Cloud — extra integration work.

### Use as
- **Fallback** for ProxyAPI outage (chat-only path).
- **Primary** for any flow that touches PII (booking-related AI prompts that mention client phone/last name).
- **Compliance hedge** if 152-ФЗ enforcement tightens.

---

## 4. Architectural decisions

### 4.1 Single LLM abstraction
```python
# apps/ai/services/llm_client.py
class LLMClient(Protocol):
    def chat(self, messages, tools=None, stream=False) -> LLMResponse: ...
    def vision(self, image_bytes, prompt) -> LLMResponse: ...

class OpenAIProxyClient(LLMClient): ...    # ProxyAPI primary
class YandexGPTClient(LLMClient): ...      # fallback / PII path

# Router based on workload type + circuit-breaker state
class LLMRouter:
    def route(self, workload: Workload) -> LLMClient: ...
```

### 4.2 PII redaction (non-negotiable)
```python
# apps/ai/redaction.py
def redact_pii(text: str) -> tuple[str, dict[str, str]]:
    """Replace phone/email/last names with placeholders. Return mapping for unredact."""
```
Run on every prompt before OpenAI/ProxyAPI. Skip for YandexGPT path.

### 4.3 Cost guardrails (mandatory before AI ships)
- Hard cap: **N tokens/user/day** (config). 429 + Sentry warn on breach.
- Daily spend ledger (`apps/ai/models.py::AIUsage`). Cron alert at 80% of budget.
- Per-endpoint cost label in Sentry tags (`ai.endpoint=chat|food_scan`).

### 4.4 Configuration
```env
# Primary
OPENAI_PROXY_BASE_URL=https://api.proxyapi.ru/openai/v1
OPENAI_PROXY_API_KEY=<from-proxyapi-dashboard>

# Fallback
YANDEX_GPT_API_KEY=<from-yandex-cloud-iam>
YANDEX_GPT_FOLDER_ID=<yandex-cloud-folder>

# Guardrails
AI_MAX_TOKENS_PER_USER_PER_DAY=50000
AI_DAILY_BUDGET_RUB=2000
```

---

## 5. Smoke-test plan (next concrete step)

**Goal:** confirm ProxyAPI works end-to-end from production VPS before any AI code lands.

1. Register account at proxyapi.ru, get API key.
2. Top up 500 руб (minimum).
3. SSH to dev VPS (`taximeter@194.87.99.126`).
4. Run a 3-call test:
   ```bash
   docker compose exec web python -c "
   from openai import OpenAI
   client = OpenAI(base_url='https://api.proxyapi.ru/openai/v1', api_key='$KEY')
   r = client.chat.completions.create(model='gpt-4o-mini', messages=[{'role':'user','content':'Привет, ответь одним словом.'}])
   print(r.choices[0].message.content)
   "
   ```
5. Measure: latency, error rate over 10 calls, token billing accuracy on dashboard.
6. **Pass criteria:** all 10 calls succeed, p95 latency < 3s, billing dashboard updates correctly.
7. **Fail → escalate:** investigate AITUNNEL or pivot to YandexGPT-only pilot scope (food scanner deferred).

ETA: 1-2 hours including registration + payment.

**User action required:** register at proxyapi.ru and provide API key (or share creds for me to do via shell).

---

## 6. Open questions for follow-up

1. **Legal review of 152-ФЗ for chat:** is anonymous user prompt (`"посоветуй мастера маникюра в Пензе"`) classified as ПД? Need lawyer eyes before scale-out.
2. **Budget approval:** ~60K руб/мес AI cost — confirm it fits pilot budget.
3. **Streaming or blocking** for chat MVP? Streaming improves UX but doubles complexity (SSE infra, frontend handling).
4. **Conversation persistence:** retain N msgs, or summarize-on-context-fill? Affects DB schema.

---

## 7. Decision log

| Date | Decision | Owner |
|---|---|---|
| 2026-04-24 | Vendor: OpenAI (via ProxyAPI) | Andrey |
| 2026-04-25 | Plan B: YandexGPT for PII path | Andrey + Claude |
| TBD | Smoke-test ProxyAPI from VPS | pending API key |
| TBD | DRF-162 close as mooted | pending |
