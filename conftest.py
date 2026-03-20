import logging

import pytest

logger = logging.getLogger("test_runner")


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
