#!/usr/bin/env bash
# Стенд 176.119.159.141: ЧИТАЮЩИЙ замер связей услуг с каноном (DRF-2361). НИЧЕГО НЕ МЕНЯЕТ.
#
# Вопрос листа: сколько на ЖИВЫХ данных стенда связей услуг с каноном и сколько из
# них verified. Экраны берут «проверенную» услугу ровно отсюда: каталог допускает
# к рекомендации только SalonService.mapping_status = 'verified'
# (recommendation/_stages.py:401, §10.1), и при нуле таких Mini App показывает
# вместо совета слова владельца «Пока у меня недостаточно подтверждённых данных…»
# (CustomerCatalogScreen, RecommendationCardScreen — без флага; полка подборок —
# под флагом VITE_RECOMMENDATION_SHELF и по OD-PILOT-9 на пилоте выключена).
#
# ДВА ПРИБОРА, ОДНА МЕРА — второй по вашему выбору:
#   шаг 1 — читающая команда каталога manage.py report_pilot_readiness (DRF-2361/#559).
#           Она ORM, а вы просили ORM на стенде не использовать, поэтому ПО УМОЛЧАНИЮ
#           ОНА ПРОПУСКАЕТСЯ. Если хотите второй прибор для сверки — запустите с
#           RUN_MANAGE=1. Команда ничего не пишет (ни одного save/update/create, нет
#           --apply); при старте Django выполняет ready() приложений (регистрация
#           сигналов и проверок) и системные проверки (читают настройки, в сеть не ходят).
#   шаг 2 — SQL в транзакции READ ONLY с ROLLBACK, с ТЕМИ ЖЕ определениями, что у команды.
# С RUN_MANAGE=1 числа печатаются парами; расхождение двух приборов на одной базе —
# находка само по себе.
#
# Определения (одинаковые у обоих шагов):
#   услуг всего        — все строки services_salonservice (без фильтра активности тенанта:
#                        команда берёт SalonService.objects.all(), у модели менеджер обычный);
#   активных           — is_active;
#   со связью с каноном — template_id IS NOT NULL;
#   по статусу         — mapping_status: unmapped / review_required / verified / not_recommendable;
#   verified и активна — is_active AND mapping_status = 'verified'.
#   Пилот сужается по тенанту: команда — по слагу через Tenant.objects (скрывает
#   неактивные тенанты!), SQL — по id b32a057a-… без этого фильтра; активность
#   тенанта печатается, чтобы расхождение по этой причине было видно.
#
# Персональных данных не печатает: только количества, статусы, слаги тенантов и
# названия услуг из прайса (для 36 строк списка владельца — только расхождения).
# Секретов не печатает: доступ к базе — изнутри контейнера, пароли не выводятся.
# Ни одного --apply, ни одного UPDATE/INSERT/DELETE.
#
# Запуск:  ! bash <путь>/docs/stand_measure_verified_links_2361.sh
#          ! RUN_MANAGE=1 bash <путь>/docs/stand_measure_verified_links_2361.sh   (с командой)
# Запускать из Git Bash: при core.autocrlf файл выписывается с CRLF, Git Bash это
# переносит, а под WSL или Linux-шеллом такая копия сломается.
set -euo pipefail

ssh -i ~/.ssh/ayla_rsa -o IdentitiesOnly=yes -o ConnectTimeout=30 taximeter@176.119.159.141 "RUN_MANAGE=${RUN_MANAGE:-0} bash -s" <<'REMOTE'
set -uo pipefail

PILOT_ID="b32a057a-56c7-4bf0-ae50-e11e76ab44be"

echo "=== 0. Контейнеры и время снятия ==="
date -u '+снято (UTC): %Y-%m-%d %H:%M:%S'
docker ps --format '{{.Names}}\t{{.Image}}' | grep -Ei '^dev-(web|db|postgres)' || true
# Имена контейнеров можно задать явно (CAT_DB=… CAT_WEB=…); иначе — по шаблону стенда.
CAT_DB=${CAT_DB:-$(docker ps --format '{{.Names}}' | grep -Ei '^dev-(db|postgres)' | head -1)}
CAT_WEB=${CAT_WEB:-$(docker ps --format '{{.Names}}' | grep -Ei '^dev-web' | head -1)}
if [ -z "${CAT_DB:-}" ]; then
  echo "!!! контейнер базы каталога не найден по шаблону ^dev-(db|postgres) — замер остановлен"; exit 0
