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
