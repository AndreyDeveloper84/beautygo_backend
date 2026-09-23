"""`backfill_solo_tenant_kind` — вид соло-мастерам, заведённым до G4 (DRF-2325, №31).

Признак `Tenant.kind` появился с G4 (DRF-1828) и пишется только provisioning
solo-workspace; миграция 0007 поставила всем прежним `salon` по умолчанию —
«умолчание, а не доказательство салона». Из-за этого соло-мастер, заведённый
раньше, получает отказ каталога «место ведёт владелец салона», хотя владелец —
он сам (замер DRF-2254).

Доказательство происхождения — не префикс слага (G4 прямо сказал, что имя
производной ничего не доказывает), а СПИСОК ИЗ БОТА: бот пересчитывает слаг
соло-тенанта по личности (`solo_onboarding._solo_tenant_slug`) и отдаёт
совпавшие. Каталог и бот связаны здесь слагом: `ensure_tenant` заводил строку
со своим UUID.

* b1 — сухой прогон по умолчанию: отчёт есть, в базе ничего;
* b2 — `--apply` переводит salon→solo; повтор говорит «уже solo» и не пишет;
* b3 — слаг не найден; тенант вне списка не тронут;
* b4 — 2+ живых мастера: не переводим, называем отдельно (решение главного
  окна: это случай B замера, смена вида отдала бы самообслуживание не только
  владельцу); ноль живых мастеров — тоже не переводим (служебный тенант);
* b5 — список из файла и из stdin, пустые строки и комментарии пропускаются;
* b6 — без списка команда отказывает: массовой заливки по префиксу нет;
* b7 — неактивный тенант и тенант неизвестного вида пропущены и названы;
* b8 — числа сухого прогона равны числам записи, и «переведено» считает
  реальные строки, а не намерение.
"""
from __future__ import annotations

from contextlib import nullcontext
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db import DatabaseError
from django.db.models import QuerySet

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

SOLO_SLUG = "solo-max-2325abcd"
TEAM_SLUG = "solo-max-2325team"


def _run(*args, stdin_text: str | None = None, **kw):
    out, err = StringIO(), StringIO()
    code = 0
    patcher = (
        patch("sys.stdin", StringIO(stdin_text)) if stdin_text is not None else nullcontext()
    )
    try:
        with patcher:
            call_command("backfill_solo_tenant_kind", *args, stdout=out, stderr=err, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def _counts(out: str) -> dict[str, str]:
    """Числовые строки ОТЧЁТА: «ключ: N» без строк-перечислений (они с отступа).

    Предмет замера печатается до отчёта и отделён пустой строкой; его строки
    тоже содержат двоеточие и несут возраст В МИНУТАХ — попади они сюда,
    сравнение двух прогонов ломалось бы на смене минуты.
    """
    report = out.split("\n\n", 1)[-1]
    return {
        line.split(":")[0]: line.split(":", 1)[1].strip()
        for line in report.splitlines()
        if ":" in line and not line.startswith(" ")
    }


def _tenant(slug: str, *, kind: str = Tenant.Kind.SALON, is_active: bool = True) -> Tenant:
    return Tenant.objects.create(
        slug=slug, name=f"Студия {slug}", kind=kind, is_active=is_active
    )


def _master(tenant: Tenant, suffix: str, *, deleted: bool = False) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"m2325{suffix}", password="x", role="specialist", phone=f"+7999000{suffix}"
    )
    if deleted:
        from django.utils import timezone

        user.deleted_at = timezone.now()
        user.save(update_fields=["deleted_at"])
    profile = user.specialist_profile
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return profile


@pytest.fixture
def solo_tenant() -> Tenant:
    tenant = _tenant(SOLO_SLUG)
    _master(tenant, "1")
    return tenant


class TestB1DryRunByDefault:
    def test_the_report_names_the_change_and_writes_nothing(self, solo_tenant) -> None:
        code, out, _ = _run(stdin_text=f"{SOLO_SLUG}\n")

        assert code == 0
        assert SOLO_SLUG in out
        assert "сухой прогон" in out.lower()
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SALON


