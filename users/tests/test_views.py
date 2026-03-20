import logging

import pytest
from django.urls import reverse
from rest_framework import status

from users.models import Profile, Service, User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestRegisterView:
    def test_register_client_success(self, api_client):
        url = reverse('register')
        data = {'phone': '+79005000001'}
        logger.info("POST %s with phone=%s, app_type=client", url, data['phone'])
        response = api_client.post(url, data, HTTP_X_APP_TYPE='client')
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['data']['phone'] == '+79005000001'
        user = User.objects.get(phone='+79005000001')
        assert user.role == 'client'

    def test_register_specialist_success(self, api_client):
        url = reverse('register')
        data = {'phone': '+79005000002'}
        logger.info("POST %s with phone=%s, app_type=pro", url, data['phone'])
        response = api_client.post(url, data, HTTP_X_APP_TYPE='pro')
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_201_CREATED
        user = User.objects.get(phone='+79005000002')
        assert user.role == 'specialist'

    def test_register_duplicate_phone(self, api_client, client_user):
        url = reverse('register')
        data = {'phone': client_user.phone}
        logger.info("POST %s with duplicate phone=%s", url, data['phone'])
        response = api_client.post(url, data, HTTP_X_APP_TYPE='client')
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'PHONE_ALREADY_REGISTERED'

    def test_register_missing_phone(self, api_client):
        url = reverse('register')
        logger.info("POST %s with empty data", url)
        response = api_client.post(url, {})
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_register_invalid_phone(self, api_client):
        url = reverse('register')
        logger.info("POST %s with invalid phone=12345", url)
        response = api_client.post(url, {'phone': '12345'})
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestLoginView:
    def test_login_success(self, api_client, client_user):
        url = reverse('login')
        logger.info("POST %s with phone=%s", url, client_user.phone)
        response = api_client.post(url, {'phone': client_user.phone})
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['message'] == 'OTP sent'

    def test_login_user_not_found(self, api_client):
        url = reverse('login')
        phone = '+79009999999'
        logger.info("POST %s with non-existent phone=%s", url, phone)
        response = api_client.post(url, {'phone': phone})
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.data['error']['code'] == 'USER_NOT_FOUND'


@pytest.mark.django_db
class TestVerifyOTPView:
    def test_verify_otp_success(self, api_client, client_user, settings):
        settings.DEBUG = True
        # First send OTP via login
        logger.info("Login + verify OTP flow for phone=%s", client_user.phone)
        api_client.post(reverse('login'), {'phone': client_user.phone})
        # Then verify
        url = reverse('verify-otp')
        response = api_client.post(url, {
            'phone': client_user.phone,
            'code': '000000',
        })
        logger.info("Verify OTP response %s: keys=%s", response.status_code, list(response.data.get('data', {}).keys()))
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert 'access' in data
        assert 'refresh' in data
        assert data['user']['phone'] == client_user.phone

    def test_verify_otp_wrong_code(self, api_client, client_user, settings):
        settings.DEBUG = True
        api_client.post(reverse('login'), {'phone': client_user.phone})
        url = reverse('verify-otp')
        logger.info("Verify OTP with wrong code for phone=%s", client_user.phone)
        response = api_client.post(url, {
            'phone': client_user.phone,
            'code': '999999',
        })
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.data['error']['code'] == 'INVALID_OTP'

    def test_verify_otp_no_code_sent(self, api_client):
        url = reverse('verify-otp')
        logger.info("Verify OTP without sending code first")
        response = api_client.post(url, {
            'phone': '+79009999999',
            'code': '000000',
        })
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST


