import pytest
from rest_framework.test import APIClient

from users.models import User


@pytest.fixture
def pro_api_client():
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'pro'
    return client


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username='svc_specialist', password='testpass123',
        role='specialist', phone='+79990100001',
    )


@pytest.fixture
def authenticated_specialist(pro_api_client, specialist_user):
    pro_api_client.force_authenticate(user=specialist_user)
    return pro_api_client
