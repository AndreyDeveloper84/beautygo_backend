import logging

import pytest

from users.models import Profile, User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestProfileSignal:
    def test_profile_created_for_specialist(self):
        user = User.objects.create_user(
            username='newspec', password='testpass123', role='specialist',
            phone='+79002000001',
        )
        exists = Profile.objects.filter(user=user).exists()
        logger.info("Specialist created: profile exists=%s", exists)
        assert exists

    def test_profile_not_created_for_client(self):
        user = User.objects.create_user(
            username='newclient', password='testpass123', role='client',
            phone='+79002000002',
        )
        exists = Profile.objects.filter(user=user).exists()
        logger.info("Client created: profile exists=%s (expected False)", exists)
        assert not exists

    def test_no_duplicate_on_save(self):
        user = User.objects.create_user(
            username='spec2', password='testpass123', role='specialist',
            phone='+79002000003',
        )
        assert Profile.objects.filter(user=user).count() == 1
        user.email = 'new@test.com'
        user.save()
        count = Profile.objects.filter(user=user).count()
        logger.info("Profile count after re-save: %d (expected 1)", count)
        assert count == 1
