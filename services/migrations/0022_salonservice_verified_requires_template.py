"""`VERIFIED` ⇒ `template NOT NULL` — DRF-1668 (план automapping, C-9 / MAP-AUTO-02b).

Живой путь на пилоте: повторный confirm YClients-драфта переписывал
`template` (в том числе в NULL) у существующей `SalonService`, оставляя
`mapping_status = verified`, а схема это пропускала. Formula Tela —
единственный YClients-салон, и владелец размечает его руками: такой
re-confirm стирал бы разметку, а гейт подбора продолжал бы читать
«проверено».

Порядок внутри миграции — сначала перепись, потом ограничение: если на
базе уже есть `verified` без шаблона, ограничение упало бы голым
`IntegrityError`, и код оказался бы впереди схемы. Здесь такие строки
печатаются по id и миграция останавливается **до** записи — их чинит
человек решением, а не миграция догадкой (шаблон по имени не
подставляется: это и был бы silent remap).

Только `verified`: `not_recommendable` — «решено, что связи НЕ БУДЕТ», у
него шаблона по смыслу может не быть.
"""

from django.db import migrations, models


class VerifiedWithoutTemplate(RuntimeError):
    """На базе есть строки, которые ограничение отвергнет — миграция не идёт."""


def _refuse_if_verified_without_template(apps, schema_editor):
    SalonService = apps.get_model("services", "SalonService")
    rows = list(
        SalonService.objects.filter(mapping_status="verified", template__isnull=True)
        .values_list("id", "tenant_id", "name")
        .order_by("tenant_id", "name")
    )
    if rows:
        listing = "\n".join(f"  {rid}  tenant={tid}  {name!r}" for rid, tid, name in rows)
        raise VerifiedWithoutTemplate(
            f"{len(rows)} SalonService(s) are VERIFIED without a template — "
            "decide each one (set the template, or move it to not_recommendable / "
            "review_required with provenance) before applying 0022:\n" + listing
        )


class Migration(migrations.Migration):
    dependencies = [
        ("services", "0021_template_lifecycle"),
    ]

    operations = [
        migrations.RunPython(_refuse_if_verified_without_template, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="salonservice",
            constraint=models.CheckConstraint(
                condition=(
                    ~models.Q(mapping_status="verified")
                    | models.Q(template__isnull=False)
                ),
                name="salonservice_verified_requires_template",
            ),
        ),
    ]