class TestB2ApplyChangesOnceAndIsIdempotent:
    def test_apply_sets_solo_and_a_repeat_writes_nothing(self, solo_tenant) -> None:
        code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\n")
        assert code == 0
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SOLO
        assert "переведено: 1" in out

        code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\n")

        assert code == 0
        assert "уже solo: 1" in out
        assert "переведено: 0" in out
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SOLO

    def test_the_conversion_leaves_a_trace_in_updated_at(self, solo_tenant) -> None:
        """`update()` минует auto_now — без явного updated_at след теряется."""
        before = Tenant.all_objects.get(pk=solo_tenant.pk).updated_at

        _run("--apply", stdin_text=f"{SOLO_SLUG}\n")

        assert Tenant.all_objects.get(pk=solo_tenant.pk).updated_at > before


class TestB3OnlyWhatTheListNames:
    def test_unknown_slug_is_named_and_a_stranger_is_untouched(self, solo_tenant) -> None:
        stranger = _tenant("salon-2325-stranger")
        _master(stranger, "9")

        code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\nsolo-max-2325none\n")

        assert code == 0
        assert "нет в каталоге: 1" in out
        stranger.refresh_from_db()
        assert stranger.kind == Tenant.Kind.SALON


class TestB4MasterCountDecides:
    def test_two_live_masters_are_skipped(self) -> None:
        team = _tenant(TEAM_SLUG)
        _master(team, "2")
        _master(team, "3")

        code, out, _ = _run("--apply", stdin_text=f"{TEAM_SLUG}\n")

        assert code == 0
        assert "с командой (2+ мастера), пропущено: 1" in out
        team.refresh_from_db()
        assert team.kind == Tenant.Kind.SALON

    def test_a_deleted_master_does_not_make_a_team(self) -> None:
        tenant = _tenant("solo-max-2325gone")
        _master(tenant, "4")
        _master(tenant, "5", deleted=True)

        code, _out, _ = _run("--apply", stdin_text="solo-max-2325gone\n")

        assert code == 0
        tenant.refresh_from_db()
        assert tenant.kind == Tenant.Kind.SOLO

    def test_a_tenant_without_live_masters_is_skipped(self) -> None:
        """Ноль мастеров — признак служебного тенанта, и вид solo снял бы с
        него сразу две защиты (стирание места + выдачу адреса как личных
        данных). Отказ печатает число — как у соседней команды переноса."""
        service = _tenant("solo-max-2325serv")
        _master(service, "6", deleted=True)

        code, out, _ = _run("--apply", stdin_text="solo-max-2325serv\n")

        assert code == 0
        assert "без живых мастеров, пропущено: 1" in out
        assert "живых мастеров нет (служебный тенант" in out
        service.refresh_from_db()
        assert service.kind == Tenant.Kind.SALON

    def test_the_count_is_scoped_to_the_tenant(self) -> None:
        """Счёт мастеров без `tenant=` был бы глобальным: соло-мастер рядом с
        чужой командой перестал бы переводиться, а чужая команда — считаться."""
        solo = _tenant("solo-max-2325one")
        _master(solo, "7")
        crowd = _tenant("salon-2325-crowd")
        _master(crowd, "8")
        _master(crowd, "10")

        code, out, _ = _run("--apply", stdin_text=f"{solo.slug}\n{crowd.slug}\n")

        assert code == 0
        assert "переведено: 1" in out
        solo.refresh_from_db()
        crowd.refresh_from_db()
        assert solo.kind == Tenant.Kind.SOLO
        assert crowd.kind == Tenant.Kind.SALON


class TestB5TheListCanComeFromAFile:
    def test_file_input_ignores_blanks_and_comments(self, solo_tenant, tmp_path) -> None:
        path = tmp_path / "slugs.txt"
        path.write_text(f"# список бота\n\n{SOLO_SLUG}\n", encoding="utf-8")

        code, out, _ = _run("--from-file", str(path), "--apply")

        assert code == 0
        assert "переведено: 1" in out
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SOLO

    def test_a_list_saved_with_a_bom_still_matches(self, solo_tenant, tmp_path) -> None:
        """Блокнот Windows пишет BOM; без utf-8-sig первый слаг «не найден»."""
        path = tmp_path / "bom.txt"
        path.write_text(f"{SOLO_SLUG}\n", encoding="utf-8-sig")

        code, out, _ = _run("--from-file", str(path), "--apply")

        assert code == 0
        assert _counts(out)["переведено"] == "1"
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SOLO

    def test_an_unreadable_file_refuses_and_names_the_path(self, tmp_path) -> None:
        missing = tmp_path / "нет-такого.txt"

        code, _out, err = _run("--from-file", str(missing))

        assert code == 2
        assert "нет-такого.txt" in err

    def test_the_report_carries_no_people(self, solo_tenant) -> None:
        code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\n")

        assert code == 0
        assert SOLO_SLUG in out  # наличие: слаг в отчёте есть
        assert "m2325" not in out  # имён и логинов людей нет
        assert str(solo_tenant.id) not in out

    def test_repeats_in_the_list_are_counted_both_ways(self, solo_tenant) -> None:
        code, out, _ = _run(stdin_text=f"{SOLO_SLUG}\n{SOLO_SLUG}\n")

        assert code == 0
        assert "строк от бота: 2" in out
        assert "в списке бота (без повторов): 1" in out


