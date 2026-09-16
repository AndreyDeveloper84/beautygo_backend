"""Телефон не приходит аргументом командной строки (DRF-2024).

`provision_salon_admin` — операторская команда онбординга, и `ADMIN_GUIDE`
предписывает выполнять её **по ssh на хосте пилота**. Пока телефон стоит в
`--phone`, он попадает в четыре места: событие Sentry (его закрывает скруббер
`extra["sys.argv"]`), вывод `ps` на хосте, история оболочки оператора и журнал
аудита хоста. Последние три мимо приложения, и никакой хук до них не дотянется —
закрыть их можно только тем, чтобы значение не становилось аргументом.

**Что сохраняется.** Докстринг команды называет осознанное решение: телефон —
аргумент, «never a literal in this repository». Чтение из stdin закрывает argv
и **сохраняет** это: в репозитории номера по-прежнему нет.

Форма вызова остаётся однострочной: `docker exec -it dev-web-1 … --tenant
<slug>`, номер печатается на приглашение. Для неинтерактивного прогона —
перенаправление из файла (`< /root/phone.txt`), у которого свой предел: права
и удаление файла живут вне этой команды.

**Не `printf '…' | docker exec …`**: канал не убирает номер, а переносит его в
argv процесса `printf`, где его видят `ps`, история оболочки и журнал аудита
хоста — ровно как прежний `--phone` (нашёл ayla-9d при сверке формы).

**Предел, названный честно:** сторож проверяет ПАРСЕР и поведение команды. Если
однажды номер начнут передавать в аргументе другой команды, этот тест этого не
увидит — перепись 16.09: из 48 управляющих команд каталога `--phone` принимала
ровно одна (эта), `--email` / `--first_name` / `--last_name` — ни одна, а
`--name` во второй команде (`backfill_tenants`) — имя ТЕНАНТА, не человека.
"""
from __future__ import annotations

import io

import pytest
from django.core.management import call_command, load_command_class
from django.core.management.base import CommandError

from tenants.models import Tenant
from users.models import TenantUserRelationship, User

#: Тестовый диапазон `+7 9xx` — маска, которую пропускает `scripts/pii_guard.py`.
PHONE = "+79990002024"
OWNER_NAME = "Владелец Салона"


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="stdin-2024", name="Салон DRF-2024")


def _option_strings() -> set[str]:
    """Опции парсера команды — предмет, а не пересказ докстринга."""
    command = load_command_class("users", "provision_salon_admin")
    parser = command.create_parser("manage.py", "provision_salon_admin")
    return {option for action in parser._actions for option in action.option_strings}


def _active(user, tenant):
    return TenantUserRelationship.objects.filter(
        user=user, tenant=tenant, is_active=True,
    ).first()


class TestPersonalDataIsNotAnArgument:
    def test_the_parser_offers_no_phone_and_no_person_name(self):
        """Нет опции — нет и значения в `ps`, истории и журнале аудита."""
        options = _option_strings()

        assert "--phone" not in options, "телефон снова стал аргументом — DRF-2024"
        assert "--name" not in options, "имя человека снова стало аргументом — DRF-2024"

    def test_the_tenant_slug_stays_an_argument(self):
        """Слаг салона — не персональные данные; его убирать не за что."""
        options = _option_strings()

        assert "--tenant" in options
        assert "--dry-run" in options


