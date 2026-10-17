"""DRF-2879 — версия знания: отпечаток содержания и основания.

Решение владельца: «Основание входит в версию знания. Значимое изменение
источника, его редакции или интерпретации требует повторного подтверждения и
перепроверки зависимых шагов. Сохранённый план не переписывать молча».

Узлы держат:

* отпечаток ставит база при любой записи строки, включая ``update()``;
* в него входит каждое поле содержания и основания и не входит срок,
  статус, «кто и когда подтвердил»;
* то, что админка считает содержанием утверждения, и то, что входит в
  отпечаток, — один и тот же перечень;
* подтверждённого утверждения с изменённым после подтверждения содержанием
  в базе быть не может — в том числе мимо модели и в две записи;
* читатель версий отдаёт то же, что читает сборка, и различает «знание
  изменилось» и «знание больше не действует» с причиной.

Пределы: строки, подтверждённые до миграции, принимаются неизменёнными с
момента подтверждения (узел держит само правило принятия, не их историю).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from services import capabilities
from services.capabilities import UnreadableReason, claim_fingerprint, knowledge_versions
from services.models import CapabilityGoalLink, GoalOption, ProcedureCapability, ServiceCategory
from services.synthetic import grant_for
from services.tests.test_synthetic_test_mark import _canon, _capability, _link
from users.models import User

pytestmark = pytest.mark.django_db


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Отпечаток", slug="fingerprint-2879")


@pytest.fixture
def curator(db):
    return User.objects.create_user(username="fingerprint-curator", password="x", is_staff=True)


@pytest.fixture
def goal(db):
    return GoalOption.objects.create(key="fingerprint-goal", label="Цель")


@pytest.fixture
def grant(settings, db):
    tester = User.objects.create_user(username="fingerprint-tester", password="x", is_test_persona=True)
    settings.SYNTHETIC_TEST_DATA_ENABLED = True
    settings.SYNTHETIC_TEST_SUBJECT_IDS = [str(tester.pk)]
    return grant_for(tester)


REFUSED = "approved_content_matches_approval"

#: Новое значение для каждого поля содержания — такое, что строка остаётся
#: допустимой для остальных ограничений базы.
CAPABILITY_EDITS = {
    "claim_type": "professional", "claim_scope": "supported", "limitations": "новое ограничение",
    "prohibited_statement": None, "evidence_source": "другая публикация", "evidence_kind": "rct",
    "source_ref": "DOC-other", "key": "renamed_effect", "text_client": "другая формулировка",
    "text_professional": "другая формулировка для мастера", "expected_effect": "другой эффект",
    "result_timeframe": "через месяц", "variability_note": "у всех по-разному",
}
LINK_EDITS = {
    "claim_type": "professional", "claim_scope": "supported", "limitations": "новое ограничение",
    "prohibited_statement": None, "evidence_source": "другая публикация", "evidence_kind": "rct",
    "source_ref": "DOC-other", "capability_id": None, "goal_id": None,
    "course_pattern": None, "result_horizon": None, "variability_note": "у всех по-разному",
}


def _stored(row) -> dict:
    return type(row).objects.filter(pk=row.pk).values("content_fingerprint", "approved_fingerprint", "status").get()


def _refused(action) -> None:
    with pytest.raises(IntegrityError, match=REFUSED):
        with transaction.atomic():
            action()


@pytest.fixture
def draft(category):
    return ProcedureCapability.objects.create(
        templates=[_canon(category, "Канон черновика")], key="draft_effect", text_client="Черновик",
    )


@pytest.fixture
def approved(category, curator):
    return _capability(_canon(category, "Канон"), "real_effect", curator=curator)


# ─── отпечаток ставит база ───────────────────────────────────────────────────


def test_the_database_stamps_a_fingerprint_on_every_row(draft, approved, goal, curator) -> None:
    link = _link(approved, goal, curator=curator)

    for row in (draft, approved, link):
        fingerprint = claim_fingerprint(row)
        assert fingerprint.startswith("v1:") and len(fingerprint) == 67
    assert len({claim_fingerprint(draft), claim_fingerprint(approved), claim_fingerprint(link)}) == 3


def test_equal_content_gives_an_equal_fingerprint(category) -> None:
    """Отпечаток — о содержании, а не о строке: идентификатор и время в него не входят."""
    first = ProcedureCapability.objects.create(templates=[_canon(category, "А")], key="same", text_client="Текст")
    before = claim_fingerprint(first)
    first.delete()

    again = ProcedureCapability.objects.create(templates=[_canon(category, "Б")], key="same", text_client="Текст")

    assert claim_fingerprint(again) == before


@pytest.mark.parametrize("field", sorted(set(ProcedureCapability.FINGERPRINT_FIELDS) | set(
    ProcedureCapability.OWN_FINGERPRINT_FIELDS)))
def test_every_content_field_of_a_capability_moves_its_fingerprint(draft, field) -> None:
    before = claim_fingerprint(draft)
    value = CAPABILITY_EDITS[field]
    if field == "prohibited_statement":
        ProcedureCapability.objects.filter(pk=draft.pk).update(claim_scope="prohibited_claim")
        before, value = claim_fingerprint(draft), "нельзя обещать"

    ProcedureCapability.objects.filter(pk=draft.pk).update(**{field: value})

    assert claim_fingerprint(draft) != before


@pytest.mark.parametrize("field", sorted(set(CapabilityGoalLink.FINGERPRINT_FIELDS) | set(
    CapabilityGoalLink.OWN_FINGERPRINT_FIELDS)))
def test_every_content_field_of_a_goal_link_moves_its_fingerprint(draft, goal, category, field) -> None:
    link = CapabilityGoalLink.objects.create(capability=draft, goal=goal)
    before = claim_fingerprint(link)
    edit = {field: LINK_EDITS[field]}
    if field == "prohibited_statement":
        CapabilityGoalLink.objects.filter(pk=link.pk).update(claim_scope="prohibited_claim")
        before, edit = claim_fingerprint(link), {field: "нельзя обещать"}
    elif field == "capability_id":
        other = ProcedureCapability.objects.create(templates=[_canon(category, "Другой")], key="other_effect")
        edit = {field: other.pk}
    elif field == "goal_id":
        edit = {field: GoalOption.objects.create(key="fingerprint-other-goal", label="Другая").pk}
    elif field == "course_pattern":
        # Курс без оговорки о разбросе и без источника база не примет.
        CapabilityGoalLink.objects.filter(pk=link.pk).update(
            variability_note="число сеансов у всех разное", evidence_source="протокол", source_ref="DOC-course",
        )
        before, edit = claim_fingerprint(link), {field: "обычно курс из нескольких сеансов"}
    elif field == "result_horizon":
        edit = {field: "обычно заметно через несколько недель"}

    CapabilityGoalLink.objects.filter(pk=link.pk).update(**edit)

    assert claim_fingerprint(link) != before


@pytest.mark.parametrize("edit", [
    {"valid_until": timezone.now() + timedelta(days=30)},
    {"reviewed_at": None},
    {"updated_at": timezone.now() + timedelta(days=1)},
])
def test_what_is_not_content_leaves_the_fingerprint_alone(approved, edit) -> None:
    """Срок годности — свойство подтверждения, а не утверждения: продление не меняет версию знания."""
    before = _stored(approved)

    ProcedureCapability.objects.filter(pk=approved.pk).update(**edit)

    assert _stored(approved) == before


def test_bound_procedures_are_not_part_of_the_version(approved, category) -> None:
    """Шаг плана несёт способность, а не процедуру: новый канон у способности версию не меняет."""
    before = claim_fingerprint(approved)

    approved.templates.add(_canon(category, "Ещё один канон"))

    assert claim_fingerprint(approved) == before


@pytest.mark.parametrize("model", ["capability", "link"])
def test_the_admin_and_the_fingerprint_agree_on_what_content_is(model, curator) -> None:
    """«Правка снимает подтверждение» (форма) и «правка меняет версию» (база) — один перечень полей."""
    from django.contrib import admin as django_admin
    from django.test import RequestFactory

    from services.admin import _NOT_CLAIM_CONTENT

    cls = {"capability": ProcedureCapability, "link": CapabilityGoalLink}[model]
    curator.is_superuser = True
    curator.save()
    request = RequestFactory().get("/")
    request.user = curator
    form_fields = set(django_admin.site._registry[cls].get_form(request).base_fields)
    # Привязки процедур — не часть версии (см. узел выше); форма их считает правкой, отпечаток — нет.
    counted_by_the_form = form_fields - set(_NOT_CLAIM_CONTENT) - {"templates", "procedures"}
    in_the_fingerprint = {
        name.removesuffix("_id") for name in (*cls.FINGERPRINT_FIELDS, *cls.OWN_FINGERPRINT_FIELDS)
    }

    assert counted_by_the_form == in_the_fingerprint


# ─── подтверждено именно то, что записано ────────────────────────────────────


def test_approval_records_the_fingerprint_it_was_given_for(draft, approved) -> None:
    assert _stored(draft)["approved_fingerprint"] == ""
    stored = _stored(approved)
    assert stored["approved_fingerprint"] == stored["content_fingerprint"] != ""


def test_approved_content_cannot_be_changed_past_the_model(approved) -> None:
    before = _stored(approved)

    _refused(lambda: ProcedureCapability.objects.filter(pk=approved.pk).update(text_client="Подменённый текст"))
    _refused(lambda: ProcedureCapability.objects.filter(pk=approved.pk).update(source_ref="DOC-forged"))

    assert _stored(approved) == before


def test_approved_content_cannot_be_changed_through_the_model_either(approved) -> None:
    row = ProcedureCapability.objects.get(pk=approved.pk)
    row.text_client = "Подменённый текст"

    _refused(row.save)


def test_a_fresh_approval_in_the_same_write_accepts_the_new_content(approved) -> None:
    before = _stored(approved)

    ProcedureCapability.objects.filter(pk=approved.pk).update(
        text_client="Новая редакция", confirmed_at=timezone.now() + timedelta(seconds=1),
    )

    after = _stored(approved)
    assert after["status"] == "approved"
    assert after["content_fingerprint"] != before["content_fingerprint"]
    assert after["approved_fingerprint"] == after["content_fingerprint"]


def test_content_may_change_once_the_approval_is_withdrawn(approved, curator) -> None:
    ProcedureCapability.objects.filter(pk=approved.pk).update(
        status="system_inference", confirmed_by=None, confirmed_at=None, text_client="Правится в черновике",
    )
    draft_state = _stored(approved)
    assert (draft_state["status"], draft_state["approved_fingerprint"]) == ("system_inference", "")

    ProcedureCapability.objects.filter(pk=approved.pk).update(
        status="approved", confirmed_by=curator, confirmed_at=timezone.now(),
    )

    again = _stored(approved)
    assert again["approved_fingerprint"] == again["content_fingerprint"] == draft_state["content_fingerprint"]


@pytest.mark.parametrize("forged", ["", "v1:forged"])
def test_the_approval_fingerprint_cannot_be_rewritten_by_hand(approved, forged) -> None:
    """Обход в две записи закрыт: стереть или подменить отпечаток подтверждения у подтверждённой строки нельзя."""
    _refused(lambda: ProcedureCapability.objects.filter(pk=approved.pk).update(approved_fingerprint=forged))
    _refused(lambda: ProcedureCapability.objects.filter(pk=approved.pk).update(
        approved_fingerprint=forged, text_client="Подменённый текст"))


def test_a_written_content_fingerprint_is_recomputed_not_trusted(approved) -> None:
    before = _stored(approved)

    ProcedureCapability.objects.filter(pk=approved.pk).update(content_fingerprint="v1:forged")

    assert _stored(approved) == before


def test_a_goal_link_is_held_to_the_same_rule(approved, goal, curator) -> None:
    link = _link(approved, goal, curator=curator)

    _refused(lambda: CapabilityGoalLink.objects.filter(pk=link.pk).update(limitations="подменённое ограничение"))

    stored = _stored(link)
    assert stored["approved_fingerprint"] == stored["content_fingerprint"] != ""


def test_rows_approved_before_the_fingerprint_adopt_their_current_content(category, curator) -> None:
    """Правило заполнения миграции: подтверждённое до неё принимается неизменённым — проверить это нечем."""
    canon = _canon(category, "Канон")
    legacy = _capability(canon, "legacy_effect", curator=curator)
    with connection.cursor() as cursor:
        # Строка «как до миграции»: подтверждена, отпечатков нет. Триггеры на
        # время этой записи выключены ролью репликации — иначе её не получить.
        cursor.execute("SET LOCAL session_replication_role = replica")
        cursor.execute(
            "UPDATE services_procedurecapability SET content_fingerprint = '', approved_fingerprint = '' "
            "WHERE id = %s", [legacy.pk],
        )
        cursor.execute("SET LOCAL session_replication_role = origin")
        assert _stored(legacy) == {"content_fingerprint": "", "approved_fingerprint": "", "status": "approved"}

        cursor.execute("UPDATE services_procedurecapability SET content_fingerprint = content_fingerprint")

    stored = _stored(legacy)
    assert stored["content_fingerprint"].startswith("v1:")
    assert stored["approved_fingerprint"] == stored["content_fingerprint"]
    _refused(lambda: ProcedureCapability.objects.filter(pk=legacy.pk).update(text_client="После принятия"))


def test_the_fingerprint_triggers_are_installed() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname LIKE %s ORDER BY tgname",
            ["%\\_fingerprint"],
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "capabilitygoallink_fingerprint", "procedurecapability_fingerprint",
        ]


# ─── читатель версий ─────────────────────────────────────────────────────────


@pytest.fixture
def world(category, curator, goal):
    real_canon = _canon(category, "Настоящий канон")
    fake_canon = _canon(category, "Синтетический канон", synthetic=True)
    real = _capability(real_canon, "real_effect", curator=curator)
    fake = _capability(fake_canon, "synthetic_effect", synthetic=True)
    return {
        "real": real, "fake": fake,
        "real_link": _link(real, goal, curator=curator), "fake_link": _link(fake, goal, synthetic=True),
    }


def test_versions_for_a_goal_cover_exactly_what_the_composer_reads(grant, world, goal) -> None:
    for asked in (None, grant):
        versions = capabilities.knowledge_versions_helping_goal(goal.key, include_synthetic=asked)
        assert sorted(versions) == list(capabilities.capability_keys_helping_goal(goal.key, include_synthetic=asked))

    versions = capabilities.knowledge_versions_helping_goal(goal.key, include_synthetic=grant)
    real, fake = versions["real_effect"], versions["synthetic_effect"]
    assert (real.capability_id, real.link_id, real.synthetic) == (world["real"].pk, world["real_link"].pk, False)
    assert (real.capability_fingerprint, real.link_fingerprint) == (
        claim_fingerprint(world["real"]), claim_fingerprint(world["real_link"]),
    )
    assert (fake.capability_id, fake.link_id, fake.synthetic) == (world["fake"].pk, world["fake_link"].pk, True)
    assert fake.capability_fingerprint == claim_fingerprint(world["fake"])


def test_a_saved_step_sees_that_its_knowledge_was_reworded_and_reapproved(world, goal) -> None:
    """«Знание изменилось»: строка читается, но отпечаток уже не тот, что сохранён в шаге."""
    saved = capabilities.knowledge_versions_helping_goal(goal.key)["real_effect"]

    ProcedureCapability.objects.filter(pk=world["real"].pk).update(
        text_client="Новая редакция", confirmed_at=timezone.now() + timedelta(seconds=1),
    )

    now = knowledge_versions([saved.capability_id], [saved.link_id])
    capability, link = now["capabilities"][saved.capability_id], now["links"][saved.link_id]
    assert (capability.readable, capability.unreadable_reason) == (True, None)
    assert capability.fingerprint != saved.capability_fingerprint
    assert (link.readable, link.fingerprint) == (True, saved.link_fingerprint)


@pytest.mark.parametrize("edit,reason", [
    ({"status": "system_inference", "confirmed_by": None, "confirmed_at": None}, UnreadableReason.NOT_APPROVED),
    ({"valid_until": timezone.now() - timedelta(days=1)}, UnreadableReason.EXPIRED),
])
def test_a_saved_step_sees_that_its_knowledge_no_longer_holds_and_why(world, goal, edit, reason) -> None:
    """«Знание больше не действует» — отдельный исход, с причиной для человека."""
    saved = capabilities.knowledge_versions_helping_goal(goal.key)["real_effect"]

    ProcedureCapability.objects.filter(pk=world["real"].pk).update(**edit)

    now = knowledge_versions([saved.capability_id], [saved.link_id])
    capability, link = now["capabilities"][saved.capability_id], now["links"][saved.link_id]
    assert (capability.readable, capability.unreadable_reason) == (False, reason)
    assert capability.fingerprint == saved.capability_fingerprint  # содержание то же — не «изменилось»
    # Связь сама в порядке, но без своей возможности не читается.
    assert (link.readable, link.unreadable_reason) == (False, UnreadableReason.NOT_APPROVED)
    assert "real_effect" not in capabilities.knowledge_versions_helping_goal(goal.key)


def test_a_withdrawn_claim_scope_is_named_as_not_supported(world, goal, curator) -> None:
    ProcedureCapability.objects.filter(pk=world["real"].pk).update(
        claim_scope="not_supported", confirmed_at=timezone.now() + timedelta(seconds=1),
    )

    state = knowledge_versions([world["real"].pk])["capabilities"][world["real"].pk]

    assert (state.readable, state.unreadable_reason) == (False, UnreadableReason.NOT_SUPPORTED)


def test_a_deleted_or_unknown_claim_is_missing(world) -> None:
    gone = world["real_link"].pk
    world["real_link"].delete()

    state = knowledge_versions([], [gone])["links"][gone]

    assert (state.fingerprint, state.readable, state.unreadable_reason) == (None, False, UnreadableReason.MISSING)


def test_synthetic_versions_follow_the_grant(grant, world) -> None:
    fake, fake_link = world["fake"].pk, world["fake_link"].pk

    without = knowledge_versions([fake], [fake_link])
    under = knowledge_versions([fake], [fake_link], include_synthetic=grant)

    assert (without["capabilities"][fake].readable, without["capabilities"][fake].unreadable_reason) == (
        False, UnreadableReason.NOT_APPROVED,
    )
    assert without["capabilities"][fake].synthetic is True
    assert (under["capabilities"][fake].readable, under["links"][fake_link].readable) == (True, True)
    assert under["capabilities"][fake].fingerprint == claim_fingerprint(world["fake"])
