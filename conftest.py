import copy
import logging
import shutil

import pytest
from django.test import override_settings

logger = logging.getLogger("test_runner")


@pytest.fixture(autouse=True, scope="session")
def _isolated_file_storage(tmp_path_factory):
    """DRF-1663: every test session writes media into a throwaway dir.

    pytest-django takes ``DJANGO_SETTINGS_MODULE`` from the ENVIRONMENT
    ahead of pytest.ini. In the ``web`` container (docker-compose.dev.yml)
    the env names ``djangoProject.settings.dev``, whose STORAGES point at
    the S3Boto3Storage bucket of the running stack — so the daily smoke
    run (.github/workflows/smoke-on-dev.yml) posted a PNG into the PILOT
    bucket. The test DB is dropped afterwards, the object is not: 119
    stubs on the old MinIO, one per day. Locally the same gap left files
    in the project MEDIA_ROOT.

    The override swaps STORAGES["default"] as a whole — not just
    MEDIA_ROOT, which the S3 backend never reads — to a FileSystemStorage
    rooted in a pytest temp dir, whatever settings module is in force.
    Django's ``setting_changed`` receivers reset the ``storages`` handler
    and ``default_storage``, so FileFields bound to ``default_storage``
    at import time follow the swap. The dir is removed after the session.
    """
    from django.conf import settings

    location = tmp_path_factory.mktemp("media")
    storages_cfg = copy.deepcopy(settings.STORAGES)
    storages_cfg["default"] = {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": str(location)},
    }
    with override_settings(STORAGES=storages_cfg, MEDIA_ROOT=str(location)):
        yield location
    shutil.rmtree(location, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_throttle_cache():
    """Clear DRF throttle state between tests.

    DRF throttles hit the Django default cache, which for dev is a
    long-lived locmem cache shared across tests. Without a reset the 5th
    /auth/login/ test would start hitting the 10/min auth throttle and
    failing with 429 for reasons unrelated to what the test is checking.
    """
    from django.core.cache import cache
    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def log_test_lifecycle(request):
    """Auto-log start, end, and result of every test."""
    test_name = request.node.nodeid
    logger.info("START: %s", test_name)
    yield
    logger.info("END: %s", test_name)


@pytest.fixture(autouse=True)
def _auto_default_tenant(request):
    """Post-#590: Appointment.tenant is null=False. Existing test
    fixtures that build SpecialistProfile / Appointment without
    setting tenant predate this invariant. To avoid touching every
    fixture in the repo, autouse two pre_save signal handlers:

    1. SpecialistProfile: when saved with tenant=None, attach a
       shared 'test-default-tenant' Tenant.
    2. Appointment: when saved with tenant=None, inherit from
       specialist.tenant (which handler #1 ensures is non-NULL).

    Tests that explicitly set tenant are unaffected — both handlers
    short-circuit when tenant_id is already populated. Tests can
    opt-out via the @pytest.mark.no_auto_tenant marker.

    Test-only behaviour. Production code paths MUST set tenant
    explicitly; model-level null=False enforces this.
    """
    if "no_auto_tenant" in request.keywords:
        yield
        return

    from django.db.models.signals import pre_save

    def _stamp_specialist_tenant(sender, instance, **kwargs):
        if instance.tenant_id is None:
            from tenants.models import Tenant
            default, _ = Tenant.objects.get_or_create(
                slug="test-default-tenant",
                defaults={"name": "Test Default Tenant"},
            )
            instance.tenant = default

    def _stamp_appointment_tenant(sender, instance, **kwargs):
        if instance.tenant_id is None and instance.specialist_id is not None:
            # Inherit from specialist — the SpecialistProfile handler
            # guarantees its tenant is non-NULL by the time we get here.
            from users.models import SpecialistProfile
            sp = SpecialistProfile.objects.filter(
                pk=instance.specialist_id,
            ).values_list("tenant_id", flat=True).first()
            if sp is not None:
                instance.tenant_id = sp

    try:
        from users.models import SpecialistProfile
        from appointments.models import Appointment
    except Exception:
        # Apps not loaded yet — collection-time path.
        yield
        return

    pre_save.connect(_stamp_specialist_tenant, sender=SpecialistProfile)
    pre_save.connect(_stamp_appointment_tenant, sender=Appointment)
    try:
        yield
    finally:
        pre_save.disconnect(
            _stamp_specialist_tenant, sender=SpecialistProfile,
        )
        pre_save.disconnect(
            _stamp_appointment_tenant, sender=Appointment,
        )


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """Log PASSED / FAILED / ERROR after each test phase."""
    outcome = yield
    report = outcome.get_result()

    if report.when == "call":
        if report.passed:
            logger.info("PASSED: %s", item.nodeid)
        elif report.failed:
            logger.error("FAILED: %s — %s", item.nodeid, report.longreprtext)
    elif report.when == "setup" and report.failed:
        logger.error("ERROR (setup): %s — %s", item.nodeid, report.longreprtext)
    elif report.when == "teardown" and report.failed:
        logger.error("ERROR (teardown): %s — %s", item.nodeid, report.longreprtext)