@pytest.mark.django_db
class TestLogoutView:
    def test_logout_success(self, api_client, client_user, settings):
        settings.DEBUG = True
        logger.info("Full login → logout flow for phone=%s", client_user.phone)
        # Login flow
        api_client.post(reverse('login'), {'phone': client_user.phone})
        resp = api_client.post(reverse('verify-otp'), {
            'phone': client_user.phone, 'code': '000000',
        })
        tokens = resp.data['data']
        logger.info("Got tokens, performing logout")
        # Logout
        api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        url = reverse('logout')
        response = api_client.post(url, {'refresh': tokens['refresh']})
        logger.info("Logout response %s", response.status_code)
        assert response.status_code == status.HTTP_200_OK

    def test_logout_unauthenticated(self, api_client):
        url = reverse('logout')
        logger.info("POST %s without auth", url)
        response = api_client.post(url, {'refresh': 'fake-token'})
        logger.info("Response %s", response.status_code)
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
        logger.info("POST %s — specialist creates service '%s'", url, data['name'])
        response = authenticated_specialist.post(url, data)
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['name'] == 'Консультация'

    def test_client_cannot_create(self, authenticated_client):
        url = reverse('services-list')
        data = {'name': 'Test', 'price': '100', 'duration_minutes': 30}
        logger.info("POST %s — client tries to create service (should be denied)", url)
        response = authenticated_client.post(url, data)
        logger.info("Response %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated_denied(self, api_client):
        url = reverse('services-list')
        logger.info("GET %s — unauthenticated (should be denied)", url)
        response = api_client.get(url)
        logger.info("Response %s", response.status_code)
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
        logger.info("GET %s — specialist should see only own services", url)
        response = authenticated_specialist.get(url)
        logger.info("Response %s: count=%d", response.status_code, len(response.data))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Mine'


@pytest.mark.django_db
class TestProfileViews:
    def test_my_profile_authenticated(self, authenticated_specialist):
        url = reverse('my-profile')
        logger.info("GET %s — authenticated specialist", url)
        response = authenticated_specialist.get(url)
        logger.info("Response %s", response.status_code)
        assert response.status_code == status.HTTP_200_OK

    def test_my_profile_unauthenticated(self, api_client):
        url = reverse('my-profile')
        logger.info("GET %s — unauthenticated", url)
        response = api_client.get(url)
        logger.info("Response %s", response.status_code)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_profile_detail_public(self, api_client, specialist_user):
        profile = Profile.objects.get(user=specialist_user)
        profile.full_name = 'Тест Специалист'
        profile.save()
        url = reverse('profile-detail', kwargs={'pk': profile.pk})
        logger.info("GET %s — public profile detail", url)
        response = api_client.get(url)
        logger.info("Response %s: full_name=%s", response.status_code, response.data.get('full_name'))
        assert response.status_code == status.HTTP_200_OK
        assert response.data['full_name'] == 'Тест Специалист'

    def test_update_own_profile(self, authenticated_specialist):
        url = reverse('my-profile')
        logger.info("PATCH %s — update full_name", url)
        response = authenticated_specialist.patch(url, {'full_name': 'Новое Имя'})
        logger.info("Response %s: full_name=%s", response.status_code, response.data.get('full_name'))
        assert response.status_code == status.HTTP_200_OK
        assert response.data['full_name'] == 'Новое Имя'


@pytest.mark.django_db
class TestClientProfileView:
    """Tests for GET/PATCH /api/v1/auth/clients/me/"""

    URL = '/api/v1/auth/clients/me/'

    def test_get_client_profile(self, authenticated_client):
        logger.info("GET %s — authenticated client", self.URL)
        response = authenticated_client.get(self.URL)
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_200_OK
        assert 'data' in response.data
        data = response.data['data']
        assert 'full_name' in data
        assert 'default_location_lat' in data
        assert 'default_location_lng' in data

    def test_update_client_profile_name(self, authenticated_client):
        logger.info("PATCH %s — update full_name", self.URL)
        response = authenticated_client.patch(
            self.URL, {'full_name': 'Анна Иванова'}, format='json',
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['full_name'] == 'Анна Иванова'

    def test_update_client_profile_location(self, authenticated_client):
        logger.info("PATCH %s — update location", self.URL)
        response = authenticated_client.patch(
            self.URL,
            {
                'default_location_lat': '55.796127',
                'default_location_lng': '49.106405',
            },
            format='json',
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_200_OK
        data = response.data['data']
        assert data['default_location_lat'] == '55.796127'
        assert data['default_location_lng'] == '49.106405'

    def test_upload_avatar(self, authenticated_client, settings, tmp_path):
        from io import BytesIO
        from PIL import Image

        settings.STORAGES = {
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }

        img = Image.new('RGB', (100, 100), color='red')
        buf = BytesIO()
        img.save(buf, format='JPEG')
        buf.seek(0)
        buf.name = 'avatar.jpg'

        logger.info("PATCH %s — upload avatar (JPEG)", self.URL)
        response = authenticated_client.patch(
            self.URL, {'avatar': buf}, format='multipart',
        )
        logger.info("Response %s: avatar=%s", response.status_code,
                    response.data.get('data', {}).get('avatar'))
        assert response.status_code == status.HTTP_200_OK
        assert response.data['data']['avatar'] is not None

    def test_avatar_invalid_mime_type(self, authenticated_client, settings, tmp_path):
        from io import BytesIO
        from PIL import Image

        settings.STORAGES = {
            "default": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path)},
            },
            "staticfiles": {
                "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
            },
        }

        img = Image.new('RGB', (100, 100), color='blue')
        buf = BytesIO()
        img.save(buf, format='GIF')
        buf.seek(0)
        buf.name = 'avatar.gif'

        logger.info("PATCH %s — upload avatar (GIF, should fail)", self.URL)
        response = authenticated_client.patch(
            self.URL, {'avatar': buf}, format='multipart',
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_name_too_short(self, authenticated_client):
        logger.info("PATCH %s — name too short", self.URL)
        response = authenticated_client.patch(
            self.URL, {'full_name': 'А'}, format='json',
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_specialist_cannot_access(self, authenticated_specialist):
        logger.info("GET %s — specialist (should be 403)", self.URL)
        response = authenticated_specialist.get(self.URL)
        logger.info("Response %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated(self, api_client):
        logger.info("GET %s — unauthenticated", self.URL)
        response = api_client.get(self.URL)
        logger.info("Response %s", response.status_code)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
