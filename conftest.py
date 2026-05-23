import logging

import pytest

logger = logging.getLogger("test_runner")


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
def _auto_default_tenant_for_specialist_profile(request):
    """Post-#590: Appointment.tenant is null=False, stamped from
    specialist.tenant_id. Existing test fixtures that build a
    SpecialistProfile without explicitly setting tenant predate
    this invariant — relying on the old null=True fallback. To
    avoid touching every fixture in the repo, autouse a pre_save
    signal handler that auto-attaches a default tenant when one
    isn't set.

    Test-only behaviour. Production code paths MUST set tenant
    explicitly via the registration / onboarding flow; the
    model-level null=False catches them if they forget. The
    signal short-circuits when tenant is already set, so tests
    that explicitly assign tenant_a/tenant_b are unaffected.

    Disabled for tests that opt-out via the
    @pytest.mark.no_auto_tenant marker (e.g. tests that want to
    pin the verify_no_null_tenant raise-path).
    """
    if "no_auto_tenant" in request.keywords:
        yield
        return

    from django.db.models.signals import pre_save

    def _set_default_tenant(sender, instance, **kwargs):
        if instance.tenant_id is None:
            from tenants.models import Tenant
            default, _ = Tenant.objects.get_or_create(
                slug="test-default-tenant",
                defaults={"name": "Test Default Tenant"},
            )
            instance.tenant = default

    # Lazy import so this conftest doesn't pull users.models at
    # collection time (slows down --co).
    try:
        from users.models import SpecialistProfile
    except Exception:
        # App not loaded yet (e.g. test collection failure path).
        yield
        return

    pre_save.connect(_set_default_tenant, sender=SpecialistProfile)
    try:
        yield
    finally:
        pre_save.disconnect(_set_default_tenant, sender=SpecialistProfile)


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
