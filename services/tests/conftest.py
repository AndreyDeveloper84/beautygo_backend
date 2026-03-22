import pytest
from rest_framework.test import APIClient

from users.models import User


@pytest.fixture
def pro_api_client():
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'pro'
    return client


@pytest.fixture
def client_api_client():
    client = APIClient()
    client.defaults['HTTP_X_APP_TYPE'] = 'client'
    return client


@pytest.fixture
def specialist_user(db):
    user = User.objects.create_user(
        username='svc_specialist', password='testpass123',
        role='specialist', phone='+79990100001',
    )
    # Signal creates SpecialistProfile — update it with test data
    profile = user.specialist_profile
    profile.display_name = 'Елена Мастер'
    profile.bio = 'Опытный мастер'
    profile.rating = 4.8
    profile.reviews_count = 25
    profile.status = 'active'
    profile.address = 'Казань, ул. Баумана 1'
    profile.location_lat = 55.796127
    profile.location_lng = 49.106405
    profile.save()
    return user


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username='svc_client', password='testpass123',
        role='client', phone='+79990200001',
    )


@pytest.fixture
def authenticated_specialist(pro_api_client, specialist_user):
    pro_api_client.force_authenticate(user=specialist_user)
    return pro_api_client


@pytest.fixture
def authenticated_client(client_api_client, client_user):
    client_api_client.force_authenticate(user=client_user)
    return client_api_client
