#!/usr/bin/env bash
# Стенд 176.119.159.141: ЧИТАЮЩИЙ замер видимости демо-салонов (DRF-2420). НИЧЕГО НЕ МЕНЯЕТ.
#
# Вопрос листа: сколько вымышленных мастеров может получить живой клиент. Каталог
# с #565 прячет демо от обычного клиента предикатом users.sellable.demo_visibility_q
# (Tenant.is_demo + User.is_test_persona). Бот этого признака не знает вовсе: в
# зеркале tenancy_tenant поля is_demo нет, синхронизация несёт is_active, а подбор
# бота (apps/marketplace/discovery.py) берёт CatalogMaster.all_tenants.filter(AVAILABLE)
# — без is_demo и без активности тенанта.
#
# Решение по развилке — (б) «демо видят только тестовые личности», главное окно по
# поручению владельца, 28.09 (docs/OWNER_QUESTIONS_2026-09-23.md, раздел «Ответы
# главного окна по поручению владельца — 28.09»). Порядок: сперва эти числа, потом правка.
#
# ЧТО СНИМАЕТСЯ (только количества и слаги салонов; слаги — не персональные данные):
#   каталог 1. тенанты: активных/неактивных × is_demo true/false;
#   каталог 2. демо-слаги файла сида (services/seeds/demo_salons_2026-08.json): есть ли
#              в базе, помечены ли is_demo — то есть прогонялся ли
#              mark_demo_and_test_personas --apply;
#   каталог 3. салоны mkt-* (35 строк сида вне генератора демо, 23.08): их is_demo —
#              отдельно, генератор их не заводил и команда их не метит;
#   каталог 4. личностей с is_test_persona — ТОЛЬКО число (ни имён, ни id);
#   бот     5. мастера по классам салонов: сколько строк и сколько проходят AVAILABLE —
#              это и есть «в выдаче бота». Классы: слаги сида, is_demo каталога,
#              mkt-*, защищённый пилот formula-tela, прочие.
#
# AVAILABLE перенесён из apps/catalog/master_state.py ДОСЛОВНО по столбцам:
#   ADMITTED       is_active AND archived_at IS NULL AND invite_status = 'accepted'
#   LINKED_TO_AYLA ayla_user_id IS NOT NULL AND (связи соло нет ИЛИ её status = 'IDENTITY_LINKED')
#   гейт личности  catalog_specialist_id IS NOT NULL — в настройках бота НЕ объявлен
#                  (getattr(settings, "MASTER_CATALOG_IDENTITY_REQUIRED", False)) → выключен
#                  при любом env; столбец печатается для полноты
#   гейт расписания schedule_confirmed_at IS NOT NULL — только при
#                  MASTER_SCHEDULE_CONFIRMATION_REQUIRED=true; значение флага печатается
# SQL-копия предиката — прокси: если завтра AVAILABLE поменяют, этот файл соврёт молча.
# Поэтому столбцы напечатаны по частям, а не одним итогом.
#
# Персональных данных не печатает. Секретов не печатает: доступ к базам — изнутри
# контейнеров, из окружения бота читаются ровно два булевых флага по имени.
# Ни одного --apply, ни одного UPDATE/INSERT/DELETE: каждая SQL-сессия — READ ONLY с ROLLBACK.
#
# Запуск:  ! bash <путь>/docs/stand_measure_demo_visibility_2420.sh
# Имена контейнеров можно задать явно: CAT_DB=… BOT_DB=… BOT_WEB=…
# Запускать из Git Bash (CRLF при core.autocrlf Git Bash переносит, WSL — нет).
set -euo pipefail

ssh -i ~/.ssh/ayla_rsa -o IdentitiesOnly=yes -o ConnectTimeout=30 taximeter@176.119.159.141 \
  "CAT_DB=${CAT_DB:-} BOT_DB=${BOT_DB:-} BOT_WEB=${BOT_WEB:-} bash -s" <<'REMOTE'
set -uo pipefail

# Слаги демо — из файла сида каталога (services/seeds/demo_salons_2026-08.json на
# c889bdc0). Здесь они только для СЧЁТА уже созданных строк; код читает признак.
SEED_SLUGS="'olhovyy-dvor','pylca-i-lyon','mednyy-kovsh','sorok-okon','fevralskiy-svet'"
PROTECTED_SLUG="formula-tela"

echo "=== 0. Контейнеры и время снятия ==="
date -u '+снято (UTC): %Y-%m-%d %H:%M:%S'
docker ps --format '{{.Names}}\t{{.Image}}' | grep -Ei 'db|postgres|web|bot' || true
CAT_DB=${CAT_DB:-$(docker ps --format '{{.Names}}' | grep -Ei '^dev-(db|postgres)' | head -1)}
BOT_DB=${BOT_DB:-$(docker ps --format '{{.Names}}' | grep -Ei 'bot.*(db|postgres)' | head -1)}
BOT_WEB=${BOT_WEB:-$(docker ps --format '{{.Names}}' | grep -Ei 'bot' | grep -Eiv 'db|postgres|redis|worker|beat|celery' | head -1)}
echo "база каталога: ${CAT_DB:-(НЕ НАЙДЕНА)}   база бота: ${BOT_DB:-(НЕ НАЙДЕНА)}   веб бота: ${BOT_WEB:-(не найден)}"
echo

