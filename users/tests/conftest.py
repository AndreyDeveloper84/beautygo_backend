import pytest
from rest_framework.test import APIClient

from users.models import User


@pytest.fixture
def api_client():
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'client'
    return client


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='testclient', password='testpass123', role='client',
        phone='+79990000001',
    )


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='testspecialist', password='testpass123', role='specialist',
        phone='+79990000002',
    )


@pytest.fixture
def authenticated_client(api_client, client_user):
    api_client.force_authenticate(user=client_user)
    return api_client


@pytest.fixture
def authenticated_specialist(api_client, specialist_user):
    api_client.force_authenticate(user=specialist_user)
    return api_client