fi
echo "база каталога: $CAT_DB   веб каталога: ${CAT_WEB:-(не найден)}"
echo

run_sql () {  # $1 = заголовок; SQL — stdin
  echo "--- $1 ---"
  { echo "BEGIN READ ONLY;"; cat; echo "ROLLBACK;"; } \
    | docker exec -i "$CAT_DB" sh -lc 'psql -X -q -P pager=off -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -v ON_ERROR_STOP=1' 2>&1 \
    | sed -n '1,80p'
  echo
}
q_one () {  # одно значение (первая строка), без оформления; SQL — stdin
  { echo "BEGIN READ ONLY;"; cat; echo "ROLLBACK;"; } \
    | docker exec -i "$CAT_DB" sh -lc 'psql -X -q -At -U "${POSTGRES_USER:-postgres}" -d "${POSTGRES_DB:-postgres}" -v ON_ERROR_STOP=1' 2>/dev/null \
    | grep -v -E '^(BEGIN|ROLLBACK)$' | head -1
}

# ---------- СТОРОЖ ПУСТОГО МНОЖЕСТВА ----------
TOTAL_ALL=$(q_one <<'SQL'
SELECT count(*) FROM services_salonservice;
SQL
)
echo "услуг в базе всего: ${TOTAL_ALL:-?}"
if [ -z "${TOTAL_ALL:-}" ] || [ "$TOTAL_ALL" = "0" ]; then
  echo
  echo "!!! УСЛУГ В БАЗЕ НЕТ НИ ОДНОЙ (или запрос не выполнился)."
  echo "!!! Дальше НЕ идём: все таблицы ниже были бы пусты не потому, что «verified нет»,"
  echo "!!! а потому, что спрашивать не о чем. Возможные причины — не та база (шаблон"
  echo "!!! контейнера), другой стенд, пустая схема. Покажите главному окну этот вывод."
  exit 0
fi

PILOT_SLUG=$(q_one <<SQL
SELECT slug FROM tenants_tenant WHERE id = '$PILOT_ID';
SQL
)
PILOT_ACTIVE=$(q_one <<SQL
SELECT is_active FROM tenants_tenant WHERE id = '$PILOT_ID';
SQL
)
echo "пилот: id $PILOT_ID  слаг ${PILOT_SLUG:-(ТЕНАНТА С ТАКИМ id НЕТ)}  активен: ${PILOT_ACTIVE:-?}"
echo

# ---------- ШАГ 1: команда каталога ----------
MANAGE_ALL=""; MANAGE_PILOT=""
if [ "${RUN_MANAGE:-0}" != "1" ] || [ -z "${CAT_WEB:-}" ]; then
  echo "=== 1. manage.py report_pilot_readiness — ПРОПУЩЕН (по умолчанию: команда — ORM; включить — RUN_MANAGE=1; или веб-контейнер не найден) ==="
else
  echo "=== 1. manage.py report_pilot_readiness (читает, не пишет) — вся база ==="
  MANAGE_ALL=$(docker exec "$CAT_WEB" python manage.py report_pilot_readiness 2>&1 || true)
  printf '%s\n' "$MANAGE_ALL" | sed -n '1,80p'
  echo
  if [ -n "${PILOT_SLUG:-}" ]; then
    echo "=== 1. manage.py report_pilot_readiness --tenant $PILOT_SLUG ==="
    MANAGE_PILOT=$(docker exec "$CAT_WEB" python manage.py report_pilot_readiness --tenant "$PILOT_SLUG" 2>&1 || true)
    printf '%s\n' "$MANAGE_PILOT" | sed -n '1,80p'
    echo
  fi
fi

