# DRF-1857 (K14): чтение мастером отзывов о себе — новая операция журнала.
# Идёт после 0005_drf1813 (профиль мастера, M21): choices — полный набор обоих.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("privacy_audit", "0005_drf1813_specialist_profile_operations"),
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
                ],
                max_length=32,
            ),
        ),
    ]
