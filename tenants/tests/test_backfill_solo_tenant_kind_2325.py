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
  владельцу);
* b5 — список из файла и из stdin, пустые строки и комментарии пропускаются;
* b6 — без списка команда отказывает: массовой заливки по префиксу нет.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.core.management import call_command

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

SOLO_SLUG = "solo-max-2325abcd"
TEAM_SLUG = "solo-max-2325team"


def _run(*args, stdin_text: str | None = None, **kw):
    out, err = StringIO(), StringIO()
    code = 0
    if stdin_text is not None:
        kw["stdin"] = StringIO(stdin_text)
    try:
        call_command("backfill_solo_tenant_kind", *args, stdout=out, stderr=err, **kw)
    except SystemExit as exc:
        code = exc.code
    return code, out.getvalue(), err.getvalue()


def _tenant(slug: str, *, kind: str = Tenant.Kind.SALON) -> Tenant:
    return Tenant.objects.create(slug=slug, name=f"Студия {slug}", kind=kind)


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


class TestB3OnlyWhatTheListNames:
    def test_unknown_slug_is_named_and_a_stranger_is_untouched(self, solo_tenant) -> None:
        stranger = _tenant("salon-2325-stranger")

        code, out, _ = _run("--apply", stdin_text=f"{SOLO_SLUG}\nsolo-max-2325none\n")

        assert code == 0
        assert "нет в каталоге: 1" in out
        stranger.refresh_from_db()
        assert stranger.kind == Tenant.Kind.SALON


class TestB4TeamTenantsAreNamedNotChanged:
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


class TestB5TheListCanComeFromAFile:
    def test_file_input_ignores_blanks_and_comments(self, solo_tenant, tmp_path) -> None:
        path = tmp_path / "slugs.txt"
        path.write_text(f"# список бота\n\n{SOLO_SLUG}\n", encoding="utf-8")

        code, out, _ = _run("--from-file", str(path), "--apply")

        assert code == 0
        assert "переведено: 1" in out
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SOLO

    def test_the_report_carries_no_people(self, solo_tenant) -> None:
        _run("--apply", stdin_text=f"{SOLO_SLUG}\n")
        code, out, _ = _run(stdin_text=f"{SOLO_SLUG}\n")

        assert code == 0
        assert SOLO_SLUG in out  # наличие: слаг в отчёте есть
        assert "m2325" not in out  # имён и логинов людей нет
        assert str(solo_tenant.id) not in out


class TestB6NoListNoWork:
    def test_without_a_list_it_refuses(self, solo_tenant) -> None:
        code, _out, err = _run(stdin_text="")

        assert code == 2
        assert "список" in err.lower()
        solo_tenant.refresh_from_db()
        assert solo_tenant.kind == Tenant.Kind.SALON
