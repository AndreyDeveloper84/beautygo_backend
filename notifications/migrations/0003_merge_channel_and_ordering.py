# Миграция слияния, без операций.
#
# Два листа `0002` в `notifications`:
#
#   0002_alter_notification_options            — #269, полный порядок сортировки
#   0002_alter_notification_channel_alter_notification_status — этот PR, канал MAX
#
# Правки не конфликтуют по существу — конфликтует нумерация: обе ветки
# отходили от одного `dev`, и каждая взяла следующий свободный номер.
#
# Тот же случай уже произошёл 30.08 в `reviews` (#271) и уронил выкладку.
# Правило: в самодеплойном репозитории мержить по одному, дожидаясь
# выкладки, И сверять номера миграций между ветками в полёте.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0002_alter_notification_options"),
        ("notifications", "0002_alter_notification_channel_alter_notification_status"),
    ]

    operations = []
