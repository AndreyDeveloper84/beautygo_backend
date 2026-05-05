# Phase 3 nutrition — smoke tests

Locally-runnable test suite that mirrors `docs/SMOKE_TESTS_PHASE_3.md` 1:1.
Each test is named after the checklist ID (`test_2_6_pregnancy_override`,
`test_3_15_restore_within_window`, etc.) so you can cross-reference both
files when triaging failures.

## How to run

```powershell
# Windows PowerShell
.venv\Scripts\python.exe -m pytest nutrition\tests\smoke -v
```

```bash
# bash / WSL
./.venv/Scripts/python.exe -m pytest nutrition/tests/smoke -v
# or inside the docker stack
docker compose exec web pytest nutrition/tests/smoke -v
```

Filter by checklist section:

```bash
pytest nutrition/tests/smoke -v -k "test_3_"     # only DRF-302 water tracker
pytest nutrition/tests/smoke -v -k "milestone"   # only milestone scenarios
pytest nutrition/tests/smoke::test_phase_3_smoke::TestSection5Patterns
```

## Scope

- **Covered locally**: P2/P3/P6 + every functional item in §1–§8 + the
  parts of §9 that work without VPS (rate-limit, idempotency, helpers).
- **Skipped with explanation**: P1/P4/P5 (deploy/env state on VPS),
  4.2 real OpenAI logs (we mock the client and assert the prompt
  built around the caption), 9.6 migrations rollback (run from CLI:
  `manage.py migrate nutrition 0005` then forward), 9.7 Sentry.
- Time-sensitive scenarios (§5 patterns, §6 returning success) inject
  past-dated rows directly via `FoodLog.objects.create(logged_at=...)`
  — no `freezegun` dependency.

## Layout

- `conftest.py` — shared fixtures (`proxy_user`, `headers`, `seed_beverages`,
  `make_profile`, `add_food_at`, `add_water_at`).
- `test_phase_3_smoke.py` — one class per checklist section, one test
  per ID.

## Notes

- Tests use the standard pytest-django test client (in-process), so a
  passing run validates the code paths but not the deployed VPS. For a
  true-deployment pass run §1.1, §2.1, §3.1, §3.18, §4.4, §5.1, §6.1
  manually with curl against `https://api-dev.beautygo.ru` first.
- The webhook section (§7) sets `NUTRITION_WEBHOOK_URL` in fixtures and
  patches `httpx.Client` so no real network call leaves the box.
