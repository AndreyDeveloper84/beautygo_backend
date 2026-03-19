import pytest
from django.urls import reverse
from rest_framework import status

from users.models import Profile, Service, User


@pytest.mark.django_db
class TestRegisterView:
    def test_register_client_success(self, api_client):
        url = reverse('register')
        data = {'phone': '+79005000001'}
        response = api_client.post(url, data, HTTP_X_APP_TYPE='client')
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['data']['phone'] == '+79005000001'
        user = User.objects.get(phone='+79005000001')
        assert user.role == 'client'

    def test_register_specialist_success(self, api_client):
        url = reverse('register')
        data = {'phone': '+79005000002'}
        response = api_client.post(url, data, HTTP_X_APP_TYPE='pro')
        assert response.status_code == status.HTTP_201_CREATED
        user = User.objects.get(phone='+79005000002')
        assert user.role == 'specialist'

    def test_register_duplicate_phone(self, api_client, client_user):
        url = reverse('register')
        data = {'phone': client_user.phone}
        response = api_client.post(url, data, HTTP_X_APP_TYPE='client')
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'PHONE_ALREADY_REGISTERED'

    def test_register_missing_phone(self, api_client):
        url = reverse('register')
        response = api_client.post(url, {})
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_register_invalid_phone(self, api_client):
        url = reverse('register')
        response = api_client.post(url, {'phone': '12345'})
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestLoginView:
    def test_login_success(self, api_client, client_user):
        url = reverse('login')
        response = api_client.post(url, {'phone': client_user.phone})
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['message'] == 'OTP sent'

    def test_login_user_not_found(self, api_client):
        url = reverse('login')
        response = api_client.post(url, {'phone': '+79009999999'})
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data['error']['code'] == 'USER_NOT_FOUND'


@pytest.mark.django_db
class TestVerifyOTPView:
    def test_verify_otp_success(self, api_client, client_user, settings):
        settings.DEBUG = True
        # First send OTP via login
        api_client.post(reverse('login'), {'phone': client_user.phone})
        # Then verify
        url = reverse('verify-otp')
        response = api_client.post(url, {
            'phone': client_user.phone,
            'code': '000000',
        })
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert 'access' in data
        assert 'refresh' in data
        assert data['user']['phone'] == client_user.phone

    def test_verify_otp_wrong_code(self, api_client, client_user, settings):
        settings.DEBUG = True
        api_client.post(reverse('login'), {'phone': client_user.phone})
        url = reverse('verify-otp')
        response = api_client.post(url, {
            'phone': client_user.phone,
            'code': '999999',
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'INVALID_OTP'

    def test_verify_otp_no_code_sent(self, api_client):
        url = reverse('verify-otp')
        response = api_client.post(url, {
            'phone': '+79009999999',
            'code': '000000',
        })
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestLogoutView:
    def test_logout_success(self, api_client, client_user, settings):
        settings.DEBUG = True
        # Login flow
        api_client.post(reverse('login'), {'phone': client_user.phone})
        resp = api_client.post(reverse('verify-otp'), {
            'phone': client_user.phone, 'code': '000000',
        })
        tokens = resp.data['data']
        # Logout
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        url = reverse('logout')
        response = api_client.post(url, {'refresh': tokens['refresh']})
        assert response.status_code == status.HTTP_200_OK

    def test_logout_unauthenticated(self, api_client):
        url = reverse('logout')
        response = api_client.post(url, {'refresh': 'fake-token'})
        assert response.status_code == status.HTTP_401_UNAUTHORIZED


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
        Service.objects.create(
            specialist=specialist_user, name='Mine',
            price='500', duration_minutes=30,
        )
        other = User.objects.create_user(
            username='other_spec', password='pass', role='specialist',
            phone='+79005000099',
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
