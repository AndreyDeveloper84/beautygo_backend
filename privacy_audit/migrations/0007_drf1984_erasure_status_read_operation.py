# DRF-1984 (C5.3): чтение ботом статуса стирания личного профиля — новая операция журнала.
# Идёт после 0006_drf1857: choices — полный набор.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("privacy_audit", "0006_drf1857_review_read_operation"),
    ]

    operations = [
        migrations.AlterField(
            model_name="personaldataaccesslog",
            name="operation",
            field=models.CharField(
                choices=[
                    ("export", "Экспорт персональных данных"),
                    ("delete", "Стирание персональных данных"),
                    ("read_context", "Чтение личного профиля"),
                    ("write_context", "Запись в личный профиль"),
                    ("erase_context", "Стирание личного профиля"),
                    ("ask_metadata", "Служебное о вопросах профиля"),
                    ("deletion_request_create", "Заявка на удаление аккаунта"),
                    ("deletion_request_read", "Чтение заявки на удаление"),
                    ("read_profile", "Чтение карточки пользователя"),
                    ("review_create", "Отзыв клиента о визите"),
                    ("write_specialist_profile", "Запись профиля мастера"),
                    ("upload_media", "Загрузка фото мастера"),
                    ("delete_media", "Удаление фото мастера"),
                    ("review_read", "Чтение мастером отзывов о себе"),
                    ("erasure_status_read", "Чтение статуса стирания"),
                ],
                max_length=32,
            ),
        ),
    ]
