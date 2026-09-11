"""DRF-1128 — привести STATE ограничения к модели, не трогая базу.

``makemigrations --check`` на голом ``dev`` требовал для ``ai``:

    - Remove constraint ai_conversation_one_active_per_user_tenant
    + Create constraint ai_conversation_one_active_per_user_tenant

Разошёлся не индекс, а **порядок ключей в ``Q``**. Миграция ``0004`` написана
руками и пишет условие как ``("is_active", True), ("deleted_at__isnull", True)``;
модель задаёт его kwargs, а kwargs Django нормализует по алфавиту —
``deleted_at__isnull`` раньше ``is_active``. Условие одно и то же, индекс в
базе один и тот же::

    CREATE UNIQUE INDEX ... ON ai_conversation (user_id, tenant_id)
        WHERE (is_active AND (deleted_at IS NULL))

Автодетектор сравнивает деконструированные объекты, а не SQL, и видит два
разных ограничения. Сгенерированная им миграция выполнила бы настоящий
``DROP INDEX`` + ``CREATE UNIQUE INDEX`` на живой таблице пилота — ACCESS
EXCLUSIVE ради нулевого изменения, и падение посреди миграции, если в
таблице найдётся хоть одна пара активных разговоров. ``0005`` (#269) видел
этот дрейф и намеренно не понёс его; здесь он закрывается.

Поэтому — ``SeparateDatabaseAndState``: state-половина повторяет то, что
хотел автодетектор, database-половина **пуста**. ``sqlmigrate`` этой миграции
печатает только заголовки. Правка самой ``0004`` дала бы тот же результат,
но переписывала бы применённую миграцию; новая — оставляет историю честной.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("ai", "0005_alter_conversation_options"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveConstraint(
                    model_name="conversation",
                    name="ai_conversation_one_active_per_user_tenant",
                ),
                migrations.AddConstraint(
                    model_name="conversation",
                    constraint=models.UniqueConstraint(
                        condition=models.Q(
                            ("deleted_at__isnull", True), ("is_active", True)
                        ),
                        fields=("user", "tenant"),
                        name="ai_conversation_one_active_per_user_tenant",
                    ),
                ),
            ],
        ),
    ]