# ---------- ШАГ 2: SQL с теми же определениями ----------
# Одна строка чисел: total|active|with_template|unmapped|review_required|verified|not_recommendable|verified_active
sql_numbers () {  # $1 = условие WHERE ('TRUE' или tenant_id = '…')
  q_one <<SQL
SELECT count(*)
    || '|' || count(*) FILTER (WHERE is_active)
    || '|' || count(*) FILTER (WHERE template_id IS NOT NULL)
    || '|' || count(*) FILTER (WHERE mapping_status = 'unmapped')
    || '|' || count(*) FILTER (WHERE mapping_status = 'review_required')
    || '|' || count(*) FILTER (WHERE mapping_status = 'verified')
    || '|' || count(*) FILTER (WHERE mapping_status = 'not_recommendable')
    || '|' || count(*) FILTER (WHERE is_active AND mapping_status = 'verified')
FROM services_salonservice WHERE $1;
SQL
}
SQL_ALL=$(sql_numbers "TRUE")
SQL_PILOT=$(sql_numbers "tenant_id = '$PILOT_ID'")

run_sql "2. SQL: по тенантам (все тенанты, включая неактивные)" <<'SQL'
SELECT t.slug, t.is_active AS tenant_active,
       count(s.id)                                                        AS services,
       count(s.id) FILTER (WHERE s.is_active)                             AS active,
       count(s.id) FILTER (WHERE s.template_id IS NOT NULL)               AS with_canon,
       count(s.id) FILTER (WHERE s.mapping_status = 'unmapped')           AS unmapped,
       count(s.id) FILTER (WHERE s.mapping_status = 'review_required')    AS review_required,
       count(s.id) FILTER (WHERE s.mapping_status = 'verified')           AS verified,
       count(s.id) FILTER (WHERE s.mapping_status = 'not_recommendable')  AS not_recommendable,
       count(s.id) FILTER (WHERE s.is_active AND s.mapping_status = 'verified') AS verified_active
FROM tenants_tenant t
JOIN services_salonservice s ON s.tenant_id = t.id
GROUP BY t.slug, t.is_active
ORDER BY services DESC;
SQL

run_sql "2. SQL: verified без связи с каноном (схема запрещает — ожидается 0)" <<'SQL'
SELECT count(*) AS verified_without_template
FROM services_salonservice WHERE mapping_status = 'verified' AND template_id IS NULL;
SQL

run_sql "2. SQL: провенанс verified-связей (правило · версия · основание), по тенантам" <<'SQL'
SELECT t.slug, s.mapping_confirmed_rule AS rule, s.mapping_rule_version AS ver,
       s.mapping_source_ref AS source_ref,
       (s.mapping_confirmed_by_id IS NOT NULL) AS by_person, count(*) AS n
FROM services_salonservice s JOIN tenants_tenant t ON t.id = s.tenant_id
WHERE s.mapping_status = 'verified'
GROUP BY 1, 2, 3, 4, 5 ORDER BY 1, n DESC;
SQL
echo "    (пустая таблица выше = verified-связей нет ни одной; число услуг в базе > 0 — см. сторож)"
echo