run_sql () {  # $1 = контейнер, $2 = заголовок; SQL — stdin
  echo "--- $2 ---"
  { echo "BEGIN READ ONLY;"; cat; echo "ROLLBACK;"; } \
    | docker exec -i "$1" sh -lc 'psql -X -q -P pager=off -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -v ON_ERROR_STOP=1' 2>&1 \
    | sed -n '1,60p'
  echo
}
q_one () {  # $1 = контейнер; одно значение первой строки; SQL — stdin
  { echo "BEGIN READ ONLY;"; cat; echo "ROLLBACK;"; } \
    | docker exec -i "$1" sh -lc 'psql -X -q -At -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -v ON_ERROR_STOP=1' 2>/dev/null \
    | grep -v -E '^(BEGIN|ROLLBACK)$' | head -1
}

# ================= КАТАЛОГ =================
if [ -z "${CAT_DB:-}" ]; then
  echo "!!! база каталога не найдена по шаблону ^dev-(db|postgres) — шаги 1–4 не сняты"
else
  # Сторож пустого множества: ноль тенантов значит «не та база», а не «демо нет».
  CAT_TENANTS=$(q_one "$CAT_DB" <<'SQL'
SELECT count(*) FROM tenants_tenant;
SQL
)
  echo "тенантов в каталоге всего: ${CAT_TENANTS:-?}"
  if [ -z "${CAT_TENANTS:-}" ] || [ "$CAT_TENANTS" = "0" ]; then
    echo "!!! ТЕНАНТОВ НЕТ (или запрос не выполнился; например, миграция 0009 не применена и"
    echo "!!! столбца is_demo нет). Каталожные шаги пропущены — покажите вывод главному окну."
  elif [ "$(q_one "$CAT_DB" <<'SQL'
SELECT count(*) FROM information_schema.columns
WHERE table_name = 'tenants_tenant' AND column_name = 'is_demo';
SQL
)" != "1" ]; then
    echo "!!! У tenants_tenant НЕТ столбца is_demo: миграция tenants 0009 (DRF-2420) на стенде не"
    echo "!!! применена. Каталожные шаги пропущены — «демо не помечены» здесь значило бы «нечем метить»."
    CAT_DB=""   # и слаги is_demo для классов бота не читаются
  else
    run_sql "$CAT_DB" "1. Каталог: тенанты по активности и is_demo" <<'SQL'
SELECT is_active, is_demo, count(*) AS tenants
FROM tenants_tenant GROUP BY 1, 2 ORDER BY 1 DESC, 2 DESC;
SQL

    run_sql "$CAT_DB" "2. Каталог: слаги сида демо — есть ли в базе и помечены ли" <<SQL
WITH seed(slug) AS (SELECT unnest(ARRAY[$SEED_SLUGS]))
SELECT seed.slug, (t.id IS NOT NULL) AS in_db, t.is_active, t.is_demo
FROM seed LEFT JOIN tenants_tenant t ON t.slug = seed.slug ORDER BY seed.slug;
SQL
    echo "    is_demo = f у салона сида → mark_demo_and_test_personas --apply не прогонялся"
    echo "    (или прогонялся с --slug, сузившим список). Код каталога такой салон считает боевым."
    echo

    run_sql "$CAT_DB" "3. Каталог: салоны mkt-* (генератор демо их не заводил)" <<'SQL'
SELECT slug, is_active, is_demo FROM tenants_tenant WHERE slug LIKE 'mkt-%' ORDER BY slug;
SQL

    run_sql "$CAT_DB" "3б. Каталог: защищённый пилот" <<SQL
SELECT slug, is_active, is_demo FROM tenants_tenant WHERE slug = '$PROTECTED_SLUG';
SQL
    echo "    ожидание: is_demo = f. Иначе боевой салон спрятан от живых клиентов — находка."
    echo

    run_sql "$CAT_DB" "4. Каталог: личности с is_test_persona (только число)" <<'SQL'
SELECT count(*) FILTER (WHERE is_test_persona)                  AS test_personas,
       count(*) FILTER (WHERE is_test_persona AND is_active)    AS test_personas_active,
       count(*)                                                  AS users_total
FROM users_user;
SQL
    echo "    test_personas = 0 → по решению (б) демо не увидит НИКТО, включая людские тесты:"
    echo "    живых тестировщиков надо пометить (--persona), прежде чем прятать демо в боте."
    echo
  fi
fi

# Слаги is_demo = true из каталога — для классов бота. Слаг — не персональные данные.
CAT_DEMO_SLUGS=""
if [ -n "${CAT_DB:-}" ]; then
  CAT_DEMO_SLUGS=$(q_one "$CAT_DB" <<'SQL'
SELECT COALESCE(string_agg(quote_literal(slug), ','), '') FROM tenants_tenant WHERE is_demo;
SQL
)
fi
[ -z "${CAT_DEMO_SLUGS:-}" ] && CAT_DEMO_SLUGS="NULL"   # IN (NULL) — пустой класс, а не ошибка

