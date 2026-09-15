"""Часовой пояс мастера — только настоящее имя IANA (раздел Q, P2 владельца).

``SpecialistProfile.timezone`` — строка без проверки. Восемь мест делают
``ZoneInfo(specialist.timezone)`` без защиты: запись, слоты, сторож записи,
день салона. Опечатка «Europe/Moskow», сохранённая один раз, превращает каждую
из этих поверхностей в 500 для всех клиентов мастера.

Замер пилота 15.09 21:48 UTC: 31 мастер, у всех «Europe/Moscow», плохих
значений 0. Это наблюдение, не контроль. Сторож — проверка на каждом писателе:
* админка: форма мастера и блок мастеров на форме салона — ошибка на поле
  ``timezone``, строка не сохраняется;
* сид ``seed_demo_salons`` — пояс из файла проверяется до записи.
API, соло-кабинет и самообслуживание мастера пояс не пишут (поля нет в
сериализаторах). Перепись ниже держит это так.

Проверка — точное членство в ``zoneinfo.available_timezones()``, а не «удалось
ли построить ``ZoneInfo``». ``zoneinfo`` читает файлы: на Windows (файловая
система без учёта регистра + пакет tzdata) ``ZoneInfo("europe/moscow")`` может
загрузиться, а на Linux в CI — нет. Проверка через построение приняла бы здесь
то, что отвергнет CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

REPO = Path(__file__).resolve().parents[2]
PROFILE_CHANGE = "admin:users_specialistprofile_change"
TENANT_CHANGE = "admin:tenants_tenant_change"


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="tz-iana-salon", name="Салон пояса")


@pytest.fixture
def master(salon):
    user = User.objects.create_user(
        username="tz-iana-master", password="x",  # pragma: allowlist secret
        role="specialist", phone="+79991980001",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = salon
    profile.display_name = "Мастер пояса"
    profile.timezone = "Europe/Moscow"
    profile.save()
    return profile


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        username="tz-iana-owner", password="pw",  # pragma: allowlist secret
        email="tz-iana@example.com", role="admin",
    )


def _client(owner) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(owner)
    return c


def _post_data_from(response) -> dict:
    """Тело POST, какое отправил бы браузер со страницы формы без правок.

    Собирается из контекста GET: основная форма, management-формы инлайнов
    и их строки. Значение берётся тем же виджетом, который его рисует.
    """
    from django import forms

    data: dict[str, str] = {}

    def put(form) -> None:
        for bf in form:
            widget = bf.field.widget
            value = bf.value()
            name = bf.html_name
            if isinstance(widget, forms.CheckboxInput):
                if value:
                    data[name] = "on"
                continue
            inner = getattr(widget, "widget", widget)  # RelatedFieldWidgetWrapper
            if isinstance(inner, forms.MultiWidget):
                parts = value if isinstance(value, (list, tuple)) else inner.decompress(value)
                for i, part in enumerate(parts):
                    data[f"{name}_{i}"] = "" if part is None else str(part)
                continue
            formatted = inner.format_value(value)
            if isinstance(formatted, (list, tuple)):
                formatted = formatted[0] if formatted else ""
            data[name] = "" if formatted is None else str(formatted)

    put(response.context["adminform"].form)
    for inline in response.context["inline_admin_formsets"]:
        put(inline.formset.management_form)
        for form in inline.formset.forms:
            put(form)
    return data


def _stored_timezone(master) -> str:
    return SpecialistProfile.objects.values_list("timezone", flat=True).get(pk=master.pk)


# ---------------------------------------------------------------------------
# Модель — любой путь через full_clean()
# ---------------------------------------------------------------------------


class TestTheModel:
    @pytest.mark.parametrize(
        "value",
        ["Europe/Moskow", "europe/moscow", "Europe/../Moscow"],
        ids=["typo", "lowercase", "path"],
    )
    def test_a_name_that_is_not_an_iana_zone_is_refused(self, master, value):
        master.timezone = value
        with pytest.raises(ValidationError) as exc:
            master.full_clean()
        assert "timezone" in exc.value.message_dict

    @pytest.mark.parametrize("value", ["UTC", "America/New_York", "Asia/Yekaterinburg"])
    def test_a_real_iana_zone_validates(self, master, value):
        master.timezone = value
        master.full_clean()


# ---------------------------------------------------------------------------
# Админка — форма мастера и блок мастеров на форме салона
# ---------------------------------------------------------------------------


class TestTheMasterAdmin:
    def test_a_typo_is_a_form_error_on_the_field_and_nothing_is_saved(self, owner, master):
        client = _client(owner)
        url = reverse(PROFILE_CHANGE, args=[master.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        data["timezone"] = "Europe/Moskow"
        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        assert "timezone" in r.context["adminform"].form.errors
        assert _stored_timezone(master) == "Europe/Moscow"

    def test_a_real_zone_saves(self, owner, master):
        # Положительная стража: сборщик тела рабочий, и правило не отказывает
        # каждой правке мастера.
        client = _client(owner)
        url = reverse(PROFILE_CHANGE, args=[master.pk])
        data = _post_data_from(client.get(url))
        data["timezone"] = "Asia/Yekaterinburg"
        r = client.post(url, data)
        assert r.status_code == 302, (
            r.status_code, r.context["adminform"].form.errors if r.context else None,
        )
        assert _stored_timezone(master) == "Asia/Yekaterinburg"


class TestTheSalonMastersInline:
    def test_a_bad_zone_in_the_masters_block_is_a_form_error_and_nothing_is_saved(self, owner, salon, master):
        client = _client(owner)
        url = reverse(TENANT_CHANGE, args=[salon.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        formset = next(
            inline.formset for inline in page.context["inline_admin_formsets"]
            if inline.formset.model is SpecialistProfile
        )
        row = next(i for i, f in enumerate(formset.forms) if f.instance.pk == master.pk)
        data[f"{formset.prefix}-{row}-timezone"] = "Mars/Olympus"
        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        masters = next(
            inline.formset for inline in r.context["inline_admin_formsets"]
            if inline.formset.model is SpecialistProfile
        )
        assert "timezone" in masters.forms[row].errors, masters.errors
        assert _stored_timezone(master) == "Europe/Moscow"


# ---------------------------------------------------------------------------
# Сид демо-салонов — пояс из файла проверяется до записи
# ---------------------------------------------------------------------------

SEEDS = REPO / "services" / "seeds"
CANONICAL = SEEDS / "canonical_catalog_2026-07.json"
GOAL_OPTIONS = SEEDS / "goal_options_2026-08.json"
DEMO_SALONS = SEEDS / "demo_salons_2026-08.json"


@pytest.fixture
def catalog(db):
    """Настоящий канонический каталог и цели — как у соседнего теста сида.

    Без него сид отказал бы по непопавшим шаблонам, и отказ по поясу был бы
    не отличим от отказа по другой причине.
    """
    from django.core.management import call_command

    call_command("seed_canonical_catalog", "--file", str(CANONICAL), verbosity=0)
    call_command("seed_goal_options", "--file", str(GOAL_OPTIONS), verbosity=0)


def _one_salon_file(tmp_path, timezone: str) -> tuple[Path, dict]:
    import json

    document = json.loads(DEMO_SALONS.read_text(encoding="utf-8"))
    document["salons"] = document["salons"][:1]
    document["salons"][0]["timezone"] = timezone
    path = tmp_path / "one_salon.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    return path, document["salons"][0]


class TestTheDemoSeed:
    def test_a_bad_zone_in_the_seed_file_is_refused_before_anything_is_written(self, catalog, tmp_path):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        path, salon_doc = _one_salon_file(tmp_path, "Europe/Moskow")
        with pytest.raises(CommandError, match="Europe/Moskow"):
            call_command("seed_demo_salons", "--file", str(path), "--apply")
        assert not Tenant.all_objects.filter(slug=salon_doc["slug"]).exists()
        usernames = [person["username"] for person in salon_doc["specialists"]]
        assert not SpecialistProfile.objects.filter(user__username__in=usernames).exists()

    def test_the_same_file_with_a_real_zone_passes_the_seed_guards(self, catalog, tmp_path):
        # Положительная стража: этот файл проходит проверку шаблонов, значит
        # отказ выше — именно по поясу. Без --apply ничего не пишется.
        from io import StringIO

        from django.core.management import call_command

        path, _ = _one_salon_file(tmp_path, "Asia/Yekaterinburg")
        call_command("seed_demo_salons", "--file", str(path), stdout=StringIO())


# ---------------------------------------------------------------------------
# Перепись: пояс мастера пишут только названные места
# ---------------------------------------------------------------------------

WRITE = re.compile(r"\b(?:profile|specialist|sp|master|instance|obj)\.timezone\s*=(?!=)")
SKIP_PARTS = {"tests", "migrations", "venv", ".venv", "node_modules", "nutrition"}

#: Where a master's timezone may be assigned in code, each with its reason.
ALLOWED = {
    "recommendation/management/commands/seed_golden.py": "seed: the constant Europe/Moscow",
    "services/management/commands/seed_demo_salons.py": (
        "seed: the value from the JSON config, validated before the write"
    ),
}


def _writers() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if set(rel.split("/")) & SKIP_PARTS:
            continue
        hits = WRITE.findall(path.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            found[rel] = len(hits)
    return found


class TestTheWriterCensus:
    def test_the_pattern_catches_the_spelling_it_names(self):
        assert WRITE.search('profile.timezone = salon.get("timezone")')
        assert WRITE.search("specialist.timezone=value")
        assert not WRITE.search("if profile.timezone == 'UTC':")

    def test_the_scan_is_not_vacuous(self):
        writers = _writers()
        assert writers, "the scan found no writer at all — the census would pass on nothing"
        for rel in ALLOWED:
            assert rel in writers, f"{rel} is allowed but no longer writes the field — update the census"

    def test_no_writer_outside_the_named_places(self):
        stray = {rel: n for rel, n in _writers().items() if rel not in ALLOWED}
        assert stray == {}, f"a master's timezone is assigned outside the named places: {stray}"
