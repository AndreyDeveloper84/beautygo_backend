"""DRF-1624 — состояние и база сходятся с моделью: порядок операндов в `Q`.

НЕ УДАЛЯТЬ, СОЧТЯ БЕССМЫСЛЕННОЙ. Ниже `DROP INDEX` и следом
`CREATE UNIQUE INDEX` с предикатом, который на первый взгляд тот же
самый. Он действительно логически тот же — и всё-таки миграция нужна.
Почему, по порядку.

ЧТО РАСХОДИЛОСЬ

`makemigrations` с 02.05.2026 просил снести и заново создать
`ai_conversation_one_active_per_user_tenant` на каждом прогоне.
Расхождение оказалось синтаксическим.

Модель пишет условие kwargs'ами::

    condition=models.Q(is_active=True, deleted_at__isnull=True)

`Q.__init__` кладёт в `children` сначала `*args`, затем
`sorted(kwargs.items())` — то есть kwargs ПЕРЕСОРТИРОВЫВАЮТСЯ, и у
модели выходит ``[('deleted_at__isnull', True), ('is_active', True)]``.

Рукописная `0004_conversation_tenant_fk` (DRF-242.2, `8f5e372`,
02.05.2026) записала то же условие ПОЗИЦИОННЫМИ кортежами::

    condition=models.Q(("is_active", True), ("deleted_at__isnull", True))

Позиционные аргументы не сортируются, порядок сохранился как написан.

`Q.__eq__` сравнивает `children` СПИСКОМ, а не множеством. Отсюда
`UniqueConstraint.__eq__` даёт False, и автодетектор выдаёт
`Remove + Create` при логически тождественном предикате. Как множество
условия равны: те же два конъюнкта, та же связка `AND`, `negated` у
обоих False. Модель с 02.05 не менялась — обе стороны написаны одним
коммитом и разошлись в момент написания.

ПОЧЕМУ ВСЁ-ТАКИ ПЕРЕСТРАИВАЕМ ИНДЕКС

Потому что иначе расхождение осталось бы висеть вечно: `makemigrations`
сравнивает модель с состоянием, а не с базой, и списочное сравнение
`children` не помирится само. Закрыть его можно тремя способами:

1. переписать условие в МОДЕЛИ позиционными кортежами — нулевой SQL, но
   в коде остаётся странный стиль, и следующий, кто напишет условие
   нормально, воскресит расхождение;
2. `SeparateDatabaseAndState` с пустым `database_operations` — тоже
   нулевой SQL, но тогда запись Django о предикате навсегда расходится
   с тем, что физически лежит в базе, пусть и эквивалентно;
3. выпустить настоящую перестройку — состояние, модель и база сходятся
   в одной точке, без оговорок.

Выбран (3), и вот на каком основании. `ACCESS EXCLUSIVE` на
`ai_conversation` держится ровно столько, сколько строится индекс, а в
таблице на пилоте 10.09.2026 было **2 строки**
(`SELECT count(*) FROM ai_conversation` на api-dev.gobeauty.site). На
двух строках это микросекунды. Замер, а не оценка: будь таблица
большой, правильным был бы (2), и решение принимал бы владелец.

ЧТО УВИДИТ POSTGRES

Предикат сохраняется в том порядке, в каком записан, поэтому
`pg_indexes.indexdef` после этой миграции меняется::

    до     WHERE (is_active AND (deleted_at IS NULL))
    после  WHERE ((deleted_at IS NULL) AND is_active)

Множество строк, которые индекс пропускает, при этом то же самое: `AND`
коммутативна, оба операнда неизменяемы и без побочных эффектов. Меняется
запись, не смысл — и в этом весь смысл миграции.
"""
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai", "0005_alter_conversation_options"),
        ("tenants", "0004_tenant_address_city"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name="conversation",
            name="ai_conversation_one_active_per_user_tenant",
        ),
        migrations.AddConstraint(
            model_name="conversation",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    ("deleted_at__isnull", True), ("is_active", True),
                ),
                fields=("user", "tenant"),
                name="ai_conversation_one_active_per_user_tenant",
            ),
        ),
    ]
