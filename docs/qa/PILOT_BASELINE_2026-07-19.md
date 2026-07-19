# Pilot Baseline — 2026-07-19

**Поток:** W6 (QA / Docs / Runbook) · **Снимок:** 2026-07-19 ~07:00 UTC, после `git fetch --all` во всех репо.
**Сравнение с:** PILOT_CONTRACTS_2026-08-15.md §11 (baseline 2026-07-18).
**Пилот:** 2026-08-15, Пенза, MAX-бот + MAX Mini App.

## 1. Сводная таблица

| Репо (канон) | Локальный путь | Основная ветка / HEAD | Чистота дерева | Отставание от origin | Δ к §11 |
|---|---|---|---|---|---|
| beautygo_backend | `…\Ayla\djangoproject` | `feat/memory-foundation-internal-api` @ `f6e9572e` | только untracked docs (оркестрационные; скопированы W6 в `0d268510`) | **+0/−16** к `origin/dev` (`6defc583`) | было +2/−9 → W1 влит в dev |
| ai-bot-platform | `…\ai-bot-platform` | `feat/memory-consent-global` @ `fe6c1f8` | **грязное**: modified `docs/plans/*.md` ×4, `.claude/settings.json`; ~70 untracked agent-worktrees в `.claude/worktrees/` | **+0/−43** к `origin/dev` (`6ff8d17`) | было +1/−20 → **drift вырос** |
| ayla-ai-core | `…\ayla-ai-core` | `main` @ `f773e7d` (= `v0.9.0`) | **грязное**: ` D uv.lock` (unstaged, похоже случайное — RELEASING.md требует uv), untracked `.hex-skills/` | +0/−0 к `origin/main` | feat/memory-context-builder **отращён как v0.9.0** |
| frontAyla (= `Shiro-Py/frontbeauty`) | `…\Ayla\frontAyla` | `dev` @ `5f9e31e` | чистое | **−20** к `origin/dev` (локальная копия устарела; последний коммит 2026-04-09) | этап 2, вне критического пути |
| ayla-knowledge | `…\Ayla\ayla-knowledge` | `review/user-journey-specification-v1.2` @ `adac3a5` | чистое | +0/−0 к upstream ветки; `main` @ `3180e55` | Decision Log: worktree `ayla-knowledge-agent` → `agent/decision-log` @ `dc25045` (v0.2, review, **не в main**) |

## 2. Пилотные ветки потоков (worktree)

| Поток | Репо | Worktree | Ветка | HEAD | Комментарий |
|---|---|---|---|---|---|
| W1 | beautygo_backend | `djangoproject-w1` | `pilot/booking-core` | `467b75b8` | код W1 уже в `dev` (`6defc583`) |
| W2 | beautygo_backend | `djangoproject-w2` | `pilot/billing` | `268f2fcf` | merge-base с dev = `88a66515` (**до** merge W1); +2 коммита (модели billing, C5 endpoints) |
| W3 | ai-bot-platform | `ai-bot-platform-p3` | `pilot/bot-backend` | `2ec90ca` | волны 1–3 влиты в `dev` (`6ff8d17`); на ветке — бамп ayla-ai-core v0.9.0 |
| W4 | ai-bot-platform | `ai-bot-platform-p4` | `pilot/miniapp` | `9afc5b4` → `ab14adb` (коммитил во время снимка) | booking-flow реальный; 4 vitest-файла |
| W5 | ayla-ai-core | — | `main` | `f773e7d` | релиз v0.9.0 завершён (tag + CHANGELOG) |
| W5 (bot-side) | ai-bot-platform | `ai-bot-platform-w5` | `pilot/concierge` | `f5a1fd0` | **отстаёт от dev** (нет personal_context_client) |
| W6 | beautygo_backend | `djangoproject-w6` | `pilot/qa-docs` | `0d268510` | этот поток |

Интеграционные ветки: beautygo_backend `dev` @ `6defc583` (2026-07-19); ai-bot-platform `dev` @ `6ff8d17` (2026-07-19).

## 3. Находки (для оркестратора)

1. **ai-bot-platform main-checkout drift −43** (было −20): основная копия на `feat/memory-consent-global` всё дальше от `origin/dev`. Сама ветка памяти-консента влита в dev; main-checkout можно синхронизировать или припарковать.
2. **djangoproject пин ayla-ai-core = `e73a1b4` (v0.8.1)** в `requirements.txt:93`. Парный бамп на v0.9.0 (SHA `f773e7d`, тег `v0.9.0`) в djangoproject **не сделан**; в ai-bot-platform бамп есть на `pilot/bot-backend` (`2ec90ca`).
3. **ayla-ai-core: `uv.lock` удалён в рабочем дереве** (unstaged) — признаков намеренного отказа от uv нет (RELEASING.md требует `uv run`). Рекомендуется `git checkout -- uv.lock`.
4. **frontAyla локально −20 к origin/dev** — для пилота не критично (этап 2), но drift-контроль фиксирует.
5. ayla-knowledge: канонический Decision Log (v0.2) живёт на `agent/decision-log` (`dc25045`, review) и не влит в `main` — ссылки на AYLA-DEC-* резолвятся вне main.
6. У W2 ветка `pilot/billing` ответвлена **до** merge W1 в dev → при merge потребуются W1-side патчи (B-1/B-5 mount app/urls, R-2 регистрация топиков, R-5 EVENT_HANDLERS) — см. CONTRACT_MATRIX.
7. Гигиена ai-bot-platform: ~70 untracked `.claude/worktrees/agent-*` (часть locked) — шум при status/чистке; modified `docs/plans/*.md` без коммита.

## 4. Процедура drift-контроля (еженедельно, волна 3)

Команды для повторения снимка (исполнялись буквально):

```bash
for repo in \
  "/c/Users/user/PycharmProjects/Ayla/djangoproject" \
  "/c/Users/user/PycharmProjects/ai-bot-platform" \
  "/c/Users/user/PycharmProjects/ayla-ai-core" \
  "/c/Users/user/PycharmProjects/Ayla/frontAyla" \
  "/c/Users/user/PycharmProjects/Ayla/ayla-knowledge"; do
  (cd "$repo" && git fetch --all --quiet && \
   git status -sb | head -1 && \
   git worktree list && \
   for rb in origin/dev origin/main; do
     git rev-parse --verify --quiet $rb >/dev/null && \
     echo "vs $rb: $(git rev-list --left-right --count $rb...HEAD)";
   done)
done
```

Следующий контроль: 2026-07-26 (еженедельно, отчёт оркестратору).
