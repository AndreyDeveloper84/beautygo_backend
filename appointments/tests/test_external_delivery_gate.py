"""Per-topic external delivery gate — Track 5 prep.

The Block C HTTP publisher (C2) ships rows where
``OutboxEvent.external_delivery_enabled=True``. The model default is
``False`` so historical rows + topics not yet promoted to
cross-service stay invisible to the publisher. This module flips the
flag at emit time per the ``OUTBOX_EXTERNAL_DELIVERY_TOPICS`` setting
allowlist.

This PR ships the wiring but NOT the flip — the env var defaults to
empty and stays empty until founder gives the per-topic odmашка
(bot-side consumer round-trip green).

Tests pin:
* Empty allowlist → every emit lands with the legacy False default.
  Equivalent to pre-B-1 behaviour. Safety floor: forgetting to set
  the env does not silently leak rows to the publisher.
* Topic-on-allowlist → emit lands with True. Single-line env change
  is sufficient to enable delivery once bot is ready.
* Topic NOT on allowlist → emit lands with False even when other
  topics are listed. Per-topic granularity, no all-or-nothing flip.
* Allowlist tolerates whitespace + empty slots in env var.
"""
from __future__ import annotations

import pytest

from appointments.infrastructure.outbox import emit_outbox_event
from appointments.models import OutboxEvent


@pytest.mark.django_db
class TestExternalDeliveryGate:
    """Per-topic gate via OUTBOX_EXTERNAL_DELIVERY_TOPICS setting."""

    def _emit(self, topic: str) -> OutboxEvent:
        return emit_outbox_event(
            topic=topic,
            data={"sentinel": "x"},
            user_id="11111111-1111-1111-1111-111111111111",
            tenant_id="22222222-2222-2222-2222-222222222222",
            actor="system",
        )

    def test_empty_allowlist_leaves_flag_false(self, settings):
        settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = ()
        ev = self._emit(OutboxEvent.Topic.PAYMENT_CAPTURED)
        assert ev.external_delivery_enabled is False

    def test_topic_on_allowlist_flips_flag_true(self, settings):
        settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = ("payment.captured",)
        ev = self._emit(OutboxEvent.Topic.PAYMENT_CAPTURED)
        assert ev.external_delivery_enabled is True

    def test_topic_not_on_allowlist_stays_false_even_when_others_listed(
        self, settings,
    ):
        # Per-topic granularity: enabling payment.captured must not
        # silently enable payment.failed. Each topic flips on its own
        # bot-consumer round-trip confirmation.
        settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = ("payment.captured",)
        ev = self._emit(OutboxEvent.Topic.PAYMENT_FAILED)
        assert ev.external_delivery_enabled is False

    def test_allowlist_supports_multiple_topics(self, settings):
        settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS = (
            "payment.captured",
            "payment.failed",
        )
        captured = self._emit(OutboxEvent.Topic.PAYMENT_CAPTURED)
        failed = self._emit(OutboxEvent.Topic.PAYMENT_FAILED)
        refunded = self._emit(OutboxEvent.Topic.PAYMENT_REFUNDED)
        assert captured.external_delivery_enabled is True
        assert failed.external_delivery_enabled is True
        # payment.refunded NOT on the allowlist (founder reserve until
        # the refund-recovery flow lands on the bot side).
        assert refunded.external_delivery_enabled is False

    def test_setting_missing_is_treated_as_empty(self, settings):
        # Defensive: if a future refactor accidentally removes the
        # setting attribute, the emit helper must NOT crash — it
        # should treat missing-setting as empty allowlist (= local
        # only). Same fail-closed shape as the bot's
        # REASON_NO_SECRET fallback.
        if hasattr(settings, "OUTBOX_EXTERNAL_DELIVERY_TOPICS"):
            del settings.OUTBOX_EXTERNAL_DELIVERY_TOPICS
        ev = self._emit(OutboxEvent.Topic.PAYMENT_CAPTURED)
        assert ev.external_delivery_enabled is False
