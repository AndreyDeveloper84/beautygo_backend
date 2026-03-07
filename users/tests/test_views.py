import pytest
from django.urls import reverse
from rest_framework import status

from users.models import Service, Profile


@pytest.mark.django_db
class TestRegisterView:
    def test_register_success(self, api_client):
        url = reverse('register')
        data = {
            'username': 'newuser',
            'password': 'testpass123',
            'role': 'specialist',
        }
        response = api_client.post(url, data)
        assert response.status_code == status.HTTP_201_CREATED

    def test_register_missing_fields(self, api_client):
        url = reverse('register')
        response = api_client.post(url, {})
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestServiceViewSet:
    def test_specialist_can_create(self, authenticated_specialist):
        url = reverse('services-list')
        data = {
            'name': 'Консультация',
            'description': 'Описание',
            'price': '1000.00',
            'duration_minutes': 60,
        }
        response = authenticated_specialist.post(url, data)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['name'] == 'Консультация'

    def test_client_cannot_create(self, authenticated_client):
        url = reverse('services-list')
        data = {'name': 'Test', 'price': '100', 'duration_minutes': 30}
        response = authenticated_client.post(url, data)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated_denied(self, api_client):
        url = reverse('services-list')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_specialist_sees_own_services(self, authenticated_specialist, specialist_user):
        from users.models import User
        Service.objects.create(
            specialist=specialist_user, name='Mine',
            price='500', duration_minutes=30,
        )
        other = User.objects.create_user(
            username='other_spec', password='pass', role='specialist',
        )
        Service.objects.create(
            specialist=other, name='NotMine',
            price='500', duration_minutes=30,
        )
        url = reverse('services-list')
        response = authenticated_specialist.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Mine'


@pytest.mark.django_db
class TestProfileViews:
    def test_my_profile_authenticated(self, authenticated_specialist):
        url = reverse('my-profile')
        response = authenticated_specialist.get(url)
        assert response.status_code == status.HTTP_200_OK

    def test_my_profile_unauthenticated(self, api_client):
        url = reverse('my-profile')
        response = api_client.get(url)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_profile_detail_public(self, api_client, specialist_user):
        profile = Profile.objects.get(user=specialist_user)
        profile.full_name = 'Тест Специалист'
        profile.save()
        url = reverse('profile-detail', kwargs={'pk': profile.pk})
        response = api_client.get(url)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['full_name'] == 'Тест Специалист'

    def test_update_own_profile(self, authenticated_specialist):
        url = reverse('my-profile')
        response = authenticated_specialist.patch(url, {'full_name': 'Новое Имя'})
        assert response.status_code == status.HTTP_200_OK
        assert response.data['full_name'] == 'Новое Имя'
