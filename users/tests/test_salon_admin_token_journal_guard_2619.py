"""DRF-2619 — сторож: каждая дверь на токене администратора салона пишет §96 или названа.

Журнал §96 на внутренней поверхности — свойство двери: миксин
:class:`AuditedPersonalDataAccess` читает вердикт субъектного сторожа. На
салонной поверхности вердикт выносит ``IsTenantAdminOrPlatformAdmin``, он
его не публикует, и строку пишет сама вьюха (``users/salon_admin_media_api``).
Обязанность места забывается молча — отсутствие строки в журнале не
краснеет. Этот сторож краснеет.

Перепись — по живому резолверу URL: каждая вьюха, у которой в
``authentication_classes`` стоит :class:`SalonAdminTokenAuthentication`.
Проверка — по AST: в методах класса (и в функциях модуля, которые они
вызывают, на один шаг) есть вызов ``record_or_lose`` / ``record_access``.
Иначе вьюха обязана стоять в :data:`JOURNALLED_OTHERWISE` с причиной.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from django.urls import URLPattern, URLResolver, get_resolver

from users.max_salon_admin_auth import SalonAdminTokenAuthentication

_RECORDERS = frozenset({"record_or_lose", "record_access"})

#: Двери на токене, которые строку §96 не пишут, — с причиной.
JOURNALLED_OTHERWISE: dict[str, str] = {
    name: (
        "расписание мастера (DRF-2607) — не категория §96; каждая запись — строка "
        "лога ``_journal`` (actor, via, tenant) в ``users/schedule_admin_api``"
    )
    for name in (
        "AdminScheduleImpactView",
        "AdminTimeOffListView",
        "AdminTimeOffDetailView",
        "AdminScheduleExceptionListView",
        "AdminScheduleExceptionDetailView",
    )
}


def _views_on_the_token() -> dict[str, type]:
    found: dict[str, type] = {}

    def walk(patterns) -> None:
        for p in patterns:
            if isinstance(p, URLResolver):
                walk(p.url_patterns)
            elif isinstance(p, URLPattern):
                cls = getattr(p.callback, "view_class", None)
                if cls is not None and SalonAdminTokenAuthentication in getattr(
                    cls, "authentication_classes", ()
                ):
                    found[cls.__name__] = cls

    walk(get_resolver().url_patterns)
    return found


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            f = call.func
            if isinstance(f, ast.Name):
                names.add(f.id)
            elif isinstance(f, ast.Attribute):
                names.add(f.attr)
    return names


def writes_the_journal(cls: type) -> bool:
    """Вызывает ли вьюха регистратор §96 — сама или через функцию своего модуля."""
    module = inspect.getmodule(cls)
    module_tree = ast.parse(inspect.getsource(module))
    helpers = {
        n.name: n for n in module_tree.body if isinstance(n, ast.FunctionDef)
    }
    class_tree = ast.parse(textwrap.dedent(inspect.getsource(cls)))
    called = _called_names(class_tree)
    if called & _RECORDERS:
        return True
    return any(
        _called_names(helpers[name]) & _RECORDERS for name in called if name in helpers
    )


class TestEveryDoorOnTheSalonAdminTokenIsJournalled:
    def test_the_census_names_the_avatar_door(self):
        # Положительный контроль: пустая перепись прошла бы любое утверждение.
        views = _views_on_the_token()
        assert "AdminSpecialistAvatarView" in views, sorted(views)
        assert len(views) == 6, sorted(views)

    def test_each_door_writes_96_or_is_named(self):
        unjournalled = {
            name for name, cls in _views_on_the_token().items()
            if not writes_the_journal(cls) and name not in JOURNALLED_OTHERWISE
        }
        assert unjournalled == set()

    def test_the_avatar_door_writes_96_itself(self):
        # Исключение — не лазейка: дверь фото в список не входит и пишет сама.
        views = _views_on_the_token()
        assert "AdminSpecialistAvatarView" not in JOURNALLED_OTHERWISE
        assert writes_the_journal(views["AdminSpecialistAvatarView"]) is True

    def test_named_exceptions_are_live_doors(self):
        # Исключение на дверь, которой больше нет, — мёртвая строка, что
        # потом прикроет новую дверь с тем же именем.
        assert set(JOURNALLED_OTHERWISE) <= set(_views_on_the_token())
