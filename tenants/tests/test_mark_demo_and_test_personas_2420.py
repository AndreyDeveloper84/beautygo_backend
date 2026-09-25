"""Команда пометки: сухой прогон по умолчанию, запись — по слову владельца (DRF-2420).

Стенд командой правит ВЛАДЕЛЕЦ, поэтому у неё два свойства, и оба проверяются:
без ``--apply`` не меняется ничего, а с ``--apply`` меняется ровно то, что
показал сухой прогон. Отчёт и запись считают одними полями (`Plan`), иначе
прогноз и результат разошлись бы — и разошлись бы молча.

* m1 — сухой прогон не пишет;
* m2 — запись ставит признак названным салонам;
* m3 — повторный запуск ничего не меняет (пишутся только непомеченные);
* m4 — боевой салон пометить нельзя (та же защита, что у сида);
* m5 — личность помечается по username и по id;
* m6 — сухой прогон говорит вслух, если тестовых личностей не названо: иначе
  демо не увидит никто, включая владельца.
"""
from __future__ import annotations

from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command

from tenants.models import Tenant

User = get_user_model()

pytestmark = pytest.mark.django_db

SLUG = "mark2420-demo"


def _run(*args) -> str:
    out = StringIO()
    call_command("mark_demo_and_test_personas", *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def demo_salon() -> Tenant:
    return Tenant.objects.create(slug=SLUG, name="Демо", is_active=True)


@pytest.fixture
def persona() -> User:
    return User.objects.create_user(
        username="mark2420_owner", password="x", role="client", phone="+79992425001",
    )


class TestM1ADryRunWritesNothing:
    def test_nothing_changes_without_apply(self, demo_salon, persona):
        text = _run("--slug", SLUG, "--persona", persona.username)

        assert "СУХОЙ ПРОГОН" in text
        assert f"+ демонстрационным: {SLUG}" in text
        demo_salon.refresh_from_db()
        persona.refresh_from_db()
        assert demo_salon.is_demo is False
        assert persona.is_test_persona is False


class TestM2ApplyMarks:
    def test_the_named_salon_becomes_demo(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username, "--apply")

        demo_salon.refresh_from_db()
        persona.refresh_from_db()
        assert demo_salon.is_demo is True
        assert persona.is_test_persona is True


class TestM3ASecondRunChangesNothing:
    def test_already_marked_rows_are_reported_not_rewritten(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username, "--apply")

        text = _run("--slug", SLUG, "--persona", persona.username, "--apply")

        assert "пометить 0" in text
        assert "уже помечены 1" in text
        assert "записано: салонов 0, личностей 0" in text


class TestM4ThePilotIsProtected:
    """Запрет по ИМЕНИ, а не по строке: он срабатывает и до всякой записи, и
    вне зависимости от того, есть ли такой тенант в этой базе. Пилот
    `formula-tela` — настоящие записи настоящих людей."""

    def test_the_live_salon_cannot_be_marked_demo(self):
        with pytest.raises(CommandError, match="боевой салон"):
            _run("--slug", "formula-tela", "--apply")

    def test_the_refusal_comes_before_the_dry_run_too(self):
        with pytest.raises(CommandError, match="боевой салон"):
            _run("--slug", "formula-tela")


class TestM5APersonaIsNamedNotGuessed:
    def test_by_username_and_by_id(self, persona):
        second = User.objects.create_user(
            username="mark2420_shooter", password="x", role="client",
            phone="+79992425002",
        )

        _run("--persona", persona.username, "--persona", str(second.pk), "--apply")

        persona.refresh_from_db()
        second.refresh_from_db()
        assert persona.is_test_persona is True
        assert second.is_test_persona is True

    def test_an_unknown_name_is_reported_not_invented(self, demo_salon):
        text = _run("--slug", SLUG, "--persona", "нет-такого")

        assert "личности нет в базе: нет-такого" in text


class TestM6SilenceAboutPersonasIsSaidOutLoud:
    def test_marking_salons_without_a_persona_warns(self, demo_salon):
        text = _run("--slug", SLUG)

        assert "демо не увидит НИКТО" in text


class TestM7TheMarkIsReversible:
    """Обратный ход: пометить не тот салон дешевле всего в тот день, когда
    список правят руками. Без `--unmark` единственным способом исправить это
    осталась бы оболочка на стенде — то есть ровно то, чего команда и
    существует, чтобы не делать (найдено ревью)."""

    def test_unmark_removes_the_flags(self, demo_salon, persona):
        _run("--slug", SLUG, "--persona", persona.username, "--apply")
        demo_salon.refresh_from_db()
        persona.refresh_from_db()
        # Сначала о НАЛИЧИИ: признаки действительно стоят.
        assert demo_salon.is_demo is True
        assert persona.is_test_persona is True

        _run("--unmark", "--slug", SLUG, "--persona", persona.username, "--apply")

        demo_salon.refresh_from_db()
        persona.refresh_from_db()
        assert demo_salon.is_demo is False
        assert persona.is_test_persona is False

    def test_unmark_dry_run_writes_nothing_and_says_which_way_it_goes(
        self, demo_salon, persona
    ):
        _run("--slug", SLUG, "--persona", persona.username, "--apply")

        text = _run("--unmark", "--slug", SLUG, "--persona", persona.username)

        assert "СНЯТИЕ" in text
        assert f"- демонстрационным: {SLUG}" in text
        demo_salon.refresh_from_db()
        assert demo_salon.is_demo is True, "сухой прогон снял признак"

    def test_a_second_unmark_changes_nothing(self, demo_salon):
        _run("--slug", SLUG, "--apply")
        _run("--unmark", "--slug", SLUG, "--apply")

        text = _run("--unmark", "--slug", SLUG, "--apply")

        assert "пометить 0" in text
        assert "уже помечены 1" in text


class TestM8TheProtectedListIsOne:
    """Список защищённых слагов берётся у сида, а не копируется.

    Вторая копия разошлась бы в первый же день, когда появится второй живой
    салон, и расхождение здесь стоит настоящих записей настоящих людей.
    """

    def test_the_command_reads_the_seed_list(self):
        from services.management.commands.seed_demo_salons import PROTECTED_SLUGS
        from tenants.management.commands import mark_demo_and_test_personas as cmd

        assert cmd.SEED_PROTECTED_SLUGS is PROTECTED_SLUGS

    def test_an_extra_protected_slug_is_refused(self, demo_salon):
        """`--protect` перечень расширяет: защищённый слаг, который нельзя
        назвать явно, защищён только до первой опечатки в списке."""
        with pytest.raises(CommandError, match="боевой салон"):
            _run("--slug", SLUG, "--protect", SLUG, "--apply")

        demo_salon.refresh_from_db()
        assert demo_salon.is_demo is False
