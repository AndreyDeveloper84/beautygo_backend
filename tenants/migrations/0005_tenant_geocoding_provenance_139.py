"""DRF-1662 — происхождение координат салона (§139, решение владельца 11.09.2026).

§139 перечисляет, что сохраняется при геокодировании адреса салона:

    исходный и нормализованный адрес
    широта и долгота
    провайдер
    точность и статус результата
    время геокодирования

До этой миграции у ``tenants_tenant`` не было ни широты, ни долготы
(замер 11.09.2026 05:42 UTC на dev-db-1: колонок нет; у 31 мастера
``location_lat/_lng`` пусты у всех). Миграция добавляет восемь колонок и
ТОЛЬКО их.

Миграция ЧИСТО схемная: ни одной строки не заполняется. Заполнение —
отдельной командой через адаптер геокодера, которого в этом тикете нет.
Слияние есть выкладка, и миграция, меняющая данные живых салонов,
сработала бы в момент слияния — без оператора и без журнала.

Умолчания:

* строки — пустая строка = «не указано / не геокодировали» (та же
  конвенция, что ``address``/``city`` из 0004);
* ``latitude``/``longitude``/``geocoded_at`` — ``NULL``. Не ``0.0``:
  ноль-ноль — точка в Гвинейском заливе, и она прошла бы любую проверку
  «заполнено» (§137 снял искусственные числа для неизвестного);
* ``geocode_status`` — пустая строка, а не ``pending``: ``pending`` по
  §139 — исход («сервис был недоступен»), а не «ещё не пробовали».
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0004_tenant_address_city"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="geocode_source_address",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Адрес в том виде, в каком его отправили геокодеру "
                    "(снимок address на момент запроса). Расхождение с "
                    "текущим address означает, что координаты устарели."
                ),
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="geocode_normalized_address",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Адрес, как его вернул провайдер. Пусто = ответа не было."
                ),
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="latitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                help_text=(
                    "Широта места оказания услуги. null = неизвестна. Само "
                    "по себе число НЕ означает «геокодировано» — см. "
                    "is_geocoded."
                ),
                max_digits=9,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="longitude",
            field=models.DecimalField(
                blank=True,
                decimal_places=6,
                help_text=(
                    "Долгота места оказания услуги. null = неизвестна. Само "
                    "по себе число НЕ означает «геокодировано» — см. "
                    "is_geocoded."
                ),
                max_digits=9,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="geocode_provider",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Кто вернул координаты (например, yandex). Пусто = "
                    "никто. Делает замену провайдера наблюдаемой (§139)."
                ),
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="geocode_precision",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Точность результата словами провайдера (у Яндекса: "
                    "exact, number, near, range, street, other). Хранится "
                    "дословно, не интерпретируется здесь."
                ),
                max_length=50,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="geocode_status",
            field=models.CharField(
                blank=True,
                choices=[
                    ("ok", "Геокодировано"),
                    ("confirmed", "Подтверждено человеком"),
                    ("ambiguous", "Неоднозначно — ждёт человека"),
                    ("pending", "Сервис недоступен — ожидает"),
                    ("failed", "Не найдено"),
                ],
                default="",
                help_text=(
                    "Исход геокодирования (§139). Пусто = адрес ни разу не "
                    "геокодировали. Геокодированной строка считается "
                    "ТОЛЬКО при ok/confirmed — pending, ambiguous и failed "
                    "с координатами в расчёте расстояния не участвуют."
                ),
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="geocoded_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Когда получен этот результат. null = не геокодировали."
                ),
                null=True,
            ),
        ),
    ]
