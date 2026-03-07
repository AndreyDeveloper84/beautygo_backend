import pytest

from users.models import User, Profile


@pytest.mark.django_db
class TestProfileSignal:
    def test_profile_created_for_specialist(self):
        user = User.objects.create_user(
            username='newspec', password='testpass123', role='specialist',
        )
        assert Profile.objects.filter(user=user).exists()

    def test_profile_not_created_for_client(self):
        user = User.objects.create_user(
            username='newclient', password='testpass123', role='client',
        )
        assert not Profile.objects.filter(user=user).exists()

    def test_no_duplicate_on_save(self):
        user = User.objects.create_user(
            username='spec2', password='testpass123', role='specialist',
        )
        assert Profile.objects.filter(user=user).count() == 1
        user.email = 'new@test.com'
        user.save()
        assert Profile.objects.filter(user=user).count() == 1
