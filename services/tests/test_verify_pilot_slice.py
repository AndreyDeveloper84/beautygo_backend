"""``verify_pilot_slice`` — 14 действий владельца одной командой, по его слову.

Что здесь стережётся:

* **таблица в команде = справочник**: каждый код из ``ACTIONS`` есть в
  seed'е 1223 и ``requires_health_check`` там такой, как обещает задание
  владельцу; строк ровно 14, номера 1..14 без дыр. Иначе команда молча
  исполняла бы не то задание;
* **сухой прогон ничего не пишет** и печатает предмет (база, салон, кто)
  до плана;
* **apply пишет через форму §76**: ``VERIFIED``, канон, кто, когда,
  основание; правило пустое (форма держит «кто» xor «правило»); шаг 4 §93
  — синоним канона словами салона — записан;
* **идемпотентно**: второй ``--apply`` — 0 записей, строки «сделано»;
* **спорные строки** (§4 списка) без ``--include-disputed`` пропускаются с
  причиной, с флагом — исполняются;
* **стоп по строке с причиной**: услуги нет у салона; RHC канона в базе не
  совпал с ожиданием задания; ``--apply`` при заблокированных строках
  выходит ненулём — «сделано не целиком» не читается как «сделано».
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from services.management.commands.verify_pilot_slice import ACTIONS, DEFAULT_TENANT, SEED, load_seed
from services.models import SalonService, ServiceCategory, ServiceTemplate, ServiceTemplateSynonym
from tenants.models import Tenant
from users.models import User

# ---------------------------------------------------------------------------
# Таблица против справочника — без базы
# ---------------------------------------------------------------------------


def test_the_table_is_fourteen_numbered_rows():
    assert [a.n for a in ACTIONS] == list(range(1, 15))
    assert len({a.service_name for a in ACTIONS}) == 14


def test_every_code_exists_in_the_seed_with_the_promised_health_check_flag():
    seed = load_seed(SEED)
    for a in ACTIONS:
        assert a.code in seed, f"строка {a.n}: кода {a.code} нет в справочнике"
        rhc = str(seed[a.code]["requires_health_check"]).lower() == "true"
        assert rhc is a.expected_rhc, f"строка {a.n}: RHC в seed {rhc}, в задании {a.expected_rhc}"


def test_exactly_the_s3_row_carries_health_check_true():
    """Строка 5 — единственная с ``true``: без неё сценарию «ноет спина» нечем
    дать исход «передадим специалисту» (S3); с любой другой — каждая бронь
    снова уйдёт оператору."""
    assert [a.n for a in ACTIONS if a.expected_rhc] == [5]


def test_disputed_rows_are_the_ones_with_an_open_owner_question():
    assert [a.n for a in ACTIONS if a.disputed] == [5, 6, 7, 8, 9]


def test_the_default_salon_is_the_pilot_one():
    assert DEFAULT_TENANT == "formula-tela"


def test_the_only_write_path_is_the_admin_form():
    """Запись идёт через ``SalonServiceAdminForm.save()`` и ничем больше:
    ни ``.save()`` модели, ни ``.update()`` — иначе отказ §76 приходил бы
    из базы именем ограничения, а не по полям, как оператору."""
    import ast
    import inspect

    from services.management.commands import verify_pilot_slice as mod

    tree = ast.parse(inspect.getsource(mod))
    writes = [
        f"{n.lineno}: {ast.unparse(n)}" for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"save", "update", "update_or_create", "bulk_update", "create"}
        and ast.unparse(n.func) != "form.save"
    ]
    assert not writes, writes
    assert any(isinstance(n, ast.Call) and ast.unparse(n.func) == "form.save" for n in ast.walk(tree))


# ---------------------------------------------------------------------------
# Поведение — на своём салоне
# ---------------------------------------------------------------------------

def _template_for(code: str) -> ServiceTemplate:
    row = load_seed(SEED)[code]
    cat, _ = ServiceCategory.objects.get_or_create(name=row["subcategory"] or row["category"])
    tpl, _ = ServiceTemplate.objects.get_or_create(
        category=cat, name=row["service"],
        defaults={
            "requires_health_check": str(row["requires_health_check"]).lower() == "true",
            "canonical_code": code,   # MAP-AUTO-01: команда резолвит по коду, не по паре
        },
    )
    return tpl


@pytest.fixture
def owner(db):
    return User.objects.create_user(username="owner-vps", password="x", phone="+79990002100", role="admin")


@pytest.fixture
def salon(db):
    # Свой slug: миграции данных заводят настоящий formula-tela в любой базе,
    # и тест, создающий его сам, падал бы на уникальности — а с get_or_create
    # стал бы зависеть от чужого содержимого.
    tenant = Tenant.objects.create(slug="ft-slice-test", name="Формула тела (тест)")
    cat = ServiceCategory.objects.create(name="Массаж проблемных зон (интейк)")
    # Три строки задания заведены как у салона: две обычные, одна спорная (S3).
    for a in ACTIONS:
        if a.n in (1, 3, 5):
            _template_for(a.code)
            SalonService.objects.create(tenant=tenant, category=cat, name=a.service_name, duration_minutes=45)
    return tenant


def _run(*args, **kw) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    kw.setdefault("tenant", "ft-slice-test")
    call_command("verify_pilot_slice", *args, stdout=out, stderr=err, **kw)
    return out.getvalue(), err.getvalue()


@pytest.mark.django_db
def test_dry_run_prints_the_subject_and_writes_nothing(salon, owner):
    out, _ = _run(by="owner-vps")
    head = out.split("\n#")[0]
    assert "салон: ft-slice-test" in head and "подтверждает: owner-vps" in head and "сухой прогон" in head
    assert "изменилось бы 2" in out            # строки 1 и 3; 5 — спорная
    assert "спорных пропущено 1" in out and "заблокировано 11" in out
    assert not SalonService.objects.filter(mapping_status=SalonService.MappingStatus.VERIFIED).exists()
    assert not ServiceTemplateSynonym.objects.exists()


@pytest.mark.django_db
def test_apply_writes_verified_with_provenance_through_the_form_and_records_the_synonym(salon, owner):
    with pytest.raises(SystemExit):        # 11 строк заблокированы — ненулевой выход, не тишина
        _run(by="owner-vps", apply=True)

    s1 = SalonService.objects.get(tenant=salon, name=ACTIONS[0].service_name)
    assert s1.mapping_status == SalonService.MappingStatus.VERIFIED
    assert s1.template.name == "Массаж шейно-воротниковой зоны"
    assert s1.mapping_confirmed_by == owner and s1.mapping_confirmed_at is not None
    assert s1.mapping_confirmed_rule == "" and s1.mapping_rule_version == ""
    assert "pilot-slice-2026-09-12 — строка 1" in s1.mapping_source_ref
    assert ServiceTemplateSynonym.objects.filter(template=s1.template, text=s1.name, confirmed_by=owner).exists()

    s3 = SalonService.objects.get(tenant=salon, name=ACTIONS[2].service_name)
    assert s3.mapping_status == SalonService.MappingStatus.VERIFIED and s3.template.name == "Массаж спины"
    # спорная строка 5 — не тронута
    s5 = SalonService.objects.get(tenant=salon, name=ACTIONS[4].service_name)
    assert s5.mapping_status == SalonService.MappingStatus.UNMAPPED and s5.template_id is None


@pytest.mark.django_db
def test_second_apply_changes_nothing(salon, owner):
    with pytest.raises(SystemExit):
        _run(by="owner-vps", apply=True)
    stamp = SalonService.objects.get(tenant=salon, name=ACTIONS[0].service_name).mapping_confirmed_at
    with pytest.raises(SystemExit):
        out, _ = _run(by="owner-vps", apply=True)
    out, _ = _run(by="owner-vps")
    assert "уже сделано 2" in out and "изменилось бы 0" in out
    assert SalonService.objects.get(tenant=salon, name=ACTIONS[0].service_name).mapping_confirmed_at == stamp
    assert ServiceTemplateSynonym.objects.count() == 2


@pytest.mark.django_db
def test_include_disputed_applies_the_s3_row_with_health_check_true(salon, owner):
    with pytest.raises(SystemExit):
        _run(by="owner-vps", apply=True, include_disputed=True)
    s5 = SalonService.objects.get(tenant=salon, name=ACTIONS[4].service_name)
    assert s5.mapping_status == SalonService.MappingStatus.VERIFIED
    assert s5.template.name == "Массаж при болях в спине" and s5.template.requires_health_check is True


@pytest.mark.django_db
def test_health_check_mismatch_between_seed_and_db_stops_the_row(salon, owner):
    """Канон в базе с другим RHC, чем обещано владельцу, — команда не решает."""
    ServiceTemplate.objects.filter(name="Массаж спины").update(requires_health_check=True)
    out, _ = _run(by="owner-vps")
    assert "RHC канона True ≠ ожиданию задания False" in out
    assert "изменилось бы 1" in out


@pytest.mark.django_db
def test_template_is_resolved_by_canonical_code_not_by_pair(salon, owner):
    """MAP-AUTO-01: пара (подкатегория, название) — не идентичность.

    Тот же канон под другим именем находится по коду; канон с той же парой,
    но без кода (bootstrap не прошёл) — не находится, строка стоит с причиной.
    """
    ServiceTemplate.objects.filter(canonical_code="1.1.5").update(name="ШВЗ (переименован)")
    out, _ = _run(by="owner-vps")
    assert "канон: 1.1.5 «ШВЗ (переименован)»" in out

    ServiceTemplate.objects.filter(canonical_code="1.1.4").update(canonical_code=None)
    out, _ = _run(by="owner-vps")
    assert "canonical_code=1.1.4 не найден в базе" in out
    assert "изменилось бы 1" in out          # осталась только строка 1


@pytest.mark.django_db
def test_unknown_confirmer_or_tenant_is_a_command_error(salon, owner):
    with pytest.raises(CommandError, match="не найден"):
        _run(by="nobody")
    with pytest.raises(CommandError, match="салон"):
        _run(by="owner-vps", tenant="no-such-salon")
