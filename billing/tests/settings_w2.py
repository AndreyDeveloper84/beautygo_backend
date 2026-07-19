"""W2-local test settings — TEMPORARY shim.

Exists only because `billing` is not yet in INSTALLED_APPS (patch B-1
belongs to W1 and lands after this branch merges to dev). Until then run:

    pytest --ds=billing.tests.settings_w2 billing/ users/tests/test_internal_users_api.py

TODO(W2): delete this module once djangoProject/settings/base.py
registers 'billing' and the canonical `pytest` command covers the app.
"""
from djangoProject.settings.test import *  # noqa: F401,F403

INSTALLED_APPS += ["billing"]  # noqa: F405