# ---------- ШАГ 3: пары чисел рядом ----------
# Значение строки команды по началу строки (после двоеточия или пробелов — число).
m_val () {  # $1 = вывод команды, $2 = регулярка начала строки
  printf '%s\n' "$1" | sed -n '/^== 1\./,/^$/p' | grep -E "$2" | head -1 | grep -oE '[0-9]+' | tail -1
}
compare () {  # $1 = заголовок, $2 = вывод команды, $3 = строка SQL
  echo "=== 3. Сравнение приборов: $1 ==="
  if [ -z "$2" ]; then echo "  (чисел команды для сравнения нет — ниже только SQL)"; fi
  IFS='|' read -r s_total s_active s_tpl s_unm s_rev s_ver s_nrec s_vact <<<"$3"
  local mism=0 label mv sv
  while IFS=';' read -r label rx sv; do
    mv=""
    [ -n "$2" ] && mv=$(m_val "$2" "$rx")
    # Число первым, подпись последней: printf считает байты, и кириллица в
    # начале строки ломает выравнивание столбцов.
    if [ -z "$2" ]; then
      printf '  SQL %6s   %s\n' "$sv" "$label"
    elif [ "$mv" = "$sv" ]; then
      printf '  команда %6s   SQL %6s   совпало       %s\n' "${mv:-—}" "$sv" "$label"
    else
      printf '  команда %6s   SQL %6s   НЕ СОВПАЛО    %s\n' "${mv:-—}" "$sv" "$label"; mism=$((mism+1))
    fi
  done <<EOF
услуг всего;^услуг всего:;$s_total
из них активных;^  из них активных:;$s_active
со связью с каноном;^со связью с каноном:;$s_tpl
unmapped;^  unmapped ;$s_unm
review_required;^  review_required ;$s_rev
verified;^  verified ;$s_ver
not_recommendable;^  not_recommendable ;$s_nrec
verified и активна;^verified и активна:;$s_vact
EOF
  if [ -n "$2" ]; then
    if [ "$mism" = "0" ]; then echo "  ИТОГ: оба прибора дали одинаковые числа ($1)."
    else echo "  ИТОГ: НЕ СОВПАЛО строк: $mism — это находка; покажите вывод главному окну целиком."; fi
  fi
  echo
}
compare "вся база" "$MANAGE_ALL" "$SQL_ALL"
if [ -n "${PILOT_SLUG:-}" ]; then
  if [ "${PILOT_ACTIVE:-}" != "t" ]; then
    # Команда ищет тенант через Tenant.objects, который скрывает неактивные: она
    # ответит «тенанта нет», и сравнение по пилоту было бы расхождением по
    # причине, а не находкой. Поэтому — только SQL и названная причина.
    echo "  !!! ПИЛОТНЫЙ ТЕНАНТ НЕ АКТИВЕН (is_active = ${PILOT_ACTIVE:-?})."
    echo "  !!! Команда его не видит (Tenant.objects скрывает неактивные) — сравнения по пилоту нет."
    echo "  !!! Само это — факт для владельца: пилот на стенде выключен."
    compare "пилот $PILOT_SLUG (только SQL)" "" "$SQL_PILOT"
  else
    compare "пилот $PILOT_SLUG" "$MANAGE_PILOT" "$SQL_PILOT"
  fi
fi

