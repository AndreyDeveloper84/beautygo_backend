"""Сверка списков: «забудь всё» ⊆ удаление аккаунта (D3) + явные исключения (DRF-2226).

Два писателя стирания по человеку:

* D3 — удаление аккаунта, :func:`users.deletion_executor._erase_catalog`;
* «забудь всё» — :func:`users.forget_all_catalog.erase_remembered_catalog`
  (бот C5.2 и Mini App, #526/#530).

Списки моделей в них ведутся руками и расходятся молча: модель, добавленная
в D3, не попадает в «забудь всё», и никто этого не видит. Сторож — по
исходнику (AST), без базы:

* s1 — всё, что стирает «забудь всё», стирает и D3 (D3 ⊇ forget_all);
* s2 — всё, что стирает D3 и не стирает «забудь всё», названо в
  ``forget_all_catalog.KEPT_BY_FORGET_ALL`` с причиной; список ТОЧНЫЙ —
  лишняя запись (модель ушла из D3 или вошла в «забудь всё») тоже красная;
* s3 — перепись: каждая модель с привязкой к человеку (FK/O2O на
  ``AUTH_USER_MODEL`` или поле ``external_user_id``) упомянута кодом стирания
  D3 (не комментарием — узлом AST) либо названа в
  ``deletion_executor.NOT_ERASED_ON_DELETION`` с причиной; список точный.
  Первая находка переписи — ``nutrition.NutritionOutboxEvent``
  (``external_user_id`` + payload с профилем и водой, без FK): его не стирает
  ни D3, ни «забудь всё»;
* s4 — контроль присутствия: разбор видит ключи в обеих функциях, перепись
  находит заведомо связанные модели (иначе пустые множества прошли бы s1–s3).

Предел s3: «упомянута кодом» ≠ «стёрта» — для моделей домена «забудь всё»
точность держат s1–s2 (ключи ``_delete``), для остальных D3 перепись
гарантирует, что модель не забыта ВОВСЕ.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.apps import apps
from django.conf import settings

ROOT = Path(__file__).resolve().parents[2]
D3_SOURCES = (
    "users/deletion_executor.py",
    "users/personal_context_erasure.py",
)


def _erase_keys(path: str, func: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == func)
    return {
        n.args[0].value
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and getattr(n.func, "id", "") == "_delete"
        and n.args
        and isinstance(n.args[0], ast.Constant)
        and isinstance(n.args[0].value, str)
    }


def _d3() -> set[str]:
    return _erase_keys("users/deletion_executor.py", "_erase_catalog")


def _forget_all() -> set[str]:
    return _erase_keys("users/forget_all_catalog.py", "erase_remembered_catalog")


def _code_identifiers(paths: tuple[str, ...]) -> set[str]:
    """Имена в КОДЕ (узлы Name/Attribute и строки-ключи), без докстрингов и комментариев."""
    names: set[str] = set()
    for rel in paths:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        # Сам список исключений — не «упоминание кодом стирания»: иначе запись
        # в нём засчитывала бы модель стёртой.
        exclusion_nodes = {
            id(sub)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                getattr(t, "id", "") == "NOT_ERASED_ON_DELETION"
                for t in (node.targets if isinstance(node, ast.Assign) else [node.target])
            )
            for sub in ast.walk(node)
        }
        for n in ast.walk(tree):
            if id(n) in exclusion_nodes:
                continue
            if isinstance(n, ast.Name):
                names.add(n.id)
            elif isinstance(n, ast.Attribute):
                names.add(n.attr)
            elif isinstance(n, ast.alias):
                names.add(n.asname or n.name.rsplit(".", 1)[-1])
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
                # «app.Model» / «app.Model.field» — ключи счётчиков стирания.
                parts = n.value.split(".")
                if len(parts) >= 2:
                    names.add(parts[1])
    return names


def _person_linked_models() -> set[str]:
    user_model = apps.get_model(settings.AUTH_USER_MODEL)
    out: set[str] = set()
    for model in apps.get_models():
        if model._meta.app_label in {"auth", "admin", "contenttypes", "sessions", "token_blacklist"}:
            continue
        # Прокси-модель — та же таблица, что у её конкретной модели
        # (``users.PendingExternalIdentity`` — строки ``User``): считаем хранение.
        if model._meta.proxy:
            continue
        for f in model._meta.get_fields():
            if not getattr(f, "concrete", False):
                continue
            linked = f.is_relation and f.related_model is user_model and (f.many_to_one or f.one_to_one)
            if linked or f.name == "external_user_id":
                out.add(f"{model._meta.app_label}.{model.__name__}")
                break
    return out


class TestS1ForgetAllIsInsideD3:
    def test_d3_erases_everything_forget_all_erases(self) -> None:
        missing = _forget_all() - _d3()
        assert missing == set(), f"«забудь всё» стирает то, чего не стирает D3: {sorted(missing)}"


class TestS2WhatForgetAllKeepsIsNamed:
    def test_the_difference_is_exactly_the_named_list(self) -> None:
        from users.forget_all_catalog import KEPT_BY_FORGET_ALL

        assert _d3() - _forget_all() == set(KEPT_BY_FORGET_ALL)

    def test_every_kept_model_has_a_reason(self) -> None:
        from users.forget_all_catalog import KEPT_BY_FORGET_ALL

        assert all(isinstance(r, str) and len(r.strip()) >= 20 for r in KEPT_BY_FORGET_ALL.values())


class TestS3EveryPersonLinkedModelIsAccountedFor:
    def test_census(self) -> None:
        from users.deletion_executor import NOT_ERASED_ON_DELETION

        mentioned = _code_identifiers(D3_SOURCES)
        forgotten = {m for m in _person_linked_models() if m.split(".")[1] not in mentioned}
        assert forgotten == set(NOT_ERASED_ON_DELETION), (
            "модель с привязкой к человеку не стирается D3 и не названа исключением: "
            f"{sorted(forgotten - set(NOT_ERASED_ON_DELETION))}; устаревшие исключения: "
            f"{sorted(set(NOT_ERASED_ON_DELETION) - forgotten)}"
        )

    def test_every_exclusion_has_a_reason(self) -> None:
        from users.deletion_executor import NOT_ERASED_ON_DELETION

        assert all(isinstance(r, str) and len(r.strip()) >= 20 for r in NOT_ERASED_ON_DELETION.values())


@pytest.mark.django_db
class TestS4PresenceControls:
    def test_the_parser_sees_both_writers(self) -> None:
        assert "nutrition.FoodLog" in _d3() and "nutrition.FoodLog" in _forget_all()
        assert "users.SocialAccount" in _d3() and "users.SocialAccount" not in _forget_all()

    def test_the_census_sees_known_linked_models(self) -> None:
        linked = _person_linked_models()
        assert {"nutrition.FoodLog", "goals.ClientGoal", "nutrition.NutritionOutboxEvent"} <= linked