class TestTheOldFormRefusesClearly:
    """Снятый флаг обязан отказывать понятно, иначе оператор повторит ход.

    Экспозицию это НЕ уменьшает: к моменту запуска Python номер уже в `ps`,
    истории оболочки, журнале аудита и `extra["sys.argv"]` события Sentry —
    утечка происходит при exec, до нашего кода. Но argparse-ное «unrecognized
    arguments: --phone» человек читает как «версия не та» и **повторяет
    вызов**, а каждый повтор — новая утечка. Поэтому отказ называет, что
    делать, и говорит, что номер уже засвечен.

    Проверяется `run_from_argv`, а не парсер: флаг не возвращается ни в
    парсер, ни в `--help` — иначе он снова начнёт ПРИНИМАТЬ значение.
    """

    def test_phone_flag_is_refused_with_guidance_not_argparse_noise(self):
        command = load_command_class("users", "provision_salon_admin")

        with pytest.raises(CommandError, match="stdin"):
            command.run_from_argv(
                ["manage.py", "provision_salon_admin", "--tenant", "x", "--phone", PHONE],
            )

    def test_name_flag_is_refused_the_same_way(self):
        command = load_command_class("users", "provision_salon_admin")

        with pytest.raises(CommandError, match="stdin"):
            command.run_from_argv(
                ["manage.py", "provision_salon_admin", "--tenant", "x", "--name", OWNER_NAME],
            )


class TestItAsksBeforeItReads:
    """Ожидание ввода обязано быть видно, иначе команда читается как зависшая.

    Приглашение печатается **всегда и в stderr**, а не по `isatty()`:
    `docker exec -i …` без перенаправления даёт не-TTY, но ждёт живого
    человека — там приглашение нужнее всего (нашёл ayla-9d). Молчаливое
    ожидание опаснее лишней строки: оператор убьёт команду и вернётся к
    прежней форме с флагом, то есть к утечке.

    `stdout` при этом остаётся чистым: приглашение — диалог с человеком, а не
    результат команды.
    """

    def test_the_prompt_is_printed_before_reading(self, salon):
        err = io.StringIO()
        call_command(
            "provision_salon_admin",
            tenant=salon.slug,
            stdin=io.StringIO(f"{PHONE}\n"),
            stderr=err,
        )

        assert "+7" in err.getvalue(), "нет приглашения — ожидание ввода выглядит зависанием"

    def test_the_prompt_does_not_pollute_stdout(self, salon):
        """Результат команды и диалог с человеком идут разными потоками."""
        out = io.StringIO()
        call_command(
            "provision_salon_admin",
            tenant=salon.slug,
            stdin=io.StringIO(f"{PHONE}\n"),
            stdout=out,
        )

        assert "Телефон" not in out.getvalue()


class TestPhoneComesFromStdin:
    def test_phone_from_stdin_creates_the_account_and_grants_admin(self, salon):
        call_command(
            "provision_salon_admin",
            tenant=salon.slug,
            stdin=io.StringIO(f"{PHONE}\n"),
        )

        user = User.objects.get(phone=PHONE)
        relationship = _active(user, salon)
        assert relationship is not None
        assert relationship.role == TenantUserRelationship.Role.ADMIN

    def test_optional_display_name_is_the_second_line(self, salon):
        call_command(
            "provision_salon_admin",
            tenant=salon.slug,
            stdin=io.StringIO(f"{PHONE}\n{OWNER_NAME}\n"),
        )

        assert User.objects.get(phone=PHONE).first_name == OWNER_NAME

    def test_empty_input_fails_loudly(self, salon):
        """Пустой stdin — не «создать без телефона», а отказ с именем причины.

        Совпадение по «stdin gave no number», а не по слову «phone»: до правки
        тест был бы зелёным по чужой причине — `call_command` без `--phone`
        отвечает «the following arguments are required: --phone», и слово
        «phone» там тоже есть. Зелёный до правки ничего не доказывает.
        """
        with pytest.raises(CommandError, match="stdin gave no number"):
            call_command(
                "provision_salon_admin",
                tenant=salon.slug,
                stdin=io.StringIO("\n"),
            )

    def test_dry_run_reads_the_phone_but_writes_nothing(self, salon):
        call_command(
            "provision_salon_admin",
            tenant=salon.slug,
            dry_run=True,
            stdin=io.StringIO(f"{PHONE}\n"),
        )

        assert not User.objects.filter(phone=PHONE).exists()