# ================= БОТ =================
if [ -z "${BOT_DB:-}" ]; then
  echo "!!! база бота не найдена по шаблону bot.*(db|postgres) — шаг 5 не снят"
  echo "=== конец замера. Записей не делалось. ==="
  exit 0
fi

SCHED_FLAG="(веб бота не найден — флаг не прочитан)"
if [ -n "${BOT_WEB:-}" ]; then
  SCHED_FLAG=$(docker exec "$BOT_WEB" printenv MASTER_SCHEDULE_CONFIRMATION_REQUIRED 2>/dev/null || true)
  SCHED_FLAG=${SCHED_FLAG:-"(не задан → false)"}
fi
echo "MASTER_SCHEDULE_CONFIRMATION_REQUIRED в вебе бота: $SCHED_FLAG"
echo "MASTER_CATALOG_IDENTITY_REQUIRED: в настройках бота не объявлен → гейт выключен при любом env"
echo

BOT_MASTERS=$(q_one "$BOT_DB" <<'SQL'
SELECT count(*) FROM catalog_catalogmaster;
SQL
)
echo "мастеров в зеркале бота всего: ${BOT_MASTERS:-?}"
if [ -z "${BOT_MASTERS:-}" ] || [ "$BOT_MASTERS" = "0" ]; then
  echo "!!! МАСТЕРОВ В ЗЕРКАЛЕ НЕТ (или запрос не выполнился) — «0 демо в выдаче» здесь значил бы"
  echo "!!! «спрашивать не о чем», а не «демо спрятаны». Шаг 5 пропущен."
  echo "=== конец замера. Записей не делалось. ==="
  exit 0
fi

run_sql "$BOT_DB" "5. Бот: мастера по классам салонов и сколько из них в выдаче (AVAILABLE)" <<SQL
WITH m AS (
  SELECT cm.*, t.slug, t.is_active AS tenant_active,
         (cm.is_active AND cm.archived_at IS NULL AND cm.invite_status = 'accepted') AS admitted,
         (cm.ayla_user_id IS NOT NULL AND (l.id IS NULL OR l.status = 'IDENTITY_LINKED')) AS linked
  FROM catalog_catalogmaster cm
  JOIN tenancy_tenant t ON t.id = cm.tenant_id
  LEFT JOIN identity_soloidentitylink l ON l.master_id = cm.id
), c AS (
  SELECT m.*,
         CASE WHEN slug IN ($SEED_SLUGS)        THEN '1 сид демо'
              WHEN slug IN ($CAT_DEMO_SLUGS)    THEN '2 is_demo каталога (вне сида)'
              WHEN slug LIKE 'mkt-%'            THEN '3 mkt-*'
              WHEN slug = '$PROTECTED_SLUG'     THEN '4 пилот formula-tela'
              ELSE                                   '5 прочие' END AS klass
  FROM m
)
SELECT klass,
       count(DISTINCT slug)                                                        AS salons,
       count(*)                                                                    AS masters,
       count(*) FILTER (WHERE admitted AND linked)                                 AS available_now,
       count(*) FILTER (WHERE admitted AND linked AND schedule_confirmed_at IS NOT NULL) AS available_if_schedule_gate,
       count(*) FILTER (WHERE admitted AND linked AND NOT tenant_active)           AS available_in_inactive_salon,
       count(*) FILTER (WHERE admitted AND linked AND catalog_specialist_id IS NULL) AS available_without_catalog_id
FROM c GROUP BY klass ORDER BY klass;
SQL
echo "    available_now — выдача бота при выключенном гейте расписания (его значение — выше)."
echo "    Флаг true → читать available_if_schedule_gate. Слово «22 против 4» сверять по классам 1–3 против 4."
echo "    available_in_inactive_salon > 0 → подбор бота не смотрит и на активность салона."
echo

run_sql "$BOT_DB" "5б. Бот: салоны сида, mkt-* и пилот по отдельности (слаг, активен в зеркале, мастеров в выдаче)" <<SQL
SELECT t.slug, t.is_active AS tenant_active,
       count(cm.id) FILTER (WHERE cm.is_active AND cm.archived_at IS NULL AND cm.invite_status = 'accepted'
                              AND cm.ayla_user_id IS NOT NULL AND (l.id IS NULL OR l.status = 'IDENTITY_LINKED')) AS available_now
FROM tenancy_tenant t
LEFT JOIN catalog_catalogmaster cm ON cm.tenant_id = t.id
LEFT JOIN identity_soloidentitylink l ON l.master_id = cm.id
WHERE t.slug IN ($SEED_SLUGS) OR t.slug LIKE 'mkt-%' OR t.slug = '$PROTECTED_SLUG'
GROUP BY 1, 2 ORDER BY 1;
SQL
echo "=== конец замера. Записей не делалось: каждая SQL-сессия — READ ONLY с ROLLBACK. ==="
REMOTE