# ---------- ШАГ 4: 36 связей по списку владельца (#574, DRF-2516) ----------
# Список — services/owner_confirmed_mapping.py::CONFIRMED, перенесён сюда машинно.
# Совпадение строки — как у команды confirm_owner_mapping: слаг тенанта + точное имя.
run_sql "4. Список владельца (36): итог" <<'SQL'
WITH owner(slug, name, code) AS (VALUES
  ('formula-tela', 'Верхняя губа', '7.1.1'),
  ('formula-tela', 'Лазерная эпиляция подбородка', '7.1.2'),
  ('formula-tela', 'Подмышки', '7.1.6'),
  ('formula-tela', 'Руки до локтя', '7.1.7'),
  ('formula-tela', 'Руки полностью', '7.1.8'),
  ('formula-tela', 'Бикини по линии белья', '7.1.16'),
  ('formula-tela', 'Бикини глубокое', '7.1.17'),
  ('formula-tela', 'Ягодицы', '7.1.19'),
  ('formula-tela', 'Лазерная эпиляция бёдер (передняя/задняя/боковая часть)', '7.1.20'),
  ('formula-tela', 'Бёдра полностью', '7.1.20'),
  ('formula-tela', 'Голени с коленями', '7.1.21'),
  ('formula-tela', 'Лазерная эпиляция ног полностью', '7.1.22'),
  ('formula-tela', 'Всё тело (руки полностью, ноги полностью, тотальное бикини, подмышки)', '7.1.24'),
  ('formula-tela', 'Must Have (подмышки + глубокое бикини)', '7.1.25'),
  ('formula-tela', 'Классика (голени с коленями + глубокое бикини + подмышки)', '7.1.26'),
  ('formula-tela', 'Массаж шейно-воротниковой зоны', '1.1.5'),
  ('formula-tela', 'Массаж стоп', '1.1.14'),
  ('mkt-spatrium', 'Тайский массаж', '1.2.21'),
  ('formula-tela', 'Спортивный массаж', '1.3.6'),
  ('formula-tela', 'Массаж ног — глубокое восстановление и лёгкость (60 минут)', '1.3.20'),
  ('formula-tela', 'Спина без боли — комплекс массажа', '1.3.24'),
  ('formula-tela', 'Антицеллюлитный массаж', '1.4.5'),
  ('formula-tela', 'RF-лифтинг — Лицо/шея', '2.2.2'),
  ('formula-tela', 'RF-лифтинг — Лицо/шея/декольте', '2.2.2'),
  ('formula-tela', 'УЗ-кавитация — 1 зона', '2.2.11'),
  ('formula-tela', 'VelaShape', '2.3.1'),
  ('formula-tela', 'Вибромассаж — Всё тело', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Бёдра/ягодицы', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Живот', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Спина/руки', '2.4.9'),
  ('formula-tela', 'Пилинг Миндальный', '4.6.4'),
  ('formula-tela', 'Пилинг AZELAIC PEEL', '4.6.8'),
  ('formula-tela', 'Пилинг FERULIC PEEL C+', '4.6.9'),
  ('formula-tela', 'Комплекс «Гладкая кожа»: антицеллюлитный массаж (45 мин) + VelaShape (60 минут)', '2.3.10'),
  ('formula-tela', 'Комплекс «Лёгкие ноги»: лимфодренажный массаж + вибромассаж (60 минут)', '1.4.28'),
  ('formula-tela', 'Чистка лица + пилинг', '20.2.2')
), matched AS (
  SELECT o.slug, o.name, o.code, s.id AS service_id, s.mapping_status,
         s.mapping_confirmed_rule, st.canonical_code AS linked_code,
         count(s.id) OVER (PARTITION BY o.slug, o.name) AS rows_by_name
  FROM owner o
  LEFT JOIN tenants_tenant t ON t.slug = o.slug
  LEFT JOIN services_salonservice s ON s.tenant_id = t.id AND s.name = o.name
  LEFT JOIN services_servicetemplate st ON st.id = s.template_id
)
SELECT count(*) FILTER (WHERE service_id IS NOT NULL)                              AS found_rows,
       count(DISTINCT (slug, name))                                               AS listed,
       count(*) FILTER (WHERE mapping_status = 'verified')                         AS verified,
       count(*) FILTER (WHERE mapping_confirmed_rule = 'product_owner_confirmed_list') AS owner_rule,
       count(*) FILTER (WHERE linked_code = code)                                  AS right_canon,
       count(*) FILTER (WHERE mapping_status = 'verified'
                          AND mapping_confirmed_rule = 'product_owner_confirmed_list'
                          AND linked_code = code)                                  AS fully_applied
FROM matched;
SQL
echo "    ожидание, если --apply команды confirm_owner_mapping прошёл: 36 во всех столбцах"
echo "    (formula-tela 35, mkt-spatrium 1). Если fully_applied = 0 — список НЕ применён."
echo

run_sql "4. Список владельца: только строки, где что-то не так (нет строки / не verified / не то правило / не тот код)" <<'SQL'
WITH owner(slug, name, code) AS (VALUES
  ('formula-tela', 'Верхняя губа', '7.1.1'),
  ('formula-tela', 'Лазерная эпиляция подбородка', '7.1.2'),
  ('formula-tela', 'Подмышки', '7.1.6'),
  ('formula-tela', 'Руки до локтя', '7.1.7'),
  ('formula-tela', 'Руки полностью', '7.1.8'),
  ('formula-tela', 'Бикини по линии белья', '7.1.16'),
  ('formula-tela', 'Бикини глубокое', '7.1.17'),
  ('formula-tela', 'Ягодицы', '7.1.19'),
  ('formula-tela', 'Лазерная эпиляция бёдер (передняя/задняя/боковая часть)', '7.1.20'),
  ('formula-tela', 'Бёдра полностью', '7.1.20'),
  ('formula-tela', 'Голени с коленями', '7.1.21'),
  ('formula-tela', 'Лазерная эпиляция ног полностью', '7.1.22'),
  ('formula-tela', 'Всё тело (руки полностью, ноги полностью, тотальное бикини, подмышки)', '7.1.24'),
  ('formula-tela', 'Must Have (подмышки + глубокое бикини)', '7.1.25'),
  ('formula-tela', 'Классика (голени с коленями + глубокое бикини + подмышки)', '7.1.26'),
  ('formula-tela', 'Массаж шейно-воротниковой зоны', '1.1.5'),
  ('formula-tela', 'Массаж стоп', '1.1.14'),
  ('mkt-spatrium', 'Тайский массаж', '1.2.21'),
  ('formula-tela', 'Спортивный массаж', '1.3.6'),
  ('formula-tela', 'Массаж ног — глубокое восстановление и лёгкость (60 минут)', '1.3.20'),
  ('formula-tela', 'Спина без боли — комплекс массажа', '1.3.24'),
  ('formula-tela', 'Антицеллюлитный массаж', '1.4.5'),
  ('formula-tela', 'RF-лифтинг — Лицо/шея', '2.2.2'),
  ('formula-tela', 'RF-лифтинг — Лицо/шея/декольте', '2.2.2'),
  ('formula-tela', 'УЗ-кавитация — 1 зона', '2.2.11'),
  ('formula-tela', 'VelaShape', '2.3.1'),
  ('formula-tela', 'Вибромассаж — Всё тело', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Бёдра/ягодицы', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Живот', '2.4.9'),
  ('formula-tela', 'Вибромассаж — Спина/руки', '2.4.9'),
  ('formula-tela', 'Пилинг Миндальный', '4.6.4'),
  ('formula-tela', 'Пилинг AZELAIC PEEL', '4.6.8'),
  ('formula-tela', 'Пилинг FERULIC PEEL C+', '4.6.9'),
  ('formula-tela', 'Комплекс «Гладкая кожа»: антицеллюлитный массаж (45 мин) + VelaShape (60 минут)', '2.3.10'),
  ('formula-tela', 'Комплекс «Лёгкие ноги»: лимфодренажный массаж + вибромассаж (60 минут)', '1.4.28'),
  ('formula-tela', 'Чистка лица + пилинг', '20.2.2')
)
SELECT o.slug, o.name, o.code AS expected_code, st.canonical_code AS linked_code,
       s.mapping_status, NULLIF(s.mapping_confirmed_rule, '') AS rule,
       CASE WHEN t.id IS NULL THEN 'нет тенанта'
            WHEN s.id IS NULL THEN 'нет услуги с таким именем' ELSE '' END AS note
FROM owner o
LEFT JOIN tenants_tenant t ON t.slug = o.slug
LEFT JOIN services_salonservice s ON s.tenant_id = t.id AND s.name = o.name
LEFT JOIN services_servicetemplate st ON st.id = s.template_id
WHERE s.id IS NULL
   OR s.mapping_status IS DISTINCT FROM 'verified'
   OR s.mapping_confirmed_rule IS DISTINCT FROM 'product_owner_confirmed_list'
   OR st.canonical_code IS DISTINCT FROM o.code
ORDER BY o.slug, o.name;
SQL

# ---------- ШАГ 5: цена пустого множества на пилоте ----------
run_sql "5. Пилот: активные услуги против допущенных к рекомендации (verified и активна)" <<SQL
SELECT count(*) FILTER (WHERE is_active)                                  AS active_services,
       count(*) FILTER (WHERE is_active AND mapping_status = 'verified')  AS recommendable,
       count(*) FILTER (WHERE is_active AND mapping_status <> 'verified') AS shown_without_verified
FROM services_salonservice WHERE tenant_id = '$PILOT_ID';
SQL
echo "    recommendable = 0 → каталог отвечает NO_VERIFIED_CANDIDATES, и экраны вместо совета"
echo "    показывают «Пока у меня недостаточно подтверждённых данных…» (полка подборок выключена флагом)."
echo "=== конец замера. Записей не делалось: каждая SQL-сессия — READ ONLY с ROLLBACK. ==="
REMOTE
