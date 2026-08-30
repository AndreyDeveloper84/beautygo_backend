# Миграция слияния, без операций.
#
# 30.08 два PR смержены в `dev` один за другим, и каждый добавил свой
# лист `0004` в приложение `reviews`:
#
#   0004_alter_review_options            — #269, полный порядок сортировки
#   0004_drf1421_review_salon_service_xor — #268, обнуляемая пара ссылок
#
# Обе правки нужны и не конфликтуют по существу — конфликтует только
# нумерация. Django отказывается строить граф с двумя листьями и роняет
# любую команду с `Conflicting migrations detected`, из-за чего упала
# выкладка #269 и краснеет каждый PR.
#
# Ошибка процесса, а не кода: мержить в самодеплойный репозиторий надо
# по одному, дожидаясь выкладки. 30.08 я смержил два подряд.
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("reviews", "0004_alter_review_options"),
        ("reviews", "0004_drf1421_review_salon_service_xor"),
    ]

    operations = []
