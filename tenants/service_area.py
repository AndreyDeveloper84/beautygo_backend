"""`ServiceArea` — зона выезда мастера (макет 5, кадр 5.3; DRF-1803, M11).

Выезд — не место: у него нет адреса, до которого считать расстояние, и
поэтому это не строка ``ServiceLocation``. Зона — город мастера и охват.

Охват — два исхода, оба названы, умолчания нет:

* ``whole_city`` — «по всему городу»;
* ``later`` — «настрою позже»: формат выбран, но клиенту ещё не готов.

«Только в некоторых районах» (макет) — только когда появится источник
районов (фриз §12.4); до того такого значения нет вовсе, а не «районы пустые».
Одна зона каждого вида на мастера — схемой, не только кодом.

Город зоны — город workspace мастера (``Tenant.city``), не ввод.
"""
from __future__ import annotations

import uuid

from django.db import models


class AreaKind(models.TextChoices):
    MOBILE = "mobile", "Выезд к клиентам"


class AreaCoverage(models.TextChoices):
    WHOLE_CITY = "whole_city", "По всему городу"
    LATER = "later", "Настрою позже"


class ServiceArea(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    specialist = models.ForeignKey(
        "users.SpecialistProfile", on_delete=models.CASCADE, related_name="service_areas",
    )
    kind = models.CharField(max_length=16, choices=AreaKind.choices)
    city = models.CharField(
        max_length=100, blank=True, default="",
        help_text="Город зоны — город workspace мастера (Tenant.city), не ввод.",
    )
    coverage = models.CharField(max_length=16, choices=AreaCoverage.choices)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Зона выезда мастера"
        verbose_name_plural = "Зоны выезда мастеров"
        constraints = [
            models.UniqueConstraint(fields=["specialist", "kind"], name="servicearea_one_per_kind"),
            # «Настрою позже» не превращается во «весь город» молча — и ни во
            # что третье: охват либо назван, либо строки нет.
            models.CheckConstraint(
                condition=models.Q(coverage__in=["whole_city", "later"]),
                name="servicearea_coverage_is_named",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} · {self.get_coverage_display()}"
