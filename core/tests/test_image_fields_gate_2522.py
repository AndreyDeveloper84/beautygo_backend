"""DRF-2522 — каждый хранимый снимок проходит через одно место очистки.

Перепись носителей ведётся по конструктору поля — реестр моделей,
``isinstance(field, ImageField)`` — а не по именам файлов: поле с именем
``photo`` или модель в новом приложении grep по ``avatar|image`` не увидел бы.

Сторож держит две вещи, и обе проверены ПОДМЕНОЙ:

* **тип:** каждый ``ImageField`` в реестре — ``MetadataFreeImageField``.
  Подмена: модель с обычным ``ImageField`` в изолированном реестре — сторож
  обязан её назвать. Это и есть «восьмое поле без срезания»;
* **поведение:** в КАЖДОЕ поле кладётся снимок с координатами, и из
  хранилища читается то, что там легло. Тип без поведения — обещание, а не
  проверка. Подмена: очистка выключена — и поле, которое «уже закрыто»,
  обязано покраснеть.
"""

from __future__ import annotations

import io

import pytest
from django.apps import apps as project_apps
from django.core.files.base import ContentFile
from django.db import models
from django.test.utils import isolate_apps
from PIL import Image
from PIL.ExifTags import IFD

from core import image_privacy
from core.image_privacy import MetadataFreeImageField
from core.tests.test_image_privacy_2518 import jpeg

#: Носители хранимых байт на день записи (DRF-2522). Список — нижняя граница:
#: новое поле не ломает этот узел, его ловит проверка типа и поведения.
KNOWN_CARRIERS = {
    "users.Profile.avatar",
    "users.SpecialistProfile.avatar",
    "users.SpecialistPortfolio.image",
    "services.Service.image",
    "nutrition.FoodScan.image",
}


def census(registry=project_apps) -> list[models.ImageField]:
    """Все ``ImageField`` реестра — по классу поля, а не по имени."""
    return [
        field
        for model in registry.get_models(include_auto_created=True)
        for field in model._meta.concrete_fields
        if isinstance(field, models.ImageField)
    ]


def label(field: models.Field) -> str:
    return f"{field.model._meta.label}.{field.name}"


def unguarded(fields) -> list[str]:
    return [label(f) for f in fields if not isinstance(f, MetadataFreeImageField)]


def leaks(field: models.ImageField) -> bool:
    """Положить снимок с GPS в поле и прочитать, что легло в хранилище."""
    instance = field.model()
    fieldfile = getattr(instance, field.attname)
    fieldfile.save("probe.jpg", ContentFile(jpeg(with_gps=True)), save=False)
    try:
        with fieldfile.storage.open(fieldfile.name, "rb") as fh:
            stored = fh.read()
    finally:
        fieldfile.storage.delete(fieldfile.name)
    with Image.open(io.BytesIO(stored)) as img:
        gps = img.getexif().get_ifd(IFD.GPSInfo)
    return bool(gps) or b"ProbePhone" in stored


class TestCensus:
    def test_census_sees_every_known_carrier(self) -> None:
        found = {label(f) for f in census()}
        # Пустая перепись прошла бы все остальные узлы молча.
        assert KNOWN_CARRIERS <= found, f"перепись потеряла: {KNOWN_CARRIERS - found}"


class TestEveryFieldIsGated:
    def test_every_image_field_is_the_gated_class(self) -> None:
        assert unguarded(census()) == []

    def test_every_image_field_drops_coordinates_on_save(self) -> None:
        leaking = [label(f) for f in census() if leaks(f)]
        assert leaking == [], f"координаты доехали до хранилища: {leaking}"


class TestTheGuardSeesASubstitution:
    @isolate_apps("core", kwarg_name="isolated")
    def test_a_plain_image_field_is_named(self, isolated=None) -> None:
        class ProbeWithPlainField(models.Model):
            photo = models.ImageField(upload_to="probe/")

            class Meta:
                app_label = "core"

        # Модель живёт в изолированном реестре, проектный не засоряется.
        assert unguarded(census(isolated)) == ["core.ProbeWithPlainField.photo"]

    def test_a_closed_field_goes_red_when_stripping_is_off(self, monkeypatch) -> None:
        field = next(f for f in census() if label(f) == "nutrition.FoodScan.image")
        assert not leaks(field), "без подмены поле обязано быть чистым"

        monkeypatch.setattr(image_privacy, "strip_metadata", lambda data: data)

        assert leaks(field), "очистка выключена, а сторож не видит координат"