class TestB6NoListNoWork:
    def test_without_a_list_it_refuses(self, solo_tenant) -> None:
        code, _out, err = _run(stdin_text="")

        assert code == 2
        assert "список" in err.lower()
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SALON


class TestB7NotOursToDecide:
    def test_an_inactive_tenant_is_named_not_converted(self) -> None:
        sleeping = _tenant("solo-max-2325off", is_active=False)
        _master(sleeping, "11")

        code, out, _ = _run("--apply", stdin_text="solo-max-2325off\n")

        assert code == 0
        assert "неактивен, пропущено: 1" in out
        assert Tenant.all_objects.get(pk=sleeping.pk).kind == Tenant.Kind.SALON

    def test_a_kind_the_code_does_not_know_is_named_not_converted(self) -> None:
        odd = _tenant("solo-max-2325odd")
        Tenant.all_objects.filter(pk=odd.pk).update(kind="agency")
        _master(odd, "12")

        code, out, _ = _run("--apply", stdin_text="solo-max-2325odd\n")

        assert code == 0
        assert "вид, которого код не знает, пропущено: 1" in out
        assert Tenant.all_objects.get(pk=odd.pk).kind == "agency"


class TestB8TheNumbersAreHonest:
    def test_the_dry_run_numbers_equal_the_apply_numbers(self, solo_tenant) -> None:
        """Сухой прогон утверждает владелец — он обязан совпасть с записью."""
        team = _tenant(TEAM_SLUG)
        _master(team, "13")
        _master(team, "14")
        listing = f"{SOLO_SLUG}\n{TEAM_SLUG}\nsolo-max-2325none\n"

        _code, dry, _ = _run(stdin_text=listing)
        _code, wet, _ = _run("--apply", stdin_text=listing)

        for key, value in _counts(dry).items():
            if key == "Режим":
                continue
            assert _counts(wet)[key] == value, key
        # Не подстрокой: «переведено: 1» входит и в «будет переведено: 1», и
        # проверка прошла бы, даже если запись вернула ноль.
        assert _counts(wet)["переведено"] == "1"

    def test_a_row_changed_between_the_plan_and_the_write_is_told(self, solo_tenant) -> None:
        """«переведено» считает реальные строки: намерение о них не отчитывается."""

        def _steal(self, plan, *, apply):  # разбор прошёл, запись ещё нет
            Tenant.all_objects.filter(pk=solo_tenant.pk).update(kind=Tenant.Kind.SOLO)

        with patch(
            "tenants.management.commands.backfill_solo_tenant_kind.Command._report", _steal
        ):
            code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\n")

        assert code == 0
        assert _counts(out)["переведено"] == "0"
        assert "не переведено: 1" in out

    def test_a_failure_midway_leaves_nothing_written(self) -> None:
        """Записи идут одной транзакцией: падение на второй строке не оставляет
        переведённой первую. Ради этого отчёт печатается ДО записи, а `atomic()`
        охватывает весь список, а не отдельную строку."""
        first = _tenant("solo-max-2325r1")
        _master(first, "15")
        second = _tenant("solo-max-2325r2")
        _master(second, "16")
        calls = {"n": 0}
        real_update = QuerySet.update

        def boom(self, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise DatabaseError("связь оборвалась на второй строке")
            return real_update(self, **kwargs)

        with patch.object(QuerySet, "update", boom), pytest.raises(DatabaseError):
            _run("--apply", stdin_text=f"{first.slug}\n{second.slug}\n")

        assert calls["n"] == 2  # наличие: до второй строки запись дошла
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.kind == Tenant.Kind.SALON
        assert second.kind == Tenant.Kind.SALON
